"""Explicit controller bindings for protocol-specific node agents.

The controller cannot infer a customer-facing Xray or Hysteria2 route from an
endpoint ID.  This module is the deliberately small configuration boundary
between deployment-owned route metadata and the protocol-neutral commerce
worker.  Bindings are opt-in, bounded, and never contain provider credentials
inside the route returned to the commerce layer.
"""

from __future__ import annotations

import json
import ipaddress
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .connectivity_adapters import (
    ConnectivityAdapterRegistry,
    Hysteria2ConnectivityAdapter,
    XrayConnectivityAdapter,
)
from .node_agent import Hysteria2NodeAgentClient, NodeAgentClient, XrayNodeAgentClient


MAX_BINDINGS = 32
MAX_ROUTE_FIELDS = 64
MAX_ROUTE_VALUE_LENGTH = 1024
SUPPORTED_PROTOCOLS = {
    "xray": XrayConnectivityAdapter,
    "hysteria2": Hysteria2ConnectivityAdapter,
}
_FORBIDDEN_ROUTE_MARKERS = (
    "token",
    "password",
    "secret",
    "private",
    "credential",
    "access_url",
    "certificate_sha",
)


class NodeAgentBindingError(ValueError):
    """A deployment binding is missing, malformed, or unsafe."""


@dataclass(frozen=True)
class ManagedNodeAgentBinding:
    endpoint_id: str
    protocol: str
    base_url: str
    token: str
    route: dict[str, Any]


def _text(value: Any, *, field: str, maximum: int = 128) -> str:
    result = str(value or "").strip()
    if not result or len(result) > maximum:
        raise NodeAgentBindingError(f"{field} is invalid")
    return result


