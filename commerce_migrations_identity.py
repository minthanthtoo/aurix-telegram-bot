"""Identity-owned commerce migration definitions."""

from __future__ import annotations

from schema_migrations import Migration
from migration_hooks import (
    _add_normalized_payment_reference_guard,
    _canonicalize_payment_provider_identity,
    _rebuild_paid_keys_for_server_identity,
)


COMMERCE_MIGRATIONS_IDENTITY = (
    Migration(
        9,
        "restart_safe_interaction_state",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS interaction_states (
                   telegram_id INTEGER NOT NULL,
                   state_key TEXT NOT NULL,
                   payload_json TEXT NOT NULL DEFAULT '{}',
                   expires_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   PRIMARY KEY (telegram_id, state_key)
               )""",
            """CREATE INDEX IF NOT EXISTS interaction_states_expiry
               ON interaction_states(expires_at)""",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS interaction_states (
                   telegram_id BIGINT NOT NULL,
                   state_key TEXT NOT NULL,
                   payload_json TEXT NOT NULL DEFAULT '{}',
                   expires_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   PRIMARY KEY (telegram_id, state_key)
               )""",
            """CREATE INDEX IF NOT EXISTS interaction_states_expiry
               ON interaction_states(expires_at)""",
        ),
    ),
    Migration(
        10,
        "receipt_perceptual_fingerprint",
        sqlite_statements=(
            "ALTER TABLE payment_evidence ADD COLUMN image_phash TEXT",
            "CREATE INDEX IF NOT EXISTS payment_evidence_phash ON payment_evidence(image_phash)",
        ),
        postgres_statements=(
            "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS image_phash TEXT",
            "CREATE INDEX IF NOT EXISTS payment_evidence_phash ON payment_evidence(image_phash)",
        ),
    ),
    Migration(
        11,
        "remote_key_review_workflow",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS outline_remote_key_reviews (
                   server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   outline_key_id TEXT NOT NULL,
                   review_state TEXT NOT NULL DEFAULT 'unreviewed'
                       CHECK (review_state IN ('unreviewed', 'accepted_external')),
                   reviewed_by INTEGER,
                   reviewed_at TEXT,
                   review_note TEXT,
                   PRIMARY KEY (server_id, outline_key_id)
               )""",
            """CREATE INDEX IF NOT EXISTS outline_remote_key_reviews_state
               ON outline_remote_key_reviews(server_id, review_state)""",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS outline_remote_key_reviews (
                   server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   outline_key_id TEXT NOT NULL,
                   review_state TEXT NOT NULL DEFAULT 'unreviewed'
                       CHECK (review_state IN ('unreviewed', 'accepted_external')),
                   reviewed_by BIGINT,
                   reviewed_at TEXT,
                   review_note TEXT,
                   PRIMARY KEY (server_id, outline_key_id)
               )""",
            """CREATE INDEX IF NOT EXISTS outline_remote_key_reviews_state
               ON outline_remote_key_reviews(server_id, review_state)""",
        ),
    ),
    Migration(
        12,
        "endpoint_health_observability",
        sqlite_statements=(
            "ALTER TABLE outline_servers ADD COLUMN health_success_streak INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE outline_servers ADD COLUMN health_failure_streak INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE outline_servers ADD COLUMN health_state_changed_at TEXT",
            "ALTER TABLE outline_servers ADD COLUMN health_last_latency_ms REAL",
            """CREATE TABLE IF NOT EXISTS endpoint_health_observations (
                   id TEXT PRIMARY KEY,
                   server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   probe_type TEXT NOT NULL,
                   observed_at TEXT NOT NULL,
                   observed_status TEXT NOT NULL CHECK (
                       observed_status IN ('healthy', 'unreachable')
                   ),
                   state_before TEXT NOT NULL,
                   state_after TEXT NOT NULL CHECK (
                       state_after IN ('unknown', 'healthy', 'degraded', 'unreachable')
                   ),
                   latency_ms REAL,
                   remote_key_count INTEGER,
                   error_type TEXT,
                   created_at TEXT NOT NULL,
                   UNIQUE (server_id, probe_type, observed_at)
               )""",
            """CREATE INDEX IF NOT EXISTS endpoint_health_recent
               ON endpoint_health_observations(server_id, observed_at)""",
        ),
        postgres_statements=(
            "ALTER TABLE outline_servers ADD COLUMN IF NOT EXISTS health_success_streak INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE outline_servers ADD COLUMN IF NOT EXISTS health_failure_streak INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE outline_servers ADD COLUMN IF NOT EXISTS health_state_changed_at TEXT",
            "ALTER TABLE outline_servers ADD COLUMN IF NOT EXISTS health_last_latency_ms DOUBLE PRECISION",
            """CREATE TABLE IF NOT EXISTS endpoint_health_observations (
                   id TEXT PRIMARY KEY,
                   server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   probe_type TEXT NOT NULL,
                   observed_at TEXT NOT NULL,
                   observed_status TEXT NOT NULL CHECK (
                       observed_status IN ('healthy', 'unreachable')
                   ),
                   state_before TEXT NOT NULL,
                   state_after TEXT NOT NULL CHECK (
                       state_after IN ('unknown', 'healthy', 'degraded', 'unreachable')
                   ),
                   latency_ms DOUBLE PRECISION,
                   remote_key_count INTEGER,
                   error_type TEXT,
                   created_at TEXT NOT NULL,
                   UNIQUE (server_id, probe_type, observed_at)
               )""",
            """CREATE INDEX IF NOT EXISTS endpoint_health_recent
               ON endpoint_health_observations(server_id, observed_at)""",
        ),
    ),
    Migration(
        13,
        "endpoint_lifecycle_drain_state",
        sqlite_statements=(
            "ALTER TABLE outline_servers ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'active' CHECK (lifecycle_state IN ('active', 'draining', 'retired'))",
            "ALTER TABLE outline_servers ADD COLUMN lifecycle_reason TEXT",
            "ALTER TABLE outline_servers ADD COLUMN lifecycle_changed_at TEXT",
            "CREATE INDEX IF NOT EXISTS outline_servers_lifecycle ON outline_servers(enabled, lifecycle_state)",
        ),
        postgres_statements=(
            "ALTER TABLE outline_servers ADD COLUMN IF NOT EXISTS lifecycle_state TEXT NOT NULL DEFAULT 'active' CHECK (lifecycle_state IN ('active', 'draining', 'retired'))",
            "ALTER TABLE outline_servers ADD COLUMN IF NOT EXISTS lifecycle_reason TEXT",
            "ALTER TABLE outline_servers ADD COLUMN IF NOT EXISTS lifecycle_changed_at TEXT",
            "CREATE INDEX IF NOT EXISTS outline_servers_lifecycle ON outline_servers(enabled, lifecycle_state)",
        ),
    ),
    Migration(
        14,
        "connectivity_registry_foundation",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS connectivity_providers (
                   provider_id TEXT PRIMARY KEY,
                   display_name TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS connectivity_regions (
                   region_id TEXT PRIMARY KEY,
                   provider_id TEXT NOT NULL REFERENCES connectivity_providers(provider_id),
                   display_name TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS connectivity_transports (
                   transport_id TEXT PRIMARY KEY,
                   protocol TEXT NOT NULL,
                   display_name TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS connectivity_endpoints (
                   endpoint_id TEXT PRIMARY KEY,
                   outline_server_id TEXT UNIQUE REFERENCES outline_servers(server_id),
                   provider_id TEXT NOT NULL REFERENCES connectivity_providers(provider_id),
                   region_id TEXT NOT NULL REFERENCES connectivity_regions(region_id),
                   transport_id TEXT NOT NULL REFERENCES connectivity_transports(transport_id),
                   status TEXT NOT NULL DEFAULT 'provisioning' CHECK (
                       status IN ('provisioning', 'active', 'degraded', 'draining', 'failed', 'retired')
                   ),
                   accepts_new_keys INTEGER NOT NULL DEFAULT 0 CHECK (accepts_new_keys IN (0, 1)),
                   management_secret_ref TEXT,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS connectivity_profiles (
                   profile_id TEXT PRIMARY KEY,
                   telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                   subscription_id TEXT UNIQUE REFERENCES subscriptions(id),
                   profile_kind TEXT NOT NULL CHECK (profile_kind IN ('free', 'paid', 'trial', 'promo')),
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ended', 'blocked')),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS endpoint_assignments (
                   assignment_id TEXT PRIMARY KEY,
                   profile_id TEXT NOT NULL REFERENCES connectivity_profiles(profile_id),
                   endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ended', 'failed')),
                   assigned_at TEXT NOT NULL,
                   ended_at TEXT,
                   reason TEXT
               )""",
            """CREATE TABLE IF NOT EXISTS connectivity_credentials (
                   credential_id TEXT PRIMARY KEY,
                   profile_id TEXT NOT NULL REFERENCES connectivity_profiles(profile_id),
                   endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   transport_id TEXT NOT NULL REFERENCES connectivity_transports(transport_id),
                   external_id TEXT NOT NULL,
                   secret_ciphertext TEXT,
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('pending', 'active', 'revoked', 'failed')),
                   created_at TEXT NOT NULL,
                   revoked_at TEXT,
                   UNIQUE(endpoint_id, external_id)
               )""",
            "CREATE INDEX IF NOT EXISTS connectivity_endpoint_admission ON connectivity_endpoints(status, accepts_new_keys)",
            "CREATE INDEX IF NOT EXISTS endpoint_assignments_current ON endpoint_assignments(profile_id, status)",
            "CREATE INDEX IF NOT EXISTS connectivity_credentials_profile ON connectivity_credentials(profile_id, status)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS connectivity_providers (
                   provider_id TEXT PRIMARY KEY,
                   display_name TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS connectivity_regions (
                   region_id TEXT PRIMARY KEY,
                   provider_id TEXT NOT NULL REFERENCES connectivity_providers(provider_id),
                   display_name TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS connectivity_transports (
                   transport_id TEXT PRIMARY KEY,
                   protocol TEXT NOT NULL,
                   display_name TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS connectivity_endpoints (
                   endpoint_id TEXT PRIMARY KEY,
                   outline_server_id TEXT UNIQUE REFERENCES outline_servers(server_id),
                   provider_id TEXT NOT NULL REFERENCES connectivity_providers(provider_id),
                   region_id TEXT NOT NULL REFERENCES connectivity_regions(region_id),
                   transport_id TEXT NOT NULL REFERENCES connectivity_transports(transport_id),
                   status TEXT NOT NULL DEFAULT 'provisioning' CHECK (
                       status IN ('provisioning', 'active', 'degraded', 'draining', 'failed', 'retired')
                   ),
                   accepts_new_keys INTEGER NOT NULL DEFAULT 0 CHECK (accepts_new_keys IN (0, 1)),
                   management_secret_ref TEXT,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS connectivity_profiles (
                   profile_id TEXT PRIMARY KEY,
                   telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                   subscription_id TEXT UNIQUE REFERENCES subscriptions(id),
                   profile_kind TEXT NOT NULL CHECK (profile_kind IN ('free', 'paid', 'trial', 'promo')),
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ended', 'blocked')),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS endpoint_assignments (
                   assignment_id TEXT PRIMARY KEY,
                   profile_id TEXT NOT NULL REFERENCES connectivity_profiles(profile_id),
                   endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ended', 'failed')),
                   assigned_at TEXT NOT NULL,
                   ended_at TEXT,
                   reason TEXT
               )""",
            """CREATE TABLE IF NOT EXISTS connectivity_credentials (
                   credential_id TEXT PRIMARY KEY,
                   profile_id TEXT NOT NULL REFERENCES connectivity_profiles(profile_id),
                   endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   transport_id TEXT NOT NULL REFERENCES connectivity_transports(transport_id),
                   external_id TEXT NOT NULL,
                   secret_ciphertext TEXT,
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('pending', 'active', 'revoked', 'failed')),
                   created_at TEXT NOT NULL,
                   revoked_at TEXT,
                   UNIQUE(endpoint_id, external_id)
               )""",
            "CREATE INDEX IF NOT EXISTS connectivity_endpoint_admission ON connectivity_endpoints(status, accepts_new_keys)",
            "CREATE INDEX IF NOT EXISTS endpoint_assignments_current ON endpoint_assignments(profile_id, status)",
            "CREATE INDEX IF NOT EXISTS connectivity_credentials_profile ON connectivity_credentials(profile_id, status)",
        ),
    ),
)
