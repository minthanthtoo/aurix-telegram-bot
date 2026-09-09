#!/usr/bin/env python3
"""Run the Telegram bot and authenticated VPN portal in one Render service.

This is the preferred single-instance MVP topology: both processes use the
same persistent SQLite file mounted at ``/var/data/bot.db``. If either process
fails, the supervisor exits so Render restarts the complete service.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from aurix_vpn.runtime import build_runtime_services
from aurix_vpn.vpn_web_api import AuriXVpnWebApplication, create_server


def _stop_child(child: subprocess.Popen[Any] | None) -> None:
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=25)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def main() -> int:
    try:
        port = int(os.environ.get("PORT", "10000"))
        max_age = int(os.environ.get("AURIX_WEB_APP_INIT_DATA_MAX_AGE", "86400"))
    except ValueError:
        print("PORT and AURIX_WEB_APP_INIT_DATA_MAX_AGE must be integers", file=sys.stderr)
        return 2
    if not 1 <= port <= 65_535:
        print("PORT must be between 1 and 65535", file=sys.stderr)
        return 2

    child: subprocess.Popen[Any] | None = None
    server = None
    runtime = None
    stopping = False

    def request_stop(*_signals: int) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        # Complete all schema initialization before the bot process starts.
        # Both processes then share the same already-migrated SQLite database.
        runtime = build_runtime_services(
            validate_telegram=True,
            check_outline=False,
            reconcile=False,
            configure_bootstrap=False,
        )
        application = AuriXVpnWebApplication(
            runtime,
            max_init_data_age=max_age,
            telegram_url=os.environ.get("AURIX_TELEGRAM_URL", ""),
        )
        server = create_server(application, port=port)
        server.timeout = 1
        child = subprocess.Popen([sys.executable, "-u", "app.py"], cwd=REPOSITORY_ROOT)
        print(f"AuriX combined bot and VPN portal listening on :{port}")
        while not stopping:
            child_status = child.poll()
            if child_status is not None:
                print(f"AuriX bot exited with status {child_status}", file=sys.stderr)
                return child_status or 1
            server.handle_request()
        return 0
    except Exception as exc:
        print(f"AuriX combined service startup failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.server_close()
        _stop_child(child)
        if runtime is not None:
            close_database = getattr(runtime.commerce_database, "close", None)
            if callable(close_database):
                close_database()


if __name__ == "__main__":
    raise SystemExit(main())
