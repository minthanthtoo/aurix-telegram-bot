"""Durable, conservative route-failover state machine.

This module owns intent and state transitions only.  Provider mutations happen
in the commerce worker after a decision has been claimed, the target adapter
has been capability-checked, and a target credential has passed verification.
That separation keeps retries idempotent and prevents a client or probe result
from directly creating infrastructure or revoking a working manual export.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from commerce_models import _new_id, _now_text
from route_failover_repository import RouteFailoverRepository


UTC = timezone.utc


class FailoverError(RuntimeError):
    """A failover observation or state transition is invalid."""


def _parse_time(value: str) -> datetime:
    timestamp = datetime.fromisoformat(str(value))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


class RouteFailoverService:
    """Plan and lease safe route changes without performing provider I/O."""

    def __init__(self, database: Any):
        self.database = database
        self.repository = RouteFailoverRepository()

    @staticmethod
    def _table_exists(connection: Any, name: str) -> bool:
        return RouteFailoverRepository.table_exists(connection, name)

    def configure_policy(
        self,
        entitlement_id: str,
        *,
        enabled: bool = False,
        failure_threshold: int = 3,
        recovery_threshold: int = 2,
        cooldown_seconds: int = 300,
        standby_lease_bytes: int = 5 * 1024 * 1024,
        max_attempts: int = 5,
        now: str | None = None,
    ) -> dict[str, Any]:
        if not 2 <= int(failure_threshold) <= 10:
            raise FailoverError("failure threshold is outside the allowed range")
        if not 1 <= int(recovery_threshold) <= 10:
            raise FailoverError("recovery threshold is outside the allowed range")
        if not 30 <= int(cooldown_seconds) <= 86_400:
            raise FailoverError("cooldown is outside the allowed range")
        if not 1 <= int(standby_lease_bytes) <= 10 * 1024 * 1024 * 1024:
            raise FailoverError("standby lease is outside the allowed range")
        if not 1 <= int(max_attempts) <= 8:
            raise FailoverError("maximum failover attempts is outside the allowed range")
        timestamp = str(now or _now_text())
        policy_id = f"failover-policy-{_new_id()}"
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            if not self._table_exists(connection, "route_failover_policies"):
                raise FailoverError("failover persistence is not initialized")
            if not self.repository.entitlement_exists(connection, str(entitlement_id)):
                raise FailoverError("entitlement does not exist")
            row = self.repository.upsert_policy(
                connection,
                policy_id=policy_id,
                entitlement_id=str(entitlement_id),
                enabled=bool(enabled),
                failure_threshold=int(failure_threshold),
                recovery_threshold=int(recovery_threshold),
                cooldown_seconds=int(cooldown_seconds),
                standby_lease_bytes=int(standby_lease_bytes),
                max_attempts=int(max_attempts),
                timestamp=timestamp,
            )
        return dict(row) if row is not None else {}

    def observe(
        self,
        generation_id: str,
        *,
        outcome: str,
        network_bucket: str | None = None,
        latency_ms: int | None = None,
        reason: str | None = None,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        normalized_outcome = str(outcome or "").strip().lower()
        if normalized_outcome not in {"success", "failure"}:
            raise FailoverError("route outcome must be success or failure")
        if latency_ms is not None and not 0 <= int(latency_ms) <= 120_000:
            raise FailoverError("route latency is outside the allowed range")
        bucket = str(network_bucket or "default").strip()
        if not bucket or len(bucket) > 128:
            raise FailoverError("network bucket is invalid")
        timestamp = str(observed_at or _now_text())
        current = _parse_time(timestamp)
        if current > datetime.now(UTC) + timedelta(minutes=5):
            raise FailoverError("route observation cannot be far in the future")
        safe_reason = str(reason or "").strip()[:256] or None
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = self.repository.generation_context(connection, str(generation_id))
            if row is None:
                raise FailoverError("generation does not exist")
            if str(row["entitlement_status"]) != "active":
                raise FailoverError("entitlement is not active")
            if not row["route_id"]:
                raise FailoverError("generation is not attached to a service route")
            inserted = self.repository.insert_observation(
                connection,
                observation_id=_new_id(),
                generation_id=str(generation_id),
                entitlement_id=str(row["entitlement_id"]),
                route_id=str(row["route_id"]),
                network_bucket=bucket,
                outcome=normalized_outcome,
                latency_ms=None if latency_ms is None else int(latency_ms),
                reason=safe_reason,
                timestamp=timestamp,
            )
            if int(getattr(inserted, "rowcount", 0) or 0) != 1:
                existing = self.repository.state(connection, str(generation_id))
                return {
                    "generation_id": str(generation_id),
                    "duplicate": True,
                    "failure_streak": int(existing["failure_streak"] or 0) if existing else 0,
                    "success_streak": int(existing["success_streak"] or 0) if existing else 0,
                    "decision_id": None,
                }
            state = self.repository.state(connection, str(generation_id))
            failure_streak = int(state["failure_streak"] or 0) if state else 0
            success_streak = int(state["success_streak"] or 0) if state else 0
            cooldown_until = str(state["cooldown_until"] or "") if state else ""
            if normalized_outcome == "failure":
                failure_streak += 1
                success_streak = 0
            else:
                success_streak += 1
                failure_streak = 0
            self.repository.upsert_state(
                connection,
                generation_id=str(generation_id),
                failure_streak=failure_streak,
                success_streak=success_streak,
                outcome=normalized_outcome,
                timestamp=timestamp,
                cooldown_until=cooldown_until or None,
            )
            policy = self.repository.policy(connection, str(row["entitlement_id"]))
            decision_id: str | None = None
            target: Any = None
            if (
                normalized_outcome == "failure"
                and policy is not None
                and bool(policy["enabled"])
                and failure_streak >= int(policy["failure_threshold"])
                and (not cooldown_until or _parse_time(cooldown_until) <= current)
            ):
                target = self.repository.target_route(connection, str(row["route_id"]))
                if target is not None:
                    idempotency_key = (
                        f"failover:{row['entitlement_id']}:{generation_id}:"
                        f"{target['route_id']}:{bucket}"
                    )
                    decision_id = f"failover-{_new_id()}"
                    inserted_decision = self.repository.insert_decision(
                        connection,
                        decision_id=decision_id,
                        idempotency_key=idempotency_key,
                        entitlement_id=str(row["entitlement_id"]),
                        generation_id=str(generation_id),
                        source_endpoint_id=str(row["endpoint_id"]),
                        source_route_id=str(row["route_id"]),
                        target_endpoint_id=str(target["endpoint_id"]),
                        target_route_id=str(target["route_id"]),
                        trigger=safe_reason or "route_failure_threshold",
                        network_bucket=bucket,
                        timestamp=timestamp,
                    )
                    if int(getattr(inserted_decision, "rowcount", 0) or 0) != 1:
                        existing = self.repository.decision_by_key(connection, idempotency_key)
                        decision_id = str(existing["decision_id"]) if existing else None
                    else:
                        cooldown = current + timedelta(seconds=int(policy["cooldown_seconds"]))
                        self.repository.set_cooldown(
                            connection, str(generation_id), cooldown.isoformat(), timestamp
                        )
            return {
                "generation_id": str(generation_id),
                "duplicate": False,
                "failure_streak": failure_streak,
                "success_streak": success_streak,
                "decision_id": decision_id,
                "target_route_id": str(target["route_id"]) if target is not None else None,
            }

    def claim(self, *, now: str | None = None) -> dict[str, Any] | None:
        timestamp = str(now or _now_text())
        stale_before = _parse_time(timestamp) - timedelta(minutes=10)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self.repository.reset_stale(connection, timestamp, stale_before.isoformat())
            row = self.repository.claim_candidate(connection, timestamp)
            if row is None:
                return None
            updated = self.repository.claim_update(
                connection, str(row["decision_id"]), timestamp
            )
            if int(getattr(updated, "rowcount", 0) or 0) != 1:
                return None
            result = dict(row)
            result["state"] = "creating"
            result["attempts"] = int(row["attempts"] or 0) + 1
            return result

    def mark_failed(self, decision_id: str, error: Exception, *, now: str | None = None) -> None:
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = self.repository.failure_context(connection, str(decision_id))
            attempts = int(row["attempts"] or 0) if row else 0
            max_attempts = int(row["max_attempts"] or 1) if row else 1
            terminal = attempts >= max_attempts
            self.repository.mark_failed(
                connection,
                state="failed" if terminal else "pending",
                next_attempt_at=(
                    "9999-12-31T00:00:00+00:00"
                    if terminal
                    else _now_text(_parse_time(timestamp) + timedelta(minutes=1))
                ),
                error=f"{type(error).__name__}: {str(error)[:500]}",
                timestamp=timestamp,
                decision_id=str(decision_id),
            )

    def mark_committed(self, decision_id: str, *, now: str | None = None) -> None:
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self.repository.mark_committed(
                connection, str(decision_id), timestamp
            )

    def mark_verified(self, decision_id: str, *, now: str | None = None) -> None:
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self.repository.mark_verified(
                connection, str(decision_id), timestamp
            )

    def mark_rolled_back(self, decision_id: str, error: Exception, *, now: str | None = None) -> None:
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self.repository.mark_rolled_back(
                connection,
                error=f"{type(error).__name__}: {str(error)[:500]}",
                timestamp=timestamp,
                decision_id=str(decision_id),
            )

    def decisions(self, *, entitlement_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = self.repository.decisions(
                connection, entitlement_id, max(1, min(200, int(limit)))
            )
        return [dict(row) for row in rows]
