"""Persistence boundary for quota enforcement and scale observations."""

from __future__ import annotations

from typing import Any


class CapacityOperationsRepository:
    """Store quota warning/revoke state and capacity evidence."""

    @staticmethod
    def active_paid_keys_for_warnings(connection: Any) -> list[Any]:
        return connection.execute(
            """SELECT k.id, k.subscription_id, k.telegram_id, k.outline_key_id,
                      k.quota_bytes, k.status, k.quota_warning_percent,
                      k.server_id, s.plan_code, s.expires_at
               FROM paid_vpn_keys k JOIN subscriptions s ON s.id = k.subscription_id
               WHERE k.status = 'active' AND s.status = 'active'
                 AND k.quota_bytes IS NOT NULL"""
        ).fetchall()

    @staticmethod
    def notification_exists(connection: Any, dedupe_key: str) -> Any:
        return connection.execute(
            "SELECT id FROM notifications WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()

    @staticmethod
    def insert_warning(connection: Any, **values: Any) -> None:
        connection.execute(
            """INSERT INTO notifications
               (id, dedupe_key, telegram_id, kind, text, status,
                next_attempt_at, created_at)
               VALUES (?, ?, ?, 'quota_warning', ?, 'pending', ?, ?)""",
            (values["id"], values["dedupe_key"], values["telegram_id"], values["text"], values["now_text"], values["now_text"]),
        )

    @staticmethod
    def update_warning_percent(connection: Any, key_id: Any, remaining_percent: int) -> None:
        connection.execute(
            "UPDATE paid_vpn_keys SET quota_warning_percent = ? WHERE id = ?",
            (remaining_percent, key_id),
        )

    @staticmethod
    def active_paid_keys_for_enforcement(connection: Any) -> list[Any]:
        return connection.execute(
            """SELECT k.id, k.subscription_id, k.outline_key_id, k.quota_bytes, k.server_id,
                      k.status, s.status AS subscription_status
               FROM paid_vpn_keys k JOIN subscriptions s ON s.id = k.subscription_id
               WHERE k.status = 'active' AND s.status = 'active' AND k.quota_bytes IS NOT NULL"""
        ).fetchall()

    @staticmethod
    def quota_event(connection: Any, subscription_id: str, reason: str) -> Any:
        return connection.execute(
            "SELECT id FROM quota_events WHERE subscription_id = ? AND reason = ?",
            (subscription_id, reason),
        ).fetchone()

    @staticmethod
    def insert_quota_event(connection: Any, **values: Any) -> None:
        connection.execute(
            """INSERT INTO quota_events
               (id, subscription_id, reason, observed_bytes, quota_bytes, observed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (values["id"], values["subscription_id"], values["reason"], values["used"], values["quota"], values["observed_at"]),
        )

    @staticmethod
    def update_usage(connection: Any, key_id: Any, used: int, observed_at: str) -> None:
        connection.execute(
            "UPDATE paid_vpn_keys SET last_usage_bytes = ?, last_usage_observed_at = ? WHERE id = ?",
            (used, observed_at, key_id),
        )

    @staticmethod
    def mark_exhausted(connection: Any, **values: Any) -> None:
        connection.execute(
            """UPDATE paid_vpn_keys SET status = 'active',
                      last_usage_bytes = ?, last_usage_observed_at = ?, quota_reason = ?
               WHERE id = ? AND status = 'active'""",
            (values["used"], values["observed_at"], values["reason"], values["key_id"]),
        )

    @staticmethod
    def revoke_subscription(connection: Any, subscription_id: str) -> None:
        connection.execute(
            "UPDATE subscriptions SET status = 'revoked' WHERE id = ? AND status = 'active'",
            (subscription_id,),
        )

    @staticmethod
    def insert_revoke_job(connection: Any, subscription_id: str, now_text: str, job_id: str) -> None:
        connection.execute(
            """INSERT INTO provisioning_jobs
               (id, subscription_id, operation, status, next_attempt_at, created_at)
               VALUES (?, ?, 'revoke', 'pending', ?, ?)
               ON CONFLICT(subscription_id, operation) DO NOTHING""",
            (job_id, subscription_id, now_text, now_text),
        )

    @staticmethod
    def table_exists(connection: Any, name: str) -> bool:
        if connection.__class__.__name__ == "_PostgresConnection":
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
    def latest_scale_observation(connection: Any) -> Any:
        return connection.execute(
            "SELECT observed_at FROM scale_observations ORDER BY observed_at DESC LIMIT 1"
        ).fetchone()

    @staticmethod
    def insert_scale_observation(connection: Any, **values: Any) -> None:
        connection.execute(
            """INSERT INTO scale_observations
               (id, fleet_fingerprint, observed_at, status,
                utilization_percent, remaining_slots, saleable_capacity,
                traffic_utilization_percent, healthy_server_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(fleet_fingerprint, observed_at) DO NOTHING""",
            (
                values["id"], values["fingerprint"], values["observed_at"], values["status"],
                values["utilization_percent"], values["remaining_slots"], values["saleable_capacity"],
                values["traffic_utilization_percent"], values["healthy_count"], values["observed_at"],
            ),
        )

    @staticmethod
    def recent_scale_observations(connection: Any) -> list[Any]:
        return connection.execute(
            """SELECT observed_at, status FROM scale_observations
               ORDER BY observed_at DESC LIMIT 10"""
        ).fetchall()
