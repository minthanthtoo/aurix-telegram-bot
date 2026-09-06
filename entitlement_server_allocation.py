"""Capacity-aware server allocation for entitlement provisioning."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from entitlement_allocation_repository import EntitlementAllocationRepository
from entitlement_models import OutlineError
from entitlement_server_allocation_steps import collect_candidates, record_route_decision
from entitlement_support import UTC


_ALLOCATION = EntitlementAllocationRepository()


def _select_server_for_tier(
    self,
    connection: Any,
    tier_code: str,
    quota_bytes: int,
    now: datetime,
    *,
    telegram_id: int | None = None,
) -> str:
    """Select a fresh, healthy server using shared key and traffic headroom."""
    if not self._server_tables_exist(connection):
        return self._default_server_id()
    if _ALLOCATION.fleet_size(connection) == 0:
        # Standalone/local free-access databases have no registered fleet.
        # Their single configured adapter remains the authoritative target.
        return self._default_server_id()
    max_age = max(30, int(os.environ.get("AURIX_SERVER_HEALTH_MAX_AGE_SECONDS", "900")))
    fresh_after = (now - timedelta(seconds=max_age)).astimezone(UTC).isoformat()
    servers = _ALLOCATION.eligible_servers(connection, fresh_after)
    has_tier_allocations = _ALLOCATION.has_tier_allocations(connection, tier_code)
    candidates = collect_candidates(
        self,
        _ALLOCATION,
        connection,
        servers=servers,
        tier_code=tier_code,
        quota_bytes=quota_bytes,
        now=now,
        max_age=max_age,
        has_tier_allocations=has_tier_allocations,
    )
    if not candidates:
        raise OutlineError("No healthy VPN server currently has capacity for this tier")
    selected = min(candidates)
    if self._table_exists(connection, "route_decisions"):
        record_route_decision(
            _ALLOCATION,
            connection,
            selected=selected,
            telegram_id=telegram_id,
            tier_code=tier_code,
            now=now,
        )
    return selected[3]
