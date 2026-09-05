"""Server, plan and tier allocation policy use cases."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from commerce_allocation_repository import CommerceAllocationRepository
from commerce_models import UTC
from commerce_models import CommerceError
from commerce_models import Plan
from commerce_models import _new_id
from commerce_models import _now_text


_ALLOCATION = CommerceAllocationRepository()


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
        updated = _ALLOCATION.update_server_capacity(
            connection,
            max_keys=max_keys,
            reserved_keys=reserved_keys,
            monthly_traffic_bytes=monthly_traffic_bytes,
            now_text=now_text,
            server_id=server_id,
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
        if _ALLOCATION.enabled_server(connection, server_id) is None:
            raise CommerceError("Outline server is unavailable")
        if _ALLOCATION.plan(connection, plan_code) is None:
            raise CommerceError("Unknown plan")
        _ALLOCATION.upsert_plan_allocation(
            connection,
            server_id=server_id,
            plan_code=plan_code,
            slot_limit=int(slot_limit),
            now_text=now_text,
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
        if _ALLOCATION.enabled_server(connection, server_id) is None:
            raise CommerceError("Outline server is unavailable")
        _ALLOCATION.upsert_tier_allocation(
            connection,
            server_id=server_id,
            tier_code=normalized,
            slot_limit=int(slot_limit),
            now_text=now_text,
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
    server, plan_total, tier_total = _ALLOCATION.allocation_capacity(connection, server_id)
    if server is None or server["max_keys"] is None:
        return
    saleable = max(0, int(server["max_keys"]) - int(server["reserved_keys"] or 0))
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
        server, existing_plans, existing_tiers = _ALLOCATION.policy_state(
            connection, server_id
        )
        if server is None:
            raise CommerceError("Outline server is unavailable")
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
        # Clear stale rows first; no intermediate strict check is performed
        # until every manifest value is present in this transaction.
        _ALLOCATION.clear_policy(connection, server_id, now_text)
        _ALLOCATION.write_policy_values(
            connection,
            server_id=server_id,
            max_keys=max_keys,
            reserved_keys=reserved_keys,
            monthly_traffic_bytes=monthly_traffic_bytes,
            now_text=now_text,
            plan_slots=desired_plans,
            tier_slots=desired_tiers,
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
    health_max_age = max(
        30, int(os.environ.get("AURIX_SERVER_HEALTH_MAX_AGE_SECONDS", "900"))
    )
    selection_time = datetime.fromisoformat(now_text).astimezone(UTC)
    fresh_after = _now_text(selection_time - timedelta(seconds=health_max_age))
    inputs = _ALLOCATION.selection_inputs(
        connection,
        plan_code=plan_code,
        fresh_after=fresh_after,
        now_text=now_text,
        include_route_health=self._table_exists(connection, "route_health_snapshots"),
    )
    plan = inputs["plan"]
    if plan is None:
        raise CommerceError("Unknown or inactive plan")
    requested_quota = int(plan["quota_bytes"] or 0)
    has_plan_allocations = inputs["has_allocations"]
    candidates: list[tuple[float, int, float, str]] = []
    for evidence in inputs["servers"]:
        server = evidence["server"]
        server_id = str(server["server_id"])
        probe_status = "unknown"
        probe_score = -1.0
        probe = evidence["probe"]
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
        allocation = evidence["allocation"]
        if has_plan_allocations and allocation is None:
            continue
        allocated_count = evidence["allocated_count"]
        if allocation is not None and int(allocated_count) >= int(allocation["slot_limit"]):
            continue
        remote_keys = int(server["remote_key_count"] or 0)
        reservations = evidence["reservations"]
        pending_keys = evidence["pending_keys"]
        max_keys = server["max_keys"]
        usable = None if max_keys is None else max(0, int(max_keys) - int(server["reserved_keys"] or 0))
        if usable is not None and remote_keys + int(reservations) + int(pending_keys) >= usable:
            continue
        traffic_budget = server["monthly_traffic_bytes"]
        if traffic_budget is not None:
            committed = evidence["committed"]
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
        _ALLOCATION.record_decision(
            connection,
            decision_id=f"decision-{_new_id()}",
            telegram_id=telegram_id,
            server_id=selected[3],
            score=selected_score,
            evidence_json=json.dumps(
                {
                    "basis": "capacity_and_fresh_probe",
                    "plan_code": str(plan_code),
                    "probe_rank": selected[1],
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
            now_text=now_text,
        )
    return selected[3]

def plans(self) -> list[Plan]:
    with self.database.connect() as connection:
        rows = _ALLOCATION.active_plans(connection)
    return [Plan(**dict(row)) for row in rows]

def get_plan(self, code: str) -> Plan:
    with self.database.connect() as connection:
        row = _ALLOCATION.active_plan(connection, code)
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
        inputs = _ALLOCATION.availability_inputs(connection, fresh_after, now_text)
        plans = inputs["plans"]
        server_count = inputs["server_count"]
        for plan in plans:
            code = str(plan["code"])
            allocations = inputs["allocations"][code]
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
                used = _ALLOCATION.used_for_plan(
                    connection, str(allocation["server_id"]), code, now_text
                )
                remaining += max(0, int(allocation["slot_limit"]) - int(used))
            result[code] = {"available": remaining > 0, "remaining_slots": remaining, "managed": True}
    return result
