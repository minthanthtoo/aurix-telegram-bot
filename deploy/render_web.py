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
import base64
import binascii
from datetime import datetime, timezone
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


REGISTRATION_MAX_BODY = 64 * 1024
TRUTHY = {"1", "true", "yes", "on"}


class HealthHandler(BaseHTTPRequestHandler):
    """Return only process health; never expose configuration or secrets."""

    child: subprocess.Popen[bytes] | None = None
    heartbeat_path: str | None = None
    registration_database: Any = None
    registration_database_lock = threading.Lock()

    @staticmethod
    def _truthy(name: str) -> bool:
        return os.environ.get(name, "").strip().lower() in TRUTHY

    @classmethod
    def _database_for_registration(cls) -> Any:
        if cls.registration_database is not None:
            return cls.registration_database
        with cls.registration_database_lock:
            if cls.registration_database is not None:
                return cls.registration_database
            from commerce import CommerceDatabase, PostgresCommerceDatabase

            database_url = os.environ.get("COMMERCE_DATABASE_URL", "").strip()
            if database_url:
                database = PostgresCommerceDatabase(database_url)
            else:
                database_path = os.environ.get("DATABASE_PATH", "").strip()
                if not database_path or not os.path.isabs(database_path):
                    raise RuntimeError("registration database path is not configured")
                database = CommerceDatabase(Path(database_path))
            database.initialize()
            cls.registration_database = database
            return database

    def _json_response(self, status: int, payload: dict[str, Any]) -> None:
        body = (json.dumps(payload, sort_keys=True) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _register_node(self) -> None:
        if not self._truthy("AURIX_FLEET_REGISTRATION_ENABLED"):
            self._json_response(404, {"error": "not_found"})
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._json_response(415, {"error": "unsupported_media_type"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 1 or length > REGISTRATION_MAX_BODY:
            self._json_response(413 if length > REGISTRATION_MAX_BODY else 400, {"error": "invalid_body"})
            return
        raw = self.rfile.read(length)
        if len(raw) != length:
            self._json_response(400, {"error": "invalid_body"})
            return
        try:
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError
            required = {"token", "job_id", "node_id", "public_ip", "access_txt_b64", "ssh_host_key_b64"}
            if set(request) != required:
                raise ValueError
            decoded: dict[str, str] = {}
            for field in ("access_txt_b64", "ssh_host_key_b64"):
                decoded[field[:-4]] = base64.b64decode(str(request[field]), validate=True).decode("utf-8")
            payload = {
                "job_id": str(request["job_id"]),
                "node_id": str(request["node_id"]),
                "public_ip": str(request["public_ip"]),
                "access_txt": decoded["access_txt"],
                "ssh_host_key": decoded["ssh_host_key"],
            }
            token = str(request["token"])
            encryption_key = os.environ.get("AURIX_FLEET_ENROLLMENT_KEY", "").strip()
            if not encryption_key:
                self._json_response(503, {"error": "registration_unavailable"})
                return
            from fleet_enrollment import EnrollmentError, receive_enrollment

            result = receive_enrollment(
                self._database_for_registration(),
                token=token,
                payload=payload,
                encryption_key=encryption_key,
            )
        except (ValueError, TypeError, UnicodeError, binascii.Error):
            self._json_response(400, {"error": "invalid_request"})
            return
        except Exception as exc:
            from fleet_enrollment import EnrollmentError

            if isinstance(exc, EnrollmentError):
                self._json_response(400, {"error": "registration_rejected"})
            else:
                self._json_response(503, {"error": "registration_unavailable"})
            return
        self._json_response(200, {"status": result["status"], "job_id": result["job_id"]})

    @classmethod
    def _maintenance_health(cls) -> tuple[bool, dict[str, Any]]:
        path = str(cls.heartbeat_path or os.environ.get("AURIX_MAINTENANCE_HEARTBEAT_PATH", "")).strip()
        if not path:
            return True, {}
        try:
            with open(path, encoding="utf-8") as stream:
                heartbeat = json.load(stream)
            status = str(heartbeat.get("status") or "starting")
            last_success = heartbeat.get("last_success_at")
            if not last_success:
                return status != "error", {"maintenance_status": status}
            success_at = datetime.fromisoformat(str(last_success)).astimezone(timezone.utc)
            interval = max(30.0, float(os.environ.get("AURIX_MAINTENANCE_INTERVAL_SECONDS", "60")))
            age = max(0.0, (datetime.now(timezone.utc) - success_at).total_seconds())
            status = str(heartbeat.get("status") or "ok")
            # A recent successful pass must not mask a currently failing pass.
            # Otherwise a database/Outline outage could leave UptimeRobot seeing
            # HTTP 200 for up to three intervals while enforcement is broken.
            healthy = status != "error" and age <= interval * 3
            return healthy, {
                "maintenance_status": status,
                "maintenance_last_success_at": last_success,
                "maintenance_age_seconds": round(age, 1),
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # A first-start or partial write should not mark an otherwise
            # healthy child down; the next heartbeat will replace it.
            return True, {"maintenance_status": "unknown"}

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path not in {"/", "/healthz"}:
            self.send_error(404)
            return
        child = self.child
        running = child is not None and child.poll() is None
        maintenance_ok, maintenance_payload = self._maintenance_health()
        healthy = running and maintenance_ok
        payload: dict[str, Any] = {
            "status": "ok" if healthy else "degraded",
            "service": "aurix-telegram-bot",
        }
        payload.update(maintenance_payload)
        if child is not None:
            payload["bot_pid"] = child.pid
            if not running:
                payload["bot_exit_code"] = child.returncode
        body = (json.dumps(payload, sort_keys=True) + "\n").encode()
        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/fleet/register":
            self.send_error(404)
            return
        self._register_node()

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

    child_env = os.environ.copy()
    child_env.setdefault(
        "AURIX_MAINTENANCE_HEARTBEAT_PATH", "/tmp/aurix-maintenance-heartbeat.json"
    )
    HealthHandler.heartbeat_path = child_env["AURIX_MAINTENANCE_HEARTBEAT_PATH"]
    child = subprocess.Popen([sys.executable, "-u", "app.py"], env=child_env)
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
