"""Data and transaction contracts for notification processing."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Mapping, Protocol


@dataclass(frozen=True)
class NotificationRecord:
    values: Mapping[str, object]


class NotificationTransaction(Protocol):
    def pending(self, now: str, limit: int) -> list[NotificationRecord]: ...

    def claim(self, now: str, limit: int, lease_until: str) -> list[NotificationRecord]: ...

    def mark_sent(self, notification_id: str, now: str) -> None: ...

    def mark_failed(self, notification_id: str, now: str, retry_at: str) -> None: ...


class NotificationTransactions(Protocol):
    def __call__(self, *, write: bool = False) -> AbstractContextManager[NotificationTransaction]: ...
