#!/usr/bin/env python3
"""Run one bounded AuriX infrastructure reconciliation pass.

This is a dedicated provider-worker entrypoint. It never runs Telegram or
customer provisioning. Provider creation remains fail-closed unless the
explicit infrastructure mutation gate and a provider token are configured.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from aurix_vpn.commerce_repositories import CommerceDatabase, PostgresCommerceDatabase
from aurix_vpn.connectivity import DigitalOceanClient, FleetController


def _database():
    database_url = os.environ.get("COMMERCE_DATABASE_URL", "").strip()
    if database_url:
        return PostgresCommerceDatabase(database_url)
    return CommerceDatabase(Path(os.environ.get("DATABASE_PATH", "data/bot.db")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=10,
        help="maximum number of sequential intents to inspect (1-100)",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.max_jobs <= 100:
        parser.error("--max-jobs must be between 1 and 100")

    database = _database()
    database.initialize()
    token = os.environ.get("DIGITALOCEAN_API_TOKEN", "").strip()
    controller = FleetController(
        database,
        DigitalOceanClient(token) if token else None,
    )
    results: list[dict[str, object]] = []
    for _ in range(args.max_jobs):
        try:
            result = controller.process_infrastructure_once()
        except Exception as exc:
            print(
                json.dumps(
                    {"status": "blocked", "error_type": type(exc).__name__},
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2
        if result is None:
            break
        # Provider identifiers and public addresses remain in durable worker
        # state, not in routine process output.
        results.append(
            {
                "job_id": result.get("job_id"),
                "status": result.get("status"),
            }
        )
        if result.get("status") in {"creating", "awaiting_verification"}:
            break
    print(json.dumps({"status": "ok", "jobs": results}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
