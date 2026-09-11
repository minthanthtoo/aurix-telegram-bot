"""Bounded in-process workers for durable browser conversation attempts."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from collections.abc import Callable
from typing import Any


JobCallback = Callable[[threading.Event], Any]


class ConversationJobManager:
    """Run at most one live worker per attempt with explicit cancellation.

    Attempt state is durable in :mod:`aurix_ai.conversations`; this manager is
    only the process-local execution handle. A restart therefore loses no
    committed state and cannot silently resubmit an interrupted attempt.
    """

    def __init__(self, *, max_workers: int = 4) -> None:
        self.executor = ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), 16)))
        self._lock = threading.Lock()
        self._jobs: dict[str, tuple[Future[Any], threading.Event]] = {}
        self._closed = False

    def submit(self, attempt_id: str, callback: JobCallback) -> bool:
        clean_id = str(attempt_id).strip()
        if not clean_id or not callable(callback):
            raise ValueError("attempt_id and callback are required")
        with self._lock:
            if self._closed:
                raise RuntimeError("conversation job manager is closed")
            if clean_id in self._jobs:
                return False
            stop_event = threading.Event()
            future = self.executor.submit(callback, stop_event)
            self._jobs[clean_id] = (future, stop_event)
            future.add_done_callback(lambda _future: self._finish(clean_id))
            return True

    def _finish(self, attempt_id: str) -> None:
        with self._lock:
            self._jobs.pop(attempt_id, None)

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
            for _future, event in self._jobs.values():
                event.set()
        self.executor.shutdown(wait=wait, cancel_futures=True)


__all__ = ["ConversationJobManager"]
