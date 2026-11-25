#!/usr/bin/env python3
"""Reconcile a bounded batch of pending orders with GET-only supplier reads.

Updates local order status, never submits a purchase or changes money balances.
Run independently of the new-sales gate so existing obligations stay visible.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

from aurix_games.order_repository import GameOrderRepository
from aurix_games.order_reconciliation import drain_provider_events, reconcile_order, write_poll_status
from aurix_games.providers.fazercards_commands import FazerCardsCommandClient
from aurix_games.providers.shop2topup_commands import Shop2TopupCommandClient
from aurix_games.fulfillment import SupplierOrderRouter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--summary", action="store_true", help="log aggregate results without order identifiers")
    base = Path(os.environ.get("AURIX_GAMES_BASE_DIR", ".local/fazercards"))
    parser.add_argument("--orders-db", default=os.environ.get("AURIX_GAMES_ORDER_DATABASE", str(base / "game-orders.sqlite3")))
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    fazer_key = os.environ.get("FAZERCARDS_API_KEY", "").strip()
    shop2topup_key = os.environ.get("SHOP2TOPUP_API_KEY", "").strip()
    if not fazer_key and not shop2topup_key:
        parser.error("FAZERCARDS_API_KEY or SHOP2TOPUP_API_KEY is required")
    repository = GameOrderRepository(args.orders_db)
    client = SupplierOrderRouter(
        FazerCardsCommandClient(fazer_key, allow_writes=False) if fazer_key else None,
        Shop2TopupCommandClient(shop2topup_key) if shop2topup_key else None,
    )
    batch_time = datetime.now(timezone.utc)
    inbox = drain_provider_events(repository, now=batch_time, limit=args.limit)
    results = []
    for order in repository.reconciliation_batch(
        limit=args.limit, now=batch_time, minimum_interval_seconds=20,
    ):
        checked_at = datetime.now(timezone.utc)
        if not repository.claim_poll_check(
            order.order_id, now=checked_at, minimum_interval_seconds=20,
        ):
            continue
        if results:
            time.sleep(1)
        try:
            result = reconcile_order(repository, client, order.order_id, now=checked_at)
            results.append(result.as_dict())
        except Exception:
            results.append({"order_id": order.order_id, "error": "supplier reconciliation pending; local state retained"})
    errors = sum("error" in row for row in results)
    status_path = Path(os.environ.get(
        "AURIX_GAMES_RECONCILIATION_STATUS_FILE", str(base / "supplier-reconciliation-status.json")
    ))
    write_poll_status(status_path, at=datetime.now(timezone.utc), checked=len(results), errors=errors)
    report = {"supplier_writes": False, "inbox": [event.as_dict() for event in inbox], "results": results}
    if args.summary:
        report = {"supplier_writes": False, "inbox_events": len(inbox), "orders_checked": len(results),
            "errors": errors}
    print(json.dumps(report))
    return 1 if any("error" in row for row in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
