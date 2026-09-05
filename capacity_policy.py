"""Pure fleet capacity and lifecycle policy decisions."""

from __future__ import annotations

import os
from typing import Any


def compute_scale_advice(servers: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a non-mutating fleet posture from declared saleable capacity."""

    def threshold(name: str, default: int) -> int:
        try:
            return max(1, min(100, int(os.environ.get(name, str(default)))))
        except (TypeError, ValueError):
            return default

    declared = [
        item
        for item in servers
        if item.get("enabled") and item.get("saleable_key_capacity") is not None
    ]
    configured = [
        item
        for item in declared
        if str(item.get("lifecycle_state") or "active") == "active"
    ]
    healthy = [item for item in configured if item.get("health_status") == "healthy"]
    if not configured:
        if declared:
            return {
                "status": "blocked",
                "utilization_percent": None,
                "remaining_slots": 0,
                "message": "All declared endpoints are draining or retired; no new allocation is allowed.",
            }
        return {
            "status": "unconfigured",
            "utilization_percent": None,
            "remaining_slots": None,
            "message": "Declare key capacity before making a scaling decision.",
        }
    if not healthy:
        return {
            "status": "blocked",
            "utilization_percent": None,
            "remaining_slots": 0,
            "message": "No healthy declared server can accept new keys.",
        }
    total_capacity = sum(max(0, int(item["saleable_key_capacity"])) for item in healthy)
    total_demand = sum(max(0, int(item.get("key_demand") or 0)) for item in healthy)
    remaining = max(0, total_capacity - total_demand)
    utilization = (
        100.0 if total_capacity <= 0 else min(100.0, total_demand / total_capacity * 100)
    )
    traffic_ratios = [
        min(
            100.0,
            max(0, int(item.get("committed_traffic_bytes") or 0))
            / max(1, int(item["monthly_traffic_bytes"]))
            * 100,
        )
        for item in healthy
        if item.get("monthly_traffic_bytes") is not None
    ]
    traffic_utilization = max(traffic_ratios, default=None)
    prepare_at = threshold("AURIX_SCALE_PREPARE_UTILIZATION_PERCENT", 75)
    urgent_at = max(
        prepare_at,
        threshold("AURIX_SCALE_URGENT_UTILIZATION_PERCENT", 90),
    )
    traffic_prepare_at = threshold("AURIX_SCALE_PREPARE_TRAFFIC_PERCENT", prepare_at)
    traffic_urgent_at = max(
        traffic_prepare_at,
        threshold("AURIX_SCALE_URGENT_TRAFFIC_PERCENT", urgent_at),
    )
    urgent_traffic = traffic_utilization is not None and traffic_utilization >= traffic_urgent_at
    prepare_traffic = traffic_utilization is not None and traffic_utilization >= traffic_prepare_at
    if utilization >= urgent_at or urgent_traffic or remaining <= 1:
        status = "urgent"
        message = "Add and verify another Outline node before accepting more demand."
    elif utilization >= prepare_at or prepare_traffic:
        status = "prepare"
        message = "Prepare and verify the next Outline node now."
    else:
        status = "stable"
        message = "Current declared fleet headroom is sufficient."
    return {
        "status": status,
        "utilization_percent": round(utilization, 1),
        "remaining_slots": remaining,
        "saleable_capacity": total_capacity,
        "traffic_utilization_percent": (
            None if traffic_utilization is None else round(traffic_utilization, 1)
        ),
        "message": message,
    }
