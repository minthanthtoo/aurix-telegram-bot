"""Protocol-neutral node-agent client and safe local config writer.

The commerce worker talks to this small contract instead of editing Xray or
Hysteria2 state directly.  The HTTP implementation is deliberately boring and
bounded: deployments can put an authenticated local agent in front of the
provider, while tests inject a callable and never need a running daemon.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
import fcntl
from copy import deepcopy
from contextlib import contextmanager
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin


class NodeAgentError(RuntimeError):
    """A node-agent request or local configuration operation failed."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _object(value: Any, *, operation: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise NodeAgentError(f"{operation} returned a non-object response")
    return dict(value)


def _user_identifier(value: Any) -> str:
    """Validate the opaque user id before putting it into a request path."""
    result = str(value or "").strip()
    if not result or len(result) > 256 or "/" in result or "\\" in result:
        raise NodeAgentError("user identifier is invalid")
    return result


class NodeAgentClient:
    """Client for the AuriX node-agent contract.

    ``requester`` receives ``(method, path, payload)`` and returns a decoded
    JSON value.  Supplying it is the preferred production integration point for
    mTLS, a Unix socket, or a provider SDK; the built-in HTTPS transport is a
    minimal fallback for a separately authenticated agent.
    """

    def __init__(
        self,
        base_url: str = "",
        *,
        token: str = "",
        timeout: float = 8.0,
        requester: Callable[[str, str, Mapping[str, Any] | None], Any] | None = None,
    ):
        self.base_url = str(base_url).strip().rstrip("/") + "/" if str(base_url).strip() else ""
        self.token = str(token)
        self.timeout = max(0.5, min(float(timeout), 60.0))
        self.requester = requester

    def request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        method = str(method).upper()
        path = "/" + str(path).lstrip("/")
        if self.requester is not None:
            return self.requester(method, path, payload)
        if not self.base_url:
            raise NodeAgentError("node-agent base URL is not configured")
        body = None
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            body = json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            urljoin(self.base_url, path.lstrip("/")),
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(512 * 1024)
        except urllib.error.HTTPError as exc:
            raise NodeAgentError(
                f"node-agent request failed: HTTP {exc.code}", status_code=int(exc.code)
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise NodeAgentError(f"node-agent request failed: {type(exc).__name__}") from exc
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NodeAgentError("node-agent returned invalid JSON") from exc

    def server_info(self) -> dict[str, Any]:
        return _object(self.request("GET", "/v1/server"), operation="server_info")

    def list_users(self) -> list[dict[str, Any]]:
        value = self.request("GET", "/v1/users")
        if isinstance(value, Mapping):
            value = value.get("users") or value.get("clients") or value.get("items")
        if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
            raise NodeAgentError("list_users returned an invalid user list")
        return [dict(item) for item in value]

    def get_user(self, external_id: str) -> dict[str, Any] | None:
        external_id = _user_identifier(external_id)
        try:
            value = self.request("GET", f"/v1/users/{quote(external_id, safe='')}")
        except NodeAgentError as exc:
            if exc.status_code == 404:
                return None
            raise
        if value in (None, {}, {"found": False}):
            return None
        return _object(value, operation="get_user")

    def create_user(
        self,
        external_id: str,
        name: str,
        route: Mapping[str, Any],
        intent: Mapping[str, Any],
    ) -> dict[str, Any]:
        external_id = _user_identifier(external_id)
        value = self.request(
            "POST",
            "/v1/users",
            {
                "external_id": external_id,
                "name": str(name)[:128],
                "route": dict(route),
                "intent": dict(intent),
            },
        )
        return _object(value, operation="create_user")

    def delete_user(self, external_id: str) -> None:
        external_id = _user_identifier(external_id)
        self.request("DELETE", f"/v1/users/{quote(external_id, safe='')}")

    def set_user_quota(self, external_id: str, quota_bytes: int) -> None:
        external_id = _user_identifier(external_id)
        if isinstance(quota_bytes, (bool, float)):
            raise NodeAgentError("user quota must be positive")
        try:
            normalized_quota = int(quota_bytes)
        except (TypeError, ValueError) as exc:
            raise NodeAgentError("user quota must be positive") from exc
        if normalized_quota <= 0:
            raise NodeAgentError("user quota must be positive")
        self.request(
            "PATCH",
            f"/v1/users/{quote(external_id, safe='')}/quota",
            {"quota_bytes": normalized_quota},
        )

    def get_user_usage(self, external_id: str) -> Any:
        external_id = _user_identifier(external_id)
        return self.request("GET", f"/v1/users/{quote(external_id, safe='')}/usage")

    def terminate_user_sessions(self, external_id: str) -> dict[str, Any]:
        external_id = _user_identifier(external_id)
        return _object(
            self.request(
                "POST", f"/v1/users/{quote(external_id, safe='')}/sessions/terminate"
            ),
            operation="terminate_user_sessions",
        )

    def probe_data_plane(self, route: Mapping[str, Any]) -> dict[str, Any]:
        return _object(self.request("POST", "/v1/probe", dict(route)), operation="probe_data_plane")


class XrayNodeAgentClient(NodeAgentClient):
    """Named client for the Xray/VLESS/VMess/Trojan/Shadowsocks agent route."""


class Hysteria2NodeAgentClient(NodeAgentClient):
    """Named client for a Hysteria2 agent with the same lifecycle contract."""


class XrayConfigWriter:
    """Update only an explicitly tagged Xray inbound, atomically.

    Unknown inbounds and users are preserved.  The writer never restarts Xray
    and never runs a shell command; a supervisor may validate the returned JSON
    and perform a separately controlled reload after the file is committed.
    """

    def __init__(self, path: str | os.PathLike[str], *, inbound_tag: str = "aurix-managed"):
        self.path = Path(path)
        self.inbound_tag = str(inbound_tag)

    @contextmanager
    def _mutation_lock(self):
        parent = self.path.parent
        lock_path = parent / f".{self.path.name}.lock"
        try:
            parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(descriptor, "a+") as handle:
                os.fchmod(handle.fileno(), 0o600)
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise NodeAgentError("Xray config mutation lock is unavailable") from exc

    def load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NodeAgentError("Xray config could not be read") from exc
        return _object(value, operation="xray config")

    def _managed_client_lists(self, config: Mapping[str, Any]) -> list[list[dict[str, Any]]]:
        lists: list[list[dict[str, Any]]] = []
        inbounds = config.get("inbounds")
        if not isinstance(inbounds, list):
            raise NodeAgentError("Xray config inbounds must be a list")
        for inbound in inbounds:
            if not isinstance(inbound, Mapping) or inbound.get("tag") != self.inbound_tag:
                continue
            settings = inbound.get("settings")
            if not isinstance(settings, dict):
                raise NodeAgentError("managed Xray inbound settings must be an object")
            clients = settings.setdefault("clients", [])
            if not isinstance(clients, list) or any(not isinstance(item, dict) for item in clients):
                raise NodeAgentError("managed Xray inbound clients must be a list")
            lists.append(clients)
        if not lists:
            raise NodeAgentError(f"managed Xray inbound {self.inbound_tag!r} was not found")
        return lists

    def _reload_or_restore(
        self, previous_config: Mapping[str, Any], reload_callback: Callable[[], Any]
    ) -> None:
        """Activate a changed config or restore the exact prior configuration.

        A config file is not equivalent to an active Xray user.  If the
        supervisor reports a reload failure, restore and reload the prior
        JSON while the mutation lock is still held.  This prevents a later
        config-file read-back from being treated as proof that a customer
        credential became usable.
        """
        try:
            reload_callback()
            return
        except Exception as reload_error:
            try:
                self.write(previous_config)
                reload_callback()
            except Exception as rollback_error:
                raise NodeAgentError(
                    "Xray config reload failed and rollback could not be verified"
                ) from rollback_error
            raise NodeAgentError(
                "Xray config reload failed; previous configuration was restored"
            ) from reload_error

    @staticmethod
    def _client(external_id: str, name: str, intent: Mapping[str, Any] | None = None) -> dict[str, Any]:
        value = {"id": str(external_id), "email": str(name)[:128]}
        for key in ("flow", "level", "alterId", "security"):
            if intent and key in intent:
                value[key] = intent[key]
        return value

    def upsert_user(
        self,
        external_id: str,
        name: str,
        *,
        intent: Mapping[str, Any] | None = None,
        write: bool = True,
        reload_callback: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        with self._mutation_lock():
            config = self.load()
            previous_config = deepcopy(config) if reload_callback is not None else None
            changed = False
            for clients in self._managed_client_lists(config):
                existing = next((item for item in clients if str(item.get("id")) == str(external_id)), None)
                rendered = self._client(external_id, name, intent)
                if existing is None:
                    clients.append(rendered)
                    changed = True
                elif existing != rendered:
                    existing.clear()
                    existing.update(rendered)
                    changed = True
            if changed and write:
                self.write(config)
                if reload_callback is not None:
                    if previous_config is None:  # pragma: no cover - defensive invariant
                        raise NodeAgentError("Xray config rollback state is unavailable")
                    self._reload_or_restore(previous_config, reload_callback)
            return {"changed": changed, "external_id": str(external_id)}

    def remove_user(
        self,
        external_id: str,
        *,
        write: bool = True,
        reload_callback: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        with self._mutation_lock():
            config = self.load()
            previous_config = deepcopy(config) if reload_callback is not None else None
            changed = False
            for clients in self._managed_client_lists(config):
                retained = [item for item in clients if str(item.get("id")) != str(external_id)]
                changed = changed or len(retained) != len(clients)
                clients[:] = retained
            if changed and write:
                self.write(config)
                if reload_callback is not None:
                    if previous_config is None:  # pragma: no cover - defensive invariant
                        raise NodeAgentError("Xray config rollback state is unavailable")
                    self._reload_or_restore(previous_config, reload_callback)
            return {"changed": changed, "external_id": str(external_id)}

    def list_users(self) -> list[dict[str, Any]]:
        """Return only users from the explicitly managed inbound(s)."""
        config = self.load()
        users: dict[str, dict[str, Any]] = {}
        for clients in self._managed_client_lists(config):
            for item in clients:
                external_id = str(item.get("id") or "").strip()
                if not external_id:
                    continue
                record = {
                    "external_id": external_id,
                    "name": str(item.get("email") or "")[:128],
                    "secret": external_id,
                }
                users.setdefault(external_id, record)
        return list(users.values())

    def get_user(self, external_id: str) -> dict[str, Any] | None:
        target = str(external_id).strip()
        if not target:
            return None
        return next(
            (item for item in self.list_users() if item.get("external_id") == target),
            None,
        )

    def write(self, config: Mapping[str, Any]) -> None:
        value = _object(config, operation="xray config")
        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=parent)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            try:
                os.unlink(temporary)
            except (OSError, UnboundLocalError):
                pass
            raise NodeAgentError("Xray config could not be written atomically") from exc
