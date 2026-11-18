#!/usr/bin/env python3
"""One explicitly authorized supplier-only top-up; never records a customer payment."""
from datetime import datetime, timezone
from decimal import Decimal
import argparse
import hashlib
import json
import os
from pathlib import Path
import re

from cryptography.fernet import Fernet
from persistence import open_sqlite_connection
from aurix_games.fazercards_account import _decimal, parse_supplier_wallet_snapshot
from aurix_games.providers.fazercards import FazerCardsReadOnlyClient
from aurix_games.providers.fazercards_commands import FazerCardsCommandClient
from aurix_games.spend_guard import schema_fingerprint


class AcceptanceError(ValueError):
    pass


def execute(request, *, database, encryption_key, client, catalog, now):
    required = {'acceptance_id', 'payload', 'max_cost_usd', 'total_limit_usd', 'fields_sha256', 'note_sha256',
        'validation_category_id', 'validation_fields', 'expected_region', 'expected_player_name', 'expected_player_id'}
    if not isinstance(request, dict) or set(request) != required:
        raise AcceptanceError('invalid acceptance request')
    identity = request['acceptance_id']
    if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,100}', identity):
        raise AcceptanceError('invalid acceptance identity')
    payload = request['payload']
    if not isinstance(payload, dict) or set(payload) != {'category_id', 'offer_id', 'fields'}:
        raise AcceptanceError('invalid top-up payload')
    if (payload['category_id'] != 'mobile_legends_global' or payload['offer_id'] != '12_diamonds'
            or not isinstance(payload['fields'], dict) or set(payload['fields']) != {'player_id', 'server_id'}
            or payload['fields']['player_id'] != request['expected_player_id']
            or request['validation_category_id'] != 'mobile_legends'
            or request['validation_fields'] != {'player_id': payload['fields']['player_id'], 'zone_id': payload['fields']['server_id']}):
        raise AcceptanceError('this acceptance command permits only the reviewed 12-diamond MLBB target')
    if (not isinstance(request['expected_region'], str) or request['expected_region'].strip().casefold()
            in {'', 'unknown', 'global', 'brazil', 'br', 'bra', 'brasil', 'indonesia', 'id', 'idn'}):
        raise AcceptanceError('selected pack does not support the confirmed account region')
    cap = _decimal(request['max_cost_usd'], 'acceptance ceiling')
    total = _decimal(request['total_limit_usd'], 'acceptance total ceiling')
    if not 0 < cap <= total <= Decimal('3'):
        raise AcceptanceError('acceptance request exceeds the authorized US$3 total')
    database = Path(database)
    if database.is_symlink():
        raise AcceptanceError('acceptance database must not be a symlink')
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if database.parent.stat().st_mode & 0o077:
        raise AcceptanceError('acceptance directory must be private')
    cipher = Fernet(encryption_key)
    fingerprint = schema_fingerprint(request)
    key = 'aurix-acceptance:' + identity
    with open_sqlite_connection(database) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS acceptance_attempts (
            acceptance_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
            ceiling_usd TEXT NOT NULL, status TEXT NOT NULL, encrypted_payload BLOB NOT NULL,
            provider_order_id TEXT UNIQUE, encrypted_response BLOB, dispatched_at TEXT NOT NULL)''')
    database.chmod(0o600)
    with open_sqlite_connection(database) as db:
        row = db.execute('SELECT * FROM acceptance_attempts WHERE acceptance_id=?', (identity,)).fetchone()
    if row:
        if row['request_hash'] != fingerprint:
            raise AcceptanceError('acceptance identity was reused with different details')
        if row['provider_order_id']:
            observed = client.get_order(row['provider_order_id'])
            order = observed.get('order') if observed.get('ok') is True else None
            if not isinstance(order, dict) or order.get('id') != row['provider_order_id']:
                raise AcceptanceError('supplier order read identity mismatch')
            return {'acceptance_id': identity, 'supplier_order_id': row['provider_order_id'],
                'status': order.get('status'), 'purchase_replayed': False, 'customer_payment_created': False}
        raise AcceptanceError('previous purchase outcome is uncertain; no further POST is permitted')
    subscription = client.get_subscription()
    if (subscription.get('ok') is not True or subscription.get('subscriptionActive') is not True
            or datetime.fromisoformat(subscription['planExpiresAt'].replace('Z', '+00:00')) <= now):
        raise AcceptanceError('active supplier subscription required')
    detail = catalog.get('/topups/offers', category_id=payload['category_id'])
    if (detail.get('category_id') != payload['category_id'] or schema_fingerprint(detail.get('fields')) != request['fields_sha256']
            or hashlib.sha256(str(detail.get('note', '')).encode()).hexdigest() != request['note_sha256']):
        raise AcceptanceError('reviewed supplier schema or regional terms changed')
    offers = [row for row in detail.get('offers', []) if row.get('offer_id') == payload['offer_id']]
    if len(offers) != 1 or not 0 < _decimal(offers[0].get('price_usd'), 'live cost') <= cap:
        raise AcceptanceError('live offer cost is outside the approved ceiling')
    if offers[0].get('stock') is not None and (type(offers[0]['stock']) is not int or offers[0]['stock'] < 1):
        raise AcceptanceError('selected supplier offer is out of stock')
    validation = client.validate_topup_recipient(request['validation_category_id'], request['validation_fields'])
    if (validation.get('ok') is not True or validation.get('valid') is not True
            or validation.get('category_id') != request['validation_category_id']
            or validation.get('player_id') != request['expected_player_id']
            or validation.get('player_name') != request['expected_player_name']
            or validation.get('region') != request['expected_region']):
        raise AcceptanceError('recipient identity or account region changed; purchase blocked')
    balance = parse_supplier_wallet_snapshot(client.get_balance(), observed_at=now)
    if balance.balance_usd < cap:
        raise AcceptanceError('insufficient reseller balance')
    with open_sqlite_connection(database) as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute('SELECT * FROM acceptance_attempts').fetchall()
        if rows:
            raise AcceptanceError('one supplier test was already submitted; another purchase requires a separately reviewed authorization')
        db.execute('INSERT INTO acceptance_attempts VALUES (?,?,?,?,?,?,?,?,?)',
            (identity, fingerprint, key, str(cap), 'dispatched', cipher.encrypt(json.dumps(payload).encode()),
             None, None, now.isoformat()))
    try:
        response = client.order_topup(**payload, idempotency_key=key)
        order = response.get('order') if isinstance(response, dict) and response.get('ok') is True else None
        if not isinstance(order, dict) or not isinstance(order.get('id'), str) or not re.fullmatch(r'ord-[0-9]+', order['id']):
            raise AcceptanceError('supplier create response is uncertain')
        with open_sqlite_connection(database) as db:
            db.execute('UPDATE acceptance_attempts SET status=?,provider_order_id=?,encrypted_response=? WHERE acceptance_id=?',
                ('accepted', order['id'], cipher.encrypt(json.dumps(response).encode()), identity))
    except Exception:
        with open_sqlite_connection(database) as db:
            db.execute("UPDATE acceptance_attempts SET status='unknown' WHERE acceptance_id=? AND provider_order_id IS NULL", (identity,))
        raise AcceptanceError('supplier test outcome is uncertain; no automatic purchase retry') from None
    return {'acceptance_id': identity, 'supplier_order_id': order['id'], 'status': order.get('status'),
        'approved_ceiling_usd': str(cap), 'observed_cost_usd': offers[0]['price_usd'], 'customer_payment_created': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not args.execute or os.environ.get('FAZERCARDS_ENABLE_WRITES') != '1':
        parser.error('both --execute and FAZERCARDS_ENABLE_WRITES=1 are required')
    path = args.request
    if (path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077
            or path.parent.stat().st_mode & 0o077 or path.stat().st_size > 65536):
        parser.error('acceptance request must be a private regular file')
    key = os.environ.get('FAZERCARDS_API_KEY', '')
    request = json.loads(path.read_text())
    try:
        result = execute(request, database=args.database, encryption_key=os.environ.get('AURIX_GAMES_FULFILLMENT_KEY', '').encode(),
            client=FazerCardsCommandClient(key, allow_writes=True, allow_recipient_validation=True),
            catalog=FazerCardsReadOnlyClient(key), now=datetime.now(timezone.utc))
    except Exception as exc:
        print(json.dumps({'status': 'stopped', 'error_type': type(exc).__name__, 'customer_payment_created': False}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
