#!/usr/bin/env python3
"""Prepare durable fulfillment tasks for already-verified customer orders.

This command only reads private checkout inputs, encrypts them into the local
fulfillment queue, and advances reserved orders to ``fulfillment_queued``. It
does not verify payments, call FazerCards, or enable supplier writes. It is
safe to rerun after interruption.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aurix_games.customer_checkout import build_env_customer_checkout  # noqa: E402
from aurix_games.fulfillment import FulfillmentTaskStore, PaidOrderDispatcher  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--base-dir",
        default=os.environ.get("AURIX_GAMES_BASE_DIR", ".local/fazercards"),
    )
    parser.add_argument(
        "--tasks-db",
        default=os.environ.get("AURIX_GAMES_FULFILLMENT_DATABASE"),
    )
    args = parser.parse_args(argv)
    if isinstance(args.limit, bool) or not 1 <= args.limit <= 100:
        raise SystemExit("--limit must be between 1 and 100")

    base = Path(args.base_dir)
    checkout = build_env_customer_checkout(root=base, for_fulfillment=True)
    if checkout is None:
        raise SystemExit("customer checkout is disabled; no paid orders were dispatched")
    encryption_key = os.environ.get("AURIX_GAMES_FULFILLMENT_KEY", "").strip()
    if not encryption_key:
        raise SystemExit("AURIX_GAMES_FULFILLMENT_KEY is required")

    tasks_path = args.tasks_db or os.environ.get(
        "AURIX_GAMES_FULFILLMENT_DATABASE", str(base / "fulfillment.sqlite3")
    )
    dispatcher = PaidOrderDispatcher(
        orders=checkout.repository,
        tasks=FulfillmentTaskStore(tasks_path, encryption_key=encryption_key),
        checkout=checkout,
    )
    outcomes = dispatcher.run_once(now=datetime.now(timezone.utc), limit=args.limit)
    print(
        json.dumps(
            [
                {"order_id": item.order_id, "status": item.status, "error": item.error}
                for item in outcomes
            ],
            sort_keys=True,
        )
    )
    return 0 if all(item.status == "queued" for item in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
