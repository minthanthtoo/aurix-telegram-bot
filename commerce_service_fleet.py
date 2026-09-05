"""Fleet registration, routing failover, lifecycle and endpoint health use cases."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from commerce_models import UTC
from commerce_models import CommerceError
from commerce_models import _new_id
from commerce_models import _now_text
from connectivity_registry import ConnectivityRegistry
from route_failover import FailoverError
from lifecycle_policy import normalize_lifecycle_state
from commerce_service_fleet_failover import (
    _failover_source_record,
    _failover_target_has_fresh_probe,
    process_route_failovers,
)
from commerce_service_fleet_health import _record_endpoint_health, set_server_lifecycle
from commerce_fleet_health_repository import FleetHealthRepository
from commerce_fleet_routing_repository import FleetRoutingRepository
from commerce_fleet_registration_repository import FleetRegistrationRepository


_FLEET_HEALTH = FleetHealthRepository()
_FLEET_ROUTES = FleetRoutingRepository()
_FLEET_REGISTRATION = FleetRegistrationRepository()


def queue_infrastructure_provision(
    self,
    requested_by: int,
    now: datetime | None = None,
    *,
    snapshot: dict[str, Any] | None = None,
) -> str:
    """Queue an owner-approved, allowlisted node request for the worker.

    This method records intent only. The separate infrastructure worker
    still enforces the provider token, budget, node limit and mutation gate.
    """
    if os.environ.get("AURIX_INFRASTRUCTURE_QUEUE_ENABLED", "0").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise CommerceError("Infrastructure provisioning is not enabled for this deployment.")
    current = now or datetime.now(UTC)
    capacity = snapshot if snapshot is not None else self.capacity_snapshot(current)
    advice = capacity.get("scale_advice") or {}
    if str(advice.get("status") or "") not in {"prepare", "urgent"}:
        raise CommerceError("A scale-out intent is allowed only in Prepare or Urgent posture.")
    if not bool(advice.get("observation_ready")):
        required = int(advice.get("required_observations") or 2)
        observed = int(advice.get("consecutive_observations") or 0)
        raise CommerceError(
            f"Scale-out evidence is not ready: collect {required} separate observations "
            f"({observed}/{required} recorded)."
        )
    from infrastructure import FleetController

    controller = FleetController(self.database)
    return controller.queue_provision(
        region=os.environ.get("AURIX_SCALE_REGION", "sgp1").strip(),
        size=os.environ.get("AURIX_SCALE_DROPLET_SIZE", "s-1vcpu-1gb").strip(),
        image=os.environ.get("AURIX_SCALE_DROPLET_IMAGE", "ubuntu-24-04-x64").strip(),
        requested_by=int(requested_by),
        now=current,
    )

def auto_queue_scale_out(
    self,
    *,
    snapshot: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Optionally turn sustained scale evidence into one durable intent.

    This method only writes an idempotent local infrastructure job. The
    separate provider worker still requires its own token, budget, and
    ``AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED`` gate before any VM change.
    The default is disabled, preserving the owner-button workflow.
    """
    enabled = os.environ.get("AURIX_INFRASTRUCTURE_AUTO_QUEUE_ENABLED", "0").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return {"status": "disabled"}
    if os.environ.get("AURIX_INFRASTRUCTURE_QUEUE_ENABLED", "0").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return {"status": "queue_disabled"}
    current = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        job_id = self.queue_infrastructure_provision(
            int(os.environ.get("AURIX_SYSTEM_ACTOR_ID", "0")),
            current,
            snapshot=snapshot,
        )
    except (CommerceError, ValueError) as exc:
        # “Not ready” is an expected state on most maintenance passes and
        # should not mark the whole heartbeat failed.
        return {"status": "blocked", "reason": type(exc).__name__}
    return {"status": "queued", "job_id": job_id}

