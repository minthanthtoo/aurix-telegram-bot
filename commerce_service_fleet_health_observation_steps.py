"""State transition and observation persistence for endpoint health probes."""

from __future__ import annotations

from typing import Any

from commerce_models import _new_id


def apply_health_transition(
    repository: Any,
    service: Any,
    connection: Any,
    *,
    server_id: str,
    observed_at: str,
    observed_status: str,
    latency_ms: float | None,
    remote_key_count: int | None,
    error_type: str | None,
    current: Any,
    previous: str,
) -> dict[str, Any]:
    """Apply hysteresis, update the server, and record the probe observation."""
    success_streak = int(current["health_success_streak"] or 0)
    failure_streak = int(current["health_failure_streak"] or 0)
    recovery_threshold = service._health_threshold(
        "AURIX_ENDPOINT_RECOVERY_THRESHOLD", 2
    )
    failure_threshold = service._health_threshold(
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
        state_after = "unreachable" if failure_streak >= failure_threshold else "degraded"
    changed_at = (
        observed_at if state_after != previous else current["health_state_changed_at"]
    )
    last_error = (
        None
        if observed_status == "healthy" and state_after == "healthy"
        else (str(error_type or current["last_error"] or "")[:128] or None)
    )
    repository.update_health(
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
    if service._table_exists(connection, "endpoint_health_observations"):
        repository.record_observation(
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
