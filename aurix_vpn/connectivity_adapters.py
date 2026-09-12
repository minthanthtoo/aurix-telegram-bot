"""Protocol-neutral connectivity adapters used by the AuriX lifecycle.

Commerce owns entitlement policy and durable state.  This module owns the
translation between that policy and a protocol/provider API.  Xray, Hysteria2
and WireGuard must register here only after their server-side lifecycle and
accounting behavior has been verified.
"""

from __future__ import annotations

import ipaddress
import re
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote, unquote, urlencode, urlsplit

from ports import ConnectivityAdapter, OutlineGateway


UTC = timezone.utc
_HOSTNAME_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\Z")


class ConnectivityAdapterError(RuntimeError):
    """A route operation failed or returned an unsafe provider shape."""


def _checked_grant(grant: Mapping[str, Any]) -> tuple[str, str]:
    external_id = str(grant.get("external_id") or grant.get("id") or "").strip()
    access_url = str(grant.get("access_url") or grant.get("accessUrl") or "").strip()
    if not external_id:
        raise ConnectivityAdapterError("credential grant lacks external_id")
    if not access_url:
        raise ConnectivityAdapterError("credential grant lacks access_url")
    return external_id, access_url


class OutlineConnectivityAdapter:
    """Adapter for one pinned Outline Management API client.

    ``ownership`` is intentionally explicit.  A deterministic read-back after
    a timeout proves that a remote credential exists, but does not prove that
    this worker knows whether the create request committed.  Such a grant is
    ``uncertain`` and is never deleted by failure cleanup.
    """

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
            "terminate_sessions": False,
            "management_probe": True,
            "data_plane_probe": False,
            "reconcile": True,
        }

    @staticmethod
    def _grant(route: Mapping[str, Any], key: Mapping[str, Any], *, created: bool, ownership: str) -> dict[str, Any]:
        if not isinstance(key, Mapping):
            raise ConnectivityAdapterError("Outline provision response is not an object")
        external_id, access_url = _checked_grant(key)
        return {
            "protocol": "outline",
            "route_id": str(route.get("route_id") or route.get("endpoint_id") or ""),
            "endpoint_id": str(route.get("endpoint_id") or ""),
            "external_id": external_id,
            "access_url": access_url,
            "name": str(key.get("name") or "")[:128],
            "quota_bytes": key.get("limit_bytes"),
            "created": bool(created),
            "ownership": ownership,
        }

    def provision(self, route: dict[str, Any], credential_intent: dict[str, Any]) -> dict[str, Any]:
        _assert_route_protocol(route, protocol=self.protocol)
        name = str(credential_intent.get("name") or "AuriX route")[:128]
        limit = credential_intent.get("quota_bytes")
        limit_bytes = None if limit is None else _positive_quota(limit, protocol=self.protocol)
        requested_id = str(credential_intent.get("external_id") or "").strip()
        getter = getattr(self.client, "get_key", None)
        key: dict[str, Any] | None = None
        created = False
        ownership = "unknown"

        if requested_id and callable(getter):
            key = getter(requested_id)
            if key is not None:
                ownership = "preexisting"

        if key is None and requested_id:
            creator = getattr(self.client, "create_key_with_id", None)
            if callable(creator):
                try:
                    key = creator(requested_id, name, limit_bytes)
                    created = True
                    ownership = "owned"
                except Exception as exc:
                    # Read-back is the only safe recovery for an ambiguous
                    # request.  It is deliberately not treated as owned.
                    recovered = getter(requested_id) if callable(getter) else None
                    if recovered is not None:
                        key = recovered
                        ownership = "uncertain"
                    elif getattr(exc, "status", None) in (404, 405, 501):
                        # An explicit unsupported-endpoint response is the
                        # only safe reason to fall back to a non-deterministic
                        # create.  A timeout must never take this path.
                        key = self.client.create_key(name, limit_bytes)
                        created = True
                        ownership = "owned"
                    else:
                        raise

        if key is None:
            key = self.client.create_key(name, limit_bytes)
            created = True
            ownership = "owned"

        external_id, access_url = _checked_grant(key)
        if limit_bytes is not None:
            self.client.set_data_limit(external_id, limit_bytes)
        grant = self._grant(route, {**dict(key), "limit_bytes": limit_bytes}, created=created, ownership=ownership)
        grant["name"] = name
        grant["quota_bytes"] = limit_bytes
        grant["access_url"] = access_url
        grant["external_id"] = external_id
        return grant

    def render_managed_config(self, grant: dict[str, Any]) -> dict[str, Any]:
        _assert_grant_protocol(grant, protocol=self.protocol)
        external_id, access_url = _checked_grant(grant)
        return {
            "protocol": self.protocol,
            "route_id": str(grant.get("route_id") or ""),
            "credential_ref": external_id,
            "access_url": access_url,
        }

    def render_manual_export(self, grant: dict[str, Any]) -> str:
        _assert_grant_protocol(grant, protocol=self.protocol)
        _external_id, access_url = _checked_grant(grant)
        return access_url

    def apply_quota_cap(self, grant: dict[str, Any], absolute_limit: int) -> None:
        _assert_grant_protocol(grant, protocol=self.protocol)
        external_id, _access_url = _checked_grant(grant)
        value = _positive_quota(absolute_limit, protocol=self.protocol)
        self.client.set_data_limit(external_id, value)

    def read_usage(self, grant: dict[str, Any]) -> dict[str, Any]:
        _assert_grant_protocol(grant, protocol=self.protocol)
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
            "counter_mode": "rolling_window",
            "observed_at": datetime.now(UTC).isoformat(),
        }

    def rotate(self, grant: dict[str, Any]) -> dict[str, Any]:
        _assert_grant_protocol(grant, protocol=self.protocol)
        intent = grant.get("credential_intent")
        if not isinstance(intent, dict):
            intent = {
                "name": str(grant.get("name") or "AuriX rotated route")[:128],
                "quota_bytes": grant.get("quota_bytes"),
                "external_id": grant.get("next_external_id"),
            }
        route = {"route_id": grant.get("route_id"), "endpoint_id": grant.get("endpoint_id")}
        return self.provision(route, intent)

    def revoke_auth(self, grant: dict[str, Any]) -> None:
        _assert_grant_protocol(grant, protocol=self.protocol)
        external_id, _access_url = _checked_grant(grant)
        self.client.delete_key(external_id)

    def verify_auth_revoked(self, grant: dict[str, Any]) -> dict[str, Any]:
        _assert_grant_protocol(grant, protocol=self.protocol)
        external_id, _access_url = _checked_grant(grant)
        getter = getattr(self.client, "get_key", None)
        if not callable(getter):
            return {"verified": False, "exists": None, "reason": "readback_unsupported"}
        exists = getter(external_id) is not None
        return {
            "verified": not exists,
            "exists": exists,
            "reason": "credential_present" if exists else "credential_absent",
        }

    def terminate_sessions(self, grant: dict[str, Any]) -> dict[str, Any]:
        _assert_grant_protocol(grant, protocol=self.protocol)
        _checked_grant(grant)
        return {
            "supported": False,
            "terminated": False,
            "reason": "Outline Management API does not expose force-disconnect",
        }

    def probe_management(self, route: dict[str, Any]) -> dict[str, Any]:
        _assert_route_protocol(route, protocol=self.protocol)
        try:
            payload = self.client.server_info()
            return {"status": "healthy", "protocol": self.protocol, "server": payload}
        except Exception as exc:
            return {"status": "failed", "protocol": self.protocol, "error": type(exc).__name__}

    def probe_data_plane(self, route: dict[str, Any]) -> dict[str, Any]:
        _assert_route_protocol(route, protocol=self.protocol)
        return {
            "status": "unsupported",
            "protocol": self.protocol,
            "reason": "Use an authenticated node-agent or client tunnel probe",
        }

    def reconcile(self, route: dict[str, Any]) -> dict[str, Any]:
        _assert_route_protocol(route, protocol=self.protocol)
        payload = self.client.list_keys()
        keys = payload.get("accessKeys", []) if isinstance(payload, dict) else []
        if not isinstance(keys, list):
            raise ConnectivityAdapterError("Outline inventory response has an invalid shape")
        return {
            "protocol": self.protocol,
            "route_id": str(route.get("route_id") or route.get("endpoint_id") or ""),
            "access_keys": len([item for item in keys if isinstance(item, dict)]),
        }


