"""Persistence boundary for paid subscription expiry and key revocation."""

from __future__ import annotations

from typing import Any


class LifecycleRepository:
    """Store lifecycle transitions, retry evidence, and revoke notifications."""

    @staticmethod
    def key_for_outline_id(connection: Any, outline_key_id: str) -> Any:
        return connection.execute(
            "SELECT id, telegram_id, server_id, data_limit_bytes, expires_at FROM keys WHERE outline_key_id = ?",
            (str(outline_key_id),),
        ).fetchone()

    @staticmethod
    def mark_legacy_key_revoked(connection: Any, key_id: Any) -> None:
        connection.execute("UPDATE keys SET status = 'revoked' WHERE id = ?", (key_id,))

    @staticmethod
    def record_legacy_cleanup(
        connection: Any, *, local: Any, outline_key_id: str, now_text: str
    ) -> None:
        connection.execute(
            """INSERT INTO key_termination_events
               (key_id, telegram_id, outline_key_id, reason, quota_bytes,
                expires_at, detected_at, remote_state, delete_attempts,
                deletion_verified_at)
               VALUES (?, ?, ?, 'paid_upgrade_cleanup', ?, ?, ?, 'delete_accepted', 1, ?)
               ON CONFLICT(key_id, reason) DO UPDATE SET
                  remote_state = excluded.remote_state,
                  delete_attempts = key_termination_events.delete_attempts + 1,
                  deletion_verified_at = excluded.deletion_verified_at""",
            (
                local["id"], local["telegram_id"], outline_key_id,
                local["data_limit_bytes"], local["expires_at"], now_text, now_text,
            ),
        )

    @staticmethod
    def record_legacy_cleanup_failure(
        connection: Any, *, local: Any, outline_key_id: str, error: str, now_text: str
    ) -> None:
        connection.execute(
            """INSERT INTO key_termination_events
               (key_id, telegram_id, outline_key_id, reason, quota_bytes,
                expires_at, detected_at, remote_state, delete_attempts, last_error)
               VALUES (?, ?, ?, 'paid_upgrade_cleanup', ?, ?, ?, 'retrying', 1, ?)
               ON CONFLICT(key_id, reason) DO UPDATE SET
                  remote_state = 'retrying', delete_attempts = key_termination_events.delete_attempts + 1,
                  last_error = excluded.last_error""",
            (
                local["id"], local["telegram_id"], outline_key_id,
                local["data_limit_bytes"], local["expires_at"], now_text, error,
            ),
        )

    @staticmethod
    def expired_subscriptions(connection: Any, now_text: str) -> list[Any]:
        return connection.execute(
            """SELECT id FROM subscriptions
               WHERE status = 'active' AND expires_at <= ?""",
            (now_text,),
        ).fetchall()

    @staticmethod
    def mark_subscription_expired(connection: Any, subscription_id: str) -> None:
        connection.execute(
            "UPDATE subscriptions SET status = 'expired' WHERE id = ?", (subscription_id,)
        )

    @staticmethod
    def insert_revoke_job(connection: Any, job_id: str, subscription_id: str, now_text: str) -> None:
        connection.execute(
            """INSERT INTO provisioning_jobs
               (id, subscription_id, operation, status, next_attempt_at, created_at)
               VALUES (?, ?, 'revoke', 'pending', ?, ?)
               ON CONFLICT(subscription_id, operation) DO NOTHING""",
            (job_id, subscription_id, now_text, now_text),
        )

    @staticmethod
    def revoke_context(connection: Any, subscription_id: str) -> Any:
        return connection.execute(
            """SELECT k.*, s.status AS subscription_status,
                      o.id AS order_id, o.refund_status
               FROM paid_vpn_keys k
               JOIN subscriptions s ON s.id = k.subscription_id
               JOIN orders o ON o.id = s.order_id
               WHERE k.subscription_id = ?""",
            (subscription_id,),
        ).fetchone()

    @staticmethod
    def mark_paid_key_revoked(connection: Any, key_id: Any, revoked_at: str) -> None:
        connection.execute(
            """UPDATE paid_vpn_keys SET status = 'revoked', revoked_at = ?
               WHERE id = ?""",
            (revoked_at, key_id),
        )

    @staticmethod
    def mark_job_done(connection: Any, job_id: str) -> None:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'done', locked_at = NULL WHERE id = ?",
            (job_id,),
        )

    @staticmethod
    def quota_event(connection: Any, subscription_id: str) -> Any:
        return connection.execute(
            """SELECT observed_bytes, quota_bytes, observed_at FROM quota_events
               WHERE subscription_id = ? AND reason IN ('quota', 'aggregate_quota')
               ORDER BY observed_at DESC LIMIT 1""",
            (subscription_id,),
        ).fetchone()

    @staticmethod
    def notification(connection: Any, **values: Any) -> None:
        connection.execute(
            """INSERT INTO notifications
               (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
               ON CONFLICT(dedupe_key) DO NOTHING""",
            (
                values["id"], values["dedupe_key"], values["telegram_id"], values["kind"],
                values["text"], values["now_text"], values["now_text"],
            ),
        )
