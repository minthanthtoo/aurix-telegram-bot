"""Notification outbox operations."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from commerce_models import (
    NOTIFICATION_RETRY_DELAY,
    UTC,
    _now_text,
)
from commerce_notification_repository import NotificationRepository


_REPOSITORY = NotificationRepository()


def pending_notifications(
    self, now: datetime | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    now_text = _now_text(now)
    with self.database.connect() as connection:
        rows = _REPOSITORY.pending(connection, now_text, max(1, min(limit, 100)))
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
        rows = _REPOSITORY.claim(connection, now_text, page_limit)
        for row in rows:
            _REPOSITORY.lease(connection, str(row["id"]), now_text, lease_until)
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
        _REPOSITORY.mark_sent(connection, notification_id, _now_text(now))

def mark_notification_failed(self, notification_id: str, now: datetime | None = None) -> None:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    with self.database.connect() as connection:
        _REPOSITORY.mark_failed(
            connection,
            notification_id,
            _now_text(current),
            _now_text(current + NOTIFICATION_RETRY_DELAY),
        )
