"""SQLite commerce database lifecycle and interaction-state persistence."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from commerce_models import _normalize_reference, _now_text
from commerce_schema_bootstrap import initialize_sqlite
from persistence import open_sqlite_connection

class CommerceDatabase:
    """SQLite repository used for local/staging MVP state."""

    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        return open_sqlite_connection(self.path, busy_timeout_ms=30_000)

    @staticmethod
    def begin_write(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        return isinstance(error, sqlite3.IntegrityError)

    def initialize(self) -> None:
        initialize_sqlite(self)

    def mark_update_seen(self, update_id: int) -> bool:
        """Durably deduplicate Telegram updates across process restarts.

        Telegram may redeliver an update after a transient failure or deploy.
        Keeping the insert in the same SQLite database as the commerce state
        makes the check restart-safe and lets the transport acknowledge a
        duplicate without running its handler twice.
        """
        with self.connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO telegram_updates (update_id, received_at) VALUES (?, ?)",
                    (int(update_id), _now_text()),
                )
            except Exception as exc:
                if self.is_integrity_error(exc):
                    return False
                raise
        return True

    @staticmethod
    def _seed_plans(connection: Any) -> None:
        plans = (
            ("basic_50gb", "50 GB", 3000, "MMK", 50_000_000_000, 30),
            ("standard_100gb", "100 GB", 6000, "MMK", 100_000_000_000, 30),
            ("wallet_topup", "Wallet Top-up", 0, "MMK", None, 1),
        )
        for plan in plans:
            connection.execute(
                """INSERT INTO plans
                   (code, name, price_minor, currency, quota_bytes, duration_days)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(code) DO NOTHING""",
                plan,
            )
        connection.execute(
            "UPDATE plans SET name = '50 GB', price_minor = 3000, currency = 'MMK', quota_bytes = ?, duration_days = 30, active = 1 WHERE code = 'basic_50gb'",
            (50_000_000_000,),
        )
        connection.execute(
            "UPDATE plans SET name = '100 GB', price_minor = 6000, currency = 'MMK', quota_bytes = ?, duration_days = 30, active = 1 WHERE code = 'standard_100gb'",
            (100_000_000_000,),
        )
        connection.execute(
            "UPDATE plans SET name = 'Wallet Top-up', price_minor = 0, currency = 'MMK', "
            "quota_bytes = NULL, duration_days = 1, active = 0 WHERE code = 'wallet_topup'"
        )

    def save_interaction_state(
        self,
        telegram_id: int,
        state_key: str,
        payload: dict[str, Any],
        expires_at: str,
    ) -> None:
        """Persist short-lived Telegram input state across process restarts.

        Only workflow metadata (never receipt images, access URLs, or secrets)
        belongs here. The transport keeps a hot in-memory copy; this table is
        the restart/retry safety net for a reply arriving after a deploy.
        """
        if not isinstance(payload, dict):
            raise ValueError("interaction state payload must be an object")
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        if len(encoded) > 4096:
            raise ValueError("interaction state payload is too large")
        key = str(state_key).strip()
        if not key or len(key) > 48:
            raise ValueError("interaction state key is invalid")
        with self.connect() as connection:
            self.begin_write(connection)
            connection.execute(
                """INSERT INTO interaction_states
                   (telegram_id, state_key, payload_json, expires_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(telegram_id, state_key) DO UPDATE SET
                     payload_json = excluded.payload_json,
                     expires_at = excluded.expires_at,
                     updated_at = excluded.updated_at""",
                (int(telegram_id), key, encoded, str(expires_at), _now_text()),
            )

    def load_interaction_state(
        self, telegram_id: int, state_key: str, now: str | None = None
    ) -> dict[str, Any] | None:
        """Return one unexpired workflow state, rejecting malformed payloads."""
        with self.connect() as connection:
            row = connection.execute(
                """SELECT payload_json FROM interaction_states
                   WHERE telegram_id = ? AND state_key = ? AND expires_at > ?""",
                (int(telegram_id), str(state_key), str(now or _now_text())),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def clear_interaction_state(self, telegram_id: int, state_key: str) -> bool:
        with self.connect() as connection:
            deleted = connection.execute(
                "DELETE FROM interaction_states WHERE telegram_id = ? AND state_key = ?",
                (int(telegram_id), str(state_key)),
            )
        return int(getattr(deleted, "rowcount", 0) or 0) == 1

    def prune_interaction_states(self, now: str | None = None) -> int:
        with self.connect() as connection:
            deleted = connection.execute(
                "DELETE FROM interaction_states WHERE expires_at <= ?",
                (str(now or _now_text()),),
            )
        return int(getattr(deleted, "rowcount", 0) or 0)