def register_outline_servers(
    self,
    labels: dict[str, str] | None = None,
    *,
    provider_resource_ids: dict[str, str] | None = None,
    endpoint_metadata: dict[str, dict[str, str]] | None = None,
) -> None:
    """Persist non-secret metadata for every environment-configured server."""
    labels = labels or {}
    provider_resource_ids = provider_resource_ids or {}
    endpoint_metadata = endpoint_metadata or {}
    server_ids = (
        self.outline.server_ids()
        if callable(getattr(self.outline, "server_ids", None))
        else ("default",)
    )
    now_text = _now_text()
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        configured_resources = [
            str(provider_resource_ids[server_id])
            for server_id in server_ids
            if provider_resource_ids.get(server_id)
        ]
        if len(configured_resources) != len(set(configured_resources)):
            raise CommerceError(
                "Each Outline server must use a different provider resource ID"
            )
        for server_id in server_ids:
            provider_resource_id = provider_resource_ids.get(server_id)
            if provider_resource_id:
                existing = _FLEET_REGISTRATION.existing_server_for_resource(
                    connection, provider_resource_id
                )
                if existing is not None and str(existing["server_id"]) != str(server_id):
                    raise CommerceError(
                        f"Provider resource {provider_resource_id} is already registered as "
                        f"server {existing['server_id']!r}; keep that stable server ID in "
                        "OUTLINE_SERVERS_JSON"
                    )
            _FLEET_REGISTRATION.upsert_server(
                connection,
                server_id=server_id,
                label=labels.get(server_id, server_id),
                provider_resource_id=provider_resource_id,
                now_text=now_text,
            )
            metadata = endpoint_metadata.get(server_id) or {}
            state_row = _FLEET_REGISTRATION.state(connection, server_id)
            ConnectivityRegistry.sync_outline_endpoint(
                connection,
                server_id=str(server_id),
                label=str(labels.get(server_id, server_id)),
                provider=str(metadata.get("provider") or "manual"),
                region=str(metadata.get("region") or "unknown"),
                lifecycle_state=str(state_row["lifecycle_state"] if state_row else "active"),
                health_status=str(state_row["health_status"] if state_row else "unknown"),
                now_text=now_text,
            )
        _FLEET_REGISTRATION.retire_missing(connection, tuple(server_ids), now_text)
        default_server_id = getattr(self.outline, "default_server_id", server_ids[0])
        _FLEET_REGISTRATION.assign_legacy_defaults(
            connection,
            default_server_id=default_server_id,
            server_ids=tuple(server_ids),
            orders_reserved_until=_now_text(datetime.now(UTC) + timedelta(hours=24)),
            include_keys=self._table_exists(connection, "keys"),
        )
        ConnectivityRegistry.rebuild_from_legacy(
            connection,
            now_text=now_text,
            encrypt_access_url=self._normalize_legacy_access_url,
        )

def _normalize_legacy_access_url(self, value: str) -> str | None:
    """Return a registry-safe ciphertext for a legacy access URL.

    Current rows already contain ciphertext. A legacy plaintext URL is
    encrypted exactly once. Anything else is retained only when this
    service can decrypt it with the active key; unknown values are omitted
    rather than copied as potentially sensitive plaintext.
    """
    raw = str(value or "")
    if not raw:
        return None
    if raw.startswith("ss://"):
        return self._encrypt_access_url(raw)
    if self._decrypt_access_url(raw) is not None:
        return raw
    return None

def _outline_client(self, server_id: str | None = None) -> Any:
    getter = getattr(self.outline, "client", None)
    return getter(server_id) if callable(getter) else self.outline

