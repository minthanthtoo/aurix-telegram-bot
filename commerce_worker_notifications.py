"""Notification outbox operations."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from commerce_repositories import _PostgresConnection
from commerce_models import (
    NOTIFICATION_RETRY_DELAY,
    UTC,
    _now_text,
)


def pending_notifications(
    self, now: datetime | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    now_text = _now_text(now)
    with self.database.connect() as connection:
        rows = connection.execute(
            """SELECT * FROM notifications
               WHERE status IN ('pending', 'failed')
                 AND dead_lettered_at IS NULL
                 AND next_attempt_at <= ?
               ORDER BY created_at LIMIT ?""",
            (now_text, max(1, min(limit, 100))),
        ).fetchall()
    return self._hydrate_notifications(rows)

def claim_pending_notifications(
    self,
    now: datetime | None = None,
    limit: int = 20,
    lease_seconds: int = 120,
) -> list[dict[str, Any]]:
    """Claim due notifications with crash recovery through a short lease.

    ``next_attempt_at`` doubles as a lease because notification rows
    predate a dedicated worker-lock column. A second worker cannot select a
    leased row, while a process that dies before marking it sent exposes the
    row again after the lease. Delivery remains at-least-once, but normal
    concurrent workers no longer duplicate the same notification.
    """

    current = (now or datetime.now(UTC)).astimezone(UTC)
    now_text = _now_text(current)
    lease_until = _now_text(current + timedelta(seconds=max(30, int(lease_seconds))))
    page_limit = max(1, min(int(limit), 100))
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        lock_clause = (
            " FOR UPDATE SKIP LOCKED" if isinstance(connection, _PostgresConnection) else ""
        )
        rows = connection.execute(
            """SELECT * FROM notifications
               WHERE status IN ('pending', 'failed')
                 AND dead_lettered_at IS NULL
                 AND next_attempt_at <= ?
               ORDER BY created_at LIMIT ?"""
            + lock_clause,
            (now_text, page_limit),
        ).fetchall()
        for row in rows:
            connection.execute(
                """UPDATE notifications SET next_attempt_at = ?
                   WHERE id = ? AND status IN ('pending', 'failed')
                     AND dead_lettered_at IS NULL AND next_attempt_at <= ?""",
                (lease_until, row["id"], now_text),
            )
    return self._hydrate_notifications(rows)

def _hydrate_notifications(self, rows: Any) -> list[dict[str, Any]]:
    notifications = []
    for row in rows:
        notification = dict(row)
        access_url = self._decrypt_access_url(notification.get("access_url_ciphertext"))
        if access_url:
            notification["text"] += f"\n\nYour Outline key:\n{access_url}"
            notification["access_url"] = access_url
        elif notification.get("access_url_ciphertext"):
            notification["secret_unavailable"] = True
        notifications.append(notification)
    return notifications

def mark_notification_sent(self, notification_id: str, now: datetime | None = None) -> None:
    with self.database.connect() as connection:
        connection.execute(
            """UPDATE notifications SET status = 'sent', sent_at = ? WHERE id = ?""",
            (_now_text(now), notification_id),
        )

def mark_notification_failed(self, notification_id: str, now: datetime | None = None) -> None:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    with self.database.connect() as connection:
        connection.execute(
            """UPDATE notifications
               SET status = 'failed', attempts = attempts + 1,
                   dead_lettered_at = CASE WHEN attempts + 1 >= 8 THEN ? ELSE dead_lettered_at END,
                   next_attempt_at = CASE WHEN attempts + 1 >= 8 THEN '9999-12-31T00:00:00+00:00' ELSE ? END
               WHERE id = ?""",
            (
                _now_text(current),
                _now_text(current + NOTIFICATION_RETRY_DELAY),
                notification_id,
            ),
        )
