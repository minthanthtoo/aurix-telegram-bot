"""CLI for redacted, deterministic pay-app discovery and capture."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .adb import AdbClient, AdbError, ForegroundInterrupted
from .profiles import PROFILES
from .runner import FlowRecorder, capture_passive_sources
from .store import ObservationStore


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--output", type=Path, default=Path("var/pay-monitor"))
    result.add_argument("--serial")
    result.add_argument("--database", type=Path)
    subparsers = result.add_subparsers(dest="command", required=True)
    probe = subparsers.add_parser("probe", help="capture read-only app flows")
    probe.add_argument("--provider", choices=(*PROFILES, "all"), default="all")
    probe.add_argument("--pin-env", default="PAY_MONITOR_PIN")
    probe.add_argument("--lock-file", type=Path, default=Path("/tmp/aurix-pay-monitor-ui.lock"))
    account = probe.add_mutually_exclusive_group()
    account.add_argument("--account-index", type=int)
    account.add_argument("--account-hash")
    probe.add_argument("--since-time")
    probe.add_argument("--after-reference-hash")
    probe.add_argument("--target-reference-hash")
    probe.add_argument("--max-items", type=int, default=20)
    subparsers.add_parser("passive", help="capture redacted notification/media metadata")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    adb = AdbClient(serial=args.serial)
    try:
        adb.require_one_device()
        selected = list(PROFILES.values())
        if args.command == "probe":
            lock_handle = args.lock_file.open("a+")
            try:
                fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("pay-monitor: another foreground reconciliation is active", file=sys.stderr)
                return 3
            if args.provider != "all":
                selected = [PROFILES[args.provider]]
            if (args.account_index is not None or args.account_hash) and args.provider != "cbpay":
                parser().error("--account-index/--account-hash require --provider cbpay")
            pin = os.environ.get(args.pin_env)
            if not 1 <= args.max_items <= 100:
                parser().error("--max-items must be between 1 and 100")
            if args.since_time:
                try:
                    since = datetime.fromisoformat(args.since_time.replace("Z", "+00:00"))
                except ValueError:
                    parser().error("--since-time must be ISO-8601")
                if since.tzinfo is None:
                    parser().error("--since-time must include a timezone")
                args.since_time = since.astimezone(timezone.utc).isoformat()
            history_store = ObservationStore(args.database or args.output / "history-ledger.db")
            history_store.initialize()
            results = {}
            for profile in selected:
                recorder = FlowRecorder(
                    adb,
                    profile,
                    args.output,
                    history_store,
                    history_since=args.since_time,
                    after_reference_hash=args.after_reference_hash,
                    target_reference_hash=args.target_reference_hash,
                    max_history_items=args.max_items,
                )
                try:
                    results[profile.key] = recorder.probe(
                        pin=pin,
                        account_index=args.account_index,
                        account_hash=args.account_hash,
                    )
                except ForegroundInterrupted as exc:
                    results[profile.key] = {"status": "interrupted", "reason": str(exc)}
                except AdbError as exc:
                    results[profile.key] = {"status": "blocked", "reason": str(exc)}
            print(json.dumps(results, indent=2, sort_keys=True))
        else:
            capture_passive_sources(adb, args.output, selected)
            print(json.dumps({"status": "captured", "output": str(args.output)}))
    except AdbError as exc:
        print(f"pay-monitor: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
