"""Quota-owned commerce migration definitions."""

from __future__ import annotations

from schema_migrations import Migration
from migration_hooks import (
    _add_normalized_payment_reference_guard,
    _canonicalize_payment_provider_identity,
    _rebuild_paid_keys_for_server_identity,
)


COMMERCE_MIGRATIONS_QUOTA = (
    Migration(
        15,
        "connectivity_migration_jobs",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS connectivity_migration_jobs (
                   id TEXT PRIMARY KEY,
                   profile_id TEXT NOT NULL REFERENCES connectivity_profiles(profile_id),
                   credential_id TEXT NOT NULL REFERENCES connectivity_credentials(credential_id),
                   source_endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   target_endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   source_server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   target_server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   source_external_id TEXT NOT NULL,
                   target_external_id TEXT NOT NULL,
                   target_name TEXT NOT NULL,
                   profile_kind TEXT NOT NULL CHECK (profile_kind IN ('free', 'paid', 'trial', 'promo')),
                   telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                   quota_bytes INTEGER NOT NULL CHECK (quota_bytes > 0),
                   expires_at TEXT NOT NULL,
                   source_used_bytes INTEGER,
                   target_access_url_ciphertext TEXT,
                   status TEXT NOT NULL DEFAULT 'pending' CHECK (
                       status IN ('pending', 'creating', 'source_delete_pending', 'completed', 'failed', 'cancelled')
                   ),
                   attempts INTEGER NOT NULL DEFAULT 0,
                   next_attempt_at TEXT NOT NULL,
                   locked_at TEXT,
                   last_error TEXT,
                   requested_by INTEGER NOT NULL,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   completed_at TEXT,
                   UNIQUE (credential_id, target_endpoint_id)
               )""",
            "CREATE INDEX IF NOT EXISTS connectivity_migrations_due ON connectivity_migration_jobs(status, next_attempt_at)",
            "CREATE INDEX IF NOT EXISTS connectivity_migrations_source ON connectivity_migration_jobs(source_endpoint_id, status)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS connectivity_migration_jobs (
                   id TEXT PRIMARY KEY,
                   profile_id TEXT NOT NULL REFERENCES connectivity_profiles(profile_id),
                   credential_id TEXT NOT NULL REFERENCES connectivity_credentials(credential_id),
                   source_endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   target_endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   source_server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   target_server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   source_external_id TEXT NOT NULL,
                   target_external_id TEXT NOT NULL,
                   target_name TEXT NOT NULL,
                   profile_kind TEXT NOT NULL CHECK (profile_kind IN ('free', 'paid', 'trial', 'promo')),
                   telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                   quota_bytes BIGINT NOT NULL CHECK (quota_bytes > 0),
                   expires_at TEXT NOT NULL,
                   source_used_bytes BIGINT,
                   target_access_url_ciphertext TEXT,
                   status TEXT NOT NULL DEFAULT 'pending' CHECK (
                       status IN ('pending', 'creating', 'source_delete_pending', 'completed', 'failed', 'cancelled')
                   ),
                   attempts INTEGER NOT NULL DEFAULT 0,
                   next_attempt_at TEXT NOT NULL,
                   locked_at TEXT,
                   last_error TEXT,
                   requested_by BIGINT NOT NULL,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   completed_at TEXT,
                   UNIQUE (credential_id, target_endpoint_id)
               )""",
            "CREATE INDEX IF NOT EXISTS connectivity_migrations_due ON connectivity_migration_jobs(status, next_attempt_at)",
            "CREATE INDEX IF NOT EXISTS connectivity_migrations_source ON connectivity_migration_jobs(source_endpoint_id, status)",
        ),
    ),
    Migration(
        16,
        "fleet_enrollment_tokens",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS infrastructure_enrollments (
                   job_id TEXT PRIMARY KEY REFERENCES infrastructure_jobs(id),
                   token_hash TEXT NOT NULL UNIQUE,
                   expires_at TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'consumed', 'rejected', 'expired')),
                   payload_ciphertext TEXT,
                   received_at TEXT,
                   consumed_at TEXT,
                   last_error TEXT,
                   created_at TEXT NOT NULL
               )""",
            "CREATE INDEX IF NOT EXISTS infrastructure_enrollments_due ON infrastructure_enrollments(status, expires_at)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS infrastructure_enrollments (
                   job_id TEXT PRIMARY KEY REFERENCES infrastructure_jobs(id),
                   token_hash TEXT NOT NULL UNIQUE,
                   expires_at TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'consumed', 'rejected', 'expired')),
                   payload_ciphertext TEXT,
                   received_at TIMESTAMPTZ,
                   consumed_at TIMESTAMPTZ,
                   last_error TEXT,
                   created_at TIMESTAMPTZ NOT NULL
               )""",
            "CREATE INDEX IF NOT EXISTS infrastructure_enrollments_due ON infrastructure_enrollments(status, expires_at)",
        ),
    ),
    Migration(
        17,
        "termination_events_rls",
        # SQLite has no row-level security. The application database remains
        # protected by its file permissions and service boundary there.
        sqlite_statements=(),
        postgres_statements=(
            # No client-facing policies are created intentionally: termination
            # events are server-side audit/worker state. The trusted commerce
            # database role used by AuriX continues to access the table, while
            # Supabase anon/authenticated roles cannot read or mutate rows.
            "ALTER TABLE public.key_termination_events ENABLE ROW LEVEL SECURITY",
        ),
    ),
    Migration(
        18,
        "normalized_payment_reference_uniqueness",
        # SQLite applies the uniqueness index only after checking for legacy
        # collisions, so startup fails closed with a safe remediation message.
        sqlite_hook=_add_normalized_payment_reference_guard,
        postgres_statements=(
            """UPDATE payments
               SET normalized_reference = lower(regexp_replace(provider_reference, '[[:space:]]+', '', 'g'))""",
            """CREATE UNIQUE INDEX IF NOT EXISTS payments_normalized_reference_unique
               ON payments(lower(provider), normalized_reference)
               WHERE normalized_reference <> ''""",
        ),
    ),
    Migration(
        19,
        "canonical_payment_provider_identity",
        # The migration is deliberately separate from version 18 so
        # deployments that already applied the reference guard also receive
        # the provider canonicalization.
        sqlite_hook=_canonicalize_payment_provider_identity,
        postgres_statements=(
            """UPDATE payments
               SET provider = lower(regexp_replace(provider, '[[:space:]]+', '', 'g'))""",
        ),
    ),
    Migration(
        20,
        "managed_key_repair_observations",
        sqlite_statements=(
            "ALTER TABLE outline_remote_keys ADD COLUMN missing_observation_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE outline_remote_keys ADD COLUMN missing_since_at TEXT",
            "ALTER TABLE outline_remote_keys ADD COLUMN last_missing_at TEXT",
            """CREATE TABLE IF NOT EXISTS managed_key_repair_jobs (
                   id TEXT PRIMARY KEY,
                   kind TEXT NOT NULL CHECK (kind IN ('free', 'paid')),
                   server_id TEXT NOT NULL,
                   telegram_id INTEGER NOT NULL,
                   local_key_ref TEXT NOT NULL,
                   source_external_id TEXT NOT NULL,
                   target_external_id TEXT NOT NULL,
                   key_name TEXT NOT NULL,
                   quota_bytes INTEGER NOT NULL CHECK (quota_bytes > 0),
                   used_bytes INTEGER,
                   expires_at TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'pending' CHECK (
                       status IN ('pending', 'running', 'done', 'failed', 'manual', 'cancelled')
                   ),
                   attempts INTEGER NOT NULL DEFAULT 0,
                   next_attempt_at TEXT NOT NULL,
                   locked_at TEXT,
                   last_error TEXT,
                   observed_at TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   completed_at TEXT,
                   UNIQUE (server_id, kind, local_key_ref)
               )""",
            "CREATE INDEX IF NOT EXISTS managed_key_repairs_due ON managed_key_repair_jobs(status, next_attempt_at)",
        ),
        postgres_statements=(
            "ALTER TABLE outline_remote_keys ADD COLUMN IF NOT EXISTS missing_observation_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE outline_remote_keys ADD COLUMN IF NOT EXISTS missing_since_at TIMESTAMPTZ",
            "ALTER TABLE outline_remote_keys ADD COLUMN IF NOT EXISTS last_missing_at TIMESTAMPTZ",
            """CREATE TABLE IF NOT EXISTS managed_key_repair_jobs (
                   id TEXT PRIMARY KEY,
                   kind TEXT NOT NULL CHECK (kind IN ('free', 'paid')),
                   server_id TEXT NOT NULL,
                   telegram_id BIGINT NOT NULL,
                   local_key_ref TEXT NOT NULL,
                   source_external_id TEXT NOT NULL,
                   target_external_id TEXT NOT NULL,
                   key_name TEXT NOT NULL,
                   quota_bytes BIGINT NOT NULL CHECK (quota_bytes > 0),
                   used_bytes BIGINT,
                   expires_at TIMESTAMPTZ NOT NULL,
                   status TEXT NOT NULL DEFAULT 'pending' CHECK (
                       status IN ('pending', 'running', 'done', 'failed', 'manual', 'cancelled')
                   ),
                   attempts INTEGER NOT NULL DEFAULT 0,
                   next_attempt_at TIMESTAMPTZ NOT NULL,
                   locked_at TIMESTAMPTZ,
                   last_error TEXT,
                   observed_at TIMESTAMPTZ NOT NULL,
                   created_at TIMESTAMPTZ NOT NULL,
                   completed_at TIMESTAMPTZ,
                   UNIQUE (server_id, kind, local_key_ref)
               )""",
            "CREATE INDEX IF NOT EXISTS managed_key_repairs_due ON managed_key_repair_jobs(status, next_attempt_at)",
        ),
    ),
    Migration(
        21,
        "durable_usage_snapshots",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS usage_snapshots (
                   id TEXT PRIMARY KEY,
                   telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                   entitlement_kind TEXT NOT NULL CHECK (
                       entitlement_kind IN ('free', 'paid', 'trial', 'promo')
                   ),
                   local_key_ref TEXT NOT NULL,
                   server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   outline_key_id TEXT NOT NULL,
                   observed_at TEXT NOT NULL,
                   used_bytes INTEGER NOT NULL CHECK (used_bytes >= 0),
                   quota_bytes INTEGER NOT NULL CHECK (quota_bytes > 0),
                   source TEXT NOT NULL DEFAULT 'outline_metrics',
                   UNIQUE (server_id, entitlement_kind, local_key_ref, observed_at)
               )""",
            """CREATE INDEX IF NOT EXISTS usage_snapshots_user_time
               ON usage_snapshots(telegram_id, observed_at)""",
            """CREATE INDEX IF NOT EXISTS usage_snapshots_server_time
               ON usage_snapshots(server_id, observed_at)""",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS usage_snapshots (
                   id TEXT PRIMARY KEY,
                   telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                   entitlement_kind TEXT NOT NULL CHECK (
                       entitlement_kind IN ('free', 'paid', 'trial', 'promo')
                   ),
                   local_key_ref TEXT NOT NULL,
                   server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   outline_key_id TEXT NOT NULL,
                   observed_at TIMESTAMPTZ NOT NULL,
                   used_bytes BIGINT NOT NULL CHECK (used_bytes >= 0),
                   quota_bytes BIGINT NOT NULL CHECK (quota_bytes > 0),
                   source TEXT NOT NULL DEFAULT 'outline_metrics',
                   UNIQUE (server_id, entitlement_kind, local_key_ref, observed_at)
               )""",
            """CREATE INDEX IF NOT EXISTS usage_snapshots_user_time
               ON usage_snapshots(telegram_id, observed_at)""",
            """CREATE INDEX IF NOT EXISTS usage_snapshots_server_time
               ON usage_snapshots(server_id, observed_at)""",
        ),
    ),
)
