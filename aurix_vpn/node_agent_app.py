"""Authenticated protocol-neutral node-agent service.

The controller-side :mod:`aurix_vpn.node_agent` client is intentionally paired
with this small WSGI boundary.  Provider implementations are injected; this
module owns HTTP authentication, request bounds, path validation, response
redaction, and error mapping.  It does not restart daemons, execute shell
commands, or decide commercial entitlement policy.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from typing import Any, Callable
from urllib.parse import unquote


class NodeAgentHTTPError(RuntimeError):
    """A client-visible node-agent contract error."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = int(status)


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _safe_json(value: Any, *, include_credential: bool = False) -> Any:
    """Redact provider material while retaining the lifecycle contract."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, nested in value.items():
            name = str(key)
            lowered = name.lower()
            if any(
                marker in lowered
                for marker in (
                    "access_url",
                    "private_key",
                    "certificate",
                    "fingerprint",
                    "api_key",
                    "token",
                )
            ):
                continue
            if lowered in {"secret", "password"} and not include_credential:
                continue
            result[name] = _safe_json(nested, include_credential=include_credential)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_json(item, include_credential=include_credential) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _object(value: Any, operation: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise NodeAgentHTTPError(502, f"{operation} returned a non-object response")
    return dict(value)


class NodeAgentService:
    """Expose one injected provider through the AuriX node-agent contract.

    ``protocols`` is optional for compatibility with pre-existing dedicated
    agents. When supplied, it turns the service into an explicit protocol
    boundary: write routes and provider records must declare one of those
    transports. This is required when a single agent fronts multiple protocol
    providers, because inventory without a protocol tag is unsafe to reconcile.
    """

    def __init__(
        self,
        provider: Any,
        *,
        bearer_token: str,
        max_body_bytes: int = 64 * 1024,
        protocols: tuple[str, ...] | list[str] = (),
    ):
        token = str(bearer_token or "")
        if not token:
            raise ValueError("node-agent bearer token is required")
        if not 4 * 1024 <= int(max_body_bytes) <= 512 * 1024:
            raise ValueError("node-agent request bound is invalid")
        normalized_protocols = tuple(
            dict.fromkeys(str(protocol or "").strip().lower() for protocol in protocols)
        )
        if len(normalized_protocols) > 8 or any(
            not protocol or len(protocol) > 64 or not protocol.replace("-", "").isalnum()
            for protocol in normalized_protocols
        ):
            raise ValueError("node-agent protocols are invalid")
        self.provider = provider
        self.bearer_token = token
        self.max_body_bytes = int(max_body_bytes)
        self.protocols = normalized_protocols

    def _protocol_for_route(self, route: Mapping[str, Any]) -> str:
        protocol = str(route.get("protocol") or "").strip().lower()
        if self.protocols and protocol not in self.protocols:
            raise NodeAgentHTTPError(400, "route protocol is not enabled for this node agent")
        return protocol

    def _protocol_record(
        self, value: Any, operation: str, *, expected_protocol: str = ""
    ) -> dict[str, Any]:
        record = _object(value, operation)
        declared = str(record.get("protocol") or "").strip().lower()
        if expected_protocol:
            if declared and declared != expected_protocol:
                raise NodeAgentHTTPError(502, f"{operation} returned a conflicting protocol")
            record.setdefault("protocol", expected_protocol)
            return record
        if not self.protocols:
            return record
        if not declared:
            if len(self.protocols) != 1:
                raise NodeAgentHTTPError(502, f"{operation} returned a user without protocol")
            record["protocol"] = self.protocols[0]
            return record
        if declared not in self.protocols:
            raise NodeAgentHTTPError(502, f"{operation} returned an unsupported protocol")
        return record

    def _authorized(self, environ: Mapping[str, Any]) -> None:
        header = str(environ.get("HTTP_AUTHORIZATION") or "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented or not hmac.compare_digest(
            presented, self.bearer_token
        ):
            raise NodeAgentHTTPError(401, "node-agent authentication required")

    def _body(self, environ: Mapping[str, Any]) -> dict[str, Any]:
        try:
            length = int(environ.get("CONTENT_LENGTH") or "0")
        except (TypeError, ValueError) as exc:
            raise NodeAgentHTTPError(400, "request body is invalid") from exc
        if length <= 0 or length > self.max_body_bytes:
            raise NodeAgentHTTPError(413, "request body is invalid")
        stream = environ.get("wsgi.input")
        if stream is None:
            raise NodeAgentHTTPError(400, "request body is invalid")
        try:
            value = json.loads(stream.read(length))
        except (TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise NodeAgentHTTPError(400, "request body is invalid") from exc
        if not isinstance(value, dict):
            raise NodeAgentHTTPError(400, "request body is invalid")
        return value

    @staticmethod
    def _id(path_part: str) -> str:
        value = unquote(str(path_part or "")).strip()
        if not value or len(value) > 256 or "/" in value or "\\" in value:
            raise NodeAgentHTTPError(400, "user identifier is invalid")
        return value

    def _provider_method(self, *names: str) -> Callable[..., Any]:
        for name in names:
            method = getattr(self.provider, name, None)
            if callable(method):
                return method
        raise NodeAgentHTTPError(501, "provider operation is unsupported")

    def handle(self, method: str, path: str, environ: Mapping[str, Any]) -> dict[str, Any]:
        self._authorized(environ)
        method = str(method or "").upper()
        path = "/" + str(path or "").lstrip("/")
        if path == "/v1/server" and method == "GET":
            return _safe_json(_object(self._provider_method("server_info")(), "server_info"))
        if path == "/v1/users" and method == "GET":
            records = self._provider_method("list_users")()
            if isinstance(records, Mapping):
                records = records.get("users") or records.get("clients") or records.get("items")
            if not isinstance(records, list):
                raise NodeAgentHTTPError(502, "list_users returned an invalid user list")
            return {
                "users": _safe_json(
                    [self._protocol_record(item, "list_users") for item in records]
                )
            }
        if path == "/v1/users" and method == "POST":
            body = self._body(environ)
            external_id = self._id(str(body.get("external_id") or ""))
            name = str(body.get("name") or "AuriX user")[:128]
            route = body.get("route")
            intent = body.get("intent")
            if not isinstance(route, Mapping) or not isinstance(intent, Mapping):
                raise NodeAgentHTTPError(400, "route and intent objects are required")
            protocol = self._protocol_for_route(route)
            value = self._provider_method("create_user")(
                external_id, name, dict(route), dict(intent)
            )
            return _safe_json(
                self._protocol_record(value, "create_user", expected_protocol=protocol),
                include_credential=True,
            )
        prefix = "/v1/users/"
        if path.startswith(prefix):
            tail = path[len(prefix) :]
            parts = tail.split("/")
            external_id = self._id(parts[0])
            if len(parts) == 1 and method == "GET":
                try:
                    value = self._provider_method("get_user")(external_id)
                except KeyError:
                    value = None
                if value is None:
                    raise NodeAgentHTTPError(404, "user not found")
                return _safe_json(
                    self._protocol_record(value, "get_user"), include_credential=True
                )
            if len(parts) == 1 and method == "DELETE":
                self._provider_method("delete_user", "remove_user")(external_id)
                return {"deleted": True, "external_id": external_id}
            if len(parts) == 2 and parts[1] == "quota" and method == "PATCH":
                body = self._body(environ)
                raw_quota = body.get("quota_bytes")
                if isinstance(raw_quota, (bool, float)):
                    raise NodeAgentHTTPError(400, "quota_bytes must be a positive integer")
                try:
                    quota_bytes = int(raw_quota)
                except (TypeError, ValueError) as exc:
                    raise NodeAgentHTTPError(400, "quota_bytes must be a positive integer") from exc
                if quota_bytes <= 0:
                    raise NodeAgentHTTPError(400, "quota_bytes must be a positive integer")
                self._provider_method("set_user_quota")(external_id, quota_bytes)
                return {"updated": True, "external_id": external_id, "quota_bytes": quota_bytes}
            if len(parts) == 2 and parts[1] == "usage" and method == "GET":
                return _safe_json(self._provider_method("get_user_usage", "user_usage")(external_id))
            if len(parts) == 3 and parts[1] == "sessions" and parts[2] == "terminate" and method == "POST":
                method_fn = getattr(self.provider, "terminate_user_sessions", None) or getattr(
                    self.provider, "disconnect_user", None
                )
                if not callable(method_fn):
                    return {"supported": False, "terminated": False, "reason": "provider has no session control"}
                result = method_fn(external_id)
                if isinstance(result, Mapping):
                    payload = dict(result)
                    terminated = payload.get("terminated") is True
                elif isinstance(result, bool):
                    payload = {}
                    terminated = result
                else:
                    payload = {}
                    terminated = result is None
                payload = _safe_json(payload)
                payload.update({"supported": True, "terminated": terminated})
                return payload
        if path == "/v1/probe" and method == "POST":
            body = self._body(environ)
            result = self._provider_method("probe_data_plane")(dict(body))
            return _safe_json(_object(result, "probe_data_plane"))
        raise NodeAgentHTTPError(404, "not found")


def create_node_agent_wsgi_app(service: NodeAgentService) -> Callable[..., list[bytes]]:
    """Create a minimal WSGI app suitable for a local agent or mTLS proxy."""

    def app(environ: Mapping[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        try:
            payload = service.handle(
                str(environ.get("REQUEST_METHOD") or "GET"),
                str(environ.get("PATH_INFO") or "/"),
                environ,
            )
            status = 200
        except NodeAgentHTTPError as exc:
            payload = {"error": str(exc)}
            status = exc.status
        except Exception as exc:  # provider failures never become stack traces
            payload = {"error": "provider operation failed", "error_type": type(exc).__name__}
            status = 502
        body = _json_bytes(payload)
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


__all__ = ["NodeAgentHTTPError", "NodeAgentService", "create_node_agent_wsgi_app"]
