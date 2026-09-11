"""Bounded in-process workers for durable browser conversation attempts."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from collections.abc import Callable
from typing import Any


JobCallback = Callable[[threading.Event], Any]


class ConversationJobManager:
    """Run bounded durable attempts with explicit cancellation.

    Attempt state is durable in :mod:`aurix_ai.conversations`; this manager is
    only the process-local execution handle. A restart therefore loses no
    committed state and cannot silently resubmit an interrupted attempt. A
    conversation has at most one live attempt, and each owner has a small
    concurrency budget inside the global executor.
    """

    def __init__(self, *, max_workers: int = 4, max_jobs_per_owner: int = 2) -> None:
        self.executor = ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), 16)))
        self.max_jobs_per_owner = max(1, min(int(max_jobs_per_owner), 16))
        self._lock = threading.Lock()
        self._jobs: dict[str, tuple[Future[Any], threading.Event, str | None, int | None]] = {}
        self._conversation_jobs: dict[str, str] = {}
        self._owner_counts: dict[int, int] = {}
        self._closed = False

    def submit(
        self,
        attempt_id: str,
        callback: JobCallback,
        *,
        conversation_id: str | None = None,
        owner_id: int | None = None,
    ) -> bool:
        clean_id = str(attempt_id).strip()
        if not clean_id or not callable(callback):
            raise ValueError("attempt_id and callback are required")
        clean_conversation_id = str(conversation_id).strip() if conversation_id else None
        clean_owner_id = int(owner_id) if owner_id is not None else None
        with self._lock:
            if self._closed:
                raise RuntimeError("conversation job manager is closed")
            if clean_id in self._jobs:
                return False
            if (
                clean_conversation_id is not None
                and clean_conversation_id in self._conversation_jobs
            ):
                return False
            if (
                clean_owner_id is not None
                and self._owner_counts.get(clean_owner_id, 0) >= self.max_jobs_per_owner
            ):
                return False
            stop_event = threading.Event()
            future = self.executor.submit(callback, stop_event)
            self._jobs[clean_id] = (
                future,
                stop_event,
                clean_conversation_id,
                clean_owner_id,
            )
            if clean_conversation_id is not None:
                self._conversation_jobs[clean_conversation_id] = clean_id
            if clean_owner_id is not None:
                self._owner_counts[clean_owner_id] = self._owner_counts.get(clean_owner_id, 0) + 1
        future.add_done_callback(lambda _future: self._finish(clean_id))
        if future.done():
            self._finish(clean_id)
        return True

    def _finish(self, attempt_id: str) -> None:
        with self._lock:
            item = self._jobs.pop(attempt_id, None)
            if item is None:
                return
            _future, _stop_event, conversation_id, owner_id = item
            if conversation_id is not None and self._conversation_jobs.get(conversation_id) == attempt_id:
                self._conversation_jobs.pop(conversation_id, None)
            if owner_id is not None:
                remaining = self._owner_counts.get(owner_id, 0) - 1
                if remaining > 0:
                    self._owner_counts[owner_id] = remaining
                else:
                    self._owner_counts.pop(owner_id, None)

    def cancel(self, attempt_id: str) -> bool:
        with self._lock:
            item = self._jobs.get(str(attempt_id).strip())
            if item is None:
                return False
            item[1].set()
            return True

    def is_running(self, attempt_id: str) -> bool:
        with self._lock:
            item = self._jobs.get(str(attempt_id).strip())
            return item is not None and not item[0].done()

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for _future, event, _conversation_id, _owner_id in self._jobs.values():
                event.set()
        self.executor.shutdown(wait=wait, cancel_futures=True)


__all__ = ["ConversationJobManager"]
