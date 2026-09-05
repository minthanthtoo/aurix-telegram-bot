"""Capacity and admission snapshot read model."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from commerce_models import UTC, _now_text
from connectivity_registry import ConnectivityRegistry


def capacity_snapshot(
    self, now: datetime | None = None, *, refresh_inventory: bool = True
) -> dict[str, Any]:
    """Return declared capacity beside observed remote inventory/telemetry."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    expiring_at = _now_text(current + timedelta(hours=24))
    if refresh_inventory:
        try:
            self.refresh_server_inventory(current)
        except Exception:
            pass
    with self.database.connect() as connection:
        counts = connection.execute(
            """SELECT
                   (SELECT COUNT(*) FROM subscriptions WHERE status = 'active') AS active_subscriptions,
                   (SELECT COUNT(*) FROM paid_vpn_keys WHERE status = 'active') AS active_keys,
                   (SELECT COUNT(*) FROM provisioning_jobs WHERE status IN ('pending', 'running')) AS pending_jobs,
                   (SELECT COUNT(*) FROM provisioning_jobs WHERE status = 'failed') AS failed_jobs,
                   (SELECT COUNT(*) FROM subscriptions
                      WHERE status = 'active'
                        AND expires_at <= ?) AS expiring_24h""",
            (expiring_at,),
        ).fetchone()
        key_rows = connection.execute(
            """SELECT outline_key_id, telegram_id, quota_bytes, server_id
               FROM paid_vpn_keys WHERE status = 'active'"""
        ).fetchall()
        server_rows = connection.execute(
            "SELECT * FROM outline_servers ORDER BY enabled DESC, label, server_id"
        ).fetchall()
        registry_by_server = {
            str(item["outline_server_id"]): item
            for item in ConnectivityRegistry.endpoint_snapshot(connection)
        }
        allocation_rows = connection.execute(
            """SELECT a.server_id, a.plan_code, a.slot_limit, p.name,
                      (SELECT COUNT(*) FROM subscriptions s
                        WHERE s.server_id = a.server_id AND s.plan_code = a.plan_code
                          AND s.status IN ('pending', 'active')) AS active_count,
                      (SELECT COUNT(*) FROM orders o
                        WHERE o.server_id = a.server_id AND o.plan_code = a.plan_code
                          AND (o.status = 'payment_submitted' OR
                               (o.status = 'awaiting_payment' AND o.capacity_reserved_until > ?))) AS reserved_count
               FROM server_plan_allocations a JOIN plans p ON p.code = a.plan_code
               ORDER BY a.server_id, p.price_minor""",
            (_now_text(current),),
        ).fetchall()
        tier_allocation_rows = connection.execute(
            """SELECT server_id, tier_code, slot_limit
               FROM server_tier_allocations ORDER BY server_id, tier_code"""
        ).fetchall()
        free_key_rows = (
            connection.execute(
                """SELECT k.server_id, k.key_type,
                          CASE WHEN g.key_id IS NULL THEN 0 ELSE 1 END AS is_promo
                   FROM keys k LEFT JOIN giveaway_claims g ON g.key_id = k.id
                   WHERE k.status IN ('active', 'revoke_failed')"""
            ).fetchall()
            if self._table_exists(connection, "keys")
            else []
        )
    default_server_id = getattr(self.outline, "default_server_id", None)
    metrics_by_server = (
        dict(getattr(self, "_server_metrics_cache", {}))
        if server_rows
        else self._metrics_by_server()
    )
    usage = []
    for row in key_rows:
        server_id = row["server_id"] or default_server_id
        by_key = metrics_by_server.get(server_id, {})
        raw_used = by_key.get(str(row["outline_key_id"]), 0)
        try:
            used_bytes = max(0, int(raw_used or 0))
        except (TypeError, ValueError):
            used_bytes = 0
        usage.append(
            {
                "outline_key_id": row["outline_key_id"],
                "telegram_id": row["telegram_id"],
                "quota_bytes": row["quota_bytes"],
                "used_bytes": used_bytes,
                "server_id": server_id,
            }
        )
    allocations_by_server: dict[str, list[dict[str, Any]]] = {}
    for row in allocation_rows:
        item = dict(row)
        item["remaining_slots"] = max(
            0,
            int(item["slot_limit"]) - int(item["active_count"]) - int(item["reserved_count"]),
        )
        allocations_by_server.setdefault(str(item["server_id"]), []).append(item)
    free_counts: dict[tuple[str, str], int] = {}
    for row in free_key_rows:
        tier_code = (
            "PROMO"
            if int(row["is_promo"])
            else "FREE300MB"
            if row["key_type"] == "daily_free"
            else "FREE3GB"
        )
        identity = (str(row["server_id"] or default_server_id), tier_code)
        free_counts[identity] = free_counts.get(identity, 0) + 1
    tier_allocations_by_server: dict[str, list[dict[str, Any]]] = {}
    for row in tier_allocation_rows:
        item = dict(row)
        active_count = free_counts.get((str(item["server_id"]), str(item["tier_code"])), 0)
        item["active_count"] = active_count
        item["remaining_slots"] = max(0, int(item["slot_limit"]) - active_count)
        tier_allocations_by_server.setdefault(str(item["server_id"]), []).append(item)
    servers = []
    strict_allocations = os.environ.get(
        "AURIX_FLEET_STRICT_ALLOCATION_VALIDATION", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    for row in server_rows:
        item = dict(row)
        registry = registry_by_server.get(str(item["server_id"]))
        if registry:
            item["connectivity"] = {
                "endpoint_id": registry["endpoint_id"],
                "provider_id": registry["provider_id"],
                "provider_name": registry["provider_name"],
                "region_id": registry["region_id"],
                "region_name": registry["region_name"],
                "transport_id": registry["transport_id"],
                "protocol": registry["protocol"],
                "transport_name": registry["transport_name"],
                "status": registry["status"],
                "accepts_new_keys": bool(registry["accepts_new_keys"]),
                "updated_at": registry["updated_at"],
            }
        max_keys = item.get("max_keys")
        usable = (
            None
            if max_keys is None
            else max(0, int(max_keys) - int(item.get("reserved_keys") or 0))
        )
        remote = int(item.get("remote_key_count") or 0)
        with self.database.connect() as connection:
            reserved_orders = int(
                connection.execute(
                    """SELECT COUNT(*) AS n FROM orders WHERE server_id = ?
                       AND (status = 'payment_submitted' OR
                            (status = 'awaiting_payment' AND capacity_reserved_until > ?))""",
                    (item["server_id"], _now_text(current)),
                ).fetchone()["n"]
            )
            pending_keys = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM subscriptions WHERE server_id = ? AND status = 'pending'",
                    (item["server_id"],),
                ).fetchone()["n"]
            )
            committed_traffic = int(
                connection.execute(
                    """SELECT
                       COALESCE((SELECT SUM(COALESCE(quota_bytes, 0)) FROM subscriptions
                         WHERE server_id = ? AND status IN ('pending', 'active')), 0) +
                       COALESCE((SELECT SUM(COALESCE(quota_bytes_snapshot, 0)) FROM orders
                         WHERE server_id = ? AND (status = 'payment_submitted' OR
                           (status = 'awaiting_payment' AND capacity_reserved_until > ?))), 0) AS n""",
                    (item["server_id"], item["server_id"], _now_text(current)),
                ).fetchone()["n"]
            )
        item["reserved_order_count"] = reserved_orders
        item["pending_key_count"] = pending_keys
        item["committed_traffic_bytes"] = committed_traffic
        # Drain/retirement evidence is kept beside capacity so the owner
        # can see exactly why an endpoint is still unsafe to remove.
        with self.database.connect() as readiness_connection:
            item["active_free_key_count"] = int(
                readiness_connection.execute(
                    "SELECT COUNT(*) AS n FROM keys WHERE server_id = ? AND status IN ('active', 'revoke_failed')",
                    (item["server_id"],),
                ).fetchone()["n"]
            ) if self._table_exists(readiness_connection, "keys") else 0
            item["active_paid_key_count"] = int(
                readiness_connection.execute(
                    "SELECT COUNT(*) AS n FROM paid_vpn_keys WHERE server_id = ? AND status IN ('active', 'revoke_failed')",
                    (item["server_id"],),
                ).fetchone()["n"]
            )
            item["open_order_count"] = int(
                readiness_connection.execute(
                    "SELECT COUNT(*) AS n FROM orders WHERE server_id = ? AND status IN ('awaiting_payment', 'payment_submitted')",
                    (item["server_id"],),
                ).fetchone()["n"]
            )
            item["pending_provisioning_count"] = int(
                readiness_connection.execute(
                    "SELECT COUNT(*) AS n FROM free_provisioning_intents WHERE server_id = ? AND status IN ('pending', 'running')",
                    (item["server_id"],),
                ).fetchone()["n"]
            ) if self._table_exists(readiness_connection, "free_provisioning_intents") else 0
        item["drain_ready_to_retire"] = not any(
            (
                item["active_free_key_count"],
                item["active_paid_key_count"],
                item["open_order_count"],
                item["pending_provisioning_count"],
                item.get("remote_key_count") is None,
                int(item.get("remote_key_count") or 0),
                int(item.get("remote_orphan_key_count") or 0),
            )
        )
        item["remaining_traffic_bytes"] = (
            None
            if item.get("monthly_traffic_bytes") is None
            else max(0, int(item["monthly_traffic_bytes"]) - committed_traffic)
        )
        item["remaining_key_slots"] = (
            None if usable is None else max(0, usable - remote - reserved_orders - pending_keys)
        )
        item["saleable_key_capacity"] = usable
        item["key_demand"] = remote + reserved_orders + pending_keys
        item["key_utilization_percent"] = (
            None
            if usable is None or usable <= 0
            else min(100.0, (item["key_demand"] / usable) * 100.0)
        )
        item["allocations"] = allocations_by_server.get(str(item["server_id"]), [])
        item["tier_allocations"] = tier_allocations_by_server.get(
            str(item["server_id"]), []
        )
        allocation_total = sum(
            int(allocation.get("slot_limit") or 0) for allocation in item["allocations"]
        ) + sum(
            int(allocation.get("slot_limit") or 0)
            for allocation in item["tier_allocations"]
        )
        orphan_count = int(item.get("remote_orphan_key_count") or 0)
        allocation_gap = None if usable is None else int(usable) - allocation_total
        policy_blockers: list[str] = []
        if allocation_gap is not None and allocation_gap < 0:
            policy_blockers.append("overallocated")
        if orphan_count:
            policy_blockers.append("untracked_remote_keys")
        item["allocation_total_slots"] = allocation_total
        item["allocation_remaining_slots"] = allocation_gap
        item["allocation_policy_status"] = (
            "overallocated"
            if allocation_gap is not None and allocation_gap < 0
            else "audit_required"
            if orphan_count
            else "ready"
            if usable is not None
            else "unconfigured"
        )
        item["allocation_policy_blockers"] = policy_blockers
        admission_blockers: list[str] = []
        if not int(item.get("enabled") or 0):
            admission_blockers.append("disabled")
        lifecycle = str(item.get("lifecycle_state") or "active")
        if lifecycle == "draining":
            admission_blockers.append("draining")
        elif lifecycle == "retired":
            admission_blockers.append("retired")
        if str(item.get("health_status") or "") != "healthy":
            admission_blockers.append("unhealthy")
        if item.get("last_synced_at") is None:
            admission_blockers.append("no_inventory")
        else:
            try:
                synced_at = datetime.fromisoformat(str(item["last_synced_at"])).astimezone(UTC)
                age_limit = max(
                    30,
                    int(os.environ.get("AURIX_SERVER_HEALTH_MAX_AGE_SECONDS", "900")),
                )
                if current - synced_at > timedelta(seconds=age_limit):
                    admission_blockers.append("stale_inventory")
            except (TypeError, ValueError, OverflowError):
                admission_blockers.append("invalid_inventory_time")
        if item.get("remaining_key_slots") is not None and int(item["remaining_key_slots"]) <= 0:
            admission_blockers.append("key_capacity")
        item["admission_status"] = "blocked" if admission_blockers else "eligible"
        item["admission_blockers"] = admission_blockers
        servers.append(item)
    outline_version = "multi" if len(servers) > 1 else "unknown"
    if not servers:
        try:
            outline_version = str(self.outline.server_info().get("version", "unknown"))[:64]
        except Exception:
            pass
    advice = self._scale_advice(servers)
    advice.update(self._record_scale_observation(current, servers, advice))
    return {
        **dict(counts),
        "outline_version": outline_version,
        "usage": usage,
        "servers": servers,
        "strict_allocation_validation": strict_allocations,
        "scale_advice": advice,
    }

