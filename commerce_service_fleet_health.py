"""Endpoint lifecycle state and health observation workflow."""

from __future__ import annotations

from typing import Any

from commerce_fleet_health_repository import FleetHealthRepository
from commerce_models import CommerceError, _new_id, _now_text
from connectivity_registry import ConnectivityRegistry
from lifecycle_policy import normalize_lifecycle_state


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
    try:
        state = normalize_lifecycle_state(lifecycle_state, strict=True)
    except ValueError as exc:
        raise CommerceError("Endpoint lifecycle must be active, draining or retired") from exc
    server = str(server_id or "").strip()
    if not server:
        raise CommerceError("Endpoint identity is required")
    now_text = _now_text()
    clean_reason = str(reason or "").strip()[:512] or None
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        row = _FLEET_HEALTH.server(connection, server)
        if row is None:
            raise CommerceError("Outline server is not configured")
        previous = str(row.get("lifecycle_state") if hasattr(row, "get") else row["lifecycle_state"] or "active")
        if previous == state:
            return {
                "server_id": server,
                "lifecycle_state": state,
                "previous_state": previous,
                "changed": False,
            }
        if state == "retired":
            blockers: list[str] = []
            counts = _FLEET_HEALTH.retirement_counts(
                connection,
                server,
                include_free_keys=self._table_exists(connection, "keys"),
                include_free_intents=self._table_exists(
                    connection, "free_provisioning_intents"
                ),
            )
            active_free = counts["active_free"]
            active_paid = counts["active_paid"]
            pending_orders = counts["pending_orders"]
            pending_subscriptions = counts["pending_subscriptions"]
            pending_intents = counts["pending_intents"]
            if active_free:
                blockers.append(f"{active_free} active free/promo key(s)")
            if active_paid:
                blockers.append(f"{active_paid} active paid key(s)")
            if pending_orders:
                blockers.append(f"{pending_orders} open order(s)")
            if pending_subscriptions:
                blockers.append(f"{pending_subscriptions} active/pending subscription(s)")
            if pending_intents:
                blockers.append(f"{pending_intents} pending provisioning intent(s)")
            remote_count = row["remote_key_count"]
            orphan_count = int(row["remote_orphan_key_count"] or 0)
            if remote_count is None:
                blockers.append("remote inventory has not been reconciled")
            elif int(remote_count or 0) != 0:
                blockers.append(f"remote inventory still has {int(remote_count)} key(s)")
            if orphan_count:
                blockers.append(f"{orphan_count} unreviewed remote key(s)")
            if blockers:
                raise CommerceError("Endpoint cannot be retired yet: " + "; ".join(blockers))
        enabled = 1 if state in {"active", "draining"} else 0
        _FLEET_HEALTH.set_lifecycle(
            connection,
            server_id=server,
            enabled=bool(enabled),
            state=state,
            reason=clean_reason,
            now_text=now_text,
        )
        ConnectivityRegistry.sync_outline_health(
            connection,
            server_id=server,
            lifecycle_state=state,
            health_status=str(row["health_status"] or "unknown"),
            now_text=now_text,
        )
        self._audit(
            connection,
            "server_lifecycle_changed",
            "outline_server",
            server,
            "owner",
            str(actor_id),
            {
                "previous_state": previous,
                "lifecycle_state": state,
                "reason": clean_reason,
            },
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
    success_streak = int(current["health_success_streak"] or 0)
    failure_streak = int(current["health_failure_streak"] or 0)
    recovery_threshold = self._health_threshold(
        "AURIX_ENDPOINT_RECOVERY_THRESHOLD", 2
    )
    failure_threshold = self._health_threshold(
        "AURIX_ENDPOINT_FAILURE_THRESHOLD", 3
    )
    if observed_status == "healthy":
        success_streak += 1
        failure_streak = 0
        state_after = (
            "healthy"
            if previous in {"unknown", "healthy"}
            or success_streak >= recovery_threshold
            else previous
        )
    else:
        failure_streak += 1
        success_streak = 0
        state_after = (
            "unreachable"
            if failure_streak >= failure_threshold
            else "degraded"
        )
    changed_at = (
        observed_at
        if state_after != previous
        else current["health_state_changed_at"]
    )
    last_error = (
        None
        if observed_status == "healthy" and state_after == "healthy"
        else (str(error_type or current["last_error"] or "")[:128] or None)
    )
    _FLEET_HEALTH.update_health(
        connection,
        server_id=server_id,
        state=state_after,
        success_streak=success_streak,
        failure_streak=failure_streak,
        changed_at=changed_at,
        latency_ms=latency_ms,
        last_error=last_error,
        observed_at=observed_at,
    )
    if self._table_exists(connection, "endpoint_health_observations"):
        _FLEET_HEALTH.record_observation(
            connection,
            observation_id=_new_id(),
            server_id=server_id,
            observed_at=observed_at,
            observed_status=observed_status,
            state_before=previous,
            state_after=state_after,
            latency_ms=latency_ms,
            remote_key_count=remote_key_count,
            error_type=str(error_type or "")[:128] or None,
        )
    return {
        "state": state_after,
        "success_streak": success_streak,
        "failure_streak": failure_streak,
        "duplicate": False,
    }
