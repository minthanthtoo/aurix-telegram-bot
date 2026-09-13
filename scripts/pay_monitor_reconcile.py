#!/usr/bin/env python3
"""Run one guarded, read-only pay-app reconciliation from a macOS LaunchAgent."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from pay_monitor.profiles import PROFILES
from pay_monitor.store import ObservationStore


UTC = timezone.utc
FAILURE_LIMIT = 3


def keychain_secret(service: str, account: str) -> str:
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-w", "-s", service, "-a", account],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    secret = result.stdout.strip()
    if result.returncode or not secret:
        raise RuntimeError("login PIN is unavailable in macOS Keychain")
    return secret


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--keychain-service", default="AuriXPayMonitorPIN")
    parser.add_argument("--keychain-account", default="aurix-pay-monitor")
    args = parser.parse_args(argv)
    health = {"paused": False, "failure_counts": {}}
    if args.state.exists():
        try:
            health.update(json.loads(args.state.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            print("Reconciliation health state is invalid", file=sys.stderr)
            return 3
    database = args.database or args.state.with_name("pay-monitor.db")
    store = ObservationStore(database)
    store.initialize()
    settings = store.get_monitor_settings()
    counts = dict(health.get("failure_counts") or {})
    circuit_open = {
        provider for provider, count in counts.items() if int(count) >= FAILURE_LIMIT
    }
    providers: dict[str, dict[str, object]] = {
        provider: {"status": "circuit_open", "reason": "three consecutive blocked runs"}
        for provider in circuit_open
    }
    request = store.claim_next_reconciliation_request()
    requested_provider = str(request["provider"]) if request else None
    if request and requested_provider in circuit_open:
        blocked_result = {"status": "blocked", "reason": "provider circuit is open"}
        store.complete_reconciliation_request(
            str(request["request_id"]), status="blocked", result=blocked_result
        )
        request = None
        requested_provider = None
    now = datetime.now(UTC)
    routine_due = requested_provider is None
    if routine_due and health.get("last_routine_at"):
        try:
            last_routine = datetime.fromisoformat(str(health["last_routine_at"]))
            routine_due = (now - last_routine).total_seconds() >= int(
                settings["effective_interval_seconds"]
            )
        except (TypeError, ValueError):
            routine_due = True
    if requested_provider is None and not routine_due:
        print(
            json.dumps(
                {
                    "status": "waiting",
                    "effective_interval_seconds": settings["effective_interval_seconds"],
                    "effective_source": settings["effective_source"],
                    "passive_capture": "immediate",
                },
                sort_keys=True,
            )
        )
        return 0
    next_cb_index = int(health.get("cbpay_next_index") or 0)
    cb_index_path = args.output / "cbpay" / "account_index.json"
    cb_account_count = 0
    try:
        cb_account_count = len(json.loads(cb_index_path.read_text(encoding="utf-8"))["accounts"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        pass
    healthy_providers = [provider for provider in PROFILES if provider not in circuit_open]
    routine_index = int(health.get("next_provider_index") or 0)
    routine_index_before = routine_index
    if requested_provider:
        provider_order = [requested_provider]
    elif healthy_providers:
        provider_order = [healthy_providers[routine_index % len(healthy_providers)]]
        routine_index = (routine_index + 1) % len(healthy_providers)
    else:
        provider_order = []
    if provider_order:
        try:
            pin = keychain_secret(args.keychain_service, args.keychain_account)
        except RuntimeError as exc:
            if request:
                store.complete_reconciliation_request(
                    str(request["request_id"]),
                    status="blocked",
                    result={"status": "blocked", "reason": str(exc)},
                )
            print(f"pay-monitor-reconcile: {exc}", file=sys.stderr)
            return 2
        environment = os.environ.copy()
        environment["ANDROID_SERIAL"] = args.serial
        environment["PAY_MONITOR_PIN"] = pin
    for provider in provider_order:
        if provider in circuit_open:
            continue
        command = [
            sys.executable,
            "-m",
            "pay_monitor.cli",
            "--serial",
            args.serial,
            "--output",
            str(args.output),
            "--database",
            str(database),
            "probe",
            "--provider",
            provider,
        ]
        if (
            provider == requested_provider
            and provider == "cbpay"
            and request
            and request.get("account_hash")
        ):
            command.extend(("--account-hash", str(request["account_hash"])))
        elif provider == "cbpay" and cb_account_count and provider != requested_provider:
            command.extend(("--account-index", str(next_cb_index % cb_account_count)))
        if provider == requested_provider and request:
            command.extend(("--max-items", str(request["max_items"])))
            if request.get("since_time"):
                command.extend(("--since-time", str(request["since_time"])))
            if request.get("after_reference_hash"):
                command.extend(
                    ("--after-reference-hash", str(request["after_reference_hash"]))
                )
            if request.get("target_reference_hash"):
                command.extend(
                    ("--target-reference-hash", str(request["target_reference_hash"]))
                )
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=4 * 60,
        )
        if result.returncode:
            lock_busy = result.returncode == 3 and "another foreground" in result.stderr
            providers[provider] = {
                "status": "interrupted" if lock_busy else "blocked",
                "reason": result.stderr.strip() or "pay-app reconciliation failed",
            }
            if provider == requested_provider and request:
                if lock_busy:
                    store.defer_reconciliation_request(str(request["request_id"]))
                else:
                    store.complete_reconciliation_request(
                        str(request["request_id"]),
                        status="blocked",
                        result=providers[provider],
                    )
            continue
        try:
            providers[provider] = json.loads(result.stdout)[provider]
        except (KeyError, TypeError, json.JSONDecodeError):
            providers[provider] = {
                "status": "blocked",
                "reason": "pay-app reconciliation returned invalid status",
            }
        if provider == requested_provider and request:
            if providers[provider].get("status") == "interrupted":
                store.defer_reconciliation_request(
                    str(request["request_id"]),
                    reason=str(providers[provider].get("reason") or "foreground_interrupted"),
                )
            else:
                request_status = (
                    "completed"
                    if providers[provider].get("status") == "captured"
                    else "blocked"
                )
                store.complete_reconciliation_request(
                    str(request["request_id"]),
                    status=request_status,
                    result=providers[provider],
                )
    blocked = {
        provider: str(details.get("reason") or "")
        for provider, details in providers.items()
        if details.get("status") == "blocked"
    }
    interrupted = {
        provider
        for provider, details in providers.items()
        if details.get("status") == "interrupted"
    }
    for provider, details in providers.items():
        status = details.get("status")
        if status in {"circuit_open", "interrupted"}:
            continue
        counts[provider] = int(counts.get(provider, 0)) + 1 if provider in blocked else 0
    if interrupted and requested_provider is None:
        routine_index = routine_index_before
    circuit_open = sorted(
        provider for provider, count in counts.items() if int(count) >= FAILURE_LIMIT
    )
    if providers.get("cbpay", {}).get("status") == "captured" and cb_account_count:
        next_cb_index = (next_cb_index + 1) % cb_account_count
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(
        json.dumps(
            {
                "updated_at": datetime.now(UTC).isoformat(),
                "paused": False,
                "circuit_open": circuit_open,
                "failure_counts": counts,
                "blocked": blocked,
                "cbpay_next_index": next_cb_index,
                "next_provider_index": routine_index,
                "last_routine_at": (
                    now.isoformat()
                    if requested_provider is None and provider_order and not interrupted
                    else health.get("last_routine_at")
                ),
                "effective_interval_seconds": settings["effective_interval_seconds"],
                "effective_source": settings["effective_source"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    summary = {
        provider: {
            key: value
            for key, value in details.items()
            if key
            in {
                "status",
                "history",
                "transaction",
                "wallets",
                "accounts",
                "history_lists",
                "new_history_items",
                "selected_account_index",
                "swipes",
            }
        }
        for provider, details in providers.items()
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
