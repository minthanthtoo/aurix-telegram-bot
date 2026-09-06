"""Versioned fresh-create/adoption schema for free-access databases."""

from __future__ import annotations

import sqlite3

from schema_migrations import Migration


def _split_sqlite(script: str) -> tuple[str, ...]:
    statements: list[str] = []
    buffer = ""
    for character in script:
        buffer += character
        if character == ";" and sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        statements.append(buffer.strip())
    return tuple(statements)


def _split_postgres(script: str) -> tuple[str, ...]:
    statements: list[str] = []
    buffer = ""
    quoted = False
    index = 0
    while index < len(script):
        character = script[index]
        buffer += character
        if character == "'":
            if quoted and index + 1 < len(script) and script[index + 1] == "'":
                buffer += script[index + 1]
                index += 1
            else:
                quoted = not quoted
        elif character == ";" and not quoted:
            statement = buffer[:-1].strip()
            if statement:
                statements.append(statement)
            buffer = ""
        index += 1
    if buffer.strip():
        statements.append(buffer.strip())
    return tuple(statements)


FREE_ACCESS_BASE_SQLITE = "\n                PRAGMA journal_mode = WAL;\n                CREATE TABLE IF NOT EXISTS users (\n                    telegram_id INTEGER PRIMARY KEY,\n                    first_name TEXT NOT NULL DEFAULT '',\n                    username TEXT,\n                    last_claim_at TEXT,\n                    trial_claimed_at TEXT,\n                    created_at TEXT NOT NULL\n                );\n                CREATE TABLE IF NOT EXISTS keys (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),\n                    outline_key_id TEXT NOT NULL,\n                    key_type TEXT NOT NULL DEFAULT 'daily_free'\n                        CHECK (key_type IN ('daily_free', 'monthly_trial', 'paid')),\n                    created_at TEXT NOT NULL,\n                    expires_at TEXT NOT NULL,\n                    data_limit_bytes INTEGER NOT NULL,\n                    status TEXT NOT NULL\n                        CHECK (status IN ('active', 'revoked', 'revoke_failed')),\n                    last_usage_observed_at TEXT,\n                    quota_warning_percent INTEGER\n                );\n                CREATE INDEX IF NOT EXISTS keys_expiry\n                    ON keys(status, expires_at);\n                CREATE TABLE IF NOT EXISTS maintenance_heartbeat (\n                    id INTEGER PRIMARY KEY CHECK (id = 1),\n                    last_started_at TEXT,\n                    last_completed_at TEXT,\n                    last_success_at TEXT,\n                    last_stage TEXT,\n                    last_error TEXT,\n                    updated_at TEXT NOT NULL\n                );\n                CREATE TABLE IF NOT EXISTS telegram_updates (\n                    update_id INTEGER PRIMARY KEY,\n                    received_at TEXT NOT NULL\n                );\n                CREATE TABLE IF NOT EXISTS key_termination_events (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    key_id INTEGER NOT NULL REFERENCES keys(id),\n                    telegram_id INTEGER NOT NULL,\n                    outline_key_id TEXT NOT NULL,\n                    reason TEXT NOT NULL,\n                    used_bytes INTEGER,\n                    quota_bytes INTEGER NOT NULL,\n                    expires_at TEXT NOT NULL,\n                    detected_at TEXT NOT NULL,\n                    remote_state TEXT NOT NULL,\n                    delete_attempts INTEGER NOT NULL DEFAULT 0,\n                    last_error TEXT,\n                    deletion_verified_at TEXT,\n                    user_notice_state TEXT,\n                    admin_notice_state TEXT,\n                    UNIQUE(key_id, reason)\n                );\n                CREATE INDEX IF NOT EXISTS key_termination_pending\n                    ON key_termination_events(remote_state, detected_at);\n                CREATE TABLE IF NOT EXISTS notifications (\n                    id TEXT PRIMARY KEY,\n                    dedupe_key TEXT NOT NULL UNIQUE,\n                    telegram_id INTEGER NOT NULL,\n                    kind TEXT NOT NULL,\n                    text TEXT NOT NULL,\n                    access_url_ciphertext TEXT,\n                    status TEXT NOT NULL DEFAULT 'pending'\n                        CHECK (status IN ('pending', 'sent', 'failed')),\n                    attempts INTEGER NOT NULL DEFAULT 0,\n                    next_attempt_at TEXT NOT NULL,\n                    created_at TEXT NOT NULL,\n                    sent_at TEXT,\n                    dead_lettered_at TEXT\n                );\n                CREATE INDEX IF NOT EXISTS notifications_due\n                    ON notifications(status, next_attempt_at);\n                CREATE TABLE IF NOT EXISTS telegram_command_scopes (\n                    chat_id INTEGER PRIMARY KEY,\n                    configured_at TEXT NOT NULL\n                );\n                CREATE TABLE IF NOT EXISTS admin_action_challenges (\n                    token_hash TEXT PRIMARY KEY,\n                    admin_id INTEGER NOT NULL,\n                    chat_id INTEGER NOT NULL,\n                    command TEXT NOT NULL,\n                    args_json TEXT NOT NULL,\n                    state_fingerprint TEXT NOT NULL,\n                    status TEXT NOT NULL DEFAULT 'pending'\n                        CHECK (status IN ('pending', 'consumed', 'cancelled')),\n                    created_at TEXT NOT NULL,\n                    expires_at TEXT NOT NULL,\n                    consumed_at TEXT,\n                    cancelled_at TEXT\n                );\n                CREATE INDEX IF NOT EXISTS admin_action_challenges_expiry\n                    ON admin_action_challenges(status, expires_at);\n                "
FREE_ACCESS_BASE_POSTGRES = None

FREE_ACCESS_BASE_MIGRATIONS = (
    Migration(
        1,
        "base_schema_adoption",
        sqlite_statements=_split_sqlite(FREE_ACCESS_BASE_SQLITE),
        postgres_statements=(),
    ),
)
