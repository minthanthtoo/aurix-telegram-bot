#!/usr/bin/env python3
"""Run the long-polling bot behind a tiny Render Web Service health port.

This entrypoint is for the experimental Free Web Service profile only.  The
bot remains the child process, while the parent binds Render's PORT so the
service can be monitored by UptimeRobot.  A child exit terminates the parent,
allowing Render to restart the service instead of leaving a green dead shell.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class HealthHandler(BaseHTTPRequestHandler):
    """Return only process health; never expose configuration or secrets."""

    child: subprocess.Popen[bytes] | None = None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path not in {"/", "/healthz"}:
            self.send_error(404)
            return
        child = self.child
        running = child is not None and child.poll() is None
        payload: dict[str, Any] = {
            "status": "ok" if running else "degraded",
            "service": "aurix-telegram-bot",
        }
        if child is not None:
            payload["bot_pid"] = child.pid
            if not running:
                payload["bot_exit_code"] = child.returncode
        body = (json.dumps(payload, sort_keys=True) + "\n").encode()
        self.send_response(200 if running else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # Avoid request URLs becoming noisy or accidentally carrying query data.
        sys.stderr.write("render health request\n")


def main() -> int:
    try:
        port = int(os.environ.get("PORT", "10000"))
    except ValueError:
        print("PORT must be an integer", file=sys.stderr)
        return 2
    if not 1 <= port <= 65535:
        print("PORT must be between 1 and 65535", file=sys.stderr)
        return 2

    child = subprocess.Popen([sys.executable, "-u", "app.py"])
    HealthHandler.child = child
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    server_thread = threading.Thread(
        target=server.serve_forever, name="render-health", daemon=True
    )
    server_thread.start()
    stopping = False

    def stop(*_signals: int) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        server.shutdown()
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=25)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while child.poll() is None and not stopping:
            time.sleep(1)
    finally:
        stop()
        server.server_close()
    return child.returncode if child.returncode not in (None, 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
