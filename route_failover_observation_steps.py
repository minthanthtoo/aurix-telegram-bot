"""Validation and persistence phases for route-failure observations."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from commerce_models import _new_id


def validate_observation(
    outcome: str,
    *,
    network_bucket: str | None,
    latency_ms: int | None,
    reason: str | None,
    observed_at: str | None,
    now_text: Callable[[datetime], str],
    parse_time: Callable[[str], datetime],
    now: datetime,
) -> tuple[str, str, int | None, str | None, str, datetime]:
    """Normalize and validate one externally observed route result."""
    normalized_outcome = str(outcome or "").strip().lower()
    if normalized_outcome not in {"success", "failure"}:
        raise ValueError("route outcome must be success or failure")
    if latency_ms is not None and not 0 <= int(latency_ms) <= 120_000:
        raise ValueError("route latency is outside the allowed range")
    bucket = str(network_bucket or "default").strip()
    if not bucket or len(bucket) > 128:
        raise ValueError("network bucket is invalid")
    timestamp = str(observed_at or now_text(now))
    current = parse_time(timestamp)
    if current > now + timedelta(minutes=5):
        raise ValueError("route observation cannot be far in the future")
    return (
        normalized_outcome,
        bucket,
        None if latency_ms is None else int(latency_ms),
        str(reason or "").strip()[:256] or None,
        timestamp,
        current,
    )


def persist_observation(
    repository: Any,
    connection: Any,
    *,
    generation_id: str,
    row: Any,
    normalized_outcome: str,
    bucket: str,
    latency_ms: int | None,
    safe_reason: str | None,
    timestamp: str,
    current: datetime,
    parse_time: Callable[[str], datetime],
) -> dict[str, Any]:
    """Insert the sample, update streaks, and create an idempotent decision."""
    inserted = repository.insert_observation(
        connection,
        observation_id=_new_id(),
        generation_id=generation_id,
        entitlement_id=str(row["entitlement_id"]),
        route_id=str(row["route_id"]),
        network_bucket=bucket,
        outcome=normalized_outcome,
        latency_ms=latency_ms,
        reason=safe_reason,
        timestamp=timestamp,
    )
    if int(getattr(inserted, "rowcount", 0) or 0) != 1:
        existing = repository.state(connection, generation_id)
        return {
            "generation_id": generation_id,
            "duplicate": True,
            "failure_streak": int(existing["failure_streak"] or 0) if existing else 0,
            "success_streak": int(existing["success_streak"] or 0) if existing else 0,
            "decision_id": None,
        }
    state = repository.state(connection, generation_id)
    failure_streak = int(state["failure_streak"] or 0) if state else 0
    success_streak = int(state["success_streak"] or 0) if state else 0
    cooldown_until = str(state["cooldown_until"] or "") if state else ""
    if normalized_outcome == "failure":
        failure_streak += 1
        success_streak = 0
    else:
        success_streak += 1
        failure_streak = 0
    repository.upsert_state(
        connection,
        generation_id=generation_id,
        failure_streak=failure_streak,
        success_streak=success_streak,
        outcome=normalized_outcome,
        timestamp=timestamp,
        cooldown_until=cooldown_until or None,
    )
    decision_id, target_route_id = create_decision_if_needed(
        repository,
        connection,
        row=row,
        generation_id=generation_id,
        normalized_outcome=normalized_outcome,
        failure_streak=failure_streak,
        policy=repository.policy(connection, str(row["entitlement_id"])),
        cooldown_until=cooldown_until,
        current=current,
        bucket=bucket,
        safe_reason=safe_reason,
        timestamp=timestamp,
        parse_time=parse_time,
    )
    return {
        "generation_id": generation_id,
        "duplicate": False,
        "failure_streak": failure_streak,
        "success_streak": success_streak,
        "decision_id": decision_id,
        "target_route_id": target_route_id,
    }


def create_decision_if_needed(
    repository: Any,
    connection: Any,
    *,
    row: Any,
    generation_id: str,
    normalized_outcome: str,
    failure_streak: int,
    policy: Any,
    cooldown_until: str,
    current: datetime,
    bucket: str,
    safe_reason: str | None,
    timestamp: str,
    parse_time: Callable[[str], datetime],
) -> tuple[str | None, str | None]:
    """Create one threshold decision and its cooldown, if policy allows."""
    if not (
        normalized_outcome == "failure"
        and policy is not None
        and bool(policy["enabled"])
        and failure_streak >= int(policy["failure_threshold"])
        and (not cooldown_until or parse_time(cooldown_until) <= current)
    ):
        return None, None
    target = repository.target_route(connection, str(row["route_id"]))
    if target is None:
        return None, None
    idempotency_key = (
        f"failover:{row['entitlement_id']}:{generation_id}:"
        f"{target['route_id']}:{bucket}"
    )
    decision_id = f"failover-{_new_id()}"
    inserted_decision = repository.insert_decision(
        connection,
        decision_id=decision_id,
        idempotency_key=idempotency_key,
        entitlement_id=str(row["entitlement_id"]),
        generation_id=generation_id,
        source_endpoint_id=str(row["endpoint_id"]),
        source_route_id=str(row["route_id"]),
        target_endpoint_id=str(target["endpoint_id"]),
        target_route_id=str(target["route_id"]),
        trigger=safe_reason or "route_failure_threshold",
        network_bucket=bucket,
        timestamp=timestamp,
    )
    if int(getattr(inserted_decision, "rowcount", 0) or 0) != 1:
        existing = repository.decision_by_key(connection, idempotency_key)
        return (str(existing["decision_id"]) if existing else None), str(target["route_id"])
    cooldown = current + timedelta(seconds=int(policy["cooldown_seconds"]))
    repository.set_cooldown(connection, generation_id, cooldown.isoformat(), timestamp)
    return decision_id, str(target["route_id"])
