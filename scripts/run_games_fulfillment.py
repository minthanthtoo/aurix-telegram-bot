#!/usr/bin/env python3
"""Run one bounded AuriX Games fulfillment-worker batch.

The worker is disabled by default. To permit a provider's POSTs, an operator
must provide ``--enable-writes`` and that provider's explicit write gate
(``FAZERCARDS_ENABLE_WRITES=1`` or ``SHOP2TOPUP_ENABLE_WRITES=1``). Each enabled
provider also requires its own private spend policy. The Fernet key for
encrypted fulfillment payloads is read from ``AURIX_GAMES_FULFILLMENT_KEY``
and is never printed.
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
    FulfillmentTaskStore,
    GameFulfillmentWorker,
    SupplierOrderRouter,
)
from aurix_games.order_repository import GameOrderRepository
from aurix_games.payment_service import CustomerPaymentService
from aurix_games.providers.fazercards_commands import FazerCardsCommandClient, SupplierCommandError
from aurix_games.providers.fazercards import FazerCardsReadOnlyClient
from aurix_games.customer_checkout import build_env_customer_checkout
from aurix_games.spend_guard import PilotSpendGuard
from aurix_games.spend_guard import ProviderSpendGuard, Shop2TopupPilotSpendGuard
from aurix_games.providers.shop2topup_commands import Shop2TopupCommandClient


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
    fazer_key = os.environ.get("FAZERCARDS_API_KEY", "").strip()
    shop2topup_key = os.environ.get("SHOP2TOPUP_API_KEY", "").strip()
    if not fazer_key and not shop2topup_key:
        raise SystemExit("FAZERCARDS_API_KEY or SHOP2TOPUP_API_KEY is required")
    encryption_key = os.environ.get("AURIX_GAMES_FULFILLMENT_KEY", "").strip()
    if not encryption_key:
        raise SystemExit("AURIX_GAMES_FULFILLMENT_KEY is required")
    fazer_writes = args.enable_writes and bool(fazer_key) and os.environ.get("FAZERCARDS_ENABLE_WRITES") == "1"
    shop2topup_writes = args.enable_writes and bool(shop2topup_key) and os.environ.get("SHOP2TOPUP_ENABLE_WRITES") == "1"
    if args.enable_writes and not (fazer_writes or shop2topup_writes):
        raise SystemExit("Enable a configured provider's write gate as the second write gate")
    orders = GameOrderRepository(args.orders_db)
    tasks = FulfillmentTaskStore(args.tasks_db, encryption_key=encryption_key)
    payments = CustomerPaymentService(orders, CustomerLedger(args.ledger_db))
    fazer_client = FazerCardsCommandClient(fazer_key, allow_writes=fazer_writes) if fazer_key else None
    shop2topup_client = Shop2TopupCommandClient(
        shop2topup_key,
        allow_orders=shop2topup_writes,
        allow_player_validation=os.environ.get("SHOP2TOPUP_PLAYER_VALIDATION_ENABLED") == "1",
    ) if shop2topup_key else None
    guards = {}
    if fazer_writes or shop2topup_writes:
        checkout = build_env_customer_checkout(root=base, for_fulfillment=True)
        if checkout is None or Path(checkout.repository.path).resolve() != Path(args.orders_db).resolve():
            raise SystemExit("fulfillment preflight and worker must share the configured order database")
        if fazer_writes:
            policy_path = os.environ.get("AURIX_GAMES_SPEND_POLICY", "")
            if not policy_path:
                raise SystemExit("AURIX_GAMES_SPEND_POLICY is required before FazerCards purchases")
            guards["fazercards"] = PilotSpendGuard.from_file(policy_path, tasks=tasks, client=fazer_client,
                catalog_client=FazerCardsReadOnlyClient(fazer_key), checkout=checkout)
        if shop2topup_writes:
            policy_path = os.environ.get("SHOP2TOPUP_SPEND_POLICY", "")
            if not policy_path:
                raise SystemExit("SHOP2TOPUP_SPEND_POLICY is required before SHOP2TOPUP purchases")
            guards["shop2topup"] = Shop2TopupPilotSpendGuard.from_file(
                policy_path, tasks=tasks, client=shop2topup_client, checkout=checkout,
            )
    guard = None if not guards else next(iter(guards.values())) if len(guards) == 1 else ProviderSpendGuard(guards)
    worker = GameFulfillmentWorker(
        orders=orders,
        tasks=tasks,
        supplier=SupplierOrderRouter(fazer_client, shop2topup_client),
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
