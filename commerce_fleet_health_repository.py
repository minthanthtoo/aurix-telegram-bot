"""Persistence boundary for endpoint lifecycle and health state."""

from __future__ import annotations

from typing import Any


class FleetHealthRepository:
    """SQL operations for lifecycle transitions and health hysteresis."""

    @staticmethod
    def server(connection: Any, server_id: str) -> Any:
        return connection.execute(
            "SELECT * FROM outline_servers WHERE server_id = ?", (server_id,)
        ).fetchone()

    @staticmethod
    def retirement_counts(
        connection: Any,
        server_id: str,
        *,
        include_free_keys: bool,
        include_free_intents: bool,
    ) -> dict[str, int]:
        active_free = 0
        if include_free_keys:
            active_free = connection.execute(
                """SELECT COUNT(*) AS n FROM keys
                   WHERE server_id = ? AND status IN ('active', 'revoke_failed')""",
                (server_id,),
            ).fetchone()["n"]
        active_paid = connection.execute(
            """SELECT COUNT(*) AS n FROM paid_vpn_keys
               WHERE server_id = ? AND status IN ('active', 'revoke_failed')""",
            (server_id,),
        ).fetchone()["n"]
        pending_orders = connection.execute(
            """SELECT COUNT(*) AS n FROM orders WHERE server_id = ?
               AND status IN ('awaiting_payment', 'payment_submitted')""",
            (server_id,),
        ).fetchone()["n"]
        pending_subscriptions = connection.execute(
            """SELECT COUNT(*) AS n FROM subscriptions WHERE server_id = ?
               AND status IN ('pending', 'active')""",
            (server_id,),
        ).fetchone()["n"]
        pending_intents = 0
        if include_free_intents:
            pending_intents = connection.execute(
                """SELECT COUNT(*) AS n FROM free_provisioning_intents
                   WHERE server_id = ? AND status IN ('pending', 'running')""",
                (server_id,),
            ).fetchone()["n"]
        return {
            "active_free": int(active_free),
            "active_paid": int(active_paid),
            "pending_orders": int(pending_orders),
            "pending_subscriptions": int(pending_subscriptions),
            "pending_intents": int(pending_intents),
        }

    @staticmethod
    def set_lifecycle(
        connection: Any,
        *,
        server_id: str,
        enabled: bool,
        state: str,
        reason: str | None,
        now_text: str,
    ) -> None:
        connection.execute(
            """UPDATE outline_servers
                  SET enabled = ?, lifecycle_state = ?, lifecycle_reason = ?,
                      lifecycle_changed_at = ?, updated_at = ?
                WHERE server_id = ?""",
            (int(enabled), state, reason, now_text, now_text, server_id),
        )

    @staticmethod
    def health_state_for_update(connection: Any, server_id: str) -> Any:
        lock_clause = (
            " FOR UPDATE" if connection.__class__.__name__ == "_PostgresConnection" else ""
        )
        return connection.execute(
            """SELECT health_status, health_success_streak,
                      health_failure_streak, health_state_changed_at, last_error
                 FROM outline_servers WHERE server_id = ?"""
            + lock_clause,
            (server_id,),
        ).fetchone()

    @staticmethod
    def duplicate_observation(connection: Any, server_id: str, observed_at: str) -> Any:
        return connection.execute(
            """SELECT state_after FROM endpoint_health_observations
               WHERE server_id = ? AND probe_type = 'management_inventory'
                 AND observed_at = ?""",
            (server_id, observed_at),
        ).fetchone()

    @staticmethod
    def update_health(
        connection: Any,
        *,
        server_id: str,
        state: str,
        success_streak: int,
        failure_streak: int,
        changed_at: str | None,
        latency_ms: float | None,
        last_error: str | None,
        observed_at: str,
    ) -> None:
        connection.execute(
            """UPDATE outline_servers
                  SET health_status = ?, health_success_streak = ?,
                      health_failure_streak = ?, health_state_changed_at = ?,
                      health_last_latency_ms = ?, last_error = ?,
                      last_synced_at = ?, updated_at = ? WHERE server_id = ?""",
            (
                state,
                success_streak,
                failure_streak,
                changed_at,
                latency_ms,
                last_error,
                observed_at,
                observed_at,
                server_id,
            ),
        )

    @staticmethod
    def record_observation(connection: Any, **values: Any) -> None:
        connection.execute(
            """INSERT INTO endpoint_health_observations
               (id, server_id, probe_type, observed_at, observed_status,
                state_before, state_after, latency_ms, remote_key_count,
                error_type, created_at)
               VALUES (?, ?, 'management_inventory', ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(server_id, probe_type, observed_at) DO NOTHING""",
            (
                values["observation_id"],
                values["server_id"],
                values["observed_at"],
                values["observed_status"],
                values["state_before"],
                values["state_after"],
                values["latency_ms"],
                values["remote_key_count"],
                values["error_type"],
                values["observed_at"],
            ),
        )
