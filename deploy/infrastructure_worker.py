#!/usr/bin/env python3
"""Run one bounded DigitalOcean infrastructure reconciliation pass.

This process is intentionally separate from the Telegram bot.  It never
activates an Outline endpoint; an operator must install and verify Outline
before a new server is eligible for customer assignment.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Support both ``python -m deploy.infrastructure_worker`` (systemd) and the
# direct path form used by operators during a controlled dry run.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commerce import CommerceDatabase, PostgresCommerceDatabase
from infrastructure import DigitalOceanClient, FleetController, InfrastructureError


UTC = timezone.utc
LOCK_PATH = Path(os.environ.get("AURIX_INFRASTRUCTURE_LOCK", "/var/lib/aurix-infrastructure/worker.lock"))


def _database() -> Any:
    database_url = os.environ.get("COMMERCE_DATABASE_URL", "").strip()
    if database_url:
        return PostgresCommerceDatabase(database_url)
    database_path = os.environ.get("DATABASE_PATH", "").strip()
    if not database_path or not Path(database_path).is_absolute():
        raise InfrastructureError("DATABASE_PATH must be an absolute persistent path")
    return CommerceDatabase(Path(database_path))


def _due_jobs(database: Any) -> list[dict[str, Any]]:
    now = datetime.now(UTC).isoformat()
    with database.connect() as connection:
        rows = connection.execute(
            """SELECT id, status FROM infrastructure_jobs
               WHERE operation = 'provision'
                 AND status IN ('pending', 'running')
                 AND next_attempt_at <= ?
               ORDER BY created_at LIMIT 8""",
            (now,),
        ).fetchall()
    return [dict(row) for row in rows]


def run_once() -> int:
    token = os.environ.get("DIGITALOCEAN_API_TOKEN", "").strip()
    if not token:
        raise InfrastructureError("DIGITALOCEAN_API_TOKEN is not configured")
    database = _database()
    database.initialize()
    controller = FleetController(database, DigitalOceanClient(token))
    inventory = controller.reconcile_provider_inventory()
    print(
        "infrastructure_worker: inventory="
        f"{inventory['managed']} managed/{inventory['matched']} registered"
    )
    jobs = _due_jobs(database)
    if not jobs:
        print("infrastructure_worker: no due jobs")
        return 0
    failures = 0
    for job in jobs:
        job_id = str(job["id"])
        try:
            if str(job["status"]) == "pending":
                result = controller.execute_provision(job_id)
            else:
                result = controller.reconcile_provision(job_id)
            print(f"infrastructure_worker: job={job_id[:12]} status={result.get('status', 'unknown')}")
        except InfrastructureError as exc:
            # A disabled mutation gate is an intentional no-op, not a crash.
            if "mutations are disabled" in str(exc):
                print("infrastructure_worker: mutations_disabled")
                continue
            failures += 1
            print(
                f"infrastructure_worker: job={job_id[:12]} failed={type(exc).__name__}",
                file=sys.stderr,
            )
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one guarded AuriX infrastructure pass")
    parser.add_argument("--once", action="store_true", help="run one bounded pass (default)")
    args = parser.parse_args()
    del args
    if not os.environ.get("DIGITALOCEAN_API_TOKEN", "").strip():
        print("infrastructure_worker: DIGITALOCEAN_API_TOKEN is not configured", file=sys.stderr)
        return 1
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("infrastructure_worker: another pass is running")
            return 0
        return run_once()


if __name__ == "__main__":
    raise SystemExit(main())
