#!/usr/bin/env python3
"""Recover a support-confirmed original supplier top-up; never purchase or pay out."""
import argparse
from datetime import datetime, timezone
from getpass import getpass
import json
import os
from pathlib import Path

from aurix_games.game_admin import build_env_admin_service
from aurix_games.fulfillment import FulfillmentTaskStore
from aurix_games.order_repository import GameOrderRepository
from aurix_games.customer_ledger import CustomerLedger
from aurix_games.payment_service import CustomerPaymentService
from aurix_games.providers.fazercards_commands import FazerCardsCommandClient
from aurix_games.supplier_recovery import inspect_unknown_attempt, recover_original_topup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--order', required=True)
    parser.add_argument('--request', type=Path, help='private JSON authorization from independently verified support evidence')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    if args.execute != (args.request is not None):
        parser.error('--execute and --request must be provided together; omit both to inspect')
    try:
        admin = build_env_admin_service()
        if admin is None:
            raise ValueError('named supplier recovery authorization is not configured')
        reviewer = admin.authenticate_treasury(getpass('Named treasury reviewer token: '))
        base = Path(os.environ.get('AURIX_GAMES_BASE_DIR', '.local/fazercards'))
        orders = GameOrderRepository(os.environ.get('AURIX_GAMES_ORDER_DATABASE', str(base/'game-orders.sqlite3')))
        tasks = FulfillmentTaskStore(os.environ.get('AURIX_GAMES_FULFILLMENT_DATABASE', str(base/'fulfillment.sqlite3')),
            encryption_key=os.environ.get('AURIX_GAMES_FULFILLMENT_KEY', ''))
        if not args.execute:
            result = inspect_unknown_attempt(orders, tasks, args.order)
        else:
            path = args.request
            if (path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077
                    or path.parent.stat().st_mode & 0o077 or path.stat().st_size > 8192):
                raise ValueError('recovery request must be a private regular JSON file')
            payments = CustomerPaymentService(orders,
                CustomerLedger(os.environ.get('AURIX_GAMES_LEDGER_DATABASE', str(base/'game-ledger.sqlite3'))))
            result = recover_original_topup(orders, tasks, payments,
                FazerCardsCommandClient(os.environ.get('FAZERCARDS_API_KEY',''), allow_writes=False),
                order_id=args.order, request=json.loads(path.read_text()), reviewer=reviewer, now=datetime.now(timezone.utc))
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({'status':'review_required','error_type':type(error).__name__,
            'supplier_purchase_created':False,'customer_bank_refund_created':False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