def _provider_method(client: Any, names: tuple[str, ...]) -> Callable[..., Any] | None:
    for name in names:
        method = getattr(client, name, None)
        if callable(method):
            return method
    return None


def _provider_id(record: Mapping[str, Any], fallback: str) -> str:
    value = record.get("external_id") or record.get("id") or record.get("uuid") or record.get("username")
    return str(value or fallback).strip()


def _provider_counter(value: Any) -> int:
    if isinstance(value, bool):
        raise ConnectivityAdapterError("provider usage is not an integer")
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError) as exc:
        raise ConnectivityAdapterError("provider usage is not an integer") from exc


def _provider_usage(record: Any) -> int:
    if isinstance(record, Mapping):
        direct = record.get("bytes_transferred")
        if direct is not None:
            return _provider_counter(direct)
        tx = record["tx_bytes"] if "tx_bytes" in record else record.get("tx", 0)
        rx = record["rx_bytes"] if "rx_bytes" in record else record.get("rx", 0)
        return _provider_counter(
            tx
        ) + _provider_counter(rx)
    return _provider_counter(record)


def _positive_quota(value: Any, *, protocol: str) -> int:
    if isinstance(value, (bool, float)):
        raise ConnectivityAdapterError(f"{protocol} quota is not a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ConnectivityAdapterError(f"{protocol} quota is not a positive integer") from exc
    if normalized <= 0:
        raise ConnectivityAdapterError(f"{protocol} quota is not a positive integer")
    return normalized


def _assert_route_protocol(route: Mapping[str, Any], *, protocol: str) -> None:
    if not isinstance(route, Mapping):
        raise ConnectivityAdapterError(f"{protocol} route is not an object")
    declared = str(route.get("protocol") or "").strip().lower()
    if declared and declared != protocol:
        raise ConnectivityAdapterError(
            f"{protocol} adapter cannot handle {declared} route"
        )


def _assert_grant_protocol(grant: Mapping[str, Any], *, protocol: str) -> None:
    if not isinstance(grant, Mapping):
        raise ConnectivityAdapterError(f"{protocol} credential grant is not an object")
    declared = str(grant.get("protocol") or "").strip().lower()
    if declared and declared != protocol:
        raise ConnectivityAdapterError(
            f"{protocol} adapter cannot handle {declared} grant"
        )


def _route_bool(route: Mapping[str, Any], field: str, *, default: bool = False) -> bool:
    value = route.get(field)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConnectivityAdapterError(f"route field {field} must be a boolean")
    return value


def _required_route_value(route: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = str(route.get(name) or "").strip()
        if value:
            return value
    raise ConnectivityAdapterError(f"route is missing {'/'.join(names)}")


def _route_host(route: Mapping[str, Any], *names: str, protocol: str) -> str:
    """Validate the public host before it can enter a customer URI."""
    value = _required_route_value(route, *names)
    normalized = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    if (
        not normalized
        or len(normalized) > 253
        or any(char.isspace() or char in "/?#@" for char in normalized)
    ):
        raise ConnectivityAdapterError(f"{protocol} route host is invalid")
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        if ".." in normalized or not _HOSTNAME_PATTERN.fullmatch(normalized):
            raise ConnectivityAdapterError(f"{protocol} route host is invalid")
    return normalized


def _route_port(route: Mapping[str, Any], *names: str, protocol: str) -> str:
    """Return a canonical TCP/UDP port and reject coercive values."""
    raw = next((route.get(name) for name in names if route.get(name) is not None), None)
    if raw is None or isinstance(raw, bool):
        raise ConnectivityAdapterError(f"{protocol} route port is invalid")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
    else:
        raise ConnectivityAdapterError(f"{protocol} route port is invalid")
    if not 1 <= value <= 65_535:
        raise ConnectivityAdapterError(f"{protocol} route port is invalid")
    return str(value)


class _ManagedCredentialAdapter:
    """Shared lifecycle translation for adapters backed by an injected client.

    The client is deliberately a small node-agent/API seam rather than an HTTP
    implementation.  A production client must provide ``get_user``,
    ``create_user``, ``delete_user``, ``get_user_usage``, ``list_users`` and
    ``server_info``.  Optional ``set_user_quota``,
    ``terminate_user_sessions`` and ``probe_data_plane`` methods advertise
    measured capabilities.  This keeps provider credentials and transport
    libraries out of commerce code and makes the contract testable offline.
    """

    protocol = ""
    DEFAULT_CAPABILITIES = {
        "managed_config": True,
        "manual_export": True,
        "quota_cap": False,
        "usage": True,
        "rotation": True,
        "terminate_sessions": False,
        "management_probe": True,
        "data_plane_probe": False,
        "reconcile": True,
    }

    def __init__(self, client: Any):
        self.client = client

    @property
    def capabilities(self) -> dict[str, bool]:
        result = dict(self.DEFAULT_CAPABILITIES)
        result["quota_cap"] = _provider_method(self.client, ("set_user_quota",)) is not None
        result["terminate_sessions"] = _provider_method(
            self.client, ("terminate_user_sessions", "disconnect_user")
        ) is not None
        result["data_plane_probe"] = _provider_method(self.client, ("probe_data_plane",)) is not None
        return result

    def _new_external_id(self, intent: Mapping[str, Any]) -> str:
        return str(intent.get("external_id") or uuid.uuid4()).strip()

    def _new_secret(self, intent: Mapping[str, Any]) -> str:
        return str(intent.get("secret") or secrets.token_urlsafe(24))

    def _lookup(self, external_id: str) -> Mapping[str, Any] | None:
        method = _provider_method(self.client, ("get_user",))
        if method is None:
            return None
        record = method(external_id)
        return record if isinstance(record, Mapping) else None

    def _inventory(self) -> list[Mapping[str, Any]]:
        method = _provider_method(self.client, ("list_users",))
        if method is None:
            raise ConnectivityAdapterError(f"{self.protocol} client lacks list_users")
        records = method()
        if isinstance(records, Mapping):
            records = records.get("users") or records.get("clients") or records.get("items")
        if not isinstance(records, list):
            raise ConnectivityAdapterError(f"{self.protocol} inventory response is not a list")
        return [item for item in records if isinstance(item, Mapping)]

    def _reconcile_secret(self, grant: Mapping[str, Any], external_id: str) -> str:
        """Recover the provider secret needed to recreate a missing user.

        Xray uses the external UUID as its VLESS credential, while other
        protocols may carry a separate secret in the encrypted access URL.
        Provider-specific adapters override this when the distinction matters.
        """
        return str(grant.get("secret") or external_id)

    def _create(
        self,
        external_id: str,
        name: str,
        route: Mapping[str, Any],
        intent: Mapping[str, Any],
        secret: str,
    ) -> Mapping[str, Any]:
        method = _provider_method(self.client, ("create_user",))
        if method is None:
            raise ConnectivityAdapterError(f"{self.protocol} client lacks create_user")
        record = method(external_id, name, dict(route), {**dict(intent), "secret": secret})
        if not isinstance(record, Mapping):
            raise ConnectivityAdapterError(f"{self.protocol} create response is not an object")
        return record

    def _route_value(self, route: Mapping[str, Any], *names: str) -> str:
        for name in names:
            value = str(route.get(name) or "").strip()
            if value:
                return value
        raise ConnectivityAdapterError(f"{self.protocol} route is missing {'/'.join(names)}")

    @staticmethod
    def _host_port(host: str, port: str) -> str:
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{host}:{port}"

    def _render_access_url(
        self, route: Mapping[str, Any], external_id: str, secret: str, name: str
    ) -> str:
        raise NotImplementedError

    def _grant(
        self,
        route: Mapping[str, Any],
        record: Mapping[str, Any],
        *,
        external_id: str,
        secret: str,
        name: str,
        created: bool,
        ownership: str,
    ) -> dict[str, Any]:
        effective_id = _provider_id(record, external_id)
        effective_secret = str(record.get("secret") or record.get("password") or secret)
        access_url = self._render_access_url(route, effective_id, effective_secret, name)
        return {
            "protocol": self.protocol,
            "route_id": str(route.get("route_id") or route.get("endpoint_id") or ""),
            "endpoint_id": str(route.get("endpoint_id") or ""),
            "external_id": effective_id,
            "access_url": access_url,
            "name": name,
            "secret": effective_secret,
            "quota_bytes": record.get("quota_bytes") or record.get("limit_bytes"),
            "created": bool(created),
            "ownership": ownership,
        }

    def provision(self, route: dict[str, Any], credential_intent: dict[str, Any]) -> dict[str, Any]:
        _assert_route_protocol(route, protocol=self.protocol)
        credential_intent = dict(credential_intent)
        limit = credential_intent.get("quota_bytes")
        if limit is not None:
            credential_intent["quota_bytes"] = _positive_quota(
                limit, protocol=self.protocol
            )
            if _provider_method(self.client, ("set_user_quota",)) is None:
                raise ConnectivityAdapterError(
                    f"{self.protocol} quota enforcement is unsupported"
                )
        name = str(credential_intent.get("name") or f"AuriX {self.protocol} route")[:128]
        external_id = self._new_external_id(credential_intent)
        secret = self._new_secret(credential_intent)
        # Validate every route field needed to render a credential before any
        # provider-side create. Rendering is pure and prevents malformed route
        # metadata from leaving an orphaned remote user behind.
        self._render_access_url(route, external_id, secret, name)
        existing = self._lookup(external_id)
        if existing is not None:
            grant = self._grant(
                route, existing, external_id=external_id, secret=secret, name=name,
                created=False, ownership="preexisting",
            )
            # Keep a recovered/idempotent grant as useful as a freshly-created
            # one. Rotation and later reconciliation need the deployment-owned
            # route metadata, while the durable layer still encrypts only the
            # access URL and never persists this transient secret.
            grant["route"] = dict(route)
            grant["credential_intent"] = {
                key: value for key, value in credential_intent.items() if key != "secret"
            }
            return grant
        try:
            record = self._create(external_id, name, route, credential_intent, secret)
            created = True
            ownership = "owned"
        except Exception:
            # A timeout or transport failure after a remote commit is
            # ambiguous.  Read-back preserves the credential for reconciliation
            # and prevents unsafe cleanup from deleting someone else's user.
            recovered = self._lookup(external_id)
            if recovered is None:
                raise
            record = recovered
            created = False
            ownership = "uncertain"
        grant = self._grant(
            route, record, external_id=external_id, secret=secret, name=name,
            created=created, ownership=ownership,
        )
        # Keep the non-secret route material with the transient grant so a
        # rotation can render the replacement against the same data-plane
        # address. The durable service encrypts access_url before persistence;
        # provider secrets must never be copied into route metadata.
        grant["route"] = dict(route)
        grant["credential_intent"] = {
            key: value for key, value in credential_intent.items() if key != "secret"
        }
        if limit is not None:
            normalized_limit = credential_intent["quota_bytes"]
            self.apply_quota_cap(grant, normalized_limit)
            grant["quota_bytes"] = normalized_limit
        return grant

    def render_managed_config(self, grant: dict[str, Any]) -> dict[str, Any]:
        _assert_grant_protocol(grant, protocol=self.protocol)
        external_id, access_url = _checked_grant(grant)
        return {
            "protocol": self.protocol,
            "route_id": str(grant.get("route_id") or ""),
            "credential_ref": external_id,
            "access_url": access_url,
        }

    def render_manual_export(self, grant: dict[str, Any]) -> str:
        _assert_grant_protocol(grant, protocol=self.protocol)
        _external_id, access_url = _checked_grant(grant)
        return access_url

    def apply_quota_cap(self, grant: dict[str, Any], absolute_limit: int) -> None:
        _assert_grant_protocol(grant, protocol=self.protocol)
        external_id, _access_url = _checked_grant(grant)
        value = _positive_quota(absolute_limit, protocol=self.protocol)
        method = _provider_method(self.client, ("set_user_quota",))
        if method is None:
            raise ConnectivityAdapterError(f"{self.protocol} quota enforcement is unsupported")
        method(external_id, value)

    def read_usage(self, grant: dict[str, Any]) -> dict[str, Any]:
        _assert_grant_protocol(grant, protocol=self.protocol)
        external_id, _access_url = _checked_grant(grant)
        method = _provider_method(self.client, ("get_user_usage", "user_usage"))
        if method is None:
            raise ConnectivityAdapterError(f"{self.protocol} client lacks per-user usage")
        return {
            "protocol": self.protocol,
            "external_id": external_id,
            "bytes_transferred": _provider_usage(method(external_id)),
            "counter_mode": "reset_on_decrease",
            "observed_at": datetime.now(UTC).isoformat(),
        }

    def rotate(self, grant: dict[str, Any]) -> dict[str, Any]:
        _assert_grant_protocol(grant, protocol=self.protocol)
        intent = grant.get("credential_intent")
        if not isinstance(intent, dict):
            intent = {
                "name": str(grant.get("name") or f"AuriX rotated {self.protocol} route")[:128],
                "quota_bytes": grant.get("quota_bytes"),
            }
        else:
            intent = dict(intent)
            # A rotation is a new generation. Reusing the source's stable
            # external ID would turn it into an idempotent no-op.
        next_external_id = str(grant.get("next_external_id") or "").strip()
        if next_external_id:
            intent["external_id"] = next_external_id
        else:
            intent.pop("external_id", None)
        route = {
            "route_id": grant.get("route_id"),
            "endpoint_id": grant.get("endpoint_id"),
            **dict(grant.get("route") or {}),
        }
        return self.provision(route, intent)

    def revoke_auth(self, grant: dict[str, Any]) -> None:
        _assert_grant_protocol(grant, protocol=self.protocol)
        external_id, _access_url = _checked_grant(grant)
        method = _provider_method(self.client, ("delete_user", "remove_user"))
        if method is None:
            raise ConnectivityAdapterError(f"{self.protocol} client lacks user revocation")
        method(external_id)

    def verify_auth_revoked(self, grant: dict[str, Any]) -> dict[str, Any]:
        _assert_grant_protocol(grant, protocol=self.protocol)
        external_id, _access_url = _checked_grant(grant)
        getter = _provider_method(self.client, ("get_user", "get_client"))
        if getter is None:
            return {"verified": False, "exists": None, "reason": "readback_unsupported"}
        exists = getter(external_id) is not None
        return {
            "verified": not exists,
            "exists": exists,
            "reason": "credential_present" if exists else "credential_absent",
        }

    def terminate_sessions(self, grant: dict[str, Any]) -> dict[str, Any]:
        _assert_grant_protocol(grant, protocol=self.protocol)
        external_id, _access_url = _checked_grant(grant)
        method = _provider_method(self.client, ("terminate_user_sessions", "disconnect_user"))
        if method is None:
            return {"supported": False, "terminated": False, "reason": "provider has no session control"}
        result = method(external_id)
        # Provider responses are untrusted.  In particular, bool({"terminated":
        # False}) is True in Python and would incorrectly allow a credential to
        # be finalized as revoked while its sessions remain usable.
        if isinstance(result, Mapping):
            terminated = result.get("terminated") is True
            details = dict(result)
        elif isinstance(result, bool):
            terminated = result
            details = {}
        else:
            # Existing provider clients use None for fire-and-forget success.
            # Preserve that contract, but require explicit truth for every
            # structured response.
            terminated = result is None
            details = {}
        details = {
            key: value
            for key, value in details.items()
            if key not in {"supported", "terminated"}
        }
        return {"supported": True, "terminated": terminated, **details}

    def probe_management(self, route: dict[str, Any]) -> dict[str, Any]:
        _assert_route_protocol(route, protocol=self.protocol)
        method = _provider_method(self.client, ("server_info",))
        if method is None:
            return {"status": "failed", "protocol": self.protocol, "error": "missing_server_info"}
        try:
            result = method()
            if not isinstance(result, Mapping):
                return {
                    "status": "failed",
                    "protocol": self.protocol,
                    "error": "invalid_server_info",
                }
            return {"status": "healthy", "protocol": self.protocol, "server": dict(result)}
        except Exception as exc:
            return {"status": "failed", "protocol": self.protocol, "error": type(exc).__name__}

    def probe_data_plane(self, route: dict[str, Any]) -> dict[str, Any]:
        _assert_route_protocol(route, protocol=self.protocol)
        method = _provider_method(self.client, ("probe_data_plane",))
        if method is None:
            return {"status": "unsupported", "protocol": self.protocol, "reason": "no authenticated probe"}
        try:
            result = method(dict(route))
            if not isinstance(result, Mapping):
                return {
                    "status": "failed",
                    "protocol": self.protocol,
                    "error": "invalid_probe_response",
                }
            status = "healthy"
            reported = str(result.get("status") or "").strip().lower()
            if reported in {"failed", "unhealthy", "unsupported"}:
                status = reported
            return {"status": status, "protocol": self.protocol, "result": dict(result)}
        except Exception as exc:
            return {"status": "failed", "protocol": self.protocol, "error": type(exc).__name__}

    def reconcile(self, route: dict[str, Any]) -> dict[str, Any]:
        _assert_route_protocol(route, protocol=self.protocol)
        records = self._inventory()
        return {
            "protocol": self.protocol,
            "route_id": str(route.get("route_id") or route.get("endpoint_id") or ""),
            "users": len(records),
        }

    def inventory(self, route: dict[str, Any]) -> dict[str, Any]:
        """Return provider IDs only for durable, secret-safe reconciliation."""
        _assert_route_protocol(route, protocol=self.protocol)
        records = self._inventory()
        external_ids = sorted(
            {
                external_id
                for record in records
                if (external_id := _provider_id(record, ""))
                and len(external_id) <= 256
                and "/" not in external_id
                and "\\" not in external_id
            }
        )
        return {
            "protocol": self.protocol,
            "route_id": str(route.get("route_id") or route.get("endpoint_id") or ""),
            "external_ids": external_ids,
        }

    def reconcile_credentials(
        self, route: dict[str, Any], expected_grants: Iterable[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Rehydrate durable generations missing from a provider inventory.

        This is intentionally separate from ``reconcile``: unknown provider
        users are never deleted, and only credentials supplied by the durable
        AuriX generation store are recreated.  A create timeout is recovered by
        read-back through the same ambiguity boundary as normal provisioning.
        """
        _assert_route_protocol(route, protocol=self.protocol)
        records = self._inventory()
        present = {
            _provider_id(record, "")
            for record in records
            if _provider_id(record, "")
        }
        expected_ids: set[str] = set()
        restored = 0
        already_present = 0
        skipped = 0
        for grant in expected_grants:
            try:
                _assert_grant_protocol(grant, protocol=self.protocol)
                external_id, _access_url = _checked_grant(grant)
            except ConnectivityAdapterError:
                skipped += 1
                continue
            expected_ids.add(external_id)
            if external_id in present:
                already_present += 1
                continue
            intent = grant.get("credential_intent")
            intent = dict(intent) if isinstance(intent, Mapping) else {}
            name = str(grant.get("name") or intent.get("name") or f"AuriX {self.protocol} route")[:128]
            secret = self._reconcile_secret(grant, external_id)
            recovery_quota = grant.get("recovery_quota_bytes")
            if recovery_quota is not None:
                recovery_quota = _positive_quota(recovery_quota, protocol=self.protocol)
                intent["quota_bytes"] = recovery_quota
            intent.update({"external_id": external_id, "name": name, "secret": secret})
            created = False
            try:
                record = self._create(external_id, name, route, intent, secret)
                created = True
            except Exception:
                recovered = self._lookup(external_id)
                if recovered is None:
                    raise
                record = recovered
            if self._lookup(external_id) is None:
                raise ConnectivityAdapterError(
                    f"{self.protocol} credential did not read back after reconciliation"
                )
            if recovery_quota is not None and created:
                restored_grant = self._grant(
                    route,
                    record,
                    external_id=external_id,
                    secret=secret,
                    name=name,
                    created=True,
                    ownership="owned",
                )
                self.apply_quota_cap(restored_grant, recovery_quota)
            present.add(external_id)
            restored += 1
        return {
            "protocol": self.protocol,
            "route_id": str(route.get("route_id") or route.get("endpoint_id") or ""),
            "users": len(records),
            "expected_credentials": len(expected_ids),
            "already_present": already_present,
            "restored": restored,
            "skipped": skipped,
            "unknown_provider_users": len(present - expected_ids),
        }


class XrayConnectivityAdapter(_ManagedCredentialAdapter):
    """Xray/VLESS adapter for a validated node-agent/API client.

    The default registry intentionally does not register this adapter.  The
    route must provide the public REALITY parameters needed to produce a
    server-bound URI, and the injected client must prove per-user accounting
    and quota enforcement before this class is enabled in production.
    """

    protocol = "xray"
    DEFAULT_CAPABILITIES = {
        **_ManagedCredentialAdapter.DEFAULT_CAPABILITIES,
        "quota_cap": True,
    }

    def _render_access_url(
        self, route: Mapping[str, Any], external_id: str, secret: str, name: str
    ) -> str:
        host = _route_host(route, "public_address", "public_host", "host", protocol=self.protocol)
        port = _route_port(route, "port", "public_port", protocol=self.protocol)
        public_key = self._route_value(route, "public_key", "reality_public_key")
        server_name = self._route_value(route, "server_name", "sni")
        short_id = self._route_value(route, "short_id", "reality_short_id")
        query = {
            "type": str(route.get("network") or "tcp"),
            "security": "reality",
            "pbk": public_key,
            "fp": str(route.get("fingerprint") or "chrome"),
            "sni": server_name,
            "sid": short_id,
        }
        flow = str(route.get("flow") or "").strip()
        if flow:
            query["flow"] = flow
        return f"vless://{quote(external_id, safe='')}@{self._host_port(host, port)}?{urlencode(query)}#{quote(name, safe='')}"


class Hysteria2ConnectivityAdapter(_ManagedCredentialAdapter):
    """Hysteria2 adapter requiring customer-scoped authentication and stats."""

    protocol = "hysteria2"

    def _reconcile_secret(self, grant: Mapping[str, Any], external_id: str) -> str:
        secret = str(grant.get("secret") or "").strip()
        if secret:
            return secret
        access_url = str(grant.get("access_url") or "").strip()
        parsed = urlsplit(access_url)
        if parsed.scheme == "hysteria2" and parsed.username:
            return unquote(parsed.username)
        raise ConnectivityAdapterError("hysteria2 durable grant lacks its customer secret")

    def _new_external_id(self, intent: Mapping[str, Any]) -> str:
        return str(intent.get("external_id") or f"aurix-{uuid.uuid4().hex[:16]}").strip()

    def _render_access_url(
        self, route: Mapping[str, Any], external_id: str, secret: str, name: str
    ) -> str:
        host = _route_host(route, "public_address", "public_host", "host", protocol=self.protocol)
        port = _route_port(route, "port", "public_port", protocol=self.protocol)
        query: dict[str, str] = {}
        sni = str(route.get("server_name") or route.get("sni") or "").strip()
        if sni:
            query["sni"] = sni
        if _route_bool(route, "insecure"):
            query["insecure"] = "1"
        suffix = f"?{urlencode(query)}" if query else ""
        return f"hysteria2://{quote(secret, safe='')}@{self._host_port(host, port)}/{suffix}#{quote(name, safe='')}"


class ConnectivityAdapterRegistry:
    """Explicit registry; unsupported protocols fail closed."""

    CANDIDATE_PROTOCOLS = {
        "xray": "Requires live per-user lifecycle, quota, restart, and client-path evidence",
        "hysteria2": "Requires per-customer auth/accounting and UDP client-path evidence",
        "wireguard": "Adapter is not implemented",
    }

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

    def is_registered(self, protocol: str) -> bool:
        """Return whether a factory can construct the requested adapter contract."""
        normalized = str(protocol or "").strip().lower()
        factory = self._factories.get(normalized)
        if not callable(factory):
            return False
        try:
            adapter = factory(None)
        except Exception:
            return False
        return bool(
            isinstance(adapter, ConnectivityAdapter)
            and str(getattr(adapter, "protocol", "")).strip().lower() == normalized
        )

    def protocol_catalog(self) -> list[dict[str, Any]]:
        """Describe registered protocol contracts without contacting a node."""
        catalog: list[dict[str, Any]] = []
        for protocol, factory in sorted(self._factories.items()):
            capabilities: dict[str, bool] = {}
            try:
                adapter = factory(None)
                capabilities = dict(getattr(adapter, "capabilities", {}) or {})
            except Exception:
                # A factory may require a concrete client.  The protocol still
                # belongs in the UI, but its capability proof is unavailable.
                capabilities = {}
            catalog.append({"protocol": protocol, "capabilities": capabilities})
        return catalog

    def protocol_readiness(self) -> list[dict[str, Any]]:
        """Expose adapter posture without confusing registration for activation.

        This is intentionally separate from :meth:`protocol_catalog`: callers
        use the catalog to construct adapters, while operator surfaces use this
        view to explain why a roadmap protocol is not allocatable yet. Outline
        is the only default production transport; every other registered
        protocol still needs endpoint evidence and explicit profile promotion.
        """
        registered = {item["protocol"]: item for item in self.protocol_catalog()}
        result: list[dict[str, Any]] = []
        for protocol, item in sorted(registered.items()):
            is_outline = protocol == "outline"
            activation_gate = self.CANDIDATE_PROTOCOLS.get(
                protocol,
                "Requires endpoint evidence and explicit operator promotion",
            )
            if not is_outline:
                activation_gate += "; endpoint evidence and explicit promotion are still required"
            result.append({
                **item,
                "registered": True,
                "status": "enabled" if is_outline else "candidate",
                "activation_gate": None if is_outline else activation_gate,
            })
        for protocol, gate in sorted(self.CANDIDATE_PROTOCOLS.items()):
            if protocol in registered:
                continue
            result.append({
                "protocol": protocol,
                "capabilities": {},
                "registered": False,
                "status": "candidate" if protocol != "wireguard" else "unimplemented",
                "activation_gate": gate,
            })
        return result

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
