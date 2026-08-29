"""SQLite repository for free and trial entitlement state."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from entitlements import TRIAL_LIMIT_BYTES, UTC
from migrations import FREE_ACCESS_MIGRATIONS, apply_migrations
from persistence import open_sqlite_connection


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        return open_sqlite_connection(self.path)

    @staticmethod
    def begin_write(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    first_name TEXT NOT NULL DEFAULT '',
                    username TEXT,
                    last_claim_at TEXT,
                    trial_claimed_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    outline_key_id TEXT NOT NULL UNIQUE,
                    key_type TEXT NOT NULL DEFAULT 'daily_free'
                        CHECK (key_type IN ('daily_free', 'monthly_trial', 'paid')),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    data_limit_bytes INTEGER NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('active', 'revoked', 'revoke_failed')),
                    quota_warning_percent INTEGER
                );
                CREATE INDEX IF NOT EXISTS keys_expiry
                    ON keys(status, expires_at);
                CREATE TABLE IF NOT EXISTS maintenance_heartbeat (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_started_at TEXT,
                    last_completed_at TEXT,
                    last_success_at TEXT,
                    last_stage TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telegram_updates (
                    update_id INTEGER PRIMARY KEY,
                    received_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS key_termination_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_id INTEGER NOT NULL REFERENCES keys(id),
                    telegram_id INTEGER NOT NULL,
                    outline_key_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    used_bytes INTEGER,
                    quota_bytes INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    remote_state TEXT NOT NULL,
                    delete_attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    deletion_verified_at TEXT,
                    user_notice_state TEXT,
                    admin_notice_state TEXT,
                    UNIQUE(key_id, reason)
                );
                CREATE INDEX IF NOT EXISTS key_termination_pending
                    ON key_termination_events(remote_state, detected_at);
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    telegram_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    access_url_ciphertext TEXT,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'sent', 'failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    dead_lettered_at TEXT
                );
                CREATE INDEX IF NOT EXISTS notifications_due
                    ON notifications(status, next_attempt_at);
                CREATE TABLE IF NOT EXISTS telegram_command_scopes (
                    chat_id INTEGER PRIMARY KEY,
                    configured_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_action_challenges (
                    token_hash TEXT PRIMARY KEY,
                    admin_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    command TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    state_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'consumed', 'cancelled')),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    cancelled_at TEXT
                );
                CREATE INDEX IF NOT EXISTS admin_action_challenges_expiry
                    ON admin_action_challenges(status, expires_at);
                """
            )
            user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
            if "trial_claimed_at" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN trial_claimed_at TEXT")
            if "username" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN username TEXT")
            key_columns = {row[1] for row in connection.execute("PRAGMA table_info(keys)")}
            if "key_type" not in key_columns:
                connection.execute(
                    "ALTER TABLE keys ADD COLUMN key_type TEXT NOT NULL DEFAULT 'daily_free'"
                )
            connection.execute(
                """UPDATE keys SET key_type = 'monthly_trial'
                   WHERE key_type = 'daily_free' AND data_limit_bytes >= ?""",
                (TRIAL_LIMIT_BYTES,),
            )
            if "last_usage_bytes" not in key_columns:
                connection.execute("ALTER TABLE keys ADD COLUMN last_usage_bytes INTEGER")
            if "quota_reason" not in key_columns:
                connection.execute("ALTER TABLE keys ADD COLUMN quota_reason TEXT")
            if "quota_warning_percent" not in key_columns:
                connection.execute("ALTER TABLE keys ADD COLUMN quota_warning_percent INTEGER")
            apply_migrations(
                connection,
                component="free_access",
                dialect="sqlite",
                migrations=FREE_ACCESS_MIGRATIONS,
            )

    def maintenance_heartbeat(
        self,
        *,
        started_at: str | None = None,
        completed_at: str | None = None,
        success_at: str | None = None,
        stage: str | None = None,
        error: str | None = None,
    ) -> None:
        """Persist the latest housekeeping lifecycle for health checks."""
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO maintenance_heartbeat
                   (id, last_started_at, last_completed_at, last_success_at,
                    last_stage, last_error, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     last_started_at = COALESCE(excluded.last_started_at, maintenance_heartbeat.last_started_at),
                     last_completed_at = COALESCE(excluded.last_completed_at, maintenance_heartbeat.last_completed_at),
                     last_success_at = COALESCE(excluded.last_success_at, maintenance_heartbeat.last_success_at),
                     last_stage = excluded.last_stage,
                     last_error = excluded.last_error,
                     updated_at = excluded.updated_at""",
                (started_at, completed_at, success_at, stage, error, now),
            )

    def get_maintenance_heartbeat(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM maintenance_heartbeat WHERE id = 1").fetchone()
        return dict(row) if row is not None else None

    def mark_update_seen(self, update_id: int) -> bool:
        """Durably dedupe Telegram updates across restarts."""
        with self.connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO telegram_updates (update_id, received_at) VALUES (?, ?)",
                    (int(update_id), datetime.now(UTC).isoformat()),
                )
            except Exception as exc:
                if self.is_integrity_error(exc):
                    return False
                raise
        return True

    def list_command_scope_ids(self) -> set[int]:
        """Return chat-specific command scopes previously configured by this bot."""
        with self.connect() as connection:
            rows = connection.execute("SELECT chat_id FROM telegram_command_scopes").fetchall()
        return {int(row[0]) for row in rows}

    def record_command_scope(self, chat_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO telegram_command_scopes (chat_id, configured_at)
                   VALUES (?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET configured_at = excluded.configured_at""",
                (int(chat_id), datetime.now(UTC).isoformat()),
            )

    def remove_command_scope(self, chat_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM telegram_command_scopes WHERE chat_id = ?",
                (int(chat_id),),
            )

    def create_admin_challenge(
        self,
        token_hash: str,
        admin_id: int,
        chat_id: int,
        command: str,
        args_json: str,
        state_fingerprint: str,
        created_at: str,
        expires_at: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO admin_action_challenges
                   (token_hash, admin_id, chat_id, command, args_json,
                    state_fingerprint, status, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    token_hash,
                    int(admin_id),
                    int(chat_id),
                    command,
                    args_json,
                    state_fingerprint,
                    created_at,
                    expires_at,
                ),
            )

    def consume_admin_challenge(
        self,
        token_hash: str,
        admin_id: int,
        chat_id: int,
        state_fingerprint: str,
        now: str,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            self.begin_write(connection)
            row = connection.execute(
                """SELECT command, args_json, state_fingerprint
                   FROM admin_action_challenges
                   WHERE token_hash = ? AND admin_id = ? AND chat_id = ?
                     AND state_fingerprint = ? AND status = 'pending'
                     AND expires_at > ?""",
                (token_hash, int(admin_id), int(chat_id), state_fingerprint, now),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """UPDATE admin_action_challenges
                   SET status = 'consumed', consumed_at = ?
                   WHERE token_hash = ? AND status = 'pending'""",
                (now, token_hash),
            )
            if getattr(updated, "rowcount", 1) != 1:
                return None
            result = dict(row)
        try:
            result["args"] = json.loads(result.pop("args_json") or "[]")
        except json.JSONDecodeError:
            return None
        return result

    def cancel_admin_challenge(
        self, token_hash: str, admin_id: int, chat_id: int, now: str
    ) -> bool:
        with self.connect() as connection:
            updated = connection.execute(
                """UPDATE admin_action_challenges
                   SET status = 'cancelled', cancelled_at = ?
                   WHERE token_hash = ? AND admin_id = ? AND chat_id = ?
                     AND status = 'pending'""",
                (now, token_hash, int(admin_id), int(chat_id)),
            )
        return getattr(updated, "rowcount", 1) == 1

    def prune_admin_challenges(self, now: str, retention_days: int = 30) -> int:
        cutoff = (
            datetime.fromisoformat(now) - timedelta(days=max(1, int(retention_days)))
        ).isoformat()
        with self.connect() as connection:
            deleted = connection.execute(
                """DELETE FROM admin_action_challenges
                   WHERE (status = 'pending' AND expires_at <= ?)
                      OR (status <> 'pending' AND created_at < ?)""",
                (now, cutoff),
            )
        return int(getattr(deleted, "rowcount", 0) or 0)

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        return isinstance(error, sqlite3.IntegrityError)
