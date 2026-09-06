"""Capacity and admission snapshot read model."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from commerce_capacity_snapshot_repository import CapacitySnapshotRepository
from commerce_capacity_projection import (
    allocations_by_server,
    free_counts,
    tier_allocations_by_server,
    usage_rows,
)
from commerce_worker_capacity_view import build_server_capacity_view
from commerce_models import UTC, _now_text
from connectivity_registry import ConnectivityRegistry


_CAPACITY_SNAPSHOTS = CapacitySnapshotRepository()


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
        has_free_keys = self._table_exists(connection, "keys")
        snapshot = _CAPACITY_SNAPSHOTS.snapshot_inputs(
            connection,
            expiring_at=expiring_at,
            current_time=_now_text(current),
            include_free_keys=has_free_keys,
        )
        counts = snapshot["counts"]
        key_rows = snapshot["key_rows"]
        server_rows = snapshot["server_rows"]
        registry_by_server = {
            str(item["outline_server_id"]): item
            for item in ConnectivityRegistry.endpoint_snapshot(connection)
        }
        allocation_rows = snapshot["allocation_rows"]
        tier_allocation_rows = snapshot["tier_allocation_rows"]
        free_key_rows = snapshot["free_key_rows"]
    default_server_id = getattr(self.outline, "default_server_id", None)
    metrics_by_server = (
        dict(getattr(self, "_server_metrics_cache", {}))
        if server_rows
        else self._metrics_by_server()
    )
    usage = usage_rows(key_rows, metrics_by_server, default_server_id)
    allocation_views = allocations_by_server(allocation_rows)
    free_count_views = free_counts(free_key_rows, default_server_id)
    tier_allocation_views = tier_allocations_by_server(
        tier_allocation_rows, free_count_views
    )
    strict_allocations = os.environ.get(
        "AURIX_FLEET_STRICT_ALLOCATION_VALIDATION", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    servers = [
        build_server_capacity_view(
            self,
            _CAPACITY_SNAPSHOTS,
            row,
            current=current,
            registry=registry_by_server.get(str(row["server_id"])),
            allocation_views=allocation_views,
            tier_allocation_views=tier_allocation_views,
        )
        for row in server_rows
    ]
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
