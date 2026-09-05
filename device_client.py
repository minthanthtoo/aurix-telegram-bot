"""Reference managed-device client for the signed AuriX device protocol.

This small client is intentionally transport-only. A native Android/iOS app
can implement the same request signatures and manifest/config lifecycle without
depending on Telegram or the server's Python runtime.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from device_api import ManifestSigner, _b64, sign_device_request


class DeviceClientError(RuntimeError):
    """Raised when the control API rejects a managed-device request."""


def _public_key(private_key: Ed25519PrivateKey) -> str:
    return _b64(
        private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )


class DeviceClient:
    """Minimal reference implementation of pairing and signed API calls."""

    def __init__(
        self,
        base_url: str,
        device_id: str,
        private_key: Ed25519PrivateKey,
        manifest_signing_public_key: str,
        *,
        request: Callable[[str, bytes, Mapping[str, str]], Mapping[str, Any]] | None = None,
    ):
        self.base_url = str(base_url).rstrip("/")
        self.device_id = str(device_id)
        self.private_key = private_key
        self.manifest_signing_public_key = str(manifest_signing_public_key)
        self._requester = request or self._urlopen_json

    @classmethod
    def pair(
        cls,
        base_url: str,
        pairing_token: str,
        private_key: Ed25519PrivateKey,
        *,
        label: str = "",
        request: Callable[[str, bytes, Mapping[str, str]], Mapping[str, Any]] | None = None,
    ) -> "DeviceClient":
        body = json.dumps(
            {
                "token": str(pairing_token),
                "public_key": _public_key(private_key),
                "label": str(label)[:128],
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        requester = request or cls._urlopen_json
        result = dict(requester(str(base_url).rstrip("/") + "/v1/devices/pair", body, {}))
        if not result.get("device_id") or not result.get("manifest_signing_public_key"):
            raise DeviceClientError("pair response is incomplete")
        return cls(
            base_url,
            str(result["device_id"]),
            private_key,
            str(result["manifest_signing_public_key"]),
            request=requester,
        )

    @staticmethod
    def _urlopen_json(url: str, body: bytes, headers: Mapping[str, str]) -> Mapping[str, Any]:
        request = urllib.request.Request(
            url,
            data=body if body else None,
            headers={"Content-Type": "application/json", **dict(headers)},
            method="POST" if body else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, UnicodeError) as exc:
            raise DeviceClientError("device API request failed") from exc
        if not isinstance(payload, dict):
            raise DeviceClientError("device API response is invalid")
        if "error" in payload:
            raise DeviceClientError(str(payload["error"])[:240])
        return payload

    def _call(self, method: str, path: str, body: bytes = b"") -> dict[str, Any]:
        timestamp = str(time.time())
        signature = sign_device_request(method, path, timestamp, body, self.private_key)
        result = self._requester(
            self.base_url + path,
            body,
            {
                "X-AuriX-Device-ID": self.device_id,
                "X-AuriX-Request-Timestamp": timestamp,
                "X-AuriX-Request-Signature": signature,
            },
        )
        if not isinstance(result, dict):
            raise DeviceClientError("device API response is invalid")
        return result

    def manifest(self) -> dict[str, Any]:
        signed = self._call("GET", "/v1/devices/manifest")
        if not ManifestSigner.verify(
            signed,
            self.manifest_signing_public_key,
            now=datetime.now(timezone.utc),
        ):
            raise DeviceClientError("manifest signature or expiry is invalid")
        manifest = signed.get("manifest")
        if not isinstance(manifest, dict):
            raise DeviceClientError("manifest payload is invalid")
        return manifest

    def config(self, route_id: str) -> dict[str, Any]:
        route = str(route_id).strip()
        if not route or len(route) > 128 or any(char in route for char in "?#&"):
            raise DeviceClientError("route_id is invalid")
        return self._call("GET", "/v1/devices/config?route_id=" + route)

    def acknowledge(
        self,
        outcome: str,
        *,
        route_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(
            {
                "outcome": str(outcome),
                "route_id": str(route_id) if route_id else None,
                "details": dict(details) if details else None,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return self._call("POST", "/v1/devices/ack", body)


__all__ = ["DeviceClient", "DeviceClientError"]
