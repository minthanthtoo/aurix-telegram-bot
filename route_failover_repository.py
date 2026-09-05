"""Persistence boundary for the route-failover state machine."""

from __future__ import annotations

from typing import Any

from commerce_repositories import _PostgresConnection


class RouteFailoverRepository:
    """Store failover observations, policies, leases, and decisions."""

    @staticmethod
    def table_exists(connection: Any, name: str) -> bool:
        if isinstance(connection, _PostgresConnection):
            row = connection.execute(
                "SELECT to_regclass(?) AS table_name", (f"public.{name}",)
            ).fetchone()
            return bool(row and row["table_name"])
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
            ).fetchone()
            is not None
        )

    @staticmethod
    def policy(connection: Any, entitlement_id: str) -> Any:
        return connection.execute(
            "SELECT * FROM route_failover_policies WHERE entitlement_id = ?",
            (str(entitlement_id),),
        ).fetchone()

    @staticmethod
    def entitlement_exists(connection: Any, entitlement_id: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM entitlements WHERE entitlement_id = ?", (str(entitlement_id),)
            ).fetchone()
            is not None
        )

    @staticmethod
    def upsert_policy(connection: Any, **values: Any) -> Any:
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
                values["policy_id"], values["entitlement_id"], values["enabled"],
                values["failure_threshold"], values["recovery_threshold"],
                values["cooldown_seconds"], values["standby_lease_bytes"],
                values["max_attempts"], values["timestamp"], values["timestamp"],
            ),
        )
        return RouteFailoverRepository.policy(connection, values["entitlement_id"])

    @staticmethod
    def generation_context(connection: Any, generation_id: str) -> Any:
        lock_clause = " FOR UPDATE" if isinstance(connection, _PostgresConnection) else ""
        return connection.execute(
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

    @staticmethod
    def insert_observation(connection: Any, **values: Any) -> Any:
        return connection.execute(
            """INSERT INTO route_observations
               (observation_id, generation_id, entitlement_id, route_id,
                network_bucket, outcome, latency_ms, reason, observed_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(generation_id, route_id, network_bucket, outcome, observed_at)
               DO NOTHING""",
            (
                values["observation_id"], values["generation_id"], values["entitlement_id"],
                values["route_id"], values["network_bucket"], values["outcome"],
                values["latency_ms"], values["reason"], values["timestamp"], values["timestamp"],
            ),
        )

    @staticmethod
    def state(connection: Any, generation_id: str) -> Any:
        return connection.execute(
            "SELECT * FROM route_failover_state WHERE generation_id = ?",
            (str(generation_id),),
        ).fetchone()

    @staticmethod
    def upsert_state(connection: Any, **values: Any) -> None:
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
                values["generation_id"], values["failure_streak"], values["success_streak"],
                values["outcome"], values["timestamp"], values["cooldown_until"], values["timestamp"],
            ),
        )

    @staticmethod
    def target_route(connection: Any, source_route_id: str) -> Any:
        true = "TRUE" if isinstance(connection, _PostgresConnection) else "1"
        return connection.execute(
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
            (str(source_route_id), str(source_route_id)),
        ).fetchone()

    @staticmethod
    def insert_decision(connection: Any, **values: Any) -> Any:
        return connection.execute(
            """INSERT INTO failover_decisions
               (decision_id, idempotency_key, entitlement_id,
                source_generation_id, source_endpoint_id, source_route_id,
                target_endpoint_id, target_route_id, trigger, network_bucket,
                state, attempts, next_attempt_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
               ON CONFLICT(idempotency_key) DO NOTHING""",
            (
                values["decision_id"], values["idempotency_key"], values["entitlement_id"],
                values["generation_id"], values["source_endpoint_id"], values["source_route_id"],
                values["target_endpoint_id"], values["target_route_id"], values["trigger"],
                values["network_bucket"], values["timestamp"], values["timestamp"], values["timestamp"],
            ),
        )

    @staticmethod
    def decision_by_key(connection: Any, idempotency_key: str) -> Any:
        return connection.execute(
            "SELECT decision_id FROM failover_decisions WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()

    @staticmethod
    def set_cooldown(connection: Any, generation_id: str, cooldown: str, timestamp: str) -> None:
        connection.execute(
            "UPDATE route_failover_state SET cooldown_until = ?, updated_at = ? WHERE generation_id = ?",
            (cooldown, timestamp, str(generation_id)),
        )

    @staticmethod
    def reset_stale(connection: Any, timestamp: str, stale_before: str) -> None:
        connection.execute(
            """UPDATE failover_decisions
                  SET state = 'pending', locked_at = NULL, updated_at = ?
                WHERE state = 'creating' AND locked_at < ?""",
            (timestamp, stale_before),
        )

    @staticmethod
    def claim_candidate(connection: Any, timestamp: str) -> Any:
        lock_clause = " FOR UPDATE SKIP LOCKED" if isinstance(connection, _PostgresConnection) else ""
        return connection.execute(
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

    @staticmethod
    def claim_update(connection: Any, decision_id: str, timestamp: str) -> Any:
        return connection.execute(
            """UPDATE failover_decisions
                  SET state = 'creating', attempts = attempts + 1,
                      locked_at = ?, updated_at = ?
                WHERE decision_id = ? AND state IN ('pending', 'failed', 'verified')""",
            (timestamp, timestamp, str(decision_id)),
        )

    @staticmethod
    def failure_context(connection: Any, decision_id: str) -> Any:
        return connection.execute(
            """SELECT d.attempts, p.max_attempts
                 FROM failover_decisions d JOIN route_failover_policies p
                   ON p.entitlement_id = d.entitlement_id
                WHERE d.decision_id = ?""",
            (str(decision_id),),
        ).fetchone()

    @staticmethod
    def mark_failed(connection: Any, **values: Any) -> None:
        connection.execute(
            """UPDATE failover_decisions
                  SET state = ?, next_attempt_at = ?, locked_at = NULL,
                      last_error = ?, updated_at = ?
                WHERE decision_id = ?""",
            (values["state"], values["next_attempt_at"], values["error"], values["timestamp"], values["decision_id"]),
        )

    @staticmethod
    def mark_committed(connection: Any, decision_id: str, timestamp: str) -> None:
        connection.execute(
            """UPDATE failover_decisions
                  SET state = 'committed', locked_at = NULL,
                      last_error = NULL, completed_at = ?, updated_at = ?
                WHERE decision_id = ? AND state IN ('creating', 'verified')""",
            (timestamp, timestamp, str(decision_id)),
        )

    @staticmethod
    def mark_verified(connection: Any, decision_id: str, timestamp: str) -> None:
        connection.execute(
            """UPDATE failover_decisions
                  SET state = 'verified', locked_at = NULL,
                      last_error = NULL, updated_at = ?
                WHERE decision_id = ? AND state = 'creating'""",
            (timestamp, str(decision_id)),
        )

    @staticmethod
    def mark_rolled_back(connection: Any, **values: Any) -> None:
        connection.execute(
            """UPDATE failover_decisions
                  SET state = 'rolled_back', locked_at = NULL,
                      last_error = ?, completed_at = ?, updated_at = ?
                WHERE decision_id = ?""",
            (values["error"], values["timestamp"], values["timestamp"], values["decision_id"]),
        )

    @staticmethod
    def decisions(connection: Any, entitlement_id: str | None, limit: int) -> list[Any]:
        return connection.execute(
            """SELECT * FROM failover_decisions
                WHERE (? IS NULL OR entitlement_id = ?)
                ORDER BY created_at DESC LIMIT ?""",
            (entitlement_id, entitlement_id, limit),
        ).fetchall()
