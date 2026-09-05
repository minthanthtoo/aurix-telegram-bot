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
                existing = connection.execute(
                    "SELECT server_id FROM outline_servers WHERE provider_resource_id = ?",
                    (provider_resource_id,),
                ).fetchone()
                if existing is not None and str(existing["server_id"]) != str(server_id):
                    raise CommerceError(
                        f"Provider resource {provider_resource_id} is already registered as "
                        f"server {existing['server_id']!r}; keep that stable server ID in "
                        "OUTLINE_SERVERS_JSON"
                    )
            connection.execute(
                """INSERT INTO outline_servers
                   (server_id, label, provider_resource_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(server_id) DO UPDATE SET
                     label = excluded.label,
                     provider_resource_id = COALESCE(excluded.provider_resource_id,
                                                     outline_servers.provider_resource_id),
                     updated_at = excluded.updated_at""",
                (
                    server_id,
                    labels.get(server_id, server_id),
                    provider_resource_id,
                    now_text,
                    now_text,
                ),
            )
            metadata = endpoint_metadata.get(server_id) or {}
            state_row = connection.execute(
                "SELECT lifecycle_state, health_status FROM outline_servers WHERE server_id = ?",
                (server_id,),
            ).fetchone()
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
        placeholders = ",".join("?" for _ in server_ids)
        connection.execute(
            f"""UPDATE outline_servers
                   SET enabled = 0,
                       lifecycle_state = 'retired',
                       lifecycle_reason = COALESCE(lifecycle_reason, 'removed from OUTLINE_SERVERS_JSON'),
                       lifecycle_changed_at = CASE
                           WHEN lifecycle_state != 'retired' OR enabled = 1 THEN ?
                           ELSE lifecycle_changed_at
                       END,
                       updated_at = ?
                 WHERE server_id NOT IN ({placeholders})""",
            (now_text, now_text, *server_ids),
        )
        default_server_id = getattr(self.outline, "default_server_id", server_ids[0])
        connection.execute(
            "UPDATE subscriptions SET server_id = ? WHERE server_id IS NULL",
            (default_server_id,),
        )
        connection.execute(
            "UPDATE paid_vpn_keys SET server_id = ? WHERE server_id IS NULL",
            (default_server_id,),
        )
        if self._table_exists(connection, "keys"):
            connection.execute(
                "UPDATE keys SET server_id = ? WHERE server_id IS NULL",
                (default_server_id,),
            )
            if "primary" not in server_ids:
                connection.execute(
                    "UPDATE keys SET server_id = ? WHERE server_id = 'primary'",
                    (default_server_id,),
                )
        connection.execute(
            """UPDATE orders SET server_id = ?, capacity_reserved_until = COALESCE(capacity_reserved_until, ?)
               WHERE server_id IS NULL AND status IN ('awaiting_payment', 'payment_submitted')""",
            (default_server_id, _now_text(datetime.now(UTC) + timedelta(hours=24))),
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
        if service_route_id:
            row = connection.execute(
                """SELECT r.route_id, r.protocol, r.endpoint_id,
                          e.outline_server_id
                     FROM connectivity_routes r
                     JOIN connectivity_endpoints e ON e.endpoint_id = r.endpoint_id
                    WHERE r.route_id = ?""",
                (str(service_route_id),),
            ).fetchone()
        elif resolved_server:
            row = connection.execute(
                """SELECT r.route_id, r.protocol, r.endpoint_id,
                          e.outline_server_id
                     FROM connectivity_routes r
                     JOIN connectivity_endpoints e ON e.endpoint_id = r.endpoint_id
                    WHERE e.outline_server_id = ? AND r.route_name = 'primary'""",
                (resolved_server,),
            ).fetchone()
        else:
            row = connection.execute(
                """SELECT r.route_id, r.protocol, r.endpoint_id,
                          e.outline_server_id
                     FROM connectivity_routes r
                     JOIN connectivity_endpoints e ON e.endpoint_id = r.endpoint_id
                    WHERE r.route_name = 'primary'
                    ORDER BY r.priority, r.route_id LIMIT 1"""
            ).fetchone()
        if row is not None:
            route = dict(row)
            resolved_server = str(row["outline_server_id"] or resolved_server or "") or None
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
        row = connection.execute(
            "SELECT lifecycle_state, enabled, remote_key_count, remote_orphan_key_count, last_synced_at FROM outline_servers WHERE server_id = ?",
            (server,),
        ).fetchone()
        if row is None:
            raise CommerceError("Outline server is not configured")
        active_free = int(connection.execute(
            "SELECT COUNT(*) AS n FROM keys WHERE server_id = ? AND status IN ('active', 'revoke_failed')",
            (server,),
        ).fetchone()["n"]) if self._table_exists(connection, "keys") else 0
        active_paid = int(connection.execute(
            "SELECT COUNT(*) AS n FROM paid_vpn_keys WHERE server_id = ? AND status IN ('active', 'revoke_failed')",
            (server,),
        ).fetchone()["n"])
        open_orders = int(connection.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE server_id = ? AND status IN ('awaiting_payment', 'payment_submitted')",
            (server,),
        ).fetchone()["n"])
        pending_intents = int(connection.execute(
            "SELECT COUNT(*) AS n FROM free_provisioning_intents WHERE server_id = ? AND status IN ('pending', 'running')",
            (server,),
        ).fetchone()["n"]) if self._table_exists(connection, "free_provisioning_intents") else 0
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
        rows = connection.execute(
            """SELECT observed_at, observed_status, state_before, state_after,
                      latency_ms, remote_key_count, error_type
                 FROM endpoint_health_observations
                WHERE server_id = ?
                ORDER BY observed_at DESC LIMIT ?""",
            (server_id, page_limit),
        ).fetchall()
    return [dict(row) for row in rows]

def connectivity_snapshot(self) -> list[dict[str, Any]]:
    """Return provider/region/transport dimensions without secrets."""
    with self.database.connect() as connection:
        return ConnectivityRegistry.endpoint_snapshot(connection)

def service_route_snapshot(self) -> list[dict[str, Any]]:
    """Return protocol routes and capability flags without secrets."""
    with self.database.connect() as connection:
        return ConnectivityRegistry.route_snapshot(connection)