def _connectivity_adapter(
    self, *, server_id: str | None = None, service_route_id: str | None = None
) -> Any:
    """Resolve one protocol adapter without exposing provider APIs upward."""
    route: dict[str, Any] | None = None
    resolved_server = str(server_id or "").strip() or None
    with self.database.connect() as connection:
        route = _FLEET_ROUTES.route(
            connection, server_id=resolved_server, service_route_id=service_route_id
        )
        if route is not None:
            resolved_server = str(route["outline_server_id"] or resolved_server or "") or None
    if route is None:
        raise CommerceError("Connectivity route is not configured")
    if str(route.get("protocol")) != "outline":
        raise CommerceError(
            f"No provider client is configured for protocol {str(route.get('protocol'))!r}"
        )
    return self.adapter_registry.for_route(route, self._outline_client(resolved_server))

def configure_route_failover_policy(self, entitlement_id: str, **kwargs: Any) -> dict[str, Any]:
    """Configure an explicit, bounded automatic-failover policy."""
    return self.failover.configure_policy(entitlement_id, **kwargs)

def observe_route_result(
    self,
    generation_id: str,
    *,
    outcome: str,
    network_bucket: str | None = None,
    latency_ms: int | None = None,
    reason: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Record client/node route evidence and possibly queue failover."""
    return self.failover.observe(
        generation_id,
        outcome=outcome,
        network_bucket=network_bucket,
        latency_ms=latency_ms,
        reason=reason,
        observed_at=observed_at,
    )

def route_failover_decisions(
    self, *, entitlement_id: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    return self.failover.decisions(entitlement_id=entitlement_id, limit=limit)

def server_drain_readiness(self, server_id: str) -> dict[str, Any]:
    """Return a non-mutating, safe drain/retirement checklist."""
    server = str(server_id or "").strip()
    with self.database.connect() as connection:
        row = _FLEET_HEALTH.drain_snapshot(
            connection,
            server,
            include_free_keys=self._table_exists(connection, "keys"),
            include_free_intents=self._table_exists(
                connection, "free_provisioning_intents"
            ),
        )
        if row is None:
            raise CommerceError("Outline server is not configured")
        active_free = int(row["active_free"])
        active_paid = int(row["active_paid"])
        open_orders = int(row["pending_orders"])
        pending_intents = int(row["pending_intents"])
    blockers = []
    if active_free:
        blockers.append("active_free_keys")
    if active_paid:
        blockers.append("active_paid_keys")
    if open_orders:
        blockers.append("open_orders")
    if pending_intents:
        blockers.append("pending_provisioning")
    if row["remote_key_count"] is None:
        blockers.append("inventory_not_reconciled")
    elif int(row["remote_key_count"] or 0):
        blockers.append("remote_keys_present")
    if int(row["remote_orphan_key_count"] or 0):
        blockers.append("unreviewed_remote_keys")
    return {
        "server_id": server,
        "lifecycle_state": str(row["lifecycle_state"] or "active"),
        "enabled": bool(row["enabled"]),
        "active_free_keys": active_free,
        "active_paid_keys": active_paid,
        "open_orders": open_orders,
        "pending_provisioning": pending_intents,
        "remote_key_count": row["remote_key_count"],
        "remote_orphan_key_count": int(row["remote_orphan_key_count"] or 0),
        "last_synced_at": row["last_synced_at"],
        "ready_to_retire": not blockers,
        "blockers": blockers,
    }

def endpoint_health_history(
    self,
    server_id: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return safe, recent health evidence without endpoint credentials."""
    try:
        page_limit = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        page_limit = 20
    with self.database.connect() as connection:
        if not self._table_exists(connection, "endpoint_health_observations"):
            return []
        return _FLEET_HEALTH.health_history(connection, server_id, page_limit)

def connectivity_snapshot(self) -> list[dict[str, Any]]:
    """Return provider/region/transport dimensions without secrets."""
    with self.database.connect() as connection:
        return ConnectivityRegistry.endpoint_snapshot(connection)

def service_route_snapshot(self) -> list[dict[str, Any]]:
    """Return protocol routes and capability flags without secrets."""
    with self.database.connect() as connection:
        return ConnectivityRegistry.route_snapshot(connection)
