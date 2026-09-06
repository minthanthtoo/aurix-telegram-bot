"""Endpoint lifecycle state and health observation workflow."""

from __future__ import annotations

from typing import Any

from commerce_fleet_health_repository import FleetHealthRepository
from commerce_models import CommerceError
from commerce_service_fleet_lifecycle_steps import (
    assert_retirement_ready,
    persist_lifecycle_change,
    validate_lifecycle_request,
)
from commerce_service_fleet_health_observation_steps import apply_health_transition


_FLEET_HEALTH = FleetHealthRepository()


def set_server_lifecycle(
    self,
    server_id: str,
    lifecycle_state: str,
    actor_id: int,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Change an endpoint's admission lifecycle without touching its VM.

    ``draining`` stops new assignments while existing credentials continue
    to work. ``retired`` is only accepted after local entitlements,
    reservations, pending intents, and the last authoritative remote
    inventory are empty. Provider deletion remains a separate, explicit
    operation outside this method.
    """
    state, server, now_text, clean_reason = validate_lifecycle_request(
        lifecycle_state, server_id, reason
    )
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        row = _FLEET_HEALTH.server(connection, server)
        if row is None:
            raise CommerceError("Outline server is not configured")
        previous = str(
            row.get("lifecycle_state")
            if hasattr(row, "get")
            else row["lifecycle_state"] or "active"
        )
        if previous == state:
            return {
                "server_id": server,
                "lifecycle_state": state,
                "previous_state": previous,
                "changed": False,
            }
        if state == "retired":
            assert_retirement_ready(_FLEET_HEALTH, self, connection, row, server)
        persist_lifecycle_change(
            _FLEET_HEALTH,
            self,
            connection,
            row=row,
            server=server,
            state=state,
            previous=previous,
            actor_id=actor_id,
            clean_reason=clean_reason,
            now_text=now_text,
        )
    return {
        "server_id": server,
        "lifecycle_state": state,
        "previous_state": previous,
        "changed": True,
        "changed_at": now_text,
        "reason": clean_reason,
    }


def _record_endpoint_health(
    self,
    connection: Any,
    server_id: str,
    observed_at: str,
    *,
    observed_status: str,
    latency_ms: float | None,
    remote_key_count: int | None = None,
    error_type: str | None = None,
) -> dict[str, Any]:
    """Persist one health probe and apply conservative state hysteresis.

    A single failed management call blocks new admission immediately by
    moving a healthy node to ``degraded``. Repeated failures make the
    state ``unreachable``; recovery needs independent successful probes.
    The unique timestamp makes repeated maintenance/UI calls idempotent.
    """
    if observed_status not in {"healthy", "unreachable"}:
        raise CommerceError("Invalid endpoint health observation")
    current = _FLEET_HEALTH.health_state_for_update(connection, server_id)
    if current is None:
        raise CommerceError(f"Unknown Outline server: {server_id}")
    previous = str(current["health_status"] or "unknown")
    if self._table_exists(connection, "endpoint_health_observations"):
        duplicate = _FLEET_HEALTH.duplicate_observation(
            connection, server_id, observed_at
        )
        if duplicate is not None:
            return {
                "state": str(duplicate["state_after"]),
                "success_streak": int(current["health_success_streak"] or 0),
                "failure_streak": int(current["health_failure_streak"] or 0),
                "duplicate": True,
            }
    return apply_health_transition(
        _FLEET_HEALTH,
        self,
        connection,
        server_id=server_id,
        observed_at=observed_at,
        observed_status=observed_status,
        latency_ms=latency_ms,
        remote_key_count=remote_key_count,
        error_type=error_type,
        current=current,
        previous=previous,
    )
