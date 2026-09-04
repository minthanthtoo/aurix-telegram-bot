#!/usr/bin/env python3
"""Standalone TLS callback service for zero-touch AuriX node enrollment.

The Render web entrypoint already exposes the same handler.  This process is
for an operator-owned control plane that needs an independent, systemd-managed
HTTPS listener.  It accepts no GET diagnostics beyond a liveness response and
never prints enrollment payloads or credentials.
"""

from __future__ import annotations

import os
import signal
import ssl
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy.render_web import HealthHandler  # noqa: E402


class RegistrationHandler(HealthHandler):
    """Health-only GET surface with the shared POST enrollment contract."""

    child = None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path not in {"/", "/healthz"}:
            self.send_error(404)
            return
        self._json_response(200, {"status": "ok", "service": "aurix-fleet-registration"})


def _required_file(name: str) -> Path:
    value = os.environ.get(name, "").strip()
    path = Path(value)
    if not value or not path.is_absolute() or not path.is_file():
        raise SystemExit(f"fleet registration server: {name} must be an existing absolute file")
    return path


def main() -> int:
    if os.environ.get("AURIX_FLEET_REGISTRATION_ENABLED", "").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        print("fleet registration server: AURIX_FLEET_REGISTRATION_ENABLED is not enabled", file=sys.stderr)
        return 2
    try:
        port = int(os.environ.get("AURIX_FLEET_REGISTRATION_PORT", "8443"))
    except ValueError:
        print("fleet registration server: port must be an integer", file=sys.stderr)
        return 2
    if not 1 <= port <= 65535:
        print("fleet registration server: port is out of range", file=sys.stderr)
        return 2
    certificate = _required_file("AURIX_FLEET_REGISTRATION_TLS_CERT")
    private_key = _required_file("AURIX_FLEET_REGISTRATION_TLS_KEY")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=certificate, keyfile=private_key)
    RegistrationHandler.registration_database = None
    server = ThreadingHTTPServer(("0.0.0.0", port), RegistrationHandler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    stopping = False

    def stop(*_signals: int) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        # ``shutdown`` must be called from a thread other than the one running
        # ``serve_forever``; signal handlers execute on the main thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    serving: threading.Thread | None = None
    try:
        print(f"fleet registration server: listening on {port}", flush=True)
        serving = threading.Thread(target=server.serve_forever, name="fleet-registration", daemon=True)
        serving.start()
        while not stopping:
            serving.join(timeout=1)
    finally:
        stop()
        if serving is not None:
            serving.join(timeout=5)
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
