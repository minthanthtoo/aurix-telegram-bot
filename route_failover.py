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
from commerce_repositories import _PostgresConnection


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

    @staticmethod
    def _table_exists(connection: Any, name: str) -> bool:
        if isinstance(connection, _PostgresConnection):
            row = connection.execute(
                "SELECT to_regclass(?) AS table_name", (f"public.{name}",)
            ).fetchone()
            return bool(row and row["table_name"])
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone() is not None

    @staticmethod
    def _true(connection: Any) -> str:
        return "TRUE" if isinstance(connection, _PostgresConnection) else "1"

    @staticmethod
    def _false(connection: Any) -> str:
        return "FALSE" if isinstance(connection, _PostgresConnection) else "0"

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
            if connection.execute(
                "SELECT 1 FROM entitlements WHERE entitlement_id = ?", (str(entitlement_id),)
            ).fetchone() is None:
                raise FailoverError("entitlement does not exist")
            connection.execute(
                """INSERT INTO route_failover_policies
                   (policy_id, entitlement_id, enabled, failure_threshold,
                    recovery_threshold, cooldown_seconds, standby_lease_bytes,
                    max_attempts, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entitlement_id) DO UPDATE SET
                     enabled = excluded.enabled,
                     failure_threshold = excluded.failure_threshold,
                     recovery_threshold = excluded.recovery_threshold,
                     cooldown_seconds = excluded.cooldown_seconds,
                     standby_lease_bytes = excluded.standby_lease_bytes,
                     max_attempts = excluded.max_attempts,
                     updated_at = excluded.updated_at""",
                (
                    policy_id,
                    str(entitlement_id),
                    bool(enabled),
                    int(failure_threshold),
                    int(recovery_threshold),
                    int(cooldown_seconds),
                    int(standby_lease_bytes),
                    int(max_attempts),
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM route_failover_policies WHERE entitlement_id = ?",
                (str(entitlement_id),),
            ).fetchone()
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
            lock_clause = " FOR UPDATE" if isinstance(connection, _PostgresConnection) else ""
            row = connection.execute(
                f"""SELECT g.generation_id, g.entitlement_id,
                              COALESCE(g.route_id, r0.route_id) AS route_id,
                              g.endpoint_id, e.status AS endpoint_status,
                              en.status AS entitlement_status
                         FROM credential_generations g
                         JOIN entitlements en ON en.entitlement_id = g.entitlement_id
                         JOIN connectivity_endpoints e ON e.endpoint_id = g.endpoint_id
                         LEFT JOIN connectivity_routes r0 ON r0.endpoint_id = g.endpoint_id
                                                        AND r0.route_name = 'primary'
                        WHERE g.generation_id = ?{lock_clause}""",
                (str(generation_id),),
            ).fetchone()
            if row is None:
                raise FailoverError("generation does not exist")
            if str(row["entitlement_status"]) != "active":
                raise FailoverError("entitlement is not active")
            if not row["route_id"]:
                raise FailoverError("generation is not attached to a service route")
            inserted = connection.execute(
                """INSERT INTO route_observations
                   (observation_id, generation_id, entitlement_id, route_id,
                    network_bucket, outcome, latency_ms, reason, observed_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(generation_id, route_id, network_bucket, outcome, observed_at)
                   DO NOTHING""",
                (
                    _new_id(),
                    str(generation_id),
                    str(row["entitlement_id"]),
                    str(row["route_id"]),
                    bucket,
                    normalized_outcome,
                    None if latency_ms is None else int(latency_ms),
                    safe_reason,
                    timestamp,
                    timestamp,
                ),
            )
            if int(getattr(inserted, "rowcount", 0) or 0) != 1:
                existing = connection.execute(
                    """SELECT failure_streak, success_streak, cooldown_until
                         FROM route_failover_state WHERE generation_id = ?""",
                    (str(generation_id),),
                ).fetchone()
                return {
                    "generation_id": str(generation_id),
                    "duplicate": True,
                    "failure_streak": int(existing["failure_streak"] or 0) if existing else 0,
                    "success_streak": int(existing["success_streak"] or 0) if existing else 0,
                    "decision_id": None,
                }
            state = connection.execute(
                "SELECT * FROM route_failover_state WHERE generation_id = ?",
                (str(generation_id),),
            ).fetchone()
            failure_streak = int(state["failure_streak"] or 0) if state else 0
            success_streak = int(state["success_streak"] or 0) if state else 0
            cooldown_until = str(state["cooldown_until"] or "") if state else ""
            if normalized_outcome == "failure":
                failure_streak += 1
                success_streak = 0
            else:
                success_streak += 1
                failure_streak = 0
            connection.execute(
                """INSERT INTO route_failover_state
                   (generation_id, failure_streak, success_streak, last_outcome,
                    last_observed_at, cooldown_until, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(generation_id) DO UPDATE SET
                     failure_streak = excluded.failure_streak,
                     success_streak = excluded.success_streak,
                     last_outcome = excluded.last_outcome,
                     last_observed_at = excluded.last_observed_at,
                     updated_at = excluded.updated_at""",
                (
                    str(generation_id),
                    failure_streak,
                    success_streak,
                    normalized_outcome,
                    timestamp,
                    cooldown_until or None,
                    timestamp,
                ),
            )
            policy = connection.execute(
                "SELECT * FROM route_failover_policies WHERE entitlement_id = ?",
                (str(row["entitlement_id"]),),
            ).fetchone()
            decision_id: str | None = None
            target: Any = None
            if (
                normalized_outcome == "failure"
                and policy is not None
                and bool(policy["enabled"])
                and failure_streak >= int(policy["failure_threshold"])
                and (not cooldown_until or _parse_time(cooldown_until) <= current)
            ):
                true = self._true(connection)
                target = connection.execute(
                    f"""SELECT r.route_id, r.endpoint_id, r.protocol, r.priority
                           FROM connectivity_routes r
                           JOIN connectivity_endpoints e ON e.endpoint_id = r.endpoint_id
                          WHERE r.route_id <> ?
                            AND r.protocol = (SELECT protocol FROM connectivity_routes WHERE route_id = ?)
                            AND r.status = 'active'
                            AND e.status IN ('active', 'degraded')
                            AND e.accepts_new_keys = {true}
                            AND r.supports_managed_config = {true}
                            AND r.supports_quota_cap = {true}
                            AND r.supports_usage = {true}
                          ORDER BY r.priority ASC, e.updated_at DESC
                          LIMIT 1""",
                    (str(row["route_id"]), str(row["route_id"])),
                ).fetchone()
                if target is not None:
                    idempotency_key = (
                        f"failover:{row['entitlement_id']}:{generation_id}:"
                        f"{target['route_id']}:{bucket}"
                    )
                    decision_id = f"failover-{_new_id()}"
                    inserted_decision = connection.execute(
                        """INSERT INTO failover_decisions
                           (decision_id, idempotency_key, entitlement_id,
                            source_generation_id, source_endpoint_id, source_route_id,
                            target_endpoint_id, target_route_id, trigger, network_bucket,
                            state, attempts, next_attempt_at, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                           ON CONFLICT(idempotency_key) DO NOTHING""",
                        (
                            decision_id,
                            idempotency_key,
                            str(row["entitlement_id"]),
                            str(generation_id),
                            str(row["endpoint_id"]),
                            str(row["route_id"]),
                            str(target["endpoint_id"]),
                            str(target["route_id"]),
                            safe_reason or "route_failure_threshold",
                            bucket,
                            timestamp,
                            timestamp,
                            timestamp,
                        ),
                    )
                    if int(getattr(inserted_decision, "rowcount", 0) or 0) != 1:
                        existing = connection.execute(
                            "SELECT decision_id FROM failover_decisions WHERE idempotency_key = ?",
                            (idempotency_key,),
                        ).fetchone()
                        decision_id = str(existing["decision_id"]) if existing else None
                    else:
                        cooldown = current + timedelta(seconds=int(policy["cooldown_seconds"]))
                        connection.execute(
                            "UPDATE route_failover_state SET cooldown_until = ?, updated_at = ? WHERE generation_id = ?",
                            (cooldown.isoformat(), timestamp, str(generation_id)),
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
            connection.execute(
                """UPDATE failover_decisions
                      SET state = 'pending', locked_at = NULL, updated_at = ?
                    WHERE state = 'creating' AND locked_at < ?""",
                (timestamp, stale_before.isoformat()),
            )
            lock_clause = " FOR UPDATE SKIP LOCKED" if isinstance(connection, _PostgresConnection) else ""
            row = connection.execute(
                f"""SELECT d.*, p.max_attempts
                       FROM failover_decisions d
                       JOIN route_failover_policies p ON p.entitlement_id = d.entitlement_id
                      WHERE d.state IN ('pending', 'failed', 'verified')
                        AND d.next_attempt_at <= ?
                        AND d.attempts < p.max_attempts
                      ORDER BY d.created_at
                      LIMIT 1{lock_clause}""",
                (timestamp,),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """UPDATE failover_decisions
                      SET state = 'creating', attempts = attempts + 1,
                          locked_at = ?, updated_at = ?
                    WHERE decision_id = ? AND state IN ('pending', 'failed', 'verified')""",
                (timestamp, timestamp, str(row["decision_id"])),
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
            row = connection.execute(
                """SELECT d.attempts, p.max_attempts
                     FROM failover_decisions d JOIN route_failover_policies p
                       ON p.entitlement_id = d.entitlement_id
                    WHERE d.decision_id = ?""",
                (str(decision_id),),
            ).fetchone()
            attempts = int(row["attempts"] or 0) if row else 0
            max_attempts = int(row["max_attempts"] or 1) if row else 1
            terminal = attempts >= max_attempts
            connection.execute(
                """UPDATE failover_decisions
                      SET state = ?, next_attempt_at = ?, locked_at = NULL,
                          last_error = ?, updated_at = ?
                    WHERE decision_id = ?""",
                (
                    "failed" if terminal else "pending",
                    "9999-12-31T00:00:00+00:00"
                    if terminal
                    else _now_text(_parse_time(timestamp) + timedelta(minutes=1)),
                    f"{type(error).__name__}: {str(error)[:500]}",
                    timestamp,
                    str(decision_id),
                ),
            )

    def mark_committed(self, decision_id: str, *, now: str | None = None) -> None:
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE failover_decisions
                      SET state = 'committed', locked_at = NULL,
                          last_error = NULL, completed_at = ?, updated_at = ?
                    WHERE decision_id = ? AND state IN ('creating', 'verified')""",
                (timestamp, timestamp, str(decision_id)),
            )

    def mark_verified(self, decision_id: str, *, now: str | None = None) -> None:
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE failover_decisions
                      SET state = 'verified', locked_at = NULL,
                          last_error = NULL, updated_at = ?
                    WHERE decision_id = ? AND state = 'creating'""",
                (timestamp, str(decision_id)),
            )

    def mark_rolled_back(self, decision_id: str, error: Exception, *, now: str | None = None) -> None:
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE failover_decisions
                      SET state = 'rolled_back', locked_at = NULL,
                          last_error = ?, completed_at = ?, updated_at = ?
                    WHERE decision_id = ?""",
                (f"{type(error).__name__}: {str(error)[:500]}", timestamp, timestamp, str(decision_id)),
            )

    def decisions(self, *, entitlement_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM failover_decisions
                    WHERE (? IS NULL OR entitlement_id = ?)
                    ORDER BY created_at DESC LIMIT ?""",
                (entitlement_id, entitlement_id, max(1, min(200, int(limit)))),
            ).fetchall()
        return [dict(row) for row in rows]
