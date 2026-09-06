"""Bind the outbox transaction contract to the existing database lifecycle."""

from __future__ import annotations

from contextlib import contextmanager
from types import MappingProxyType
from typing import Any, Iterator

from commerce_notification_repository import NotificationRepository
from notification_contracts import NotificationRecord, NotificationTransaction
from repositories import RepositoryDatabase


class _OutboxSession:
    def __init__(self, connection: Any):
        self._connection = connection

    @staticmethod
    def _records(rows: Any) -> list[NotificationRecord]:
        return [NotificationRecord(MappingProxyType(dict(row))) for row in rows]

    def pending(self, now: str, limit: int) -> list[NotificationRecord]:
        return self._records(NotificationRepository.pending(self._connection, now, limit))

    def claim(self, now: str, limit: int, lease_until: str) -> list[NotificationRecord]:
        rows = NotificationRepository.claim(self._connection, now, limit)
        for row in rows:
            NotificationRepository.lease(self._connection, str(row["id"]), now, lease_until)
        return self._records(rows)

    def mark_sent(self, notification_id: str, now: str) -> None:
        NotificationRepository.mark_sent(self._connection, notification_id, now)

    def mark_failed(self, notification_id: str, now: str, retry_at: str) -> None:
        NotificationRepository.mark_failed(self._connection, notification_id, now, retry_at)


class NotificationOutbox:
    def __init__(self, database: RepositoryDatabase):
        self._database = database

    @contextmanager
    def __call__(self, *, write: bool = False) -> Iterator[NotificationTransaction]:
        with self._database.connect() as connection:
            if write:
                self._database.begin_write(connection)
            yield _OutboxSession(connection)
