#!/usr/bin/env python3
"""Run one bounded AuriX Games fulfillment-worker batch.

The worker is disabled by default.  To permit supplier POSTs, an operator must
provide both ``--enable-writes`` and ``FAZERCARDS_ENABLE_WRITES=1``.  The
Fernet key for encrypted fulfillment payloads is read from
``AURIX_GAMES_FULFILLMENT_KEY`` and is never printed.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from aurix_games.customer_ledger import CustomerLedger
from aurix_games.fulfillment import (
    FazerCardsOrderAdapter,
    FulfillmentTaskStore,
    GameFulfillmentWorker,
)
from aurix_games.order_repository import GameOrderRepository
from aurix_games.payment_service import CustomerPaymentService
from aurix_games.providers.fazercards_commands import FazerCardsCommandClient, SupplierCommandError
from aurix_games.providers.fazercards import FazerCardsReadOnlyClient
from aurix_games.customer_checkout import build_env_customer_checkout
from aurix_games.spend_guard import PilotSpendGuard


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-writes", action="store_true", help="first half of the explicit supplier-write gate")
    parser.add_argument("--limit", type=int, default=10)
    base = Path(os.environ.get("AURIX_GAMES_BASE_DIR", ".local/fazercards"))
    parser.add_argument("--orders-db", default=os.environ.get("AURIX_GAMES_ORDER_DATABASE", str(base / "game-orders.sqlite3")))
    parser.add_argument("--ledger-db", default=os.environ.get("AURIX_GAMES_LEDGER_DATABASE", str(base / "game-ledger.sqlite3")))
    parser.add_argument("--tasks-db", default=os.environ.get("AURIX_GAMES_FULFILLMENT_DATABASE", str(base / "fulfillment.sqlite3")))
    args = parser.parse_args(argv)
    if isinstance(args.limit, bool) or not 1 <= args.limit <= 100:
        raise SystemExit("--limit must be between 1 and 100")
    api_key = os.environ.get("FAZERCARDS_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("FAZERCARDS_API_KEY is required")
    encryption_key = os.environ.get("AURIX_GAMES_FULFILLMENT_KEY", "").strip()
    if not encryption_key:
        raise SystemExit("AURIX_GAMES_FULFILLMENT_KEY is required")
    if args.enable_writes and os.environ.get("FAZERCARDS_ENABLE_WRITES") != "1":
        raise SystemExit("Set FAZERCARDS_ENABLE_WRITES=1 as the second write gate")
    allow_writes = bool(args.enable_writes)
    orders = GameOrderRepository(args.orders_db)
    tasks = FulfillmentTaskStore(args.tasks_db, encryption_key=encryption_key)
    payments = CustomerPaymentService(orders, CustomerLedger(args.ledger_db))
    client = FazerCardsCommandClient(api_key, allow_writes=allow_writes)
    guard = None
    if allow_writes:
        policy_path = os.environ.get("AURIX_GAMES_SPEND_POLICY", "")
        if not policy_path:
            raise SystemExit("AURIX_GAMES_SPEND_POLICY is required before supplier purchases")
        checkout = build_env_customer_checkout(root=base, for_fulfillment=True)
        if checkout is None or Path(checkout.repository.path).resolve() != Path(args.orders_db).resolve():
            raise SystemExit("fulfillment preflight and worker must share the configured order database")
        guard = PilotSpendGuard.from_file(policy_path, tasks=tasks, client=client,
            catalog_client=FazerCardsReadOnlyClient(api_key), checkout=checkout)
    worker = GameFulfillmentWorker(
        orders=orders,
        tasks=tasks,
        supplier=FazerCardsOrderAdapter(client),
        payments=payments,
        spend_guard=guard,
    )
    try:
        results = worker.run_once(now=datetime.now(timezone.utc), limit=args.limit)
    except (ValueError, SupplierCommandError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps([result.__dict__ for result in results], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
