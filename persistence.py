"""Shared persistence primitives with explicit SQLite connection ownership.

The application keeps its existing ``Database.connect()`` and
``CommerceDatabase.connect()`` APIs. Connections returned by those APIs are still
ordinary ``sqlite3.Connection`` instances, but a context-managed connection now
closes deterministically after SQLite commits or rolls back the transaction.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


class ClosingSQLiteConnection(sqlite3.Connection):
    """A SQLite connection whose context manager always releases its handle.

    ``sqlite3.Connection.__exit__`` commits or rolls back but intentionally does
    not close. The repositories consistently use ``with database.connect()``, so
    closing here preserves that transaction behavior while preventing file-handle
    accumulation and Python 3.13 ``ResourceWarning`` noise.
    """

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def open_sqlite_connection(
    path: Path | str,
    *,
    timeout_seconds: float = 30.0,
    busy_timeout_ms: int | None = None,
) -> sqlite3.Connection:
    """Open a configured SQLite connection with deterministic context cleanup."""
    connection = sqlite3.connect(
        path,
        timeout=timeout_seconds,
        factory=ClosingSQLiteConnection,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if busy_timeout_ms is not None:
            if busy_timeout_ms < 0:
                raise ValueError("busy_timeout_ms must not be negative")
            connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        return connection
    except Exception:
        connection.close()
        raise
