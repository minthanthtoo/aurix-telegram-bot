"""Independent notification worker with explicit persistence and decryption ports."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from commerce_models import NOTIFICATION_RETRY_DELAY, UTC
from domain_time import utc_text
from notification_contracts import NotificationRecord, NotificationTransactions


class NotificationWorker:
    def __init__(
        self,
        transactions: NotificationTransactions,
        decrypt: Callable[[str | None], str | None],
    ):
        self._transactions = transactions
        self._decrypt = decrypt

    def _hydrate(self, rows: list[NotificationRecord]) -> list[dict[str, object]]:
        notifications: list[dict[str, object]] = []
        for row in rows:
            notification = dict(row.values)
            ciphertext = notification.get("access_url_ciphertext")
            access_url = self._decrypt(str(ciphertext) if ciphertext else None)
            if access_url:
                notification["text"] = str(notification["text"]) + f"\n\nYour Outline key:\n{access_url}"
                notification["access_url"] = access_url
            elif ciphertext:
                notification["secret_unavailable"] = True
            notifications.append(notification)
        return notifications

    def pending_notifications(
        self, now: datetime | None = None, limit: int = 20,
    ) -> list[dict[str, object]]:
        with self._transactions() as transaction:
            rows = transaction.pending(utc_text(now), max(1, min(limit, 100)))
        return self._hydrate(rows)

    def claim_pending_notifications(
        self, now: datetime | None = None, limit: int = 20, lease_seconds: int = 120,
    ) -> list[dict[str, object]]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        lease_until = utc_text(current + timedelta(seconds=max(30, int(lease_seconds))))
        with self._transactions(write=True) as transaction:
            rows = transaction.claim(utc_text(current), max(1, min(int(limit), 100)), lease_until)
        return self._hydrate(rows)

    def mark_notification_sent(self, notification_id: str, now: datetime | None = None) -> None:
        with self._transactions(write=True) as transaction:
            transaction.mark_sent(notification_id, utc_text(now))

    def mark_notification_failed(self, notification_id: str, now: datetime | None = None) -> None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with self._transactions(write=True) as transaction:
            transaction.mark_failed(
                notification_id, utc_text(current), utc_text(current + NOTIFICATION_RETRY_DELAY),
            )
