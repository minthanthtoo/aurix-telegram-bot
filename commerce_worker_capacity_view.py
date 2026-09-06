"""Per-server capacity and admission projection for the worker snapshot."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from commerce_models import UTC


def build_server_capacity_view(
    row: Any,
    commitments: dict[str, Any],
    *,
    current: datetime,
    registry: dict[str, Any] | None,
    allocation_views: dict[str, list[dict[str, Any]]],
    tier_allocation_views: dict[str, list[dict[str, Any]]],
    health_max_age_seconds: int,
) -> dict[str, Any]:
    item = dict(row)
    _attach_registry(item, registry)
    _calculate_capacity(item, commitments)
    _set_allocation_policy(item, allocation_views, tier_allocation_views)
    _set_admission_policy(item, current=current, health_max_age_seconds=health_max_age_seconds)
    return item


def _attach_registry(item: dict[str, Any], registry: dict[str, Any] | None) -> None:
    if not registry:
        return
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


def _calculate_capacity(item: dict[str, Any], commitments: dict[str, Any]) -> None:
    max_keys = item.get("max_keys")
    usable = (
        None
        if max_keys is None
        else max(0, int(max_keys) - int(item.get("reserved_keys") or 0))
    )
    remote = int(item.get("remote_key_count") or 0)
    item.update(commitments)
    reserved_orders = commitments["reserved_order_count"]
    pending_keys = commitments["pending_key_count"]
    committed_traffic = commitments["committed_traffic_bytes"]
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


def _set_allocation_policy(
    item: dict[str, Any],
    allocation_views: dict[str, list[dict[str, Any]]],
    tier_allocation_views: dict[str, list[dict[str, Any]]],
) -> None:
    usable = item.get("saleable_key_capacity")
    item["allocations"] = allocation_views.get(str(item["server_id"]), [])
    item["tier_allocations"] = tier_allocation_views.get(str(item["server_id"]), [])
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


def _set_admission_policy(
    item: dict[str, Any], *, current: datetime, health_max_age_seconds: int
) -> None:
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
            if current - synced_at > timedelta(seconds=health_max_age_seconds):
                admission_blockers.append("stale_inventory")
        except (TypeError, ValueError, OverflowError):
            admission_blockers.append("invalid_inventory_time")
    if item.get("remaining_key_slots") is not None and int(item["remaining_key_slots"]) <= 0:
        admission_blockers.append("key_capacity")
    item["admission_status"] = "blocked" if admission_blockers else "eligible"
    item["admission_blockers"] = admission_blockers
