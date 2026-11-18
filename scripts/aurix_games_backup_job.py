#!/usr/bin/env python3
"""Quiesce Games writers, then create and verify one encrypted backup."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

from aurix_games.backup_encryption import create_encrypted_backup, verify_encrypted_backup
from scripts.aurix_games_backup import _passphrase


TIMERS = (
    "aurix-games-catalog.timer",
    "aurix-games-reconcile.timer",
    "aurix-games-dispatch.timer",
    "aurix-games-fulfill.timer",
    "aurix-games-integrity.timer",
)
WORKERS = tuple(unit.removesuffix(".timer") + ".service" for unit in TIMERS)
APP = "aurix-games.service"


def _active(unit: str) -> bool:
    return subprocess.run(
        ["systemctl", "is-active", "--quiet", unit],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def _systemctl(action: str, unit: str) -> None:
    result = subprocess.run(
        ["systemctl", action, unit],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"systemctl {action} {unit} failed ({result.returncode})")


def _wait_for_workers(timeout_seconds: int = 600) -> None:
    deadline = time.monotonic() + timeout_seconds
    while any(_active(unit) for unit in WORKERS):
        if time.monotonic() >= deadline:
            raise TimeoutError("Games worker did not finish; backup was not started")
        time.sleep(1)


def _backup(source: Path, output_dir: Path, passphrase: str) -> dict[str, object]:
    if output_dir.is_symlink():
        raise ValueError("backup directory must not be a symlink")
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = output_dir / f"aurix-games-{stamp}.agb"
    created = create_encrypted_backup(source, output, passphrase)
    verified = verify_encrypted_backup(output, passphrase)
    return {
        "status": "verified",
        "backup_file": output.name,
        "encrypted": created.get("encrypted") is True,
        "file_count": verified.get("file_count"),
        "sqlite_count": verified.get("sqlite_count"),
        "bytes": output.stat().st_size,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--passphrase-file", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        passphrase = _passphrase(SimpleNamespace(passphrase_file=args.passphrase_file))
        if len(passphrase.encode("utf-8")) < 16:
            raise ValueError("backup passphrase must contain at least 16 UTF-8 bytes")
        if args.source.is_symlink() or not args.source.is_dir():
            raise ValueError("backup source directory is unavailable")
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Games backup preflight failed: {error}", file=sys.stderr)
        return 1

    active_timers = [unit for unit in TIMERS if _active(unit)]
    app_was_active = _active(APP)
    failure: Exception | None = None
    restart_errors: list[str] = []
    result: dict[str, object] | None = None
    try:
        for unit in active_timers:
            _systemctl("stop", unit)
        _wait_for_workers()
        if app_was_active:
            _systemctl("stop", APP)
        result = _backup(args.source, args.output_dir, passphrase)
    except Exception as error:  # Keep service restoration in the finally path.
        failure = error
    finally:
        if app_was_active:
            try:
                _systemctl("start", APP)
            except Exception as error:
                restart_errors.append(str(error))
        for unit in active_timers:
            try:
                _systemctl("start", unit)
            except Exception as error:
                restart_errors.append(str(error))

    if failure is not None:
        print(f"Games backup failed: {failure}", file=sys.stderr)
        if restart_errors:
            print("Service restoration failed: " + "; ".join(restart_errors), file=sys.stderr)
        return 1
    if restart_errors:
        print("Service restoration failed: " + "; ".join(restart_errors), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
