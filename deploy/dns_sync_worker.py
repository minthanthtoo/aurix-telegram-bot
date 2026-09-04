#!/usr/bin/env python3
"""Run one guarded Cloudflare DNS reconciliation pass.

DNS writes are deliberately opt-in. With the default disabled setting this
worker is a harmless no-op, so installing its timer does not change external
state. When enabled, the manifest remains authoritative and the sync is
idempotent (create/update/unchanged) through ``dns_records.py``.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy.dns_records import FleetError, sync
from deploy.fleet_reconcile import environment


TRUTHY = {"1", "true", "yes", "on"}
LOCK_PATH = Path(os.environ.get("AURIX_DNS_SYNC_LOCK", "/var/lib/aurix-dns-sync/worker.lock"))


def enabled(env: dict[str, str]) -> bool:
    return env.get("AURIX_DNS_SYNC_ENABLED", "0").strip().lower() in TRUTHY


def run_once(env_file: Path) -> int:
    env = environment(env_file)
    if not enabled(env):
        print("dns_sync: disabled")
        return 0
    if not env.get("AURIX_DNS_PROVIDER", "").strip():
        print("dns_sync: not_configured")
        return 0
    report = sync(env)
    actions = ",".join(
        str(item.get("action") or "unknown")
        for item in report.get("records", [])
    ) or "none"
    print(f"dns_sync: status={report.get('status', 'unknown')} actions={actions}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        default=os.environ.get("AURIX_FLEET_ENV_FILE", "/etc/aurix-bot/aurix.env"),
    )
    args = parser.parse_args(argv)
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("dns_sync: another pass is running")
            return 0
        try:
            return run_once(Path(args.env_file))
        except (FleetError, OSError, ValueError) as exc:
            print(f"dns_sync: failed={type(exc).__name__}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
