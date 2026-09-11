"""Signed managed-device pairing, route manifests, and config delivery."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .identity import IdentityError, IdentityService


MAX_BODY_BYTES = 128 * 1024
REQUEST_CLOCK_SKEW_SECONDS = 300
ALLOWED_CONFIG_SCHEMES = {"ss", "ssconf", "vless", "hysteria2", "hy2", "trojan", "vmess", "wireguard", "wg"}


class DeviceAPIError(RuntimeError):
    """Raised when a device request cannot be authenticated or validated."""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = int(status_code)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(str(value) + "=" * (-len(str(value)) % 4))


def sign_device_request(
    method: str,
    path: str,
    timestamp: str,
    body: bytes,
    private_key: Ed25519PrivateKey,
) -> str:
    message = b"\n".join(
        (
            str(method).upper().encode(),
            str(path).encode(),
            str(timestamp).encode(),
            hashlib.sha256(body).hexdigest().encode(),
        )
    )
    return _b64(private_key.sign(message))


class ManifestSigner:
    def __init__(self, private_key: Ed25519PrivateKey | None = None, *, key_id: str = "aurix-manifest-1"):
        self.private_key = private_key or Ed25519PrivateKey.generate()
        self.key_id = str(key_id)[:64]

    @classmethod
    def from_base64_seed(cls, encoded: str, *, key_id: str = "aurix-manifest-1") -> "ManifestSigner":
        """Load the durable 32-byte Ed25519 seed used by managed devices.

        The value is intentionally passed in by the caller instead of read from
        the environment here, keeping the signer usable by web, CLI, and test
        entrypoints without coupling this protocol module to process config.
        """
        try:
            seed = _unb64(str(encoded).strip())
        except (ValueError, TypeError, binascii.Error) as exc:
            raise ValueError("manifest signing seed is not valid base64") from exc
        if len(seed) != 32:
            raise ValueError("manifest signing seed must decode to 32 bytes")
        return cls(Ed25519PrivateKey.from_private_bytes(seed), key_id=key_id)

    @property
    def public_key(self) -> str:
        return _b64(
            self.private_key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        )

    def sign(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(manifest)
        return {
            "manifest": payload,
            "signature": _b64(self.private_key.sign(_canonical(payload))),
            "key_id": self.key_id,
        }

    @staticmethod
    def verify(signed: Mapping[str, Any], public_key: str, *, now: datetime | None = None) -> bool:
        try:
            key = Ed25519PublicKey.from_public_bytes(_unb64(public_key))
            manifest = signed["manifest"]
            if not isinstance(manifest, Mapping):
                return False
            key.verify(_unb64(str(signed["signature"])), _canonical(manifest))
            if now is not None:
                issued = datetime.fromisoformat(str(manifest["issued_at"]).replace("Z", "+00:00"))
                expiry = datetime.fromisoformat(str(manifest["expires_at"]).replace("Z", "+00:00"))
                if issued.tzinfo is None:
                    issued = issued.replace(tzinfo=timezone.utc)
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
                if issued - timedelta(seconds=REQUEST_CLOCK_SKEW_SECONDS) > current or expiry <= current:
                    return False
            return True
        except (KeyError, ValueError, TypeError, InvalidSignature, OverflowError, binascii.Error):
            return False


class DeviceAPIService:
    def __init__(
        self,
        database: Any,
        *,
        manifest_signer: ManifestSigner,
        route_provider: Callable[[str], list[dict[str, Any]]],
        secret_decryptor: Callable[[str], str | None] | None = None,
        identity: IdentityService | None = None,
        max_active_devices: int | None = None,
        clock: Callable[[], float] = time.time,
    ):
        if max_active_devices is not None:
            if isinstance(max_active_devices, bool):
                raise ValueError("max_active_devices must be an integer")
            try:
                max_active_devices = int(max_active_devices)
            except (TypeError, ValueError) as exc:
                raise ValueError("max_active_devices must be an integer") from exc
            if not 1 <= max_active_devices <= 1000:
                raise ValueError("max_active_devices is outside the allowed range")
        self.database = database
        self.identity = identity or IdentityService(database)
        self.manifest_signer = manifest_signer
        self.route_provider = route_provider
        self.secret_decryptor = secret_decryptor
        self.max_active_devices = max_active_devices
        self.clock = clock

    def _authenticate(
        self,
        device_id: str,
        method: str,
        path: str,
        timestamp: str,
        body: bytes,
        signature: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        record = self.identity.device_auth_record(device_id)
        if record is None or record.get("status") != "active" or record.get("account_status") != "active":
            raise DeviceAPIError("device is not active", status_code=401)
        try:
            request_time = float(timestamp)
        except (TypeError, ValueError) as exc:
            raise DeviceAPIError("request timestamp is invalid", status_code=401) from exc
        if abs(float(self.clock() if now is None else now) - request_time) > REQUEST_CLOCK_SKEW_SECONDS:
            raise DeviceAPIError("request timestamp is expired", status_code=401)
        message = b"\n".join(
            (
                str(method).upper().encode(),
                str(path).encode(),
                str(timestamp).encode(),
                hashlib.sha256(body).hexdigest().encode(),
            )
        )
        try:
            Ed25519PublicKey.from_public_bytes(_unb64(str(record["public_key"]))).verify(
                _unb64(signature), message
            )
        except (ValueError, TypeError, InvalidSignature, binascii.Error) as exc:
            raise DeviceAPIError("device request signature is invalid", status_code=401) from exc
        self.identity.touch_device(device_id)
        return record

    def pair(self, token: str, public_key: str, *, label: str = "") -> dict[str, Any]:
        try:
            Ed25519PublicKey.from_public_bytes(_unb64(str(public_key)))
        except (ValueError, TypeError, binascii.Error) as exc:
            raise DeviceAPIError("public key is invalid") from exc
        try:
            pairing_options = {"label": label}
            if self.max_active_devices is not None:
                pairing_options["max_active_devices"] = self.max_active_devices
            result = self.identity.consume_pairing_token(token, public_key, **pairing_options)
            result["manifest_signing_key_id"] = self.manifest_signer.key_id
            result["manifest_signing_public_key"] = self.manifest_signer.public_key
            return result
        except IdentityError as exc:
            status_code = 409 if str(exc) == "active managed device limit reached" else 400
            raise DeviceAPIError(str(exc), status_code=status_code) from exc

    def manifest(self, device_id: str) -> dict[str, Any]:
        record = self.identity.device_auth_record(device_id)
        if record is None or record.get("status") != "active" or record.get("account_status") != "active":
            raise DeviceAPIError("device is not active", status_code=401)
        routes = self.route_provider(str(record["account_id"]))
        if not isinstance(routes, list) or len(routes) > 100 or any(not isinstance(route, Mapping) for route in routes):
            raise DeviceAPIError("route provider returned invalid data")
        issued_at = datetime.fromtimestamp(float(self.clock()), timezone.utc)
        manifest = {
            "version": 1,
            "device_id": str(device_id),
            "account_id": str(record["account_id"]),
            "revocation_epoch": int(record.get("revocation_epoch") or 0),
            "issued_at": issued_at.isoformat(),
            "expires_at": (issued_at + timedelta(minutes=15)).isoformat(),
            "routes": [dict(route) for route in routes],
        }
        return self.manifest_signer.sign(manifest)

    def config(self, device_id: str, route_id: str) -> dict[str, Any]:
        if self.secret_decryptor is None:
            raise DeviceAPIError("route configuration is not available", status_code=503)
        record = self.identity.device_auth_record(device_id)
        if record is None or record.get("status") != "active" or record.get("account_status") != "active":
            raise DeviceAPIError("device is not active", status_code=401)
        route = self.identity.route_secret_record(str(record["account_id"]), str(route_id))
        if route is None:
            raise DeviceAPIError("route is not available", status_code=404)
        try:
            access_url = self.secret_decryptor(str(route.get("secret_ciphertext") or ""))
        except Exception as exc:
            raise DeviceAPIError("route configuration could not be decrypted", status_code=503) from exc
        scheme = urlsplit(str(access_url or "")).scheme.lower()
        if not access_url or scheme not in ALLOWED_CONFIG_SCHEMES:
            raise DeviceAPIError("route configuration is unavailable", status_code=503)
        return {
            "route_id": str(route["generation_id"]),
            "endpoint_id": str(route["endpoint_id"]),
            "region": str(route["region"]),
            "protocol": str(route["protocol"]),
            "transport": str(route["transport"]),
            "generation": int(route["generation_no"]),
            "access_url": str(access_url),
        }

    def acknowledge(self, device_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        try:
            outcome = str(body.get("outcome") or "")
            route_id = str(body.get("route_id") or "")[:128] or None
            details = body.get("details") if isinstance(body.get("details"), dict) else None
            accepted = self.identity.acknowledge_device(
                device_id, route_id=route_id, outcome=outcome, details=details
            )
        except IdentityError as exc:
            raise DeviceAPIError(str(exc)) from exc
        return {"accepted": accepted}


def _response(status: str, value: Mapping[str, Any]) -> tuple[str, list[tuple[str, str]], list[bytes]]:
    body = json.dumps(dict(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return status, [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("Pragma", "no-cache"),
    ], [body]


def create_device_wsgi_app(
    service: DeviceAPIService,
    *,
    clock: Callable[[], float] = time.time,
) -> Callable[..., Any]:
    def app(environ: Mapping[str, Any], start_response: Callable[..., Any]):
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        path = str(environ.get("PATH_INFO") or "/")
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except (TypeError, ValueError):
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            status, headers, body = _response("413 Request Entity Too Large", {"error": "body_too_large"})
            start_response(status, headers)
            return body
        stream = environ.get("wsgi.input")
        body = stream.read(length) if stream is not None else b""
        if method == "GET" and path in {"/healthz", "/v1/devices/healthz"}:
            status, headers, parts = _response("200 OK", {"status": "ok"})
            start_response(status, headers)
            return parts
        try:
            value = json.loads(body.decode("utf-8")) if body else {}
            if not isinstance(value, dict):
                raise DeviceAPIError("request body must be an object")
            if method == "POST" and path == "/v1/devices/pair":
                result = service.pair(
                    str(value.get("token") or ""),
                    str(value.get("public_key") or ""),
                    label=str(value.get("label") or ""),
                )
            else:
                device_id = str(environ.get("HTTP_X_AURIX_DEVICE_ID") or "")
                timestamp = str(environ.get("HTTP_X_AURIX_REQUEST_TIMESTAMP") or "")
                signature = str(environ.get("HTTP_X_AURIX_REQUEST_SIGNATURE") or "")
                request_path = path + (
                    "?" + str(environ.get("QUERY_STRING")) if environ.get("QUERY_STRING") else ""
                )
                service._authenticate(device_id, method, request_path, timestamp, body, signature, now=clock())
                if method == "GET" and path == "/v1/devices/manifest":
                    result = service.manifest(device_id)
                elif method == "GET" and path == "/v1/devices/config":
                    route_values = parse_qs(str(environ.get("QUERY_STRING") or ""), keep_blank_values=False)
                    route_id = str((route_values.get("route_id") or [""])[0])
                    if not route_id or len(route_id) > 128:
                        raise DeviceAPIError("route_id is required")
                    result = service.config(device_id, route_id)
                elif method == "POST" and path == "/v1/devices/ack":
                    result = service.acknowledge(device_id, value)
                else:
                    status, headers, parts = _response("404 Not Found", {"error": "not_found"})
                    start_response(status, headers)
                    return parts
            status, headers, parts = _response("200 OK", result)
        except (DeviceAPIError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            status_code = int(getattr(exc, "status_code", 400))
            reason = {401: "Unauthorized", 404: "Not Found", 409: "Conflict", 413: "Request Entity Too Large", 503: "Service Unavailable"}.get(
                status_code, "Bad Request"
            )
            status, headers, parts = _response(f"{status_code} {reason}", {"error": str(exc)[:240]})
        except Exception:
            status, headers, parts = _response(
                "500 Internal Server Error", {"error": "device_api_unavailable"}
            )
        start_response(status, headers)
        return parts

    return app
