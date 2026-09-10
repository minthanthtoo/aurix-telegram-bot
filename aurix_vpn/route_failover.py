"""Durable, conservative route-failover state transitions.

This module owns observations and intent records only.  The worker remains the
only place that provisions or revokes remote credentials.  A committed
failover retires the source route for selection purposes but does not revoke or
drop its accounting generation until remote deletion is verified.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .commerce_models import _new_id
from .commerce_repositories import _PostgresConnection


UTC = timezone.utc


class FailoverError(RuntimeError):
    """An observation or state transition violates the failover contract."""


def _time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def _text(value: str | datetime | None = None) -> str:
    return (_time(value) if value is not None else datetime.now(UTC)).isoformat()


class RouteFailoverService:
    def __init__(self, database: Any):
        self.database = database

    def configure_policy(
        self,
        entitlement_key: str,
        *,
        enabled: bool = False,
        failure_threshold: int = 3,
        recovery_threshold: int = 2,
        cooldown_seconds: int = 300,
        standby_lease_bytes: int = 100 * 1024 * 1024,
        max_attempts: int = 5,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        if not str(entitlement_key).strip():
            raise FailoverError("entitlement key is required")
        if not 1 <= int(failure_threshold) <= 100:
            raise FailoverError("failure threshold is invalid")
        if not 1 <= int(recovery_threshold) <= 100:
            raise FailoverError("recovery threshold is invalid")
        if not 0 <= int(cooldown_seconds) <= 86_400:
            raise FailoverError("cooldown is invalid")
        if not 1 <= int(standby_lease_bytes) <= 10 * 1024 * 1024 * 1024:
            raise FailoverError("standby lease size is invalid")
        if not 1 <= int(max_attempts) <= 20:
            raise FailoverError("maximum attempts is invalid")
        timestamp = _text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """INSERT INTO route_failover_policies
                   (entitlement_key, enabled, failure_threshold, recovery_threshold,
                    cooldown_seconds, standby_lease_bytes, max_attempts, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entitlement_key) DO UPDATE SET
                     enabled = excluded.enabled,
                     failure_threshold = excluded.failure_threshold,
                     recovery_threshold = excluded.recovery_threshold,
                     cooldown_seconds = excluded.cooldown_seconds,
                     standby_lease_bytes = excluded.standby_lease_bytes,
                     max_attempts = excluded.max_attempts,
                     updated_at = excluded.updated_at""",
                (str(entitlement_key), bool(enabled), int(failure_threshold), int(recovery_threshold),
                 int(cooldown_seconds), int(standby_lease_bytes), int(max_attempts), timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM route_failover_policies WHERE entitlement_key = ?", (str(entitlement_key),)
            ).fetchone()
        return dict(row) if row is not None else {}

    @staticmethod
    def _target_endpoint(connection: Any, source_endpoint_id: str) -> dict[str, Any] | None:
        true = "TRUE" if isinstance(connection, _PostgresConnection) else "1"
        row = connection.execute(
            f"""SELECT id FROM vpn_endpoints
                   WHERE id <> ? AND UPPER(state) IN ('ACTIVE', 'DEGRADED')
                     AND accepts_new_assignments = {true}
                   ORDER BY code, id LIMIT 1""",
            (str(source_endpoint_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def observe(
        self,
        generation_id: str,
        *,
        outcome: str,
        network_bucket: str = "default",
        latency_ms: int | None = None,
        reason: str | None = None,
        observed_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        normalized = str(outcome or "").strip().lower()
        if normalized not in {"success", "failure"}:
            raise FailoverError("route outcome must be success or failure")
        bucket = str(network_bucket or "default").strip()[:128]
        if not bucket:
            raise FailoverError("network bucket is invalid")
        if latency_ms is not None and not 0 <= int(latency_ms) <= 120_000:
            raise FailoverError("latency is invalid")
        timestamp = _text(observed_at)
        if _time(timestamp) > datetime.now(UTC) + timedelta(minutes=5):
            raise FailoverError("observation cannot be far in the future")
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            lock = " FOR UPDATE" if isinstance(connection, _PostgresConnection) else ""
            generation = connection.execute(
                f"""SELECT generation_id, entitlement_key, endpoint_id, protocol, status
                       FROM credential_generations
                      WHERE generation_id = ?{lock}""",
                (str(generation_id),),
            ).fetchone()
            if generation is None:
                raise FailoverError("generation does not exist")
            if str(generation["status"]) not in {"active", "retiring", "unknown", "pending"}:
                raise FailoverError("generation is not remotely usable")
            inserted = connection.execute(
                """INSERT INTO route_observations
                   (observation_id, generation_id, entitlement_key, endpoint_id,
                    network_bucket, outcome, latency_ms, reason, observed_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(generation_id, endpoint_id, network_bucket, outcome, observed_at)
                   DO NOTHING""",
                (_new_id(), str(generation_id), str(generation["entitlement_key"]), str(generation["endpoint_id"]),
                 bucket, normalized, None if latency_ms is None else int(latency_ms), str(reason or "")[:256] or None,
                 timestamp, timestamp),
            )
            state = connection.execute(
                "SELECT * FROM route_failover_state WHERE generation_id = ?", (str(generation_id),)
            ).fetchone()
            if int(getattr(inserted, "rowcount", 0) or 0) != 1:
                return {
                    "generation_id": str(generation_id),
                    "duplicate": True,
                    "failure_streak": int(state["failure_streak"] or 0) if state else 0,
                    "success_streak": int(state["success_streak"] or 0) if state else 0,
                    "decision_id": None,
                }
            failure_streak = int(state["failure_streak"] or 0) if state else 0
            success_streak = int(state["success_streak"] or 0) if state else 0
            cooldown_until = str(state["cooldown_until"] or "") if state else ""
            if normalized == "failure":
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
                (str(generation_id), failure_streak, success_streak, normalized, timestamp, cooldown_until or None, timestamp),
            )
            policy = connection.execute(
                "SELECT * FROM route_failover_policies WHERE entitlement_key = ?",
                (str(generation["entitlement_key"]),),
            ).fetchone()
            decision_id = None
            target_id = None
            if policy is not None and bool(policy["enabled"]) and normalized == "failure" and failure_streak >= int(policy["failure_threshold"]):
                if not cooldown_until or _time(cooldown_until) <= _time(timestamp):
                    target = self._target_endpoint(connection, str(generation["endpoint_id"]))
                    if target is not None:
                        target_id = str(target["id"])
                        idem = f"failover:{generation['entitlement_key']}:{generation_id}:{target_id}:{bucket}:{failure_streak}"
                        candidate = f"failover-{_new_id()}"
                        inserted_decision = connection.execute(
                            """INSERT INTO failover_decisions
                               (decision_id, idempotency_key, entitlement_key, source_generation_id,
                                source_endpoint_id, target_endpoint_id, trigger, network_bucket,
                                state, next_attempt_at, created_at, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                               ON CONFLICT(idempotency_key) DO NOTHING""",
                            (candidate, idem, str(generation["entitlement_key"]), str(generation_id),
                             str(generation["endpoint_id"]), target_id, str(reason or "route_failure_threshold")[:256],
                             bucket, timestamp, timestamp, timestamp),
                        )
                        if int(getattr(inserted_decision, "rowcount", 0) or 0) == 1:
                            decision_id = candidate
                            cooldown = _time(timestamp) + timedelta(seconds=int(policy["cooldown_seconds"]))
                            connection.execute(
                                "UPDATE route_failover_state SET cooldown_until = ?, updated_at = ? WHERE generation_id = ?",
                                (cooldown.isoformat(), timestamp, str(generation_id)),
                            )
                        else:
                            existing = connection.execute(
                                "SELECT decision_id FROM failover_decisions WHERE idempotency_key = ?", (idem,)
                            ).fetchone()
                            decision_id = str(existing["decision_id"]) if existing else None
            return {
                "generation_id": str(generation_id),
                "duplicate": False,
                "failure_streak": failure_streak,
                "success_streak": success_streak,
                "decision_id": decision_id,
                "target_endpoint_id": target_id,
            }

    def claim(self, *, now: str | datetime | None = None) -> dict[str, Any] | None:
        timestamp = _text(now)
        stale = (_time(timestamp) - timedelta(minutes=10)).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE failover_decisions SET state = 'pending', locked_at = NULL, updated_at = ?
                    WHERE state = 'creating' AND locked_at < ?""",
                (timestamp, stale),
            )
            lock = " FOR UPDATE SKIP LOCKED" if isinstance(connection, _PostgresConnection) else ""
            row = connection.execute(
                """SELECT d.*, p.max_attempts FROM failover_decisions d
                    JOIN route_failover_policies p ON p.entitlement_key = d.entitlement_key
                   WHERE d.state IN ('pending', 'failed', 'verified')
                     AND d.next_attempt_at <= ? AND d.attempts < p.max_attempts
                   ORDER BY d.created_at LIMIT 1""" + lock,
                (timestamp,),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """UPDATE failover_decisions SET state = 'creating', attempts = attempts + 1,
                          locked_at = ?, updated_at = ?
                    WHERE decision_id = ? AND state IN ('pending', 'failed', 'verified')""",
                (timestamp, timestamp, row["decision_id"]),
            )
            if int(getattr(updated, "rowcount", 0) or 0) != 1:
                return None
            result = dict(row)
            result["state"] = "creating"
            result["attempts"] = int(row["attempts"] or 0) + 1
            return result

    def attach_target_generation(self, decision_id: str, generation_id: str, *, now: str | datetime | None = None) -> None:
        timestamp = _text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            decision = connection.execute("SELECT * FROM failover_decisions WHERE decision_id = ?", (decision_id,)).fetchone()
            generation = connection.execute("SELECT * FROM credential_generations WHERE generation_id = ?", (generation_id,)).fetchone()
            if decision is None or generation is None or str(generation["entitlement_key"]) != str(decision["entitlement_key"]):
                raise FailoverError("target generation does not match the decision")
            if str(generation["endpoint_id"]) != str(decision["target_endpoint_id"]):
                raise FailoverError("target generation is on the wrong endpoint")
            connection.execute(
                """UPDATE failover_decisions SET target_generation_id = ?, state = 'verified',
                          locked_at = NULL, updated_at = ?
                    WHERE decision_id = ? AND state = 'creating'""",
                (generation_id, timestamp, decision_id),
            )

    def mark_committed(self, decision_id: str, *, now: str | datetime | None = None) -> None:
        timestamp = _text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            decision = connection.execute("SELECT * FROM failover_decisions WHERE decision_id = ?", (decision_id,)).fetchone()
            if decision is None or str(decision["state"]) not in {"creating", "verified"} or not decision["target_generation_id"]:
                raise FailoverError("failover is not verified for commit")
            connection.execute(
                """UPDATE credential_generations SET status = 'retiring'
                    WHERE generation_id = ? AND status = 'active'""",
                (decision["source_generation_id"],),
            )
            connection.execute(
                """UPDATE failover_decisions SET state = 'committed', locked_at = NULL,
                          last_error = NULL, completed_at = ?, updated_at = ?
                    WHERE decision_id = ?""",
                (timestamp, timestamp, decision_id),
            )

    def mark_failed(self, decision_id: str, error: Exception, *, now: str | datetime | None = None) -> None:
        timestamp = _text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                """SELECT d.attempts, p.max_attempts FROM failover_decisions d
                    JOIN route_failover_policies p ON p.entitlement_key = d.entitlement_key
                   WHERE d.decision_id = ?""",
                (decision_id,),
            ).fetchone()
            attempts = int(row["attempts"] or 0) if row else 0
            maximum = int(row["max_attempts"] or 1) if row else 1
            terminal = attempts >= maximum
            next_attempt = "9999-12-31T00:00:00+00:00" if terminal else (_time(timestamp) + timedelta(minutes=1)).isoformat()
            connection.execute(
                """UPDATE failover_decisions SET state = ?, next_attempt_at = ?, locked_at = NULL,
                          last_error = ?, updated_at = ? WHERE decision_id = ?""",
                ("failed" if terminal else "pending", next_attempt, f"{type(error).__name__}: {str(error)[:500]}", timestamp, decision_id),
            )

    def mark_rolled_back(self, decision_id: str, error: Exception, *, now: str | datetime | None = None) -> None:
        timestamp = _text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE failover_decisions SET state = 'rolled_back', locked_at = NULL,
                          last_error = ?, completed_at = ?, updated_at = ? WHERE decision_id = ?""",
                (f"{type(error).__name__}: {str(error)[:500]}", timestamp, timestamp, decision_id),
            )

    def decisions(self, *, entitlement_key: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM failover_decisions
                    WHERE (? IS NULL OR entitlement_key = ?)
                    ORDER BY created_at DESC LIMIT ?""",
                (entitlement_key, entitlement_key, max(1, min(200, int(limit)))),
            ).fetchall()
        return [dict(row) for row in rows]
