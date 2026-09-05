"""Persistence boundary for notification outbox leasing and retries."""

from __future__ import annotations

from typing import Any


class NotificationRepository:
    @staticmethod
    def pending(connection: Any, now_text: str, limit: int) -> list[Any]:
        return connection.execute(
            """SELECT * FROM notifications
               WHERE status IN ('pending', 'failed') AND dead_lettered_at IS NULL
                 AND next_attempt_at <= ? ORDER BY created_at LIMIT ?""",
            (now_text, limit),
        ).fetchall()

    @staticmethod
    def claim(connection: Any, now_text: str, limit: int) -> list[Any]:
        lock_clause = (
            " FOR UPDATE SKIP LOCKED" if connection.__class__.__name__ == "_PostgresConnection" else ""
        )
        rows = connection.execute(
            """SELECT * FROM notifications
               WHERE status IN ('pending', 'failed') AND dead_lettered_at IS NULL
                 AND next_attempt_at <= ? ORDER BY created_at LIMIT ?""" + lock_clause,
            (now_text, limit),
        ).fetchall()
        return rows

    @staticmethod
    def lease(connection: Any, notification_id: str, now_text: str, lease_until: str) -> None:
        connection.execute(
            """UPDATE notifications SET next_attempt_at = ?
               WHERE id = ? AND status IN ('pending', 'failed')
                 AND dead_lettered_at IS NULL AND next_attempt_at <= ?""",
            (lease_until, notification_id, now_text),
        )

    @staticmethod
    def mark_sent(connection: Any, notification_id: str, now_text: str) -> None:
        connection.execute(
            "UPDATE notifications SET status = 'sent', sent_at = ? WHERE id = ?",
            (now_text, notification_id),
        )

    @staticmethod
    def mark_failed(
        connection: Any, notification_id: str, failed_at: str, next_attempt_at: str
    ) -> None:
        connection.execute(
            """UPDATE notifications
               SET status = 'failed', attempts = attempts + 1,
                   dead_lettered_at = CASE WHEN attempts + 1 >= 8 THEN ? ELSE dead_lettered_at END,
                   next_attempt_at = CASE WHEN attempts + 1 >= 8 THEN '9999-12-31T00:00:00+00:00' ELSE ? END
               WHERE id = ?""",
            (failed_at, next_attempt_at, notification_id),
        )
