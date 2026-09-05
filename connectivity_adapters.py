"""Protocol-neutral connectivity adapters.

The commerce and failover layers deal in route and credential dictionaries;
this module is the only place where an Outline Management API object is
translated into that contract.  Future Xray or WireGuard implementations can
register beside this adapter without teaching commerce about their APIs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from ports import ConnectivityAdapter, OutlineGateway


UTC = timezone.utc


class ConnectivityAdapterError(RuntimeError):
    """A route operation failed or returned an unsafe shape."""


def _checked_grant(grant: Mapping[str, Any]) -> tuple[str, str]:
    external_id = str(grant.get("external_id") or grant.get("id") or "").strip()
    access_url = str(grant.get("access_url") or grant.get("accessUrl") or "").strip()
    if not external_id:
        raise ConnectivityAdapterError("credential grant lacks external_id")
    if not access_url:
        raise ConnectivityAdapterError("credential grant lacks access_url")
    return external_id, access_url


class OutlineConnectivityAdapter:
    """Adapter for a pinned Outline Management API client."""

    protocol = "outline"

    def __init__(self, client: OutlineGateway):
        self.client = client

    @property
    def capabilities(self) -> dict[str, bool]:
        return {
            "managed_config": True,
            "manual_export": True,
            "quota_cap": True,
            "usage": True,
            "rotation": True,
            # Outline's documented management API has no force-disconnect.
            "terminate_sessions": False,
            "management_probe": True,
            # A node agent or client must prove the customer data plane.
            "data_plane_probe": False,
            "reconcile": True,
        }

    def provision(self, route: dict[str, Any], credential_intent: dict[str, Any]) -> dict[str, Any]:
        name = str(credential_intent.get("name") or "AuriX route")[:128]
        limit = credential_intent.get("quota_bytes")
        limit_bytes = None if limit is None else int(limit)
        requested_id = str(credential_intent.get("external_id") or "").strip()
        created: dict[str, Any] | None = None
        existed_before = False
        if requested_id:
            getter = getattr(self.client, "get_key", None)
            if callable(getter):
                created = getter(requested_id)
                existed_before = created is not None
            creator = getattr(self.client, "create_key_with_id", None)
            if created is None and callable(creator):
                try:
                    created = creator(requested_id, name, limit_bytes)
                except Exception:
                    # An ambiguous timeout may have created the key.  Only
                    # accept it after a deterministic read-back.
                    if callable(getter):
                        created = getter(requested_id)
                    if created is None:
                        raise
        if created is None:
            created = self.client.create_key(name, limit_bytes)
        if not isinstance(created, dict):
            raise ConnectivityAdapterError("Outline provision response is not an object")
        external_id, access_url = _checked_grant(created)
        if limit_bytes is not None:
            self.client.set_data_limit(external_id, limit_bytes)
        return {
            "protocol": self.protocol,
            "route_id": str(route.get("route_id") or ""),
            "external_id": external_id,
            "access_url": access_url,
            "name": name,
            "quota_bytes": limit_bytes,
            "created": not existed_before,
        }

    def render_managed_config(self, grant: dict[str, Any]) -> dict[str, Any]:
        external_id, access_url = _checked_grant(grant)
        return {
            "protocol": self.protocol,
            "route_id": str(grant.get("route_id") or ""),
            "credential_ref": external_id,
            "access_url": access_url,
        }

    def render_manual_export(self, grant: dict[str, Any]) -> str:
        _external_id, access_url = _checked_grant(grant)
        return access_url

    def apply_quota_cap(self, grant: dict[str, Any], absolute_limit: int) -> None:
        external_id, _access_url = _checked_grant(grant)
        value = int(absolute_limit)
        if value <= 0:
            raise ConnectivityAdapterError("Outline quota cap must be positive")
        self.client.set_data_limit(external_id, value)

    def read_usage(self, grant: dict[str, Any]) -> dict[str, Any]:
        external_id, _access_url = _checked_grant(grant)
        payload = self.client.transfer_metrics()
        by_key = payload.get("bytesTransferredByUserId", {}) if isinstance(payload, dict) else {}
        if not isinstance(by_key, dict):
            raise ConnectivityAdapterError("Outline usage response has an invalid shape")
        try:
            transferred = max(0, int(by_key.get(external_id, 0) or 0))
        except (TypeError, ValueError) as exc:
            raise ConnectivityAdapterError("Outline usage is not an integer") from exc
        return {
            "protocol": self.protocol,
            "external_id": external_id,
            "bytes_transferred": transferred,
            "observed_at": datetime.now(UTC).isoformat(),
        }

    def rotate(self, grant: dict[str, Any], credential_intent: dict[str, Any]) -> dict[str, Any]:
        return self.provision(
            {"route_id": grant.get("route_id")},
            credential_intent,
        )

    def revoke_auth(self, grant: dict[str, Any]) -> None:
        external_id, _access_url = _checked_grant(grant)
        self.client.delete_key(external_id)

    def terminate_sessions(self, grant: dict[str, Any]) -> dict[str, Any]:
        _checked_grant(grant)
        return {
            "supported": False,
            "terminated": False,
            "reason": "Outline Management API does not expose force-disconnect",
        }

    def probe_management(self, route: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = self.client.server_info()
            return {"status": "healthy", "protocol": self.protocol, "server": payload}
        except Exception as exc:
            return {"status": "failed", "protocol": self.protocol, "error": type(exc).__name__}

    def probe_data_plane(self, route: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "unsupported",
            "protocol": self.protocol,
            "reason": "Use an authenticated node-agent or client tunnel probe",
        }

    def reconcile(self, route: dict[str, Any]) -> dict[str, Any]:
        payload = self.client.list_keys()
        keys = payload.get("accessKeys", []) if isinstance(payload, dict) else []
        if not isinstance(keys, list):
            raise ConnectivityAdapterError("Outline inventory response has an invalid shape")
        return {
            "protocol": self.protocol,
            "route_id": str(route.get("route_id") or ""),
            "access_keys": len([item for item in keys if isinstance(item, dict)]),
        }


class ConnectivityAdapterRegistry:
    """Small explicit registry; unsupported protocols fail closed."""

    def __init__(self, factories: Mapping[str, Callable[[Any], ConnectivityAdapter]] | None = None):
        self._factories: dict[str, Callable[[Any], ConnectivityAdapter]] = {
            "outline": OutlineConnectivityAdapter,
            **dict(factories or {}),
        }

    def register(self, protocol: str, factory: Callable[[Any], ConnectivityAdapter]) -> None:
        normalized = str(protocol or "").strip().lower()
        if not normalized or not callable(factory):
            raise ValueError("adapter protocol and factory are required")
        self._factories[normalized] = factory

    def for_route(self, route: Mapping[str, Any], client: Any) -> ConnectivityAdapter:
        protocol = str(route.get("protocol") or "").strip().lower()
        factory = self._factories.get(protocol)
        if factory is None:
            raise ConnectivityAdapterError(f"No connectivity adapter is registered for {protocol!r}")
        adapter = factory(client)
        if not isinstance(adapter, ConnectivityAdapter):
            raise ConnectivityAdapterError(f"Adapter for {protocol!r} violates the contract")
        return adapter


DEFAULT_ADAPTER_REGISTRY = ConnectivityAdapterRegistry()
