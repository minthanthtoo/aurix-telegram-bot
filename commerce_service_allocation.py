"""Server, plan and tier allocation policy use cases."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from commerce_models import UTC
from commerce_models import CommerceError
from commerce_models import Plan
from commerce_models import _new_id
from commerce_models import _now_text


def configure_server_capacity(
    self,
    server_id: str,
    admin_id: int,
    *,
    max_keys: int | None,
    reserved_keys: int = 2,
    monthly_traffic_bytes: int | None = None,
) -> None:
    if max_keys is not None and int(max_keys) <= 0:
        raise CommerceError("Maximum keys must be positive")
    if int(reserved_keys) < 0:
        raise CommerceError("Reserved headroom cannot be negative")
    if monthly_traffic_bytes is not None and int(monthly_traffic_bytes) <= 0:
        raise CommerceError("Traffic budget must be positive")
    now_text = _now_text()
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        updated = connection.execute(
            """UPDATE outline_servers SET max_keys = ?, reserved_keys = ?,
                      monthly_traffic_bytes = ?, updated_at = ? WHERE server_id = ?""",
            (max_keys, reserved_keys, monthly_traffic_bytes, now_text, server_id),
        )
        if getattr(updated, "rowcount", 1) == 0:
            raise CommerceError("Outline server is not configured in the environment")
        self._validate_server_allocation_capacity(connection, server_id)
        self._audit(
            connection, "server_capacity_changed", "outline_server", server_id,
            "admin", str(admin_id),
            {"max_keys": max_keys, "reserved_keys": reserved_keys,
             "monthly_traffic_bytes": monthly_traffic_bytes},
        )

def configure_plan_allocation(
    self, server_id: str, plan_code: str, slot_limit: int, admin_id: int
) -> None:
    if int(slot_limit) < 0:
        raise CommerceError("Plan slots cannot be negative")
    now_text = _now_text()
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        if connection.execute(
            "SELECT 1 FROM outline_servers WHERE server_id = ? AND enabled = 1", (server_id,)
        ).fetchone() is None:
            raise CommerceError("Outline server is unavailable")
        if connection.execute("SELECT 1 FROM plans WHERE code = ?", (plan_code,)).fetchone() is None:
            raise CommerceError("Unknown plan")
        connection.execute(
            """INSERT INTO server_plan_allocations
               (server_id, plan_code, slot_limit, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(server_id, plan_code) DO UPDATE SET
                 slot_limit = excluded.slot_limit, updated_at = excluded.updated_at""",
            (server_id, plan_code, int(slot_limit), now_text),
        )
        self._validate_server_allocation_capacity(connection, server_id)
        self._audit(
            connection, "plan_capacity_changed", "outline_server", server_id,
            "admin", str(admin_id), {"plan_code": plan_code, "slot_limit": int(slot_limit)},
        )

def configure_tier_allocation(
    self, server_id: str, tier_code: str, slot_limit: int, admin_id: int
) -> None:
    """Allocate free/promo issuance slots without moving existing keys."""
    normalized = str(tier_code).upper()
    if normalized not in {"FREE300MB", "FREE3GB", "PROMO"}:
        raise CommerceError("Unknown free or promotional tier")
    if int(slot_limit) < 0:
        raise CommerceError("Tier slots cannot be negative")
    now_text = _now_text()
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        if connection.execute(
            "SELECT 1 FROM outline_servers WHERE server_id = ? AND enabled = 1", (server_id,)
        ).fetchone() is None:
            raise CommerceError("Outline server is unavailable")
        connection.execute(
            """INSERT INTO server_tier_allocations
               (server_id, tier_code, slot_limit, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(server_id, tier_code) DO UPDATE SET
                 slot_limit = excluded.slot_limit, updated_at = excluded.updated_at""",
            (server_id, normalized, int(slot_limit), now_text),
        )
        self._validate_server_allocation_capacity(connection, server_id)
        self._audit(
            connection,
            "tier_capacity_changed",
            "outline_server",
            server_id,
            "admin",
            str(admin_id),
            {"tier_code": normalized, "slot_limit": int(slot_limit)},
        )

def _validate_server_allocation_capacity(connection: Any, server_id: str) -> None:
    """Reject a new over-allocation when strict fleet policy is enabled.

    Existing deployments can remain in compatibility mode while their
    manifest is migrated. Once strict validation is enabled, Telegram
    capacity controls and declarative reconciliation share the same
    invariant: the sum of all plan/tier slots cannot exceed saleable key
    headroom. The transaction rolls back on failure, so an admin cannot
    leave a partially updated policy behind.
    """
    if os.environ.get("AURIX_FLEET_STRICT_ALLOCATION_VALIDATION", "").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return
    server = connection.execute(
        "SELECT max_keys, reserved_keys FROM outline_servers WHERE server_id = ?",
        (server_id,),
    ).fetchone()
    if server is None or server["max_keys"] is None:
        return
    saleable = max(0, int(server["max_keys"]) - int(server["reserved_keys"] or 0))
    plan_total = int(
        connection.execute(
            "SELECT COALESCE(SUM(slot_limit), 0) AS n FROM server_plan_allocations WHERE server_id = ?",
            (server_id,),
        ).fetchone()["n"]
        or 0
    )
    tier_total = int(
        connection.execute(
            "SELECT COALESCE(SUM(slot_limit), 0) AS n FROM server_tier_allocations WHERE server_id = ?",
            (server_id,),
        ).fetchone()["n"]
        or 0
    )
    total = plan_total + tier_total
    if total > saleable:
        raise CommerceError(
            f"server {server_id} allocates {total} slots but only {saleable} remain after reserved headroom"
        )

def apply_server_policy(
    self,
    server_id: str,
    admin_id: int,
    *,
    max_keys: int | None,
    reserved_keys: int,
    monthly_traffic_bytes: int | None,
    plan_slots: dict[str, int],
    tier_slots: dict[str, int],
) -> None:
    """Apply one complete server policy atomically.

    Fleet reconciliation can migrate a legacy over-allocated database to a
    strict manifest only if capacity and all allocations are changed in one
    transaction. Calling the individual Telegram controls sequentially
    would reject the transient state before the later reductions arrive.
    """
    if max_keys is not None and int(max_keys) <= 0:
        raise CommerceError("Maximum keys must be positive")
    if int(reserved_keys) < 0:
        raise CommerceError("Reserved headroom cannot be negative")
    if monthly_traffic_bytes is not None and int(monthly_traffic_bytes) <= 0:
        raise CommerceError("Traffic budget must be positive")
    normalized_plans = {str(code): int(limit) for code, limit in plan_slots.items()}
    normalized_tiers = {str(code).upper(): int(limit) for code, limit in tier_slots.items()}
    if any(limit < 0 for limit in normalized_plans.values()):
        raise CommerceError("Plan slots cannot be negative")
    if any(limit < 0 for limit in normalized_tiers.values()):
        raise CommerceError("Tier slots cannot be negative")
    if any(code not in {str(plan.code) for plan in self.plans()} for code in normalized_plans):
        raise CommerceError("Unknown plan")
    if any(code not in {"FREE300MB", "FREE3GB", "PROMO"} for code in normalized_tiers):
        raise CommerceError("Unknown free or promotional tier")
    now_text = _now_text()
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        server = connection.execute(
            "SELECT max_keys, reserved_keys, monthly_traffic_bytes FROM outline_servers "
            "WHERE server_id = ? AND enabled = 1",
            (server_id,),
        ).fetchone()
        if server is None:
            raise CommerceError("Outline server is unavailable")
        existing_plans = {
            str(row["plan_code"]): int(row["slot_limit"] or 0)
            for row in connection.execute(
                "SELECT plan_code, slot_limit FROM server_plan_allocations WHERE server_id = ?",
                (server_id,),
            ).fetchall()
            if int(row["slot_limit"] or 0) > 0
        }
        existing_tiers = {
            str(row["tier_code"]): int(row["slot_limit"] or 0)
            for row in connection.execute(
                "SELECT tier_code, slot_limit FROM server_tier_allocations WHERE server_id = ?",
                (server_id,),
            ).fetchall()
            if int(row["slot_limit"] or 0) > 0
        }
        desired_plans = {
            code: limit for code, limit in normalized_plans.items() if limit > 0
        }
        desired_tiers = {
            code: limit for code, limit in normalized_tiers.items() if limit > 0
        }
        changed = (
            server["max_keys"] != max_keys
            or int(server["reserved_keys"] or 0) != int(reserved_keys)
            or server["monthly_traffic_bytes"] != monthly_traffic_bytes
            or existing_plans != desired_plans
            or existing_tiers != desired_tiers
        )
        if not changed:
            return
        connection.execute(
            """UPDATE outline_servers SET max_keys = ?, reserved_keys = ?,
                      monthly_traffic_bytes = ?, updated_at = ? WHERE server_id = ?""",
            (max_keys, reserved_keys, monthly_traffic_bytes, now_text, server_id),
        )
        # Clear stale rows first; no intermediate strict check is performed
        # until every manifest value is present in this transaction.
        connection.execute(
            "UPDATE server_plan_allocations SET slot_limit = 0, updated_at = ? WHERE server_id = ?",
            (now_text, server_id),
        )
        connection.execute(
            "UPDATE server_tier_allocations SET slot_limit = 0, updated_at = ? WHERE server_id = ?",
            (now_text, server_id),
        )
        for plan_code, slot_limit in desired_plans.items():
            connection.execute(
                """INSERT INTO server_plan_allocations
                   (server_id, plan_code, slot_limit, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(server_id, plan_code) DO UPDATE SET
                     slot_limit = excluded.slot_limit, updated_at = excluded.updated_at""",
                (server_id, plan_code, slot_limit, now_text),
            )
        for tier_code, slot_limit in desired_tiers.items():
            connection.execute(
                """INSERT INTO server_tier_allocations
                   (server_id, tier_code, slot_limit, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(server_id, tier_code) DO UPDATE SET
                     slot_limit = excluded.slot_limit, updated_at = excluded.updated_at""",
                (server_id, tier_code, slot_limit, now_text),
            )
        self._validate_server_allocation_capacity(connection, server_id)
        self._audit(
            connection,
            "server_policy_reconciled",
            "outline_server",
            server_id,
            "admin",
            str(admin_id),
            {
                "max_keys": max_keys,
                "reserved_keys": int(reserved_keys),
                "monthly_traffic_bytes": monthly_traffic_bytes,
                "plan_slots": desired_plans,
                "tier_slots": desired_tiers,
            },
        )

def _select_server_for_plan(
    self,
    connection: Any,
    plan_code: str,
    now_text: str,
    *,
    telegram_id: int | None = None,
) -> str:
    plan = connection.execute(
        "SELECT quota_bytes FROM plans WHERE code = ? AND active = 1", (plan_code,)
    ).fetchone()
    if plan is None:
        raise CommerceError("Unknown or inactive plan")
    requested_quota = int(plan["quota_bytes"] or 0)
    has_plan_allocations = int(
        connection.execute(
            "SELECT COUNT(*) AS n FROM server_plan_allocations WHERE plan_code = ?",
            (plan_code,),
        ).fetchone()["n"]
    ) > 0
    health_max_age = max(
        30, int(os.environ.get("AURIX_SERVER_HEALTH_MAX_AGE_SECONDS", "900"))
    )
    selection_time = datetime.fromisoformat(now_text).astimezone(UTC)
    fresh_after = _now_text(selection_time - timedelta(seconds=health_max_age))
    servers = connection.execute(
        """SELECT * FROM outline_servers
           WHERE enabled = 1 AND lifecycle_state = 'active'
             AND health_status = 'healthy'
             AND last_synced_at IS NOT NULL AND last_synced_at >= ?
           ORDER BY server_id""",
        (fresh_after,),
    ).fetchall()
    candidates: list[tuple[float, int, float, str]] = []
    for server in servers:
        server_id = str(server["server_id"])
        probe_status = "unknown"
        probe_score = -1.0
        probe = (
            connection.execute(
                """SELECT status, score, last_observed_at
                     FROM route_health_snapshots WHERE server_id = ?""",
                (server_id,),
            ).fetchone()
            if self._table_exists(connection, "route_health_snapshots")
            else None
        )
        if probe is not None and probe["last_observed_at"] is not None:
            try:
                probe_fresh = datetime.fromisoformat(str(probe["last_observed_at"])).astimezone(UTC) >= selection_time - timedelta(seconds=health_max_age)
            except (TypeError, ValueError, OverflowError):
                probe_fresh = False
            if probe_fresh:
                probe_status = str(probe["status"] or "unknown")
                probe_score = float(probe["score"]) if probe["score"] is not None else -1.0
                if probe_status == "unreachable":
                    continue
                require_probe = os.environ.get("AURIX_REQUIRE_PROBE_EVIDENCE_FOR_ISSUANCE", "0").strip().lower() in {"1", "true", "yes", "on"}
                if require_probe and probe_status == "unknown":
                    continue
        allocation = connection.execute(
            "SELECT slot_limit FROM server_plan_allocations WHERE server_id = ? AND plan_code = ?",
            (server_id, plan_code),
        ).fetchone()
        if has_plan_allocations and allocation is None:
            continue
        allocated_count = connection.execute(
            """SELECT
                 (SELECT COUNT(*) FROM subscriptions WHERE server_id = ? AND plan_code = ?
                    AND status IN ('pending', 'active')) +
                 (SELECT COUNT(*) FROM orders WHERE server_id = ? AND plan_code = ?
                    AND (status = 'payment_submitted' OR
                         (status = 'awaiting_payment' AND capacity_reserved_until > ?))) AS n""",
            (server_id, plan_code, server_id, plan_code, now_text),
        ).fetchone()["n"]
        if allocation is not None and int(allocated_count) >= int(allocation["slot_limit"]):
            continue
        remote_keys = int(server["remote_key_count"] or 0)
        reservations = connection.execute(
            """SELECT COUNT(*) AS n FROM orders WHERE server_id = ?
               AND (status = 'payment_submitted' OR
                    (status = 'awaiting_payment' AND capacity_reserved_until > ?))""",
            (server_id, now_text),
        ).fetchone()["n"]
        pending_keys = connection.execute(
            "SELECT COUNT(*) AS n FROM subscriptions WHERE server_id = ? AND status = 'pending'",
            (server_id,),
        ).fetchone()["n"]
        max_keys = server["max_keys"]
        usable = None if max_keys is None else max(0, int(max_keys) - int(server["reserved_keys"] or 0))
        if usable is not None and remote_keys + int(reservations) + int(pending_keys) >= usable:
            continue
        traffic_budget = server["monthly_traffic_bytes"]
        if traffic_budget is not None:
            committed = connection.execute(
                """SELECT
                   COALESCE((SELECT SUM(COALESCE(quota_bytes, 0)) FROM subscriptions
                     WHERE server_id = ? AND status IN ('pending', 'active')), 0) +
                   COALESCE((SELECT SUM(COALESCE(quota_bytes_snapshot, 0)) FROM orders
                     WHERE server_id = ? AND (status = 'payment_submitted' OR
                       (status = 'awaiting_payment' AND capacity_reserved_until > ?))), 0) AS n""",
                (server_id, server_id, now_text),
            ).fetchone()["n"]
            if int(committed or 0) + requested_quota > int(traffic_budget):
                continue
        denominator = int(allocation["slot_limit"]) if allocation is not None and int(allocation["slot_limit"]) else (usable or 1)
        probe_rank = {"healthy": 0, "degraded": 1, "unknown": 2}.get(probe_status, 2)
        candidates.append((int(allocated_count) / max(1, denominator), probe_rank, -probe_score, server_id))
    if not candidates:
        raise CommerceError("This plan is temporarily full. Please check again later.")
    selected = min(candidates)
    if self._table_exists(connection, "route_decisions"):
        selected_score = -selected[2] if selected[2] >= 0 else None
        connection.execute(
            """INSERT INTO route_decisions
               (decision_id, telegram_id, entitlement_ref, requested_region,
                selected_server_id, decision_mode, score, evidence_json, created_at)
               VALUES (?, ?, NULL, NULL, ?, 'automatic', ?, ?, ?)""",
            (
                f"decision-{_new_id()}",
                telegram_id,
                selected[3],
                selected_score,
                json.dumps(
                    {
                        "basis": "capacity_and_fresh_probe",
                        "plan_code": str(plan_code),
                        "probe_rank": selected[1],
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                now_text,
            ),
        )
    return selected[3]

def plans(self) -> list[Plan]:
    with self.database.connect() as connection:
        rows = connection.execute(
            """SELECT code, name, price_minor, currency, quota_bytes, duration_days
               FROM plans WHERE active = 1 ORDER BY price_minor"""
        ).fetchall()
    return [Plan(**dict(row)) for row in rows]

def get_plan(self, code: str) -> Plan:
    with self.database.connect() as connection:
        row = connection.execute(
            """SELECT code, name, price_minor, currency, quota_bytes, duration_days
               FROM plans WHERE code = ? AND active = 1""",
            (code,),
        ).fetchone()
    if row is None:
        raise CommerceError("Unknown or inactive plan")
    return Plan(**dict(row))

def plan_availability(self, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Return admission availability from configured per-server allocations."""
    now_text = _now_text(now)
    result: dict[str, dict[str, Any]] = {}
    health_max_age = max(
        30, int(os.environ.get("AURIX_SERVER_HEALTH_MAX_AGE_SECONDS", "900"))
    )
    fresh_after = _now_text((now or datetime.now(UTC)) - timedelta(seconds=health_max_age))
    with self.database.connect() as connection:
        plans = connection.execute("SELECT code FROM plans WHERE active = 1").fetchall()
        server_count = int(
            connection.execute(
                "SELECT COUNT(*) AS n FROM outline_servers WHERE enabled = 1"
            ).fetchone()["n"]
        )
        for plan in plans:
            code = str(plan["code"])
            allocations = connection.execute(
                """SELECT a.server_id, a.slot_limit FROM server_plan_allocations a
                   JOIN outline_servers s ON s.server_id = a.server_id
                   WHERE a.plan_code = ? AND s.enabled = 1
                     AND s.health_status = 'healthy'
                     AND s.last_synced_at IS NOT NULL AND s.last_synced_at >= ?""",
                (code, fresh_after),
            ).fetchall()
            if not server_count:
                result[code] = {"available": True, "remaining_slots": None, "managed": False}
                continue
            if not allocations:
                # Total server limits still protect admission; plan-specific
                # allocation remains optional until the owner configures it.
                try:
                    self._select_server_for_plan(connection, code, now_text)
                    result[code] = {"available": True, "remaining_slots": None, "managed": False}
                except CommerceError:
                    result[code] = {"available": False, "remaining_slots": 0, "managed": False}
                continue
            remaining = 0
            for allocation in allocations:
                used = connection.execute(
                    """SELECT
                       (SELECT COUNT(*) FROM subscriptions WHERE server_id = ? AND plan_code = ?
                          AND status IN ('pending', 'active')) +
                       (SELECT COUNT(*) FROM orders WHERE server_id = ? AND plan_code = ?
                          AND (status = 'payment_submitted' OR
                               (status = 'awaiting_payment' AND capacity_reserved_until > ?))) AS n""",
                    (allocation["server_id"], code, allocation["server_id"], code, now_text),
                ).fetchone()["n"]
                remaining += max(0, int(allocation["slot_limit"]) - int(used))
            result[code] = {"available": remaining > 0, "remaining_slots": remaining, "managed": True}
    return result
