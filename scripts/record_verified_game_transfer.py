#!/usr/bin/env python3
"""Allocate one transfer already verified in the receiving merchant account."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aurix_games.customer_ledger import CustomerLedger, LedgerConflictError, LedgerInputError
from aurix_games.order_repository import GameOrderRepository, OrderRepositoryError
from aurix_games.payment_service import CustomerPaymentService, PaymentBoundaryError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orders-db", default=os.environ.get("AURIX_GAMES_ORDER_DATABASE"),
                        required=not os.environ.get("AURIX_GAMES_ORDER_DATABASE"))
    parser.add_argument("--ledger-db", default=os.environ.get("AURIX_GAMES_LEDGER_DATABASE"),
                        required=not os.environ.get("AURIX_GAMES_LEDGER_DATABASE"))
    parser.add_argument("--verified", action="store_true", required=True,
                        help="attest that the transfer was verified against the merchant's records")
    parser.add_argument("--order", required=True)
    parser.add_argument("--rail", required=True)
    parser.add_argument("--receiver", required=True)
    parser.add_argument("--amount-minor", type=int, required=True)
    parser.add_argument("--currency", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--transfer-time", type=datetime.fromisoformat, required=True)
    args = parser.parse_args()
    if not args.actor.startswith("staff:") or args.actor == "staff:":
        parser.error("a named staff actor is required")
    transaction_id = input("Verified rail transaction ID: ").strip()
    service = CustomerPaymentService(GameOrderRepository(args.orders_db), CustomerLedger(args.ledger_db))
    try:
        result = service.settle_verified_transfer(args.order, rail=args.rail, receiver_id=args.receiver,
            transaction_id=transaction_id, amount_minor=args.amount_minor, currency=args.currency,
            actor=args.actor, occurred_at=args.transfer_time)
    except (LedgerConflictError, LedgerInputError, OrderRepositoryError, PaymentBoundaryError) as exc:
        parser.exit(1, f"Transfer requires review. {exc}\n")
    print(json.dumps({"order_id": result.order.order_id, "state": result.order.state.value,
                      "replayed": result.ledger.replayed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
