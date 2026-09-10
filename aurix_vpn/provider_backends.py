"""Concrete, locally supervised provider backends for the node-agent seam.

These backends are intentionally separate from commercial policy. They own the
provider-specific local state and control surfaces only. Every external runner,
reload hook, probe, and HTTP request is injectable so the contract can be tested
without a live VPN server.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from cryptography.fernet import Fernet, InvalidToken

from .node_agent import NodeAgentError, XrayConfigWriter


class ProviderBackendError(NodeAgentError):
    """A provider backend cannot safely complete an operation."""


def _int_value(value: Any, *, field: str) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError) as exc:
        raise ProviderBackendError(f"{field} is not an integer") from exc


class XrayStatsParser:
    """Parse the documented Xray ``statsquery`` JSON shape."""

    @staticmethod
    def parse(payload: Any, external_id: str) -> dict[str, Any]:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ProviderBackendError("Xray stats response is invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise ProviderBackendError("Xray stats response is not an object")
        rows = payload.get("stat")
        if not isinstance(rows, list):
            raise ProviderBackendError("Xray stats response lacks a stat list")
        prefix = f"user>>>{external_id}>>>traffic>>>"
        result: dict[str, Any] = {"external_id": external_id, "tx_bytes": 0, "rx_bytes": 0}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            name = str(row.get("name") or "")
            if not name.startswith(prefix):
                continue
            value = _int_value(row.get("value"), field="Xray stat value")
            if name == prefix + "uplink":
                result["tx_bytes"] = value
            elif name == prefix + "downlink":
                result["rx_bytes"] = value
            elif name == prefix + "online":
                result["online"] = value
        result["bytes_transferred"] = result["tx_bytes"] + result["rx_bytes"]
        return result


class XrayConfigProvider:
    """Xray provider using an explicitly tagged config and supervised reload.

    This is the conservative fallback for hosts where Xray's gRPC API is not
    enrolled. The reload hook must be supplied by the host supervisor; this
    class never invokes a shell or restarts a daemon itself. Per-user counters
    come from an injected StatsService query. There is deliberately no
    ``set_user_quota`` method because Xray native hard-quota enforcement is not
    proven by the statistics interface.
    """

    def __init__(
        self,
        writer: XrayConfigWriter,
        *,
        reload_callback: Callable[[], Any] | None,
        stats_query: Callable[[str], Any] | None = None,
        probe_callback: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        version: str = "config-writer",
    ):
        if not callable(reload_callback):
            raise ValueError("Xray provider requires a supervised reload callback")
        self.writer = writer
        self.reload_callback = reload_callback
        self.stats_query = stats_query
        self.probe_callback = probe_callback
        self.version = str(version)[:64]

    def server_info(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "provider": "xray-config-writer",
            "capabilities": ["managed_users", "usage", "reconciliation"],
        }

    def list_users(self) -> list[dict[str, Any]]:
        return self.writer.list_users()

    def get_user(self, external_id: str) -> dict[str, Any] | None:
        return self.writer.get_user(external_id)

    def _reload_if_changed(self, result: Mapping[str, Any]) -> None:
        if not bool(result.get("changed")):
            return
        try:
            self.reload_callback()
        except Exception as exc:
            raise ProviderBackendError("Xray config changed but supervised reload failed") from exc

    def create_user(
        self,
        external_id: str,
        name: str,
        route: Mapping[str, Any],
        intent: Mapping[str, Any],
    ) -> dict[str, Any]:
        requested_tag = str(route.get("inbound_tag") or self.writer.inbound_tag)
        if requested_tag != self.writer.inbound_tag:
            raise ProviderBackendError("Xray route targets a different managed inbound")
        result = self.writer.upsert_user(
            external_id,
            name,
            intent=intent,
        )
        self._reload_if_changed(result)
        return {
            "external_id": str(external_id),
            "name": str(name)[:128],
            "secret": str(external_id),
            "changed": bool(result.get("changed")),
        }

    def delete_user(self, external_id: str) -> None:
        result = self.writer.remove_user(external_id)
        self._reload_if_changed(result)

    def get_user_usage(self, external_id: str) -> dict[str, Any]:
        if not callable(self.stats_query):
            raise ProviderBackendError("Xray StatsService query is not configured")
        return XrayStatsParser.parse(self.stats_query(str(external_id)), str(external_id))

    def terminate_user_sessions(self, _external_id: str) -> dict[str, Any]:
        return {
            "supported": False,
            "terminated": False,
            "reason": "Xray user removal does not prove existing-session termination",
        }

    def probe_data_plane(self, route: Mapping[str, Any]) -> dict[str, Any]:
        if not callable(self.probe_callback):
            return {"status": "unsupported", "reason": "authenticated probe is not configured"}
        result = self.probe_callback(dict(route))
        if not isinstance(result, Mapping):
            raise ProviderBackendError("Xray data-plane probe returned an invalid response")
        return dict(result)


class Hysteria2TrafficStatsClient:
    """Bounded client for Hysteria2's local Traffic Stats API."""

    def __init__(
        self,
        base_url: str,
        secret: str,
        *,
        timeout: float = 5.0,
        requester: Callable[[str, str, bytes | None, Mapping[str, str]], Any] | None = None,
    ):
        normalized = str(base_url).strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("Hysteria2 stats URL must use HTTP or HTTPS")
        if not str(secret):
            raise ValueError("Hysteria2 stats API secret is required")
        self.base_url = normalized + "/"
        self.secret = str(secret)
        self.timeout = max(0.5, min(float(timeout), 30.0))
        self.requester = requester

    def request(self, method: str, path: str, payload: Any = None) -> Any:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Accept": "application/json", "Authorization": self.secret}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.requester is not None:
            return self.requester(method, path, body, headers)
        request = urllib.request.Request(
            urljoin(self.base_url, str(path).lstrip("/")),
            data=body,
            headers=headers,
            method=str(method).upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(1024 * 1024)
        except urllib.error.HTTPError as exc:
            raise ProviderBackendError(f"Hysteria2 stats request failed: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderBackendError("Hysteria2 stats request failed") from exc
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderBackendError("Hysteria2 stats response is invalid JSON") from exc

    def server_info(self) -> dict[str, Any]:
        self.request("GET", "/online")
        return {"provider": "hysteria2-traffic-stats", "capabilities": ["usage", "kick"]}

    def traffic(self) -> dict[str, Any]:
        value = self.request("GET", "/traffic")
        if not isinstance(value, Mapping):
            raise ProviderBackendError("Hysteria2 traffic response is not an object")
        return {str(key): dict(record) for key, record in value.items() if isinstance(record, Mapping)}

    def kick(self, external_ids: list[str]) -> Any:
        return self.request("POST", "/kick", [str(item) for item in external_ids])


class Hysteria2UserStore:
    """Encrypted local auth store for Hysteria2 HTTP authentication."""

    def __init__(self, path: str | os.PathLike[str], *, encryption_key: bytes | str):
        try:
            key = encryption_key.encode() if isinstance(encryption_key, str) else bytes(encryption_key)
            self.cipher = Fernet(key)
        except (TypeError, ValueError) as exc:
            raise ValueError("Hysteria2 user-store key must be a Fernet key") from exc
        self.path = Path(path)
        self._digest_key = hashlib.sha256(key).digest()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "users": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderBackendError("Hysteria2 user store could not be read") from exc
        if not isinstance(value, Mapping) or not isinstance(value.get("users"), Mapping):
            raise ProviderBackendError("Hysteria2 user store has an invalid shape")
        return {"version": int(value.get("version") or 1), "users": dict(value["users"])}

    def _write(self, value: Mapping[str, Any]) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(dict(value), handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise ProviderBackendError("Hysteria2 user store could not be written") from exc

    def _digest(self, secret: str) -> str:
        return hmac.new(self._digest_key, str(secret).encode(), hashlib.sha256).hexdigest()

    def _record(self, external_id: str, row: Mapping[str, Any], *, include_secret: bool) -> dict[str, Any]:
        result = {
            "external_id": str(external_id),
            "name": str(row.get("name") or "")[:128],
            "quota_bytes": row.get("quota_bytes"),
        }
        if include_secret:
            try:
                result["secret"] = self.cipher.decrypt(str(row["secret_ciphertext"]).encode()).decode()
            except (KeyError, InvalidToken, UnicodeDecodeError) as exc:
                raise ProviderBackendError("Hysteria2 user secret cannot be decrypted") from exc
        return result

    def list_users(self) -> list[dict[str, Any]]:
        value = self._load()
        return [self._record(key, row, include_secret=False) for key, row in value["users"].items()]

    def get_user(self, external_id: str) -> dict[str, Any] | None:
        value = self._load()
        row = value["users"].get(str(external_id))
        return self._record(str(external_id), row, include_secret=True) if isinstance(row, Mapping) else None

    def create_user(
        self,
        external_id: str,
        name: str,
        secret: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        value = self._load()
        key = str(external_id)
        existing = value["users"].get(key)
        if isinstance(existing, Mapping):
            if not hmac.compare_digest(str(existing.get("auth_digest") or ""), self._digest(secret)):
                raise ProviderBackendError("Hysteria2 external ID already belongs to another secret")
            return self._record(key, existing, include_secret=True)
        record = {
            "name": str(name)[:128],
            "auth_digest": self._digest(secret),
            "secret_ciphertext": self.cipher.encrypt(str(secret).encode()).decode(),
            "quota_bytes": (metadata or {}).get("quota_bytes"),
        }
        value["users"][key] = record
        self._write(value)
        return self._record(key, record, include_secret=True)

    def delete_user(self, external_id: str) -> None:
        value = self._load()
        value["users"].pop(str(external_id), None)
        self._write(value)

    def authenticate(self, presented: str) -> dict[str, Any]:
        digest = self._digest(presented)
        value = self._load()
        for external_id, row in value["users"].items():
            if isinstance(row, Mapping) and hmac.compare_digest(str(row.get("auth_digest") or ""), digest):
                return {"ok": True, "id": str(external_id)}
        return {"ok": False}


class Hysteria2Provider:
    """Hysteria2 provider using HTTP auth plus the Traffic Stats API."""

    def __init__(
        self,
        users: Hysteria2UserStore,
        stats: Hysteria2TrafficStatsClient,
        *,
        probe_callback: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        version: str = "traffic-stats",
    ):
        self.users = users
        self.stats = stats
        self.probe_callback = probe_callback
        self.version = str(version)[:64]

    def server_info(self) -> dict[str, Any]:
        info = self.stats.server_info()
        return {"version": self.version, **info}

    def list_users(self) -> list[dict[str, Any]]:
        return self.users.list_users()

    def get_user(self, external_id: str) -> dict[str, Any] | None:
        return self.users.get_user(external_id)

    def create_user(
        self,
        external_id: str,
        name: str,
        _route: Mapping[str, Any],
        intent: Mapping[str, Any],
    ) -> dict[str, Any]:
        secret = str(intent.get("secret") or "").strip()
        if not secret:
            raise ProviderBackendError("Hysteria2 customer secret is required")
        return self.users.create_user(external_id, name, secret, intent)

    def delete_user(self, external_id: str) -> None:
        self.users.delete_user(external_id)

    def get_user_usage(self, external_id: str) -> dict[str, Any]:
        value = self.stats.traffic().get(str(external_id), {})
        if not isinstance(value, Mapping):
            raise ProviderBackendError("Hysteria2 user traffic has an invalid shape")
        tx = _int_value(value.get("tx"), field="Hysteria2 tx")
        rx = _int_value(value.get("rx"), field="Hysteria2 rx")
        return {"external_id": str(external_id), "tx_bytes": tx, "rx_bytes": rx}

    def terminate_user_sessions(self, external_id: str) -> dict[str, Any]:
        self.stats.kick([str(external_id)])
        return {
            "supported": True,
            "terminated": False,
            "requested": True,
            "reason": "kick requested; authentication revocation must prevent reconnect",
        }

    def probe_data_plane(self, route: Mapping[str, Any]) -> dict[str, Any]:
        if not callable(self.probe_callback):
            return {"status": "unsupported", "reason": "authenticated probe is not configured"}
        result = self.probe_callback(dict(route))
        if not isinstance(result, Mapping):
            raise ProviderBackendError("Hysteria2 data-plane probe returned an invalid response")
        return dict(result)


def create_hysteria2_auth_wsgi_app(
    users: Hysteria2UserStore, *, max_body_bytes: int = 16 * 1024
) -> Callable[..., list[bytes]]:
    """Create the loopback-only HTTP auth callback expected by Hysteria2."""

    if not 1024 <= int(max_body_bytes) <= 128 * 1024:
        raise ValueError("Hysteria2 auth request bound is invalid")

    def app(environ: Mapping[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        status = 200
        payload: dict[str, Any]
        try:
            if str(environ.get("REQUEST_METHOD") or "").upper() != "POST":
                status = 405
                payload = {"ok": False}
            else:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                if length <= 0 or length > int(max_body_bytes):
                    raise ProviderBackendError("Hysteria2 auth request is invalid")
                value = json.loads(environ["wsgi.input"].read(length))
                if not isinstance(value, Mapping):
                    raise ProviderBackendError("Hysteria2 auth request is invalid")
                payload = users.authenticate(str(value.get("auth") or ""))
        except (ProviderBackendError, TypeError, ValueError, json.JSONDecodeError):
            status = 400
            payload = {"ok": False}
        body = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        start_response(
            f"{status} {'OK' if status < 300 else 'Error'}",
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
            ],
        )
        return [body]

    return app


__all__ = [
    "Hysteria2Provider",
    "Hysteria2TrafficStatsClient",
    "Hysteria2UserStore",
    "ProviderBackendError",
    "XrayConfigProvider",
    "XrayStatsParser",
    "create_hysteria2_auth_wsgi_app",
]
