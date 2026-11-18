#!/usr/bin/env python3
"""Private payment review operations; never calls the supplier or initiates a bank transfer."""
import argparse
from datetime import datetime, timezone
from getpass import getpass
import json
import os
import sqlite3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aurix_games.customer_ledger import CustomerLedger, LedgerConflictError, LedgerInputError, LedgerOperationError
from aurix_games.order_repository import GameOrderRepository, OrderRepositoryError
from aurix_games.payment_review import PaymentReviewService, PaymentReviewError
from aurix_games.payment_service import CustomerPaymentService, PaymentBoundaryError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['list', 'submit', 'approve', 'reject', 'authorize-refund', 'complete-refund', 'resume', 'receipt'])
    parser.add_argument('--orders-db', default=os.environ.get('AURIX_GAMES_ORDER_DATABASE'),
                        required=not os.environ.get('AURIX_GAMES_ORDER_DATABASE'))
    parser.add_argument('--ledger-db', default=os.environ.get('AURIX_GAMES_LEDGER_DATABASE'),
                        required=not os.environ.get('AURIX_GAMES_LEDGER_DATABASE'))
    parser.add_argument('--order')
    parser.add_argument('--revision', type=int, help='exact submission revision from the queue')
    parser.add_argument('--output', help='new private file for receipt export; never opened automatically')
    args = parser.parse_args()
    if args.action != 'list' and not args.order:
        parser.error('--order is required')
    if args.action in {'approve', 'reject', 'authorize-refund', 'complete-refund', 'resume', 'receipt'} and (args.revision is None or args.revision < 1):
        parser.error('--revision is required for this action')
    try:
        reviewers = json.loads(os.environ.get('AURIX_GAMES_PAYMENT_REVIEWERS', '{}'))
        service = PaymentReviewService(CustomerPaymentService(
            GameOrderRepository(args.orders_db), CustomerLedger(args.ledger_db)), reviewers,
            evidence_key=os.environ.get('AURIX_GAMES_PAYMENT_EVIDENCE_KEY') or None)
        token = getpass('Named reviewer token: ')
        service.authenticate(token)
        now = datetime.now(timezone.utc)
        if args.action == 'receipt':
            if not args.output:
                raise PaymentReviewError('--output is required for receipt export')
            content = service.receipt(args.order, token=token, revision=args.revision)
            output = Path(args.output)
            if output.parent.stat().st_mode & 0o077:
                raise PaymentReviewError('receipt export requires a private mode-0700 directory')
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as target:
                target.write(content)
            result = {'exported': True}
        elif args.action == 'list':
            result = service.pending(token)
        elif args.action == 'resume':
            result = service.resume(args.order, token=token, now=now, revision=args.revision)
        else:
            # Bounded JSON from stdin keeps transfer references out of command arguments.
            raw = sys.stdin.buffer.read(8193)
            if len(raw) > 8192:
                raise PaymentReviewError('input exceeds 8192 bytes')
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise PaymentReviewError('JSON object required')
            if args.action == 'submit':
                if set(payload) not in ({'customer_id', 'transaction_reference', 'evidence_sha256'}, {'customer_id', 'transaction_reference', 'evidence_sha256', 'expected_revision'}):
                    raise PaymentReviewError('submit requires customer_id, transaction_reference, evidence_sha256')
                result = service.submit(args.order, now=now, **payload)
            elif args.action == 'complete-refund':
                if set(payload) != {'refund_transaction_id', 'refunded_at', 'reason'} or not isinstance(payload.get('refunded_at'), str):
                    raise PaymentReviewError('complete-refund requires refund_transaction_id, refunded_at and reason')
                payload['refunded_at'] = datetime.fromisoformat(payload['refunded_at'])
                result = service.complete_external_refund(args.order, token=token,
                    revision=args.revision, now=now, **payload)
            else:
                expected = {'reason', 'verified_transfer'} if args.action == 'approve' else (
                    {'reason', 'external_refund'} if args.action == 'authorize-refund' else {'reason'})
                if set(payload) != expected:
                    raise PaymentReviewError('unexpected decision fields')
                if args.action in {'approve', 'authorize-refund'}:
                    transfer_key = 'verified_transfer' if args.action == 'approve' else 'external_refund'
                    transfer = payload[transfer_key]
                    if not isinstance(transfer, dict) or not isinstance(transfer.get('occurred_at'), str):
                        raise PaymentReviewError('transfer requires an ISO timestamp')
                    transfer['occurred_at'] = datetime.fromisoformat(transfer['occurred_at'])
                if args.action == 'approve':
                    payload['verified_transfer'] = transfer
                    result = service.decide(args.order, token=token, approve=True, now=now, revision=args.revision, **payload)
                elif args.action == 'authorize-refund':
                    payload['external_refund'] = transfer
                    result = service.decide(args.order, token=token, approve=False, now=now, revision=args.revision, **payload)
                else:
                    result = service.decide(args.order, token=token, approve=False, now=now, revision=args.revision, **payload)
    except (ValueError, TypeError, OSError, sqlite3.Error, LedgerConflictError, LedgerInputError, LedgerOperationError, OrderRepositoryError, PaymentBoundaryError) as exc:
        # Do not echo payloads, bank references, or authentication material on failure.
        parser.exit(1, f'Payment review failed ({type(exc).__name__}); inspect the private case before retrying.\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
