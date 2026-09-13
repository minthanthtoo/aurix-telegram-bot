"""Loopback-only HTTP receiver for encrypted Android collector events."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import hashlib
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .crypto import MAX_ENVELOPE_BYTES, open_envelope
from .events import normalize_payload, safe_summary
from .store import ObservationStore


class CollectorServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        private_key: Path,
        store: ObservationStore,
        commerce_database: Path | None,
        admin_token: str | None = None,
    ):
        super().__init__(address, CollectorHandler)
        self.private_key = private_key
        self.store = store
        self.commerce_database = commerce_database
        self.admin_token = admin_token


class CollectorHandler(BaseHTTPRequestHandler):
    server: CollectorServer

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/v1/reconciliation-requests":
            self._create_reconciliation_request()
            return
        if path == "/v1/monitor-settings":
            self._update_server_settings()
            return
        if path == "/v1/device-settings":
            self._update_device_settings()
            return
        if path != "/v1/device-events":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if length <= 0 or length > MAX_ENVELOPE_BYTES:
            self.send_error(413)
            return
        envelope = self.rfile.read(length)
        try:
            payload = open_envelope(envelope, self.server.private_key)
            observation = normalize_payload(payload)
            result = self.server.store.ingest(
                observation, envelope, self.server.commerce_database
            )
        except ValueError:
            self.send_error(422)
            return
        response = json.dumps(
            {
                "accepted": True,
                "inserted": result.inserted,
                "event_id": result.event_id,
                "review_state": result.review_state,
                "suggested_order_id": result.suggested_order_id,
                "reconciliation_request_id": result.reconciliation_request_id,
                "monitor_settings": self.server.store.get_monitor_settings(),
            }
        )
        self._send_json(200, response)
        print(safe_summary(observation), flush=True)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/v1/reconciliation-requests":
                status = query.get("status", ["pending"])[0]
                limit = int(query.get("limit", ["20"])[0])
                self._send_json(
                    200,
                    {"requests": self.server.store.list_reconciliation_requests(
                        status=status, limit=limit
                    )},
                )
                return
            if parsed.path == "/v1/history-checkpoint":
                provider = query.get("provider", [""])[0]
                account_hash = query.get("account_hash", [None])[0]
                if not provider:
                    raise ValueError("provider is required")
                checkpoint = self.server.store.history_checkpoint(
                    provider=provider, account_hash=account_hash
                )
                self._send_json(200, {"provider": provider, "account_hash": account_hash, **checkpoint})
                return
            if parsed.path == "/v1/monitor-settings":
                self._send_json(200, {"monitor_settings": self.server.store.get_monitor_settings()})
                return
        except (ValueError, TypeError):
            self.send_error(400)
            return
        self.send_error(404)

    def do_PUT(self) -> None:
        if urlsplit(self.path).path == "/v1/monitor-settings":
            self._update_server_settings()
            return
        self.send_error(404)

    def _update_server_settings(self) -> None:
        if not self._authorized_admin():
            return
        try:
            payload = self._read_json(32 * 1024)
            interval = (
                None
                if payload.get("clear_override") is True
                else int(payload["routine_interval_seconds"])
            )
            settings = self.server.store.update_monitor_settings(
                source="server", interval_seconds=interval
            )
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            self.send_error(400)
            return
        self._send_json(200, {"accepted": True, "monitor_settings": settings})

    def _authorized_admin(self) -> bool:
        if not self.server.admin_token:
            self.send_error(503, "admin settings API is not configured")
            return False
        authorization = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.admin_token
        if not hmac.compare_digest(authorization, expected):
            self.send_response(401)
            self.send_header("WWW-Authenticate", "Bearer")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        return True

    def _update_device_settings(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_ENVELOPE_BYTES:
                raise ValueError("invalid envelope size")
            payload = open_envelope(self.rfile.read(length), self.server.private_key)
            if payload.get("type") != "monitor_settings":
                raise ValueError("invalid settings payload")
            settings = self.server.store.update_monitor_settings(
                source="device",
                interval_seconds=int(payload["routine_interval_seconds"]),
                device_id=str(payload["device_id"]),
            )
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            self.send_error(422)
            return
        self._send_json(200, {"accepted": True, "monitor_settings": settings})

    def _read_json(self, maximum: int) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > maximum:
            raise ValueError("invalid request size")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        return payload

    def _create_reconciliation_request(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 32 * 1024:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            idempotency_key = str(payload.get("idempotency_key") or "")
            request_id = (
                hashlib.sha256(f"server\0{idempotency_key}".encode()).hexdigest()
                if idempotency_key
                else None
            )
            identifier = self.server.store.create_reconciliation_request(
                provider=str(payload.get("provider") or ""),
                account_hash=payload.get("account_hash"),
                since_time=payload.get("since_time"),
                after_reference_hash=payload.get("after_reference_hash"),
                target_reference_hash=payload.get("target_reference_hash"),
                max_items=int(payload.get("max_items", 20)),
                reason=str(payload.get("reason") or "server_request"),
                request_id=request_id,
            )
        except (AttributeError, ValueError, TypeError, json.JSONDecodeError):
            self.send_error(400)
            return
        self._send_json(202, {"accepted": True, "request_id": identifier, "status": "pending"})

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        response = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        return


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=8765)
    result.add_argument("--private-key", type=Path, default=None)
    result.add_argument("--database", type=Path, default=Path("data/pay-monitor.db"))
    result.add_argument("--commerce-database", type=Path, default=None)
    result.add_argument("--admin-token-keychain-service", default=None)
    result.add_argument("--admin-token-keychain-account", default="aurix-pay-monitor-admin")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    private_key = args.private_key or (
        Path(os.environ["PAY_MONITOR_PRIVATE_KEY"])
        if os.environ.get("PAY_MONITOR_PRIVATE_KEY")
        else None
    )
    if private_key is None or not private_key.is_file():
        raise SystemExit("PAY_MONITOR_PRIVATE_KEY or --private-key must name a readable key")
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("The collector receiver must remain loopback-only")
    store = ObservationStore(args.database)
    store.initialize()
    admin_token = None
    if args.admin_token_keychain_service:
        secret = subprocess.run(
            [
                "/usr/bin/security", "find-generic-password", "-w",
                "-s", args.admin_token_keychain_service,
                "-a", args.admin_token_keychain_account,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        admin_token = secret.stdout.strip() if secret.returncode == 0 else None
        if not admin_token:
            raise SystemExit("configured pay-monitor admin token is unavailable in Keychain")
    server = CollectorServer(
        (args.host, args.port), private_key, store, args.commerce_database, admin_token
    )
    print(f"AuriX pay monitor listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
