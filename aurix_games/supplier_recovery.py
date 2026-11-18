"""Named, evidence-bound recovery of an original top-up; never sends a purchase."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

from persistence import open_sqlite_connection
from .fulfillment import FulfillmentInputError
from .order_reconciliation import reconcile_order, _target_for_status
from .order_state import OrderState
from .spend_guard import schema_fingerprint


def inspect_unknown_attempt(orders, tasks, order_id):
    order = orders.get(order_id)
    task = tasks.get(order_id)
    if (task is None or task.fulfillment_kind != 'direct_topup' or task.dispatch_started_at is None
            or task.status not in {'failed', 'submitted'}):
        raise FulfillmentInputError('a held original direct-top-up dispatch is required')
    return {'order_id': order_id, 'expected_attempts': task.attempts,
        'expected_payload_sha256': schema_fingerprint(task.payload),
        'expected_provider_idempotency_key': order.provider_idempotency_key,
        'provider_order_id': task.provider_order_id or order.provider_order_id,
        'supplier_post_permitted': False}


def recover_original_topup(orders, tasks, payments, client, *, order_id, request, reviewer, now):
    """Require exact supplier facts plus support confirmation of the original key.

    Supplier order reads omit the idempotency key, so an exact payload match alone
    cannot distinguish repeat identical purchases. The named reviewer must have
    independently confirmed that key-to-order binding with supplier support.
    """
    required = {'provider_order_id', 'expected_attempts', 'expected_payload_sha256',
        'expected_provider_idempotency_key', 'provider_reference', 'confirmed_original_attempt'}
    if (not isinstance(request, dict) or set(request) != required or request['confirmed_original_attempt'] is not True
            or type(request['expected_attempts']) is not int or request['expected_attempts'] < 1
            or not isinstance(reviewer, str) or not re.fullmatch(r'staff:[A-Za-z0-9._-]{1,64}', reviewer)
            or not isinstance(now, datetime) or now.utcoffset() is None):
        raise FulfillmentInputError('invalid original-attempt recovery authorization')
    reference = request['provider_reference']
    if (not isinstance(reference, str) or not 3 <= len(reference) <= 240 or reference != reference.strip()
            or any(ord(c) < 32 for c in reference)):
        raise FulfillmentInputError('supplier support evidence reference is required')
    provider_id = request['provider_order_id']
    if not isinstance(provider_id, str) or not re.fullmatch(r'ord-[0-9]+', provider_id):
        raise FulfillmentInputError('invalid supplier order identity')
    facts = inspect_unknown_attempt(orders, tasks, order_id)
    if any(request[key] != facts[key] for key in (
            'expected_attempts', 'expected_payload_sha256', 'expected_provider_idempotency_key')):
        raise FulfillmentInputError('original dispatch facts changed; recovery requires review')
    task = tasks.get(order_id)
    local = orders.get(order_id)
    if local.state != OrderState.UNKNOWN and local.provider_order_id != provider_id:
        raise FulfillmentInputError('local order is not the held original supplier attempt')
    if local.provider_order_id not in (None, provider_id) or task.provider_order_id not in (None, provider_id):
        raise FulfillmentInputError('original supplier receipt cannot change')
    with open_sqlite_connection(orders.path) as db:
        if db.execute('SELECT 1 FROM game_orders WHERE provider_order_id=? AND order_id<>?', (provider_id, order_id)).fetchone():
            raise FulfillmentInputError('supplier receipt is already assigned to another order')
    with open_sqlite_connection(payments.ledger.path) as db:
        reservation = db.execute('SELECT * FROM game_order_reservations WHERE order_id=?', (order_id,)).fetchone()
        if (reservation is None or reservation['status'] not in {'reserved','captured'}
                or (reservation['customer_id'], reservation['currency'], reservation['amount_minor']) !=
                    (local.customer_id, local.snapshot.currency, local.snapshot.amount_minor)):
            raise FulfillmentInputError('original customer reservation does not match the frozen order')
    payload = task.payload
    if set(payload) != {'category_id', 'offer_id', 'fields'}:
        raise FulfillmentInputError('recovery supports exact standard top-up payloads only')
    observed = client.get_order(provider_id)
    remote = observed.get('order') if isinstance(observed, dict) and observed.get('ok') is True else None
    if (not isinstance(remote, dict) or remote.get('id') != provider_id or remote.get('kind') != 'topup'
            or any(remote.get(key) != payload[key] for key in ('category_id', 'offer_id', 'fields'))
            or remote.get('status') not in {'created','processing','completed','failed','refund','refunded'}):
        raise FulfillmentInputError('supplier order does not exactly match the saved original dispatch')
    try:
        created = datetime.fromisoformat(remote['created_at'].replace('Z', '+00:00'))
        dispatch = datetime.fromisoformat(task.dispatch_started_at)
        if (created.utcoffset() is None or dispatch.utcoffset() is None
                or not dispatch - timedelta(minutes=2) <= created <= now + timedelta(minutes=5)):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise FulfillmentInputError('supplier creation time does not fit the original dispatch') from None
    fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    created_link = tasks.link_recovered_receipt(task, provider_order_id=provider_id,
        request_sha256=fingerprint, reviewer=reviewer, evidence={'authorization':request, 'supplier_order':remote}, now=now)
    local = orders.get(order_id)
    if local.state == OrderState.UNKNOWN:
        orders.transition(order_id, OrderState.RECONCILIATION, at=now,
            reason='named reviewer linked the support-confirmed original supplier attempt',
            provider_order_id=provider_id, provider_status=remote['status'])
    payments.capture_order(order_id, event_key=f'capture:{order_id}', actor=reviewer,
        reason='support-confirmed original supplier acceptance recovered', occurred_at=now)
    # Ordinary polling deliberately leaves unknown/reconciliation for human review.
    target = _target_for_status(remote['status'])
    if orders.get(order_id).state == OrderState.RECONCILIATION and target in {
            OrderState.PROCESSING, OrderState.DELIVERED, OrderState.FAILED}:
        orders.transition(order_id, target, at=now,
            reason='named reviewer confirmed exact original top-up status',
            provider_order_id=provider_id, provider_status=remote['status'])
    outcome = reconcile_order(orders, client, order_id, now=now)
    tasks.finish_recovered_receipt(order_id, provider_id, now=now)
    return {'order_id':order_id, 'state':outcome.state, 'replayed':not created_link,
        'original_receipt_recovered':True, 'supplier_purchase_created':False,
        'customer_bank_refund_created':False}
