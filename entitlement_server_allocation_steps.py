"""Candidate filtering and decision recording for entitlement allocation."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any

from entitlement_models import OutlineError
from entitlement_support import UTC, new_id as _new_id


def collect_candidates(
    service: Any,
    repository: Any,
    connection: Any,
    *,
    servers: Any,
    tier_code: str,
    quota_bytes: int,
    now: datetime,
    max_age: int,
    has_tier_allocations: bool,
) -> list[tuple[float, int, float, str]]:
    """Filter fleet rows by probe, tier, key, and traffic admission policy."""
    candidates: list[tuple[float, int, float, str]] = []
    for server in servers:
        server_id = str(server["server_id"])
        probe_status = "unknown"
        probe_score = -1.0
        probe = (
            repository.route_health(connection, server_id)
            if service._table_exists(connection, "route_health_snapshots")
            else None
        )
        if probe is not None and probe["last_observed_at"] is not None:
            try:
                probe_fresh = datetime.fromisoformat(
                    str(probe["last_observed_at"])
                ).astimezone(UTC) >= now - timedelta(seconds=max_age)
            except (TypeError, ValueError, OverflowError):
                probe_fresh = False
            if probe_fresh:
                probe_status = str(probe["status"] or "unknown")
                probe_score = float(probe["score"]) if probe["score"] is not None else -1.0
                if probe_status == "unreachable":
                    continue
                if (
                    os.environ.get("AURIX_REQUIRE_PROBE_EVIDENCE_FOR_ISSUANCE", "0")
                    .strip()
                    .lower()
                    in {"1", "true", "yes", "on"}
                    and probe_status == "unknown"
                ):
                    continue
        allocation = repository.tier_allocation(connection, server_id, tier_code)
        if has_tier_allocations and allocation is None:
            continue
        active_tier = repository.active_tier_count(connection, server_id, tier_code)
        pending_tier = 0
        if service._intent_tables_exist(connection):
            pending_kind = {"FREE300MB": "daily", "FREE3GB": "trial", "PROMO": "promo"}.get(tier_code)
            if pending_kind:
                pending_tier = repository.pending_tier_count(connection, server_id, pending_kind)
        active_tier = int(active_tier) + pending_tier
        if allocation is not None and active_tier >= int(allocation["slot_limit"]):
            continue
        remote_keys = int(server["remote_key_count"] or 0) + pending_tier
        max_keys = server["max_keys"]
        usable = None if max_keys is None else max(0, int(max_keys) - int(server["reserved_keys"] or 0))
        if usable is not None and remote_keys >= usable:
            continue
        traffic_budget = server["monthly_traffic_bytes"]
        if traffic_budget is not None:
            free_committed, paid_committed = repository.traffic_commitment(
                connection, server_id
            )
            if free_committed + paid_committed + quota_bytes > int(traffic_budget):
                continue
        denominator = (
            int(allocation["slot_limit"])
            if allocation is not None and int(allocation["slot_limit"])
            else (usable or max(1, remote_keys + 1))
        )
        probe_rank = {"healthy": 0, "degraded": 1, "unknown": 2}.get(probe_status, 2)
        candidates.append((int(active_tier) / max(1, denominator), probe_rank, -probe_score, server_id))
    return candidates


def record_route_decision(
    repository: Any,
    connection: Any,
    *,
    selected: tuple[float, int, float, str],
    telegram_id: int | None,
    tier_code: str,
    now: datetime,
) -> None:
    """Record only non-secret routing evidence when its table exists."""
    selected_score = -selected[2] if selected[2] >= 0 else None
    repository.record_decision(
        connection,
        decision_id=f"decision-{_new_id()}",
        telegram_id=telegram_id,
        server_id=selected[3],
        score=selected_score,
        evidence_json=json.dumps(
            {
                "basis": "capacity_and_fresh_probe",
                "tier_code": str(tier_code),
                "probe_rank": selected[1],
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        created_at=now.astimezone(UTC).isoformat(),
    )
