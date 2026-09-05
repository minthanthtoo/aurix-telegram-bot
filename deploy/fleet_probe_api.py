#!/usr/bin/env python3
"""Run the AuriX node-agent probe API behind a TLS-terminating edge."""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server

from cryptography.fernet import Fernet, InvalidToken

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commerce import CommerceDatabase, PostgresCommerceDatabase  # noqa: E402
from control_api import create_control_wsgi_app  # noqa: E402
from device_api import DeviceAPIService, ManifestSigner, create_device_wsgi_app  # noqa: E402
from fleet_probe import FleetProbeService  # noqa: E402
from fleet_probe_api import create_probe_wsgi_app  # noqa: E402
from identity import IdentityService  # noqa: E402


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return None


def _agent_secrets() -> dict[str, str]:
    try:
        value = json.loads(os.environ.get("AURIX_PROBE_AGENT_SECRETS_JSON", "{}"))
    except json.JSONDecodeError as exc:
        raise SystemExit("AURIX_PROBE_AGENT_SECRETS_JSON must be a JSON object") from exc
    if not isinstance(value, dict):
        raise SystemExit("AURIX_PROBE_AGENT_SECRETS_JSON must be a JSON object")
    return {str(key): str(secret) for key, secret in value.items() if str(secret)}


def _manifest_signer() -> ManifestSigner:
    encoded = os.environ.get("AURIX_MANIFEST_SIGNING_KEY", "").strip()
    if not encoded:
        raise SystemExit("AURIX_MANIFEST_SIGNING_KEY is required for the control API")
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        return ManifestSigner(Ed25519PrivateKey.from_private_bytes(raw))
    except (ValueError, TypeError, binascii.Error) as exc:
        raise SystemExit("AURIX_MANIFEST_SIGNING_KEY must be a URL-safe base64 Ed25519 private key") from exc


def _access_url_decryptor():
    encoded = os.environ.get("AURIX_ACCESS_URL_KEY", "").strip()
    if not encoded:
        raise SystemExit("AURIX_ACCESS_URL_KEY is required for device route configuration")
    try:
        cipher = Fernet(encoded)
    except (TypeError, ValueError) as exc:
        raise SystemExit("AURIX_ACCESS_URL_KEY must be a Fernet key") from exc

    def decrypt(value: str) -> str | None:
        if not value:
            return None
        try:
            return cipher.decrypt(str(value).encode()).decode()
        except (InvalidToken, UnicodeDecodeError, ValueError):
            return None

    return decrypt


def main() -> None:
    if os.environ.get("AURIX_PROBE_API_ENABLED", "0").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        raise SystemExit("AURIX_PROBE_API_ENABLED must be enabled for the probe API")
    database_url = os.environ.get("COMMERCE_DATABASE_URL", "").strip()
    database = (
        PostgresCommerceDatabase(database_url)
        if database_url
        else CommerceDatabase(Path(os.environ.get("DATABASE_PATH", "data/bot.db")))
    )
    database.initialize()
    identity = IdentityService(database)
    try:
        identity.sync_existing_users()
        identity.sync_existing_entitlements()
    except Exception as exc:
        raise SystemExit(f"identity backfill failed: {type(exc).__name__}") from exc
    try:
        stale = int(os.environ.get("AURIX_PROBE_STALE_AFTER_SECONDS", "900"))
        ttl = int(os.environ.get("AURIX_PROBE_JOB_TTL_SECONDS", "180"))
        port = int(os.environ.get("AURIX_PROBE_API_PORT", "8080"))
    except ValueError as exc:
        raise SystemExit("probe API numeric environment settings are invalid") from exc
    service = FleetProbeService(
        database,
        agent_secrets=_agent_secrets(),
        stale_after_seconds=stale,
        job_ttl_seconds=ttl,
    )
    device_service = DeviceAPIService(
        database,
        manifest_signer=_manifest_signer(),
        route_provider=identity.routes_for_account,
        secret_decryptor=_access_url_decryptor(),
        identity=identity,
    )
    app = create_control_wsgi_app(create_probe_wsgi_app(service), create_device_wsgi_app(device_service))
    with make_server("127.0.0.1", port, app, handler_class=_QuietHandler) as server:
        print(f"AuriX probe API listening on 127.0.0.1:{port}")
        server.serve_forever()


if __name__ == "__main__":
    main()
