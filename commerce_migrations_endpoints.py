"""Endpoints-owned commerce migration definitions."""

from __future__ import annotations

from schema_migrations import Migration
from migration_hooks import (
    _add_normalized_payment_reference_guard,
    _canonicalize_payment_provider_identity,
    _rebuild_paid_keys_for_server_identity,
)


COMMERCE_MIGRATIONS_ENDPOINTS = (
    Migration(
        23,
        "accounts_entitlements_devices_and_leases",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS accounts (
                   account_id TEXT PRIMARY KEY,
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'blocked', 'closed')),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS account_identities (
                   account_id TEXT NOT NULL REFERENCES accounts(account_id),
                   identity_type TEXT NOT NULL CHECK (identity_type IN ('telegram')),
                   identity_value TEXT NOT NULL,
                   verified_at TEXT,
                   created_at TEXT NOT NULL,
                   PRIMARY KEY (identity_type, identity_value)
               )""",
            """CREATE INDEX IF NOT EXISTS account_identities_account
               ON account_identities(account_id)""",
            """CREATE TABLE IF NOT EXISTS devices (
                   device_id TEXT PRIMARY KEY,
                   account_id TEXT NOT NULL REFERENCES accounts(account_id),
                   public_key TEXT NOT NULL UNIQUE,
                   label TEXT NOT NULL DEFAULT '',
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked', 'pending')),
                   created_at TEXT NOT NULL,
                   last_seen_at TEXT,
                   revoked_at TEXT
               )""",
            """CREATE TABLE IF NOT EXISTS pairing_tokens (
                   token_hash TEXT PRIMARY KEY,
                   account_id TEXT NOT NULL REFERENCES accounts(account_id),
                   requested_by INTEGER,
                   expires_at TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'consumed', 'revoked', 'expired')),
                   created_at TEXT NOT NULL,
                   consumed_at TEXT
               )""",
            """CREATE INDEX IF NOT EXISTS pairing_tokens_due
               ON pairing_tokens(status, expires_at)""",
            """CREATE TABLE IF NOT EXISTS device_sessions (
                   session_id TEXT PRIMARY KEY,
                   device_id TEXT NOT NULL REFERENCES devices(device_id),
                   manifest_version INTEGER NOT NULL DEFAULT 1,
                   created_at TEXT NOT NULL,
                   last_seen_at TEXT,
                   expires_at TEXT NOT NULL,
                   revoked_at TEXT
               )""",
            """CREATE INDEX IF NOT EXISTS device_sessions_device
               ON device_sessions(device_id, expires_at)""",
            """CREATE TABLE IF NOT EXISTS device_revocation_epochs (
                   account_id TEXT PRIMARY KEY REFERENCES accounts(account_id),
                   epoch INTEGER NOT NULL DEFAULT 0 CHECK (epoch >= 0),
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS entitlements (
                   entitlement_id TEXT PRIMARY KEY,
                   account_id TEXT NOT NULL REFERENCES accounts(account_id),
                   subscription_id TEXT UNIQUE REFERENCES subscriptions(id),
                   kind TEXT NOT NULL CHECK (kind IN ('free', 'paid', 'trial', 'promo')),
                   quota_bytes INTEGER NOT NULL CHECK (quota_bytes > 0),
                   expires_at TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('pending', 'active', 'expired', 'revoked', 'cancelled')),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE INDEX IF NOT EXISTS entitlements_account_status
               ON entitlements(account_id, status, expires_at)""",
            """CREATE TABLE IF NOT EXISTS credential_generations (
                   generation_id TEXT PRIMARY KEY,
                   entitlement_id TEXT NOT NULL REFERENCES entitlements(entitlement_id),
                   endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   credential_id TEXT REFERENCES connectivity_credentials(credential_id),
                   generation_no INTEGER NOT NULL CHECK (generation_no > 0),
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('pending', 'active', 'revoked', 'failed')),
                   created_at TEXT NOT NULL,
                   revoked_at TEXT,
                   UNIQUE(entitlement_id, generation_no),
                   UNIQUE(entitlement_id, endpoint_id, generation_no)
               )""",
            """CREATE TABLE IF NOT EXISTS quota_leases (
                   lease_id TEXT PRIMARY KEY,
                   entitlement_id TEXT NOT NULL REFERENCES entitlements(entitlement_id),
                   generation_id TEXT REFERENCES credential_generations(generation_id),
                   endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   lease_bytes INTEGER NOT NULL CHECK (lease_bytes > 0),
                   used_bytes INTEGER NOT NULL DEFAULT 0 CHECK (used_bytes >= 0),
                   expires_at TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released', 'expired', 'exhausted')),
                   created_at TEXT NOT NULL,
                   released_at TEXT
               )""",
            """CREATE INDEX IF NOT EXISTS quota_leases_entitlement
               ON quota_leases(entitlement_id, status, expires_at)""",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS accounts (
                   account_id TEXT PRIMARY KEY,
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'blocked', 'closed')),
                   created_at TIMESTAMPTZ NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS account_identities (
                   account_id TEXT NOT NULL REFERENCES accounts(account_id),
                   identity_type TEXT NOT NULL CHECK (identity_type IN ('telegram')),
                   identity_value TEXT NOT NULL,
                   verified_at TIMESTAMPTZ,
                   created_at TIMESTAMPTZ NOT NULL,
                   PRIMARY KEY (identity_type, identity_value)
               )""",
            """CREATE INDEX IF NOT EXISTS account_identities_account
               ON account_identities(account_id)""",
            """CREATE TABLE IF NOT EXISTS devices (
                   device_id TEXT PRIMARY KEY,
                   account_id TEXT NOT NULL REFERENCES accounts(account_id),
                   public_key TEXT NOT NULL UNIQUE,
                   label TEXT NOT NULL DEFAULT '',
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked', 'pending')),
                   created_at TIMESTAMPTZ NOT NULL,
                   last_seen_at TIMESTAMPTZ,
                   revoked_at TIMESTAMPTZ
               )""",
            """CREATE TABLE IF NOT EXISTS pairing_tokens (
                   token_hash TEXT PRIMARY KEY,
                   account_id TEXT NOT NULL REFERENCES accounts(account_id),
                   requested_by BIGINT,
                   expires_at TIMESTAMPTZ NOT NULL,
                   status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'consumed', 'revoked', 'expired')),
                   created_at TIMESTAMPTZ NOT NULL,
                   consumed_at TIMESTAMPTZ
               )""",
            """CREATE INDEX IF NOT EXISTS pairing_tokens_due
               ON pairing_tokens(status, expires_at)""",
            """CREATE TABLE IF NOT EXISTS device_sessions (
                   session_id TEXT PRIMARY KEY,
                   device_id TEXT NOT NULL REFERENCES devices(device_id),
                   manifest_version INTEGER NOT NULL DEFAULT 1,
                   created_at TIMESTAMPTZ NOT NULL,
                   last_seen_at TIMESTAMPTZ,
                   expires_at TIMESTAMPTZ NOT NULL,
                   revoked_at TIMESTAMPTZ
               )""",
            """CREATE INDEX IF NOT EXISTS device_sessions_device
               ON device_sessions(device_id, expires_at)""",
            """CREATE TABLE IF NOT EXISTS device_revocation_epochs (
                   account_id TEXT PRIMARY KEY REFERENCES accounts(account_id),
                   epoch INTEGER NOT NULL DEFAULT 0 CHECK (epoch >= 0),
                   updated_at TIMESTAMPTZ NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS entitlements (
                   entitlement_id TEXT PRIMARY KEY,
                   account_id TEXT NOT NULL REFERENCES accounts(account_id),
                   subscription_id TEXT UNIQUE REFERENCES subscriptions(id),
                   kind TEXT NOT NULL CHECK (kind IN ('free', 'paid', 'trial', 'promo')),
                   quota_bytes BIGINT NOT NULL CHECK (quota_bytes > 0),
                   expires_at TIMESTAMPTZ NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('pending', 'active', 'expired', 'revoked', 'cancelled')),
                   created_at TIMESTAMPTZ NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL
               )""",
            """CREATE INDEX IF NOT EXISTS entitlements_account_status
               ON entitlements(account_id, status, expires_at)""",
            """CREATE TABLE IF NOT EXISTS credential_generations (
                   generation_id TEXT PRIMARY KEY,
                   entitlement_id TEXT NOT NULL REFERENCES entitlements(entitlement_id),
                   endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   credential_id TEXT REFERENCES connectivity_credentials(credential_id),
                   generation_no INTEGER NOT NULL CHECK (generation_no > 0),
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('pending', 'active', 'revoked', 'failed')),
                   created_at TIMESTAMPTZ NOT NULL,
                   revoked_at TIMESTAMPTZ,
                   UNIQUE(entitlement_id, generation_no),
                   UNIQUE(entitlement_id, endpoint_id, generation_no)
               )""",
            """CREATE TABLE IF NOT EXISTS quota_leases (
                   lease_id TEXT PRIMARY KEY,
                   entitlement_id TEXT NOT NULL REFERENCES entitlements(entitlement_id),
                   generation_id TEXT REFERENCES credential_generations(generation_id),
                   endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   lease_bytes BIGINT NOT NULL CHECK (lease_bytes > 0),
                   used_bytes BIGINT NOT NULL DEFAULT 0 CHECK (used_bytes >= 0),
                   expires_at TIMESTAMPTZ NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released', 'expired', 'exhausted')),
                   created_at TIMESTAMPTZ NOT NULL,
                   released_at TIMESTAMPTZ
               )""",
            """CREATE INDEX IF NOT EXISTS quota_leases_entitlement
               ON quota_leases(entitlement_id, status, expires_at)""",
        ),
    ),
    Migration(
        24,
        "entitlement_source_identity",
        sqlite_statements=(
            "ALTER TABLE entitlements ADD COLUMN source_ref TEXT",
            "CREATE UNIQUE INDEX IF NOT EXISTS entitlements_source_ref_unique ON entitlements(source_ref) WHERE source_ref IS NOT NULL",
        ),
        postgres_statements=(
            "ALTER TABLE entitlements ADD COLUMN IF NOT EXISTS source_ref TEXT",
            "CREATE UNIQUE INDEX IF NOT EXISTS entitlements_source_ref_unique ON entitlements(source_ref) WHERE source_ref IS NOT NULL",
        ),
    ),
    Migration(
        25,
        "aggregate_entitlement_usage_ledger",
        sqlite_statements=(
            "ALTER TABLE entitlements ADD COLUMN consumed_bytes INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE entitlements ADD COLUMN quota_exhausted_at TEXT",
            "UPDATE entitlements SET consumed_bytes = COALESCE((SELECT SUM(used_bytes) FROM quota_leases q WHERE q.entitlement_id = entitlements.entitlement_id), 0)",
            "CREATE INDEX IF NOT EXISTS entitlements_quota_status ON entitlements(status, quota_exhausted_at)",
            """CREATE TABLE IF NOT EXISTS entitlement_usage_epochs (
                   epoch_id TEXT PRIMARY KEY,
                   entitlement_id TEXT NOT NULL REFERENCES entitlements(entitlement_id),
                   generation_id TEXT NOT NULL REFERENCES credential_generations(generation_id),
                   endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   source_external_id TEXT NOT NULL,
                   epoch_no INTEGER NOT NULL CHECK (epoch_no > 0),
                   last_remote_bytes INTEGER NOT NULL CHECK (last_remote_bytes >= 0),
                   credited_bytes INTEGER NOT NULL DEFAULT 0 CHECK (credited_bytes >= 0),
                   reset_count INTEGER NOT NULL DEFAULT 0 CHECK (reset_count >= 0),
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'reset', 'closed')),
                   last_observed_at TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   UNIQUE(entitlement_id, generation_id, endpoint_id, source_external_id, epoch_no)
               )""",
            """CREATE INDEX IF NOT EXISTS entitlement_usage_epochs_lookup
               ON entitlement_usage_epochs(entitlement_id, generation_id, endpoint_id, source_external_id, status)""",
            """CREATE TABLE IF NOT EXISTS entitlement_usage_samples (
                   sample_id TEXT PRIMARY KEY,
                   epoch_id TEXT NOT NULL REFERENCES entitlement_usage_epochs(epoch_id),
                   entitlement_id TEXT NOT NULL REFERENCES entitlements(entitlement_id),
                   generation_id TEXT NOT NULL REFERENCES credential_generations(generation_id),
                   endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   source_external_id TEXT NOT NULL,
                   lease_id TEXT REFERENCES quota_leases(lease_id),
                   remote_bytes INTEGER NOT NULL CHECK (remote_bytes >= 0),
                   delta_bytes INTEGER NOT NULL CHECK (delta_bytes >= 0),
                   accepted INTEGER NOT NULL CHECK (accepted IN (0, 1)),
                   reason TEXT NOT NULL,
                   observed_at TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   UNIQUE(epoch_id, observed_at, remote_bytes)
               )""",
            """CREATE INDEX IF NOT EXISTS entitlement_usage_samples_entitlement
               ON entitlement_usage_samples(entitlement_id, observed_at)""",
            """CREATE TABLE IF NOT EXISTS entitlement_quota_ledger (
                   entry_id TEXT PRIMARY KEY,
                   entitlement_id TEXT NOT NULL REFERENCES entitlements(entitlement_id),
                   generation_id TEXT REFERENCES credential_generations(generation_id),
                   endpoint_id TEXT REFERENCES connectivity_endpoints(endpoint_id),
                   lease_id TEXT REFERENCES quota_leases(lease_id),
                   epoch_id TEXT REFERENCES entitlement_usage_epochs(epoch_id),
                   event_type TEXT NOT NULL CHECK (event_type IN ('grant', 'usage', 'release', 'exhaust', 'counter_reset', 'reconcile')),
                   bytes INTEGER NOT NULL CHECK (bytes >= 0),
                   consumed_bytes INTEGER NOT NULL CHECK (consumed_bytes >= 0),
                   remaining_bytes INTEGER NOT NULL CHECK (remaining_bytes >= 0),
                   idempotency_key TEXT NOT NULL UNIQUE,
                   details_json TEXT NOT NULL DEFAULT '{}',
                   created_at TEXT NOT NULL
               )""",
            """CREATE INDEX IF NOT EXISTS entitlement_quota_ledger_entitlement
               ON entitlement_quota_ledger(entitlement_id, created_at)""",
        ),
        postgres_statements=(
            "ALTER TABLE entitlements ADD COLUMN IF NOT EXISTS consumed_bytes BIGINT NOT NULL DEFAULT 0",
            "ALTER TABLE entitlements ADD COLUMN IF NOT EXISTS quota_exhausted_at TIMESTAMPTZ",
            "UPDATE entitlements SET consumed_bytes = COALESCE((SELECT SUM(used_bytes) FROM quota_leases q WHERE q.entitlement_id = entitlements.entitlement_id), 0)",
            "CREATE INDEX IF NOT EXISTS entitlements_quota_status ON entitlements(status, quota_exhausted_at)",
            """CREATE TABLE IF NOT EXISTS entitlement_usage_epochs (
                   epoch_id TEXT PRIMARY KEY,
                   entitlement_id TEXT NOT NULL REFERENCES entitlements(entitlement_id),
                   generation_id TEXT NOT NULL REFERENCES credential_generations(generation_id),
                   endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   source_external_id TEXT NOT NULL,
                   epoch_no INTEGER NOT NULL CHECK (epoch_no > 0),
                   last_remote_bytes BIGINT NOT NULL CHECK (last_remote_bytes >= 0),
                   credited_bytes BIGINT NOT NULL DEFAULT 0 CHECK (credited_bytes >= 0),
                   reset_count INTEGER NOT NULL DEFAULT 0 CHECK (reset_count >= 0),
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'reset', 'closed')),
                   last_observed_at TIMESTAMPTZ NOT NULL,
                   created_at TIMESTAMPTZ NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL,
                   UNIQUE(entitlement_id, generation_id, endpoint_id, source_external_id, epoch_no)
               )""",
            """CREATE INDEX IF NOT EXISTS entitlement_usage_epochs_lookup
               ON entitlement_usage_epochs(entitlement_id, generation_id, endpoint_id, source_external_id, status)""",
            """CREATE TABLE IF NOT EXISTS entitlement_usage_samples (
                   sample_id TEXT PRIMARY KEY,
                   epoch_id TEXT NOT NULL REFERENCES entitlement_usage_epochs(epoch_id),
                   entitlement_id TEXT NOT NULL REFERENCES entitlements(entitlement_id),
                   generation_id TEXT NOT NULL REFERENCES credential_generations(generation_id),
                   endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   source_external_id TEXT NOT NULL,
                   lease_id TEXT REFERENCES quota_leases(lease_id),
                   remote_bytes BIGINT NOT NULL CHECK (remote_bytes >= 0),
                   delta_bytes BIGINT NOT NULL CHECK (delta_bytes >= 0),
                   accepted INTEGER NOT NULL CHECK (accepted IN (0, 1)),
                   reason TEXT NOT NULL,
                   observed_at TIMESTAMPTZ NOT NULL,
                   created_at TIMESTAMPTZ NOT NULL,
                   UNIQUE(epoch_id, observed_at, remote_bytes)
               )""",
            """CREATE INDEX IF NOT EXISTS entitlement_usage_samples_entitlement
               ON entitlement_usage_samples(entitlement_id, observed_at)""",
            """CREATE TABLE IF NOT EXISTS entitlement_quota_ledger (
                   entry_id TEXT PRIMARY KEY,
                   entitlement_id TEXT NOT NULL REFERENCES entitlements(entitlement_id),
                   generation_id TEXT REFERENCES credential_generations(generation_id),
                   endpoint_id TEXT REFERENCES connectivity_endpoints(endpoint_id),
                   lease_id TEXT REFERENCES quota_leases(lease_id),
                   epoch_id TEXT REFERENCES entitlement_usage_epochs(epoch_id),
                   event_type TEXT NOT NULL CHECK (event_type IN ('grant', 'usage', 'release', 'exhaust', 'counter_reset', 'reconcile')),
                   bytes BIGINT NOT NULL CHECK (bytes >= 0),
                   consumed_bytes BIGINT NOT NULL CHECK (consumed_bytes >= 0),
                   remaining_bytes BIGINT NOT NULL CHECK (remaining_bytes >= 0),
                   idempotency_key TEXT NOT NULL UNIQUE,
                   details_json TEXT NOT NULL DEFAULT '{}',
                   created_at TIMESTAMPTZ NOT NULL
               )""",
            """CREATE INDEX IF NOT EXISTS entitlement_quota_ledger_entitlement
               ON entitlement_quota_ledger(entitlement_id, created_at)""",
        ),
    ),
)
