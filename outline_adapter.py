"""Pinned-TLS adapter for the Outline Management API."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import ssl
import time
import urllib.parse
from typing import Any, Mapping

from entitlements import OutlineError
from observability import latency_log as _latency_log


class OutlineClient:
    def __init__(self, api_url: str, cert_sha256: str):
        parsed = urllib.parse.urlsplit(api_url.rstrip("/"))
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("OUTLINE_API_URL must be an https URL")
        fingerprint = cert_sha256.lower().replace(":", "")
        if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ValueError("OUTLINE_CERT_SHA256 must be 64 hexadecimal characters")
        self.host = parsed.hostname
        self.port = parsed.port or 443
        self.base_path = parsed.path.rstrip("/")
        self.fingerprint = fingerprint

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        accepted_statuses: tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        # Outline commonly uses a self-signed certificate. Trust only exact pinned cert.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        started_at = time.perf_counter()
        connection = http.client.HTTPSConnection(self.host, self.port, context=context, timeout=15)
        payload = json.dumps(body).encode() if body is not None else None
        response_status: int | None = None
        try:
            connection.connect()
            certificate = connection.sock.getpeercert(binary_form=True)
            actual = hashlib.sha256(certificate).hexdigest()
            if not hmac.compare_digest(actual, self.fingerprint):
                raise OutlineError("Outline TLS certificate fingerprint mismatch")
            connection.request(
                method,
                self.base_path + path,
                body=payload,
                headers={"Content-Type": "application/json"} if payload else {},
            )
            response = connection.getresponse()
            response_status = response.status
            raw = response.read()
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise OutlineError(f"Outline request failed: {exc}") from exc
        finally:
            connection.close()
            _latency_log(
                "outline_request",
                started_at,
                method=method,
                resource=path.split("/", 2)[1]
                if path.startswith("/") and "/" in path[1:]
                else path,
                status=response_status or "error",
            )
        if response.status not in accepted_statuses:
            raise OutlineError(f"Outline returned HTTP {response.status}", status=response.status)
        if response.status == 404:
            return None
        return json.loads(raw) if raw else None

    def server_info(self) -> dict[str, Any]:
        result = self._request("GET", "/server")
        if not isinstance(result, dict):
            raise OutlineError("Outline server response is not an object")
        return result

    def transfer_metrics(self) -> dict[str, Any]:
        result = self._request("GET", "/metrics/transfer")
        if not isinstance(result, dict):
            raise OutlineError("Outline transfer metrics response is not an object")
        return result

    def experimental_metrics(self, since: str = "30d") -> dict[str, Any]:
        """Return Outline 1.12+ operational telemetry.

        The endpoint is explicitly experimental in Outline's OpenAPI contract,
        so callers must tolerate 404/shape changes and fall back to transfer
        metrics. ``since`` is kept conservative and URL encoded.
        """
        query = urllib.parse.urlencode({"since": str(since)[:32]})
        result = self._request("GET", f"/experimental/server/metrics?{query}")
        if not isinstance(result, dict):
            raise OutlineError("Outline experimental metrics response is not an object")
        return result

    def set_hostname_for_access_keys(self, hostname: str) -> None:
        """Set the public host embedded in newly listed/generated access URLs."""
        value = str(hostname or "").strip().rstrip(".")
        if not value:
            raise ValueError("hostname is required")
        self._request(
            "PUT",
            "/server/hostname-for-access-keys",
            {"hostname": value},
            accepted_statuses=(204,),
        )

    def create_key(self, name: str, limit_bytes: int | None) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name}
        if limit_bytes is not None:
            body["limit"] = {"bytes": limit_bytes}
        result = self._request("POST", "/access-keys", body)
        if not isinstance(result, dict) or not result.get("id") or not result.get("accessUrl"):
            raise OutlineError("Outline create response lacks id or accessUrl")
        return result

    def list_keys(self) -> dict[str, Any]:
        result = self._request("GET", "/access-keys")
        if not isinstance(result, dict) or not isinstance(result.get("accessKeys"), list):
            raise OutlineError("Outline list response lacks accessKeys")
        return result

    def get_key(self, key_id: str) -> dict[str, Any] | None:
        result = self._request(
            "GET",
            f"/access-keys/{urllib.parse.quote(key_id, safe='')}",
            accepted_statuses=(200, 404),
        )
        if result is None:
            return None
        if not isinstance(result, dict) or not result.get("id"):
            raise OutlineError("Outline key response lacks id")
        return result

    def set_data_limit(self, key_id: str, limit_bytes: int) -> None:
        self._request(
            "PUT",
            f"/access-keys/{urllib.parse.quote(key_id, safe='')}/data-limit",
            {"limit": {"bytes": limit_bytes}},
        )

    def delete_key(self, key_id: str) -> None:
        # A 404 is converged state when revoking a key already known to AuriX.
        self._request(
            "DELETE",
            f"/access-keys/{urllib.parse.quote(key_id, safe='')}",
            accepted_statuses=(200, 204, 404),
        )

    def create_key_with_id(self, key_id: str, name: str, limit_bytes: int | None) -> dict[str, Any]:
        """Create a caller-selected key where the deployed API supports it."""
        body: dict[str, Any] = {"name": name}
        if limit_bytes is not None:
            body["limit"] = {"bytes": limit_bytes}
        result = self._request("PUT", f"/access-keys/{urllib.parse.quote(key_id, safe='')}", body)
        if not isinstance(result, dict) or not result.get("id") or not result.get("accessUrl"):
            raise OutlineError("Outline deterministic create response lacks id or accessUrl")
        return result

    def delete_data_limit(self, key_id: str) -> None:
        self._request(
            "DELETE",
            f"/access-keys/{urllib.parse.quote(key_id, safe='')}/data-limit",
            accepted_statuses=(200, 204, 404),
        )

    def rename_key(self, key_id: str, name: str) -> None:
        self._request(
            "PUT",
            f"/access-keys/{urllib.parse.quote(key_id, safe='')}/name",
            {"name": name[:128]},
        )


class OutlineServerPool:
    """Named Outline clients with legacy delegation to the default server."""

    def __init__(self, clients: Mapping[str, OutlineClient], default_server_id: str):
        normalized = {str(key): value for key, value in clients.items() if str(key)}
        if not normalized or default_server_id not in normalized:
            raise ValueError("Outline server pool requires a valid default server")
        self.clients = normalized
        self.default_server_id = str(default_server_id)

    def server_ids(self) -> tuple[str, ...]:
        return tuple(self.clients)

    def client(self, server_id: str | None = None) -> OutlineClient:
        target = str(server_id or self.default_server_id)
        try:
            return self.clients[target]
        except KeyError as exc:
            raise OutlineError(f"Outline server {target!r} is not configured") from exc

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client(), name)
