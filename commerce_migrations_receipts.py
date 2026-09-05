"""Receipt-owned commerce migration definitions."""

from __future__ import annotations

from schema_migrations import Migration
from migration_hooks import (
    _add_normalized_payment_reference_guard,
    _canonicalize_payment_provider_identity,
    _rebuild_paid_keys_for_server_identity,
)


COMMERCE_MIGRATIONS_RECEIPTS = (
    Migration(1, "legacy_commerce_schema"),
    Migration(
        2,
        "receipt_control_and_diagnostics",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS receipt_verification_policy (
                   id INTEGER PRIMARY KEY CHECK (id = 1),
                   mode TEXT NOT NULL CHECK (mode IN ('manual', 'assisted')),
                   version INTEGER NOT NULL DEFAULT 1,
                   updated_by INTEGER,
                   updated_at TEXT NOT NULL,
                   change_reason TEXT
               )""",
            """INSERT OR IGNORE INTO receipt_verification_policy
               (id, mode, version, updated_at, change_reason)
               VALUES (1, 'manual', 1, '1970-01-01T00:00:00+00:00', 'safe migration default')""",
            """CREATE TABLE IF NOT EXISTS receipt_diagnostic_runs (
                   id TEXT PRIMARY KEY,
                   admin_id INTEGER NOT NULL,
                   status TEXT NOT NULL CHECK (status IN ('running', 'passed', 'failed')),
                   result_json TEXT NOT NULL DEFAULT '{}',
                   started_at TEXT NOT NULL,
                   completed_at TEXT
               )""",
            "CREATE INDEX IF NOT EXISTS receipt_diagnostic_recent ON receipt_diagnostic_runs(started_at)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS receipt_verification_policy (
                   id INTEGER PRIMARY KEY CHECK (id = 1),
                   mode TEXT NOT NULL CHECK (mode IN ('manual', 'assisted')),
                   version INTEGER NOT NULL DEFAULT 1,
                   updated_by BIGINT,
                   updated_at TEXT NOT NULL,
                   change_reason TEXT
               )""",
            """INSERT INTO receipt_verification_policy
               (id, mode, version, updated_at, change_reason)
               VALUES (1, 'manual', 1, '1970-01-01T00:00:00+00:00', 'safe migration default')
               ON CONFLICT(id) DO NOTHING""",
            """CREATE TABLE IF NOT EXISTS receipt_diagnostic_runs (
                   id TEXT PRIMARY KEY,
                   admin_id BIGINT NOT NULL,
                   status TEXT NOT NULL CHECK (status IN ('running', 'passed', 'failed')),
                   result_json TEXT NOT NULL DEFAULT '{}',
                   started_at TEXT NOT NULL,
                   completed_at TEXT
               )""",
            "CREATE INDEX IF NOT EXISTS receipt_diagnostic_recent ON receipt_diagnostic_runs(started_at)",
        ),
    ),
    Migration(
        3,
        "receipt_extraction_jobs",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS receipt_extraction_jobs (
                   id TEXT PRIMARY KEY,
                   evidence_id TEXT NOT NULL UNIQUE REFERENCES payment_evidence(id),
                   status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'failed')),
                   attempts INTEGER NOT NULL DEFAULT 0,
                   next_attempt_at TEXT NOT NULL,
                   locked_at TEXT,
                   last_error TEXT,
                   created_at TEXT NOT NULL,
                   completed_at TEXT
               )""",
            """CREATE INDEX IF NOT EXISTS receipt_extraction_due
               ON receipt_extraction_jobs(status, next_attempt_at)""",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS receipt_extraction_jobs (
                   id TEXT PRIMARY KEY,
                   evidence_id TEXT NOT NULL UNIQUE REFERENCES payment_evidence(id),
                   status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'failed')),
                   attempts INTEGER NOT NULL DEFAULT 0,
                   next_attempt_at TEXT NOT NULL,
                   locked_at TEXT,
                   last_error TEXT,
                   created_at TEXT NOT NULL,
                   completed_at TEXT
               )""",
            """CREATE INDEX IF NOT EXISTS receipt_extraction_due
               ON receipt_extraction_jobs(status, next_attempt_at)""",
        ),
    ),
)
