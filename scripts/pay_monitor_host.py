#!/usr/bin/env python3
"""Start the loopback collector receiver and configure Android ADB reverse."""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

from pay_monitor.adb import DEFAULT_ADB, AdbClient, AdbError
from pay_monitor.server import main as server_main


def configure_reverse(adb_path: Path, serial: str, device_port: int, host_port: int) -> bool:
    if ":" in serial:
        subprocess.run(
            [str(adb_path), "connect", serial],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    state = subprocess.run(
        [str(adb_path), "-s", serial, "get-state"],
        check=False,
        capture_output=True,
        text=True,
        timeout=8,
    )
    if state.returncode or state.stdout.strip() != "device":
        return False
    reverse = subprocess.run(
        [
            str(adb_path),
            "-s",
            serial,
            "reverse",
            f"tcp:{device_port}",
            f"tcp:{host_port}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=8,
    )
    if reverse.returncode:
        return False
    subprocess.run(
        [
            str(adb_path),
            "-s",
            serial,
            "shell",
            "am",
            "broadcast",
            "-a",
            "com.aurix.paymonitor.FLUSH",
            "-n",
            "com.aurix.paymonitor/.FlushReceiver",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=8,
    )
    return True


def maintain_reverse(
    adb_path: Path,
    serial: str,
    device_port: int,
    host_port: int,
    refresh_seconds: float,
) -> None:
    last_state: bool | None = None
    while True:
        try:
            connected = configure_reverse(adb_path, serial, device_port, host_port)
        except (OSError, subprocess.TimeoutExpired):
            connected = False
        if connected != last_state:
            state = "connected" if connected else "waiting for device"
            print(f"ADB reverse supervisor: {state}", flush=True)
            last_state = connected
        time.sleep(refresh_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial")
    parser.add_argument("--adb", type=Path, default=DEFAULT_ADB)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=Path("data/pay-monitor.db"))
    parser.add_argument("--commerce-database", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--device-port", type=int, default=8765)
    parser.add_argument("--refresh-seconds", type=float, default=15.0)
    parser.add_argument("--admin-token-keychain-service")
    parser.add_argument("--admin-token-keychain-account", default="aurix-pay-monitor-admin")
    args = parser.parse_args(argv)
    adb = AdbClient(args.adb, args.serial)
    serial = args.serial or adb.require_one_device()
    if args.refresh_seconds < 5:
        parser.error("--refresh-seconds must be at least 5")
    threading.Thread(
        target=maintain_reverse,
        args=(args.adb, serial, args.device_port, args.port, args.refresh_seconds),
        name="aurix-adb-reverse",
        daemon=True,
    ).start()
    print(
        f"Supervising {serial}: device {args.device_port} -> host {args.port}",
        flush=True,
    )
    return server_main(
        [
            "--port",
            str(args.port),
            "--private-key",
            str(args.private_key),
            "--database",
            str(args.database),
            *(
                ["--commerce-database", str(args.commerce_database)]
                if args.commerce_database
                else []
            ),
            *(
                [
                    "--admin-token-keychain-service",
                    args.admin_token_keychain_service,
                    "--admin-token-keychain-account",
                    args.admin_token_keychain_account,
                ]
                if args.admin_token_keychain_service
                else []
            ),
        ]
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AdbError as exc:
        print(f"pay-monitor-host: {exc}", file=sys.stderr)
        raise SystemExit(2)
