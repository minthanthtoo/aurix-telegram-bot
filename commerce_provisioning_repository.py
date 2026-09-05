"""Transaction-aware persistence for paid subscription provisioning."""

from __future__ import annotations

from typing import Any


class ProvisioningRepository:
    """SQL operations for provisioning while the worker owns transaction scope."""

    @staticmethod
    def context(connection: Any, subscription_id: str) -> tuple[Any, Any]:
        subscription = connection.execute(
            """SELECT s.*, p.quota_bytes AS catalog_quota_bytes,
                      p.name AS catalog_plan_name, u.username
               FROM subscriptions s JOIN plans p ON p.code = s.plan_code
               JOIN users u ON u.telegram_id = s.telegram_id WHERE s.id = ?""",
            (subscription_id,),
        ).fetchone()
        existing = connection.execute(
            "SELECT * FROM paid_vpn_keys WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchone()
        return subscription, existing

    @staticmethod
    def defer_job(connection: Any, job_id: str, next_attempt_at: str) -> None:
        connection.execute(
            """UPDATE provisioning_jobs
               SET status = 'pending', next_attempt_at = ?, locked_at = NULL WHERE id = ?""",
            (next_attempt_at, job_id),
        )

    @staticmethod
    def expire_subscription(
        connection: Any, subscription_id: str, expected_status: str
    ) -> None:
        connection.execute(
            "UPDATE subscriptions SET status = 'expired' WHERE id = ? AND status = ?",
            (subscription_id, expected_status),
        )

    @staticmethod
    def mark_job_expired(connection: Any, job_id: str) -> None:
        connection.execute(
            """UPDATE provisioning_jobs SET status = 'done', locked_at = NULL,
                      last_error = 'expired before provision' WHERE id = ?""",
            (job_id,),
        )

    @staticmethod
    def insert_key(
        connection: Any,
        *,
        key_id: str,
        subscription_id: str,
        telegram_id: int,
        outline_key_id: str,
        access_url: str,
        quota_bytes: int | None,
        created_at: str,
        server_id: str | None,
    ) -> None:
        connection.execute(
            """INSERT INTO paid_vpn_keys
               (id, subscription_id, telegram_id, outline_key_id, access_url,
                quota_bytes, status, created_at, server_id)
               VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (
                key_id,
                subscription_id,
                telegram_id,
                outline_key_id,
                access_url,
                quota_bytes,
                created_at,
                server_id,
            ),
        )

    @staticmethod
    def activate_subscription(
        connection: Any,
        *,
        subscription_id: str,
        activated_at: str,
        expires_at: str,
    ) -> None:
        connection.execute(
            """UPDATE subscriptions
               SET status = 'active', activated_at = ?, starts_at = ?, expires_at = ?
               WHERE id = ?""",
            (activated_at, activated_at, expires_at, subscription_id),
        )

    @staticmethod
    def queue_ready_notification(
        connection: Any,
        *,
        notification_id: str,
        dedupe_key: str,
        telegram_id: int,
        text: str,
        access_url_ciphertext: str,
        now_text: str,
    ) -> None:
        connection.execute(
            """INSERT INTO notifications
               (id, dedupe_key, telegram_id, kind, text, access_url_ciphertext,
                status, next_attempt_at, created_at)
               VALUES (?, ?, ?, 'vpn_ready', ?, ?, 'pending', ?, ?)
               ON CONFLICT(dedupe_key) DO NOTHING""",
            (
                notification_id,
                dedupe_key,
                telegram_id,
                text,
                access_url_ciphertext,
                now_text,
                now_text,
            ),
        )

    @staticmethod
    def mark_job_done(connection: Any, job_id: str) -> None:
        connection.execute(
            """UPDATE provisioning_jobs
               SET status = 'done', locked_at = NULL, last_error = NULL WHERE id = ?""",
            (job_id,),
        )