def _route(value: Any, *, endpoint_id: str, protocol: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise NodeAgentBindingError("route must be an object")
    if len(value) > MAX_ROUTE_FIELDS:
        raise NodeAgentBindingError("route has too many fields")
    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = _text(raw_key, field="route field", maximum=128)
        lowered = key.lower()
        if any(marker in lowered for marker in _FORBIDDEN_ROUTE_MARKERS):
            raise NodeAgentBindingError(f"route field {key} is not allowed")
        if isinstance(raw_value, (dict, list, tuple)):
            raise NodeAgentBindingError(f"route field {key} must be scalar")
        if raw_value is not None and not isinstance(raw_value, (bool, int, float, str)):
            raise NodeAgentBindingError(f"route field {key} must be scalar")
        if isinstance(raw_value, str) and len(raw_value) > MAX_ROUTE_VALUE_LENGTH:
            raise NodeAgentBindingError(f"route field {key} is too long")
        result[key] = raw_value
    declared_endpoint = str(result.get("endpoint_id") or endpoint_id).strip()
    declared_protocol = str(result.get("protocol") or protocol).strip().lower()
    if declared_endpoint != endpoint_id or declared_protocol != protocol:
        raise NodeAgentBindingError("route identity does not match its binding")
    result["endpoint_id"] = endpoint_id
    result["protocol"] = protocol
    result.setdefault("route_id", f"{protocol}:{endpoint_id}")
    if protocol == "xray":
        xray_protocol = str(result.get("xray_protocol") or "vless").strip().lower()
        if xray_protocol != "vless":
            raise NodeAgentBindingError(
                "xray route must declare xray_protocol=vless"
            )
        result["xray_protocol"] = xray_protocol
    if protocol == "hysteria2":
        auth_mode = str(result.get("auth_mode") or "").strip().lower()
        if auth_mode != "http":
            raise NodeAgentBindingError(
                "hysteria2 route must declare customer-scoped auth_mode=http"
            )
        result["auth_mode"] = auth_mode
    return result


class ManagedNodeAgentBindings:
    """Resolve explicit endpoint/protocol bindings into clients and adapters."""

    def __init__(
        self,
        bindings: list[ManagedNodeAgentBinding] | tuple[ManagedNodeAgentBinding, ...] = (),
        *,
        adapter_registry: ConnectivityAdapterRegistry | None = None,
        client_factory: Callable[[str, str, str], NodeAgentClient] | None = None,
    ):
        self._bindings: dict[tuple[str, str], ManagedNodeAgentBinding] = {}
        self._clients: dict[tuple[str, str], NodeAgentClient] = {}
        self.adapter_registry = adapter_registry
        self.client_factory = client_factory or self._default_client
        for binding in bindings:
            key = (binding.endpoint_id, binding.protocol)
            if key in self._bindings:
                raise NodeAgentBindingError("duplicate endpoint/protocol binding")
            self._bindings[key] = binding
            self._clients[key] = self.client_factory(
                binding.protocol, binding.base_url, binding.token
            )
        if adapter_registry is not None:
            for protocol in {binding.protocol for binding in self._bindings.values()}:
                adapter_registry.register(protocol, SUPPORTED_PROTOCOLS[protocol])

    @staticmethod
    def _default_client(protocol: str, base_url: str, token: str) -> NodeAgentClient:
        client_type = {
            "xray": XrayNodeAgentClient,
            "hysteria2": Hysteria2NodeAgentClient,
        }[protocol]
        return client_type(base_url, token=token)

    @classmethod
    def from_json(
        cls,
        encoded: str | None,
        *,
        adapter_registry: ConnectivityAdapterRegistry | None = None,
        client_factory: Callable[[str, str, str], NodeAgentClient] | None = None,
    ) -> "ManagedNodeAgentBindings":
        raw_text = str(encoded or "").strip()
        if not raw_text:
            return cls(
                adapter_registry=adapter_registry,
                client_factory=client_factory,
            )
        try:
            raw = json.loads(raw_text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise NodeAgentBindingError("managed node-agent bindings are invalid JSON") from exc
        if isinstance(raw, Mapping):
            raw = raw.get("bindings")
        if not isinstance(raw, list) or len(raw) > MAX_BINDINGS:
            raise NodeAgentBindingError("managed node-agent bindings must be a bounded list")
        bindings: list[ManagedNodeAgentBinding] = []
        seen: set[tuple[str, str]] = set()
        for item in raw:
            if not isinstance(item, Mapping):
                raise NodeAgentBindingError("managed node-agent binding must be an object")
            endpoint_id = _text(item.get("endpoint_id"), field="endpoint_id")
            protocol = _text(item.get("protocol"), field="protocol", maximum=64).lower()
            if protocol not in SUPPORTED_PROTOCOLS:
                raise NodeAgentBindingError(f"managed protocol {protocol!r} is unsupported")
            key = (endpoint_id, protocol)
            if key in seen:
                raise NodeAgentBindingError("duplicate endpoint/protocol binding")
            seen.add(key)
            base_url = _text(item.get("base_url"), field="base_url", maximum=2048).rstrip("/")
            parsed = urlsplit(base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise NodeAgentBindingError("base_url must be an HTTP(S) URL")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise NodeAgentBindingError("base_url must not contain credentials or URL decorations")
            if parsed.scheme == "http":
                try:
                    loopback = ipaddress.ip_address(parsed.hostname).is_loopback
                except ValueError:
                    loopback = parsed.hostname.lower() == "localhost"
                if not loopback:
                    raise NodeAgentBindingError(
                        "base_url must use HTTPS unless the agent is loopback-local"
                    )
            token = _text(item.get("token"), field="token", maximum=4096)
            bindings.append(
                ManagedNodeAgentBinding(
                    endpoint_id=endpoint_id,
                    protocol=protocol,
                    base_url=base_url,
                    token=token,
                    route=_route(item.get("route"), endpoint_id=endpoint_id, protocol=protocol),
                )
            )
        return cls(
            bindings,
            adapter_registry=adapter_registry,
            client_factory=client_factory,
        )

    @property
    def configured(self) -> bool:
        return bool(self._bindings)

    def route_for(self, endpoint_id: str, protocol: str) -> dict[str, Any]:
        key = (str(endpoint_id or "").strip(), str(protocol or "").strip().lower())
        binding = self._bindings.get(key)
        if binding is None:
            raise NodeAgentBindingError("managed route binding is not configured")
        return dict(binding.route)

    def client_for(self, route: Mapping[str, Any]) -> NodeAgentClient:
        endpoint_id = str(route.get("endpoint_id") or "").strip()
        protocol = str(route.get("protocol") or "").strip().lower()
        binding = self._bindings.get((endpoint_id, protocol))
        if binding is None:
            raise NodeAgentBindingError("managed route binding is not configured")
        configured_route = self.route_for(endpoint_id, protocol)
        for key, value in configured_route.items():
            if route.get(key) != value:
                raise NodeAgentBindingError("managed route differs from configured binding")
        return self._clients[(endpoint_id, protocol)]

    def adapter_for(self, route: Mapping[str, Any]) -> Any:
        if self.adapter_registry is None:
            raise NodeAgentBindingError("adapter registry is not configured")
        client = self.client_for(route)
        return self.adapter_registry.for_route(dict(route), client)

    def routes(self) -> list[dict[str, Any]]:
        return [dict(binding.route) for binding in self._bindings.values()]


__all__ = [
    "MAX_BINDINGS",
    "ManagedNodeAgentBinding",
    "ManagedNodeAgentBindings",
    "NodeAgentBindingError",
]
