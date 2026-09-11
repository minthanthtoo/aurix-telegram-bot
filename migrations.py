"""Numbered, component-scoped database migration registry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


UTC = timezone.utc


class MigrationError(RuntimeError):
    """Raised when recorded migration history disagrees with the code registry."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sqlite_statements: tuple[str, ...] = ()
    postgres_statements: tuple[str, ...] = ()

    def statements_for(self, dialect: str) -> tuple[str, ...]:
        if dialect == "sqlite":
            return self.sqlite_statements
        if dialect == "postgres":
            return self.postgres_statements
        raise MigrationError(f"Unsupported migration dialect: {dialect}")


FREE_ACCESS_MIGRATIONS = (
    Migration(1, "legacy_free_access_schema"),
    Migration(
        2,
        "giveaway_campaigns",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS giveaway_campaigns (
                   code TEXT PRIMARY KEY,
                   quota_bytes INTEGER NOT NULL CHECK (quota_bytes > 0),
                   duration_days INTEGER NOT NULL CHECK (duration_days > 0),
                   winner_limit INTEGER NOT NULL CHECK (winner_limit > 0),
                   claimed_count INTEGER NOT NULL DEFAULT 0
                       CHECK (claimed_count >= 0 AND claimed_count <= winner_limit),
                   active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                   created_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS giveaway_claims (
                   campaign_code TEXT NOT NULL REFERENCES giveaway_campaigns(code),
                   telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                   key_id INTEGER NOT NULL UNIQUE REFERENCES keys(id),
                   winner_number INTEGER NOT NULL CHECK (winner_number > 0),
                   claimed_at TEXT NOT NULL,
                   PRIMARY KEY (campaign_code, telegram_id),
                   UNIQUE (campaign_code, winner_number)
               )""",
            "CREATE INDEX IF NOT EXISTS giveaway_claims_user ON giveaway_claims(telegram_id)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS giveaway_campaigns (
                   code TEXT PRIMARY KEY,
                   quota_bytes BIGINT NOT NULL CHECK (quota_bytes > 0),
                   duration_days INTEGER NOT NULL CHECK (duration_days > 0),
                   winner_limit INTEGER NOT NULL CHECK (winner_limit > 0),
                   claimed_count INTEGER NOT NULL DEFAULT 0
                       CHECK (claimed_count >= 0 AND claimed_count <= winner_limit),
                   active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                   created_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS giveaway_claims (
                   campaign_code TEXT NOT NULL REFERENCES giveaway_campaigns(code),
                   telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                   key_id BIGINT NOT NULL UNIQUE REFERENCES keys(id),
                   winner_number INTEGER NOT NULL CHECK (winner_number > 0),
                   claimed_at TEXT NOT NULL,
                   PRIMARY KEY (campaign_code, telegram_id),
                   UNIQUE (campaign_code, winner_number)
               )""",
            "CREATE INDEX IF NOT EXISTS giveaway_claims_user ON giveaway_claims(telegram_id)",
        ),
    ),
    Migration(
        3,
        "configurable_promo_campaigns",
        sqlite_statements=(
            "ALTER TABLE giveaway_campaigns ADD COLUMN starts_at TEXT",
            "ALTER TABLE giveaway_campaigns ADD COLUMN ends_at TEXT",
            "ALTER TABLE giveaway_campaigns ADD COLUMN frequency TEXT NOT NULL DEFAULT 'campaign'",
            "ALTER TABLE giveaway_campaigns ADD COLUMN updated_at TEXT",
            """CREATE TABLE IF NOT EXISTS giveaway_windows (
                   campaign_code TEXT NOT NULL REFERENCES giveaway_campaigns(code),
                   window_start TEXT NOT NULL,
                   claimed_count INTEGER NOT NULL DEFAULT 0 CHECK (claimed_count >= 0),
                   PRIMARY KEY (campaign_code, window_start)
               )""",
            """INSERT INTO giveaway_campaigns
               (code, quota_bytes, duration_days, winner_limit, claimed_count, active,
                created_at, frequency, updated_at)
               VALUES ('100GBFREE', 100000000000, 30, 5, 0, 1,
                       '2026-08-27T00:00:00+00:00', 'campaign', '2026-08-27T00:00:00+00:00')
               ON CONFLICT(code) DO NOTHING""",
            "UPDATE giveaway_campaigns SET quota_bytes = 100000000000 WHERE code = '100GBFREE'",
            """UPDATE keys SET data_limit_bytes = 100000000000
               WHERE id IN (SELECT key_id FROM giveaway_claims WHERE campaign_code = '100GBFREE')""",
        ),
        postgres_statements=(
            "ALTER TABLE giveaway_campaigns ADD COLUMN IF NOT EXISTS starts_at TEXT",
            "ALTER TABLE giveaway_campaigns ADD COLUMN IF NOT EXISTS ends_at TEXT",
            "ALTER TABLE giveaway_campaigns ADD COLUMN IF NOT EXISTS frequency TEXT NOT NULL DEFAULT 'campaign'",
            "ALTER TABLE giveaway_campaigns ADD COLUMN IF NOT EXISTS updated_at TEXT",
            """CREATE TABLE IF NOT EXISTS giveaway_windows (
                   campaign_code TEXT NOT NULL REFERENCES giveaway_campaigns(code),
                   window_start TEXT NOT NULL,
                   claimed_count INTEGER NOT NULL DEFAULT 0 CHECK (claimed_count >= 0),
                   PRIMARY KEY (campaign_code, window_start)
               )""",
            """INSERT INTO giveaway_campaigns
               (code, quota_bytes, duration_days, winner_limit, claimed_count, active,
                created_at, frequency, updated_at)
               VALUES ('100GBFREE', 100000000000, 30, 5, 0, 1,
                       '2026-08-27T00:00:00+00:00', 'campaign', '2026-08-27T00:00:00+00:00')
               ON CONFLICT(code) DO NOTHING""",
            "UPDATE giveaway_campaigns SET quota_bytes = 100000000000 WHERE code = '100GBFREE'",
            """UPDATE keys SET data_limit_bytes = 100000000000
               WHERE id IN (SELECT key_id FROM giveaway_claims WHERE campaign_code = '100GBFREE')""",
        ),
    ),
    Migration(
        4,
        "endpoint_identity_and_capacity",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS vpn_endpoints (
                   id TEXT PRIMARY KEY,
                   code TEXT NOT NULL UNIQUE,
                   provider TEXT NOT NULL DEFAULT 'manual',
                   provider_resource_id TEXT,
                   region TEXT NOT NULL DEFAULT 'sgp1',
                   state TEXT NOT NULL DEFAULT 'ACTIVE',
                   accepts_new_assignments INTEGER NOT NULL DEFAULT 1,
                   management_url_ciphertext TEXT,
                   certificate_sha256 TEXT,
                   outline_version TEXT,
                   public_address TEXT,
                   private_address TEXT,
                   max_active_keys INTEGER,
                   reserved_transfer_bytes INTEGER,
                   created_at TEXT NOT NULL,
                   verified_at TEXT,
                   last_healthy_at TEXT,
                   retired_at TEXT,
                   UNIQUE(provider, provider_resource_id)
               )""",
            """INSERT INTO vpn_endpoints
               (id, code, provider, region, state, accepts_new_assignments, created_at)
               VALUES ('legacy-default', 'SGP-01', 'manual', 'sgp1', 'ACTIVE', 1,
                       '2026-09-01T00:00:00+00:00')
               ON CONFLICT(id) DO NOTHING""",
            # SQLite cannot add a REFERENCES column with a non-NULL default to
            # a populated table. Endpoint validity remains enforced by the
            # registry and assignment repository on every write path.
            "ALTER TABLE keys ADD COLUMN endpoint_id TEXT NOT NULL DEFAULT 'legacy-default'",
            """CREATE TRIGGER IF NOT EXISTS keys_endpoint_insert_guard
               BEFORE INSERT ON keys
               WHEN NOT EXISTS (SELECT 1 FROM vpn_endpoints WHERE id = NEW.endpoint_id)
               BEGIN SELECT RAISE(ABORT, 'unknown key endpoint'); END""",
            """CREATE TRIGGER IF NOT EXISTS keys_endpoint_update_guard
               BEFORE UPDATE OF endpoint_id ON keys
               WHEN NOT EXISTS (SELECT 1 FROM vpn_endpoints WHERE id = NEW.endpoint_id)
               BEGIN SELECT RAISE(ABORT, 'unknown key endpoint'); END""",
            "CREATE UNIQUE INDEX IF NOT EXISTS keys_endpoint_external ON keys(endpoint_id, outline_key_id)",
            """CREATE TABLE IF NOT EXISTS endpoint_capacity_snapshots (
                   id TEXT PRIMARY KEY,
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   observed_at TEXT NOT NULL,
                   healthy INTEGER NOT NULL,
                   active_key_count INTEGER,
                   observed_transfer_bytes INTEGER,
                   cpu_percent REAL,
                   memory_percent REAL,
                   peak_mbps REAL,
                   management_latency_ms REAL,
                   last_error TEXT
               )""",
            "CREATE INDEX IF NOT EXISTS endpoint_capacity_latest ON endpoint_capacity_snapshots(endpoint_id, observed_at)",
            """CREATE TABLE IF NOT EXISTS endpoint_plan_limits (
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   plan_code TEXT NOT NULL,
                   enabled INTEGER NOT NULL DEFAULT 1,
                   max_active_assignments INTEGER,
                   reservation_weight_bytes INTEGER,
                   PRIMARY KEY(endpoint_id, plan_code)
               )""",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS vpn_endpoints (
                   id TEXT PRIMARY KEY,
                   code TEXT NOT NULL UNIQUE,
                   provider TEXT NOT NULL DEFAULT 'manual',
                   provider_resource_id TEXT,
                   region TEXT NOT NULL DEFAULT 'sgp1',
                   state TEXT NOT NULL DEFAULT 'ACTIVE',
                   accepts_new_assignments BOOLEAN NOT NULL DEFAULT TRUE,
                   management_url_ciphertext TEXT,
                   certificate_sha256 TEXT,
                   outline_version TEXT,
                   public_address TEXT,
                   private_address TEXT,
                   max_active_keys INTEGER,
                   reserved_transfer_bytes BIGINT,
                   created_at TEXT NOT NULL,
                   verified_at TEXT,
                   last_healthy_at TEXT,
                   retired_at TEXT,
                   UNIQUE(provider, provider_resource_id)
               )""",
            """INSERT INTO vpn_endpoints
               (id, code, provider, region, state, accepts_new_assignments, created_at)
               VALUES ('legacy-default', 'SGP-01', 'manual', 'sgp1', 'ACTIVE', TRUE,
                       '2026-09-01T00:00:00+00:00')
               ON CONFLICT(id) DO NOTHING""",
            "ALTER TABLE keys ADD COLUMN IF NOT EXISTS endpoint_id TEXT NOT NULL DEFAULT 'legacy-default' REFERENCES vpn_endpoints(id)",
            "ALTER TABLE keys DROP CONSTRAINT IF EXISTS keys_outline_key_id_key",
            "CREATE UNIQUE INDEX IF NOT EXISTS keys_endpoint_external ON keys(endpoint_id, outline_key_id)",
            """CREATE TABLE IF NOT EXISTS endpoint_capacity_snapshots (
                   id TEXT PRIMARY KEY,
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   observed_at TEXT NOT NULL,
                   healthy BOOLEAN NOT NULL,
                   active_key_count INTEGER,
                   observed_transfer_bytes BIGINT,
                   cpu_percent DOUBLE PRECISION,
                   memory_percent DOUBLE PRECISION,
                   peak_mbps DOUBLE PRECISION,
                   management_latency_ms DOUBLE PRECISION,
                   last_error TEXT
               )""",
            "CREATE INDEX IF NOT EXISTS endpoint_capacity_latest ON endpoint_capacity_snapshots(endpoint_id, observed_at)",
            """CREATE TABLE IF NOT EXISTS endpoint_plan_limits (
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   plan_code TEXT NOT NULL,
                   enabled BOOLEAN NOT NULL DEFAULT TRUE,
                   max_active_assignments INTEGER,
                   reservation_weight_bytes BIGINT,
                   PRIMARY KEY(endpoint_id, plan_code)
               )""",
        ),
    ),
    Migration(
        5,
        "durable_free_provisioning_jobs",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS free_provisioning_jobs (
                   id TEXT PRIMARY KEY,
                   telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                   plan_code TEXT NOT NULL,
                   key_type TEXT NOT NULL,
                   first_name TEXT NOT NULL DEFAULT '',
                   username TEXT,
                   quota_bytes INTEGER NOT NULL CHECK (quota_bytes > 0),
                   duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0),
                   status TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'running', 'done', 'failed')),
                   attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                   next_attempt_at TEXT NOT NULL,
                   locked_at TEXT,
                   endpoint_id TEXT,
                   external_id TEXT,
                   key_id INTEGER REFERENCES keys(id),
                   last_error TEXT,
                   created_at TEXT NOT NULL,
                   completed_at TEXT
               )""",
            "CREATE INDEX IF NOT EXISTS free_provisioning_jobs_due ON free_provisioning_jobs(status, next_attempt_at)",
            "CREATE INDEX IF NOT EXISTS free_provisioning_jobs_account ON free_provisioning_jobs(telegram_id, plan_code, created_at)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS free_provisioning_jobs (
                   id TEXT PRIMARY KEY,
                   telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                   plan_code TEXT NOT NULL,
                   key_type TEXT NOT NULL,
                   first_name TEXT NOT NULL DEFAULT '',
                   username TEXT,
                   quota_bytes BIGINT NOT NULL CHECK (quota_bytes > 0),
                   duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0),
                   status TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'running', 'done', 'failed')),
                   attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                   next_attempt_at TIMESTAMPTZ NOT NULL,
                   locked_at TIMESTAMPTZ,
                   endpoint_id TEXT,
                   external_id TEXT,
                   key_id BIGINT REFERENCES keys(id),
                   last_error TEXT,
                   created_at TIMESTAMPTZ NOT NULL,
                   completed_at TIMESTAMPTZ
               )""",
            "CREATE INDEX IF NOT EXISTS free_provisioning_jobs_due ON free_provisioning_jobs(status, next_attempt_at)",
            "CREATE INDEX IF NOT EXISTS free_provisioning_jobs_account ON free_provisioning_jobs(telegram_id, plan_code, created_at)",
        ),
    ),
    Migration(
        6,
        "durable_giveaway_provisioning_jobs",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS giveaway_provisioning_jobs (
                   id TEXT PRIMARY KEY,
                   campaign_code TEXT NOT NULL REFERENCES giveaway_campaigns(code),
                   telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                   first_name TEXT NOT NULL DEFAULT '',
                   username TEXT,
                   window_start TEXT NOT NULL,
                   winner_number INTEGER NOT NULL CHECK (winner_number > 0),
                   quota_bytes INTEGER NOT NULL CHECK (quota_bytes > 0),
                   duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0),
                   status TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'running', 'done', 'failed')),
                   attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                   next_attempt_at TEXT NOT NULL,
                   locked_at TEXT,
                   endpoint_id TEXT,
                   external_id TEXT,
                   key_id INTEGER UNIQUE REFERENCES keys(id),
                   last_error TEXT,
                   created_at TEXT NOT NULL,
                   completed_at TEXT,
                   UNIQUE (campaign_code, telegram_id),
                   UNIQUE (campaign_code, window_start, winner_number)
               )""",
            "CREATE INDEX IF NOT EXISTS giveaway_provisioning_jobs_due ON giveaway_provisioning_jobs(status, next_attempt_at)",
            "CREATE INDEX IF NOT EXISTS giveaway_provisioning_jobs_window ON giveaway_provisioning_jobs(campaign_code, window_start, status)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS giveaway_provisioning_jobs (
                   id TEXT PRIMARY KEY,
                   campaign_code TEXT NOT NULL REFERENCES giveaway_campaigns(code),
                   telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                   first_name TEXT NOT NULL DEFAULT '',
                   username TEXT,
                   window_start TEXT NOT NULL,
                   winner_number INTEGER NOT NULL CHECK (winner_number > 0),
                   quota_bytes BIGINT NOT NULL CHECK (quota_bytes > 0),
                   duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0),
                   status TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'running', 'done', 'failed')),
                   attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                   next_attempt_at TIMESTAMPTZ NOT NULL,
                   locked_at TIMESTAMPTZ,
                   endpoint_id TEXT,
                   external_id TEXT,
                   key_id BIGINT UNIQUE REFERENCES keys(id),
                   last_error TEXT,
                   created_at TIMESTAMPTZ NOT NULL,
                   completed_at TIMESTAMPTZ,
                   UNIQUE (campaign_code, telegram_id),
                   UNIQUE (campaign_code, window_start, winner_number)
               )""",
            "CREATE INDEX IF NOT EXISTS giveaway_provisioning_jobs_due ON giveaway_provisioning_jobs(status, next_attempt_at)",
            "CREATE INDEX IF NOT EXISTS giveaway_provisioning_jobs_window ON giveaway_provisioning_jobs(campaign_code, window_start, status)",
        ),
    ),
    Migration(
        7,
        "endpoint_protocol_profiles",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS endpoint_protocol_profiles (
                   profile_id TEXT PRIMARY KEY,
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   protocol TEXT NOT NULL,
                   adapter_type TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'candidate'
                       CHECK (status IN ('candidate', 'enabled', 'degraded', 'disabled', 'retired')),
                   capabilities_json TEXT NOT NULL DEFAULT '{}',
                   verified_at TEXT,
                   last_healthy_at TEXT,
                   created_at TEXT NOT NULL,
                   retired_at TEXT,
                   UNIQUE(endpoint_id, protocol)
               )""",
            "CREATE INDEX IF NOT EXISTS endpoint_protocol_profiles_lookup ON endpoint_protocol_profiles(endpoint_id, status, protocol)",
            """INSERT OR IGNORE INTO endpoint_protocol_profiles
                   (profile_id, endpoint_id, protocol, adapter_type, status,
                    capabilities_json, verified_at, last_healthy_at, created_at)
               SELECT 'outline:' || id, id, 'outline', 'outline', 'enabled',
                      '{}', verified_at, last_healthy_at, created_at
                 FROM vpn_endpoints""",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS endpoint_protocol_profiles (
                   profile_id TEXT PRIMARY KEY,
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   protocol TEXT NOT NULL,
                   adapter_type TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'candidate'
                       CHECK (status IN ('candidate', 'enabled', 'degraded', 'disabled', 'retired')),
                   capabilities_json TEXT NOT NULL DEFAULT '{}',
                   verified_at TIMESTAMPTZ,
                   last_healthy_at TIMESTAMPTZ,
                   created_at TIMESTAMPTZ NOT NULL,
                   retired_at TIMESTAMPTZ,
                   UNIQUE(endpoint_id, protocol)
               )""",
            "CREATE INDEX IF NOT EXISTS endpoint_protocol_profiles_lookup ON endpoint_protocol_profiles(endpoint_id, status, protocol)",
            """INSERT INTO endpoint_protocol_profiles
                   (profile_id, endpoint_id, protocol, adapter_type, status,
                    capabilities_json, verified_at, last_healthy_at, created_at)
               SELECT 'outline:' || id, id, 'outline', 'outline', 'enabled',
                      '{}', NULLIF(verified_at, '')::timestamptz,
                      NULLIF(last_healthy_at, '')::timestamptz,
                      created_at::timestamptz
                 FROM vpn_endpoints
                ON CONFLICT(endpoint_id, protocol) DO NOTHING""",
        ),
    ),
    Migration(
        8,
        "protocol_health_observations",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS endpoint_protocol_observations (
                   observation_id TEXT PRIMARY KEY,
                   profile_id TEXT NOT NULL REFERENCES endpoint_protocol_profiles(profile_id),
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   protocol TEXT NOT NULL,
                   signal TEXT NOT NULL,
                   status TEXT NOT NULL
                       CHECK (status IN ('healthy', 'degraded', 'failed', 'unsupported', 'unknown')),
                   details_json TEXT NOT NULL DEFAULT '{}',
                   latency_ms REAL,
                   observed_at TEXT NOT NULL,
                   expires_at TEXT,
                   source TEXT NOT NULL,
                   created_at TEXT NOT NULL
               )""",
            "CREATE INDEX IF NOT EXISTS endpoint_protocol_observations_lookup ON endpoint_protocol_observations(endpoint_id, protocol, signal, observed_at)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS endpoint_protocol_observations (
                   observation_id TEXT PRIMARY KEY,
                   profile_id TEXT NOT NULL REFERENCES endpoint_protocol_profiles(profile_id),
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   protocol TEXT NOT NULL,
                   signal TEXT NOT NULL,
                   status TEXT NOT NULL
                       CHECK (status IN ('healthy', 'degraded', 'failed', 'unsupported', 'unknown')),
                   details_json TEXT NOT NULL DEFAULT '{}',
                   latency_ms DOUBLE PRECISION,
                   observed_at TIMESTAMPTZ NOT NULL,
                   expires_at TIMESTAMPTZ,
                   source TEXT NOT NULL,
                   created_at TIMESTAMPTZ NOT NULL
               )""",
            "CREATE INDEX IF NOT EXISTS endpoint_protocol_observations_lookup ON endpoint_protocol_observations(endpoint_id, protocol, signal, observed_at)",
        ),
    ),
)

COMMERCE_MIGRATIONS = (
    Migration(1, "legacy_commerce_schema"),
    Migration(
        2,
        "endpoint_assignments_and_infrastructure_jobs",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS vpn_endpoints (
                   id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE,
                   provider TEXT NOT NULL DEFAULT 'manual', provider_resource_id TEXT,
                   region TEXT NOT NULL DEFAULT 'sgp1', state TEXT NOT NULL DEFAULT 'ACTIVE',
                   accepts_new_assignments INTEGER NOT NULL DEFAULT 1,
                   management_url_ciphertext TEXT, certificate_sha256 TEXT,
                   outline_version TEXT, public_address TEXT, private_address TEXT,
                   max_active_keys INTEGER, reserved_transfer_bytes INTEGER,
                   created_at TEXT NOT NULL, verified_at TEXT, last_healthy_at TEXT,
                   retired_at TEXT, UNIQUE(provider, provider_resource_id)
               )""",
            """INSERT INTO vpn_endpoints
               (id, code, provider, region, state, accepts_new_assignments, created_at)
               VALUES ('legacy-default', 'SGP-01', 'manual', 'sgp1', 'ACTIVE', 1,
                       '2026-09-01T00:00:00+00:00') ON CONFLICT(id) DO NOTHING""",
            """CREATE TABLE IF NOT EXISTS endpoint_plan_limits (
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   plan_code TEXT NOT NULL,
                   enabled INTEGER NOT NULL DEFAULT 1,
                   max_active_assignments INTEGER,
                   reservation_weight_bytes INTEGER,
                   PRIMARY KEY(endpoint_id, plan_code)
               )""",
            """CREATE TABLE IF NOT EXISTS endpoint_capacity_snapshots (
                   id TEXT PRIMARY KEY,
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   observed_at TEXT NOT NULL,
                   healthy INTEGER NOT NULL,
                   active_key_count INTEGER,
                   observed_transfer_bytes INTEGER,
                   cpu_percent REAL,
                   memory_percent REAL,
                   peak_mbps REAL,
                   management_latency_ms REAL,
                   last_error TEXT
               )""",
            # See the equivalent free-key migration above. Keeping this ALTER
            # compatible with populated legacy databases is required for the
            # one-disk upgrade path.
            "ALTER TABLE paid_vpn_keys ADD COLUMN endpoint_id TEXT NOT NULL DEFAULT 'legacy-default'",
            """CREATE TRIGGER IF NOT EXISTS paid_keys_endpoint_insert_guard
               BEFORE INSERT ON paid_vpn_keys
               WHEN NOT EXISTS (SELECT 1 FROM vpn_endpoints WHERE id = NEW.endpoint_id)
               BEGIN SELECT RAISE(ABORT, 'unknown paid key endpoint'); END""",
            """CREATE TRIGGER IF NOT EXISTS paid_keys_endpoint_update_guard
               BEFORE UPDATE OF endpoint_id ON paid_vpn_keys
               WHEN NOT EXISTS (SELECT 1 FROM vpn_endpoints WHERE id = NEW.endpoint_id)
               BEGIN SELECT RAISE(ABORT, 'unknown paid key endpoint'); END""",
            "CREATE UNIQUE INDEX IF NOT EXISTS paid_keys_endpoint_external ON paid_vpn_keys(endpoint_id, outline_key_id)",
            """CREATE TABLE IF NOT EXISTS endpoint_assignments (
                   id TEXT PRIMARY KEY,
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   subscription_id TEXT UNIQUE REFERENCES subscriptions(id),
                   free_key_id INTEGER,
                   plan_code TEXT NOT NULL,
                   status TEXT NOT NULL,
                   reason TEXT NOT NULL,
                   reserved_quota_bytes INTEGER,
                   assigned_at TEXT NOT NULL,
                   released_at TEXT,
                   CHECK ((subscription_id IS NOT NULL) != (free_key_id IS NOT NULL))
               )""",
            "CREATE UNIQUE INDEX IF NOT EXISTS endpoint_assignment_free_key ON endpoint_assignments(free_key_id)",
            """INSERT INTO endpoint_assignments
               (id, endpoint_id, subscription_id, free_key_id, plan_code, status,
                reason, reserved_quota_bytes, assigned_at, released_at)
               SELECT 'paid-' || s.id, k.endpoint_id, s.id, NULL, s.plan_code,
                      CASE WHEN k.status = 'active' AND s.status = 'active' THEN 'active' ELSE 'released' END,
                      'legacy-backfill', k.quota_bytes, k.created_at,
                      CASE WHEN k.status = 'active' AND s.status = 'active' THEN NULL ELSE k.created_at END
               FROM paid_vpn_keys k JOIN subscriptions s ON s.id = k.subscription_id
               ON CONFLICT(subscription_id) DO NOTHING""",
            "ALTER TABLE provisioning_jobs ADD COLUMN endpoint_assignment_id TEXT REFERENCES endpoint_assignments(id)",
            """CREATE TABLE IF NOT EXISTS infrastructure_jobs (
                   id TEXT PRIMARY KEY, operation TEXT NOT NULL, endpoint_id TEXT,
                   status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                   next_attempt_at TEXT NOT NULL, locked_at TEXT,
                   provider_resource_id TEXT, provider_action_id TEXT,
                   request_fingerprint TEXT NOT NULL UNIQUE, last_error TEXT,
                   created_at TEXT NOT NULL, completed_at TEXT
               )""",
            """CREATE TABLE IF NOT EXISTS infrastructure_events (
                   id TEXT PRIMARY KEY, infrastructure_job_id TEXT,
                   endpoint_id TEXT, event_type TEXT NOT NULL,
                   metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
               )""",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS vpn_endpoints (
                   id TEXT PRIMARY KEY,
                   code TEXT NOT NULL UNIQUE,
                   provider TEXT NOT NULL DEFAULT 'manual',
                   provider_resource_id TEXT,
                   region TEXT NOT NULL DEFAULT 'sgp1',
                   state TEXT NOT NULL DEFAULT 'ACTIVE',
                   accepts_new_assignments BOOLEAN NOT NULL DEFAULT TRUE,
                   management_url_ciphertext TEXT,
                   certificate_sha256 TEXT,
                   outline_version TEXT,
                   public_address TEXT,
                   private_address TEXT,
                   max_active_keys INTEGER,
                   reserved_transfer_bytes BIGINT,
                   created_at TEXT NOT NULL,
                   verified_at TEXT,
                   last_healthy_at TEXT,
                   retired_at TEXT,
                   UNIQUE(provider, provider_resource_id)
               )""",
            """INSERT INTO vpn_endpoints
               (id, code, provider, region, state, accepts_new_assignments, created_at)
               VALUES ('legacy-default', 'SGP-01', 'manual', 'sgp1', 'ACTIVE', TRUE,
                       '2026-09-01T00:00:00+00:00') ON CONFLICT(id) DO NOTHING""",
            """CREATE TABLE IF NOT EXISTS endpoint_plan_limits (
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   plan_code TEXT NOT NULL,
                   enabled BOOLEAN NOT NULL DEFAULT TRUE,
                   max_active_assignments INTEGER,
                   reservation_weight_bytes BIGINT,
                   PRIMARY KEY(endpoint_id, plan_code)
               )""",
            """CREATE TABLE IF NOT EXISTS endpoint_capacity_snapshots (
                   id TEXT PRIMARY KEY,
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   observed_at TEXT NOT NULL,
                   healthy BOOLEAN NOT NULL,
                   active_key_count INTEGER,
                   observed_transfer_bytes BIGINT,
                   cpu_percent DOUBLE PRECISION,
                   memory_percent DOUBLE PRECISION,
                   peak_mbps DOUBLE PRECISION,
                   management_latency_ms DOUBLE PRECISION,
                   last_error TEXT
               )""",
            "ALTER TABLE paid_vpn_keys ADD COLUMN IF NOT EXISTS endpoint_id TEXT NOT NULL DEFAULT 'legacy-default' REFERENCES vpn_endpoints(id)",
            "ALTER TABLE paid_vpn_keys DROP CONSTRAINT IF EXISTS paid_vpn_keys_outline_key_id_key",
            "CREATE UNIQUE INDEX IF NOT EXISTS paid_keys_endpoint_external ON paid_vpn_keys(endpoint_id, outline_key_id)",
            """CREATE TABLE IF NOT EXISTS endpoint_assignments (
                   id TEXT PRIMARY KEY,
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   subscription_id TEXT UNIQUE REFERENCES subscriptions(id),
                   free_key_id BIGINT,
                   plan_code TEXT NOT NULL,
                   status TEXT NOT NULL,
                   reason TEXT NOT NULL,
                   reserved_quota_bytes BIGINT,
                   assigned_at TEXT NOT NULL,
                   released_at TEXT,
                   CHECK ((subscription_id IS NOT NULL) <> (free_key_id IS NOT NULL))
               )""",
            "CREATE UNIQUE INDEX IF NOT EXISTS endpoint_assignment_free_key ON endpoint_assignments(free_key_id)",
            """INSERT INTO endpoint_assignments
               (id, endpoint_id, subscription_id, free_key_id, plan_code, status,
                reason, reserved_quota_bytes, assigned_at, released_at)
               SELECT 'paid-' || s.id, k.endpoint_id, s.id, NULL, s.plan_code,
                      CASE WHEN k.status = 'active' AND s.status = 'active' THEN 'active' ELSE 'released' END,
                      'legacy-backfill', k.quota_bytes, k.created_at,
                      CASE WHEN k.status = 'active' AND s.status = 'active' THEN NULL ELSE k.created_at END
               FROM paid_vpn_keys k JOIN subscriptions s ON s.id = k.subscription_id
               ON CONFLICT(subscription_id) DO NOTHING""",
            "ALTER TABLE provisioning_jobs ADD COLUMN IF NOT EXISTS endpoint_assignment_id TEXT REFERENCES endpoint_assignments(id)",
            """CREATE TABLE IF NOT EXISTS infrastructure_jobs (
                   id TEXT PRIMARY KEY,
                   operation TEXT NOT NULL,
                   endpoint_id TEXT,
                   status TEXT NOT NULL,
                   attempts INTEGER NOT NULL DEFAULT 0,
                   next_attempt_at TEXT NOT NULL,
                   locked_at TEXT,
                   provider_resource_id TEXT,
                   provider_action_id TEXT,
                   request_fingerprint TEXT NOT NULL UNIQUE,
                   last_error TEXT,
                   created_at TEXT NOT NULL,
                   completed_at TEXT
               )""",
            """CREATE TABLE IF NOT EXISTS infrastructure_events (
                   id TEXT PRIMARY KEY,
                   infrastructure_job_id TEXT,
                   endpoint_id TEXT,
                   event_type TEXT NOT NULL,
                   metadata_json TEXT NOT NULL DEFAULT '{}',
                   created_at TEXT NOT NULL
               )""",
        ),
    ),
    Migration(
        3,
        "customer_endpoint_preferences",
        sqlite_statements=(
            "ALTER TABLE orders ADD COLUMN requested_endpoint_id TEXT",
            "ALTER TABLE subscriptions ADD COLUMN preferred_endpoint_id TEXT",
        ),
        postgres_statements=(
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS requested_endpoint_id TEXT",
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS preferred_endpoint_id TEXT",
        ),
    ),
    Migration(
        4,
        "generation_accounting_and_ambiguous_intents",
        sqlite_statements=(
            "ALTER TABLE subscriptions ADD COLUMN consumed_bytes INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE subscriptions ADD COLUMN quota_exhausted_at TEXT",
            "ALTER TABLE provisioning_jobs ADD COLUMN remote_credential_id TEXT",
            "ALTER TABLE provisioning_jobs ADD COLUMN remote_ownership TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE provisioning_jobs ADD COLUMN remote_state TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE paid_vpn_keys ADD COLUMN remote_ownership TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE paid_vpn_keys ADD COLUMN remote_state TEXT NOT NULL DEFAULT 'unknown'",
            "CREATE INDEX IF NOT EXISTS subscriptions_quota_status ON subscriptions(status, quota_exhausted_at)",
            """CREATE TABLE IF NOT EXISTS credential_generations (
                   generation_id TEXT PRIMARY KEY,
                   entitlement_key TEXT NOT NULL,
                   source_type TEXT NOT NULL CHECK (source_type IN ('paid', 'free')),
                   source_id TEXT NOT NULL,
                   endpoint_id TEXT NOT NULL,
                   protocol TEXT NOT NULL,
                   external_id TEXT NOT NULL,
                   access_url_ciphertext TEXT,
                   generation_no INTEGER NOT NULL CHECK (generation_no > 0),
                   status TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('pending', 'active', 'retiring', 'unknown', 'revoked', 'failed')),
                   remote_state TEXT NOT NULL DEFAULT 'unknown'
                       CHECK (remote_state IN ('unknown', 'observed', 'delete_requested', 'revoked_verified')),
                   intent_key TEXT,
                   created_at TEXT NOT NULL,
                   revoked_at TEXT,
                   revoke_verified_at TEXT,
                   UNIQUE(entitlement_key, generation_no),
                   UNIQUE(entitlement_key, endpoint_id, external_id)
               )""",
            "CREATE INDEX IF NOT EXISTS credential_generations_accounting ON credential_generations(entitlement_key, status, endpoint_id)",
            """CREATE TABLE IF NOT EXISTS quota_leases (
                   lease_id TEXT PRIMARY KEY,
                   entitlement_key TEXT NOT NULL,
                   generation_id TEXT NOT NULL REFERENCES credential_generations(generation_id),
                   endpoint_id TEXT NOT NULL,
                   lease_bytes INTEGER NOT NULL CHECK (lease_bytes > 0),
                   used_bytes INTEGER NOT NULL DEFAULT 0 CHECK (used_bytes >= 0),
                   expires_at TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'released', 'expired', 'exhausted')),
                   created_at TEXT NOT NULL,
                   released_at TEXT,
                   UNIQUE(generation_id, status)
               )""",
            "CREATE INDEX IF NOT EXISTS quota_leases_entitlement ON quota_leases(entitlement_key, status, expires_at)",
            """CREATE TABLE IF NOT EXISTS entitlement_usage_epochs (
                   epoch_id TEXT PRIMARY KEY,
                   entitlement_key TEXT NOT NULL,
                   generation_id TEXT NOT NULL REFERENCES credential_generations(generation_id),
                   endpoint_id TEXT NOT NULL,
                   source_external_id TEXT NOT NULL,
                   epoch_no INTEGER NOT NULL CHECK (epoch_no > 0),
                   last_remote_bytes INTEGER NOT NULL CHECK (last_remote_bytes >= 0),
                   credited_bytes INTEGER NOT NULL DEFAULT 0 CHECK (credited_bytes >= 0),
                   reset_count INTEGER NOT NULL DEFAULT 0 CHECK (reset_count >= 0),
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'reset', 'closed')),
                   last_observed_at TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   UNIQUE(entitlement_key, generation_id, endpoint_id, source_external_id, epoch_no)
               )""",
            "CREATE INDEX IF NOT EXISTS entitlement_usage_epochs_lookup ON entitlement_usage_epochs(entitlement_key, generation_id, endpoint_id, source_external_id, status)",
            """CREATE TABLE IF NOT EXISTS entitlement_usage_samples (
                   sample_id TEXT PRIMARY KEY,
                   epoch_id TEXT NOT NULL REFERENCES entitlement_usage_epochs(epoch_id),
                   entitlement_key TEXT NOT NULL,
                   generation_id TEXT NOT NULL REFERENCES credential_generations(generation_id),
                   endpoint_id TEXT NOT NULL,
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
            "CREATE INDEX IF NOT EXISTS entitlement_usage_samples_lookup ON entitlement_usage_samples(entitlement_key, observed_at)",
            """CREATE TABLE IF NOT EXISTS entitlement_quota_ledger (
                   entry_id TEXT PRIMARY KEY,
                   entitlement_key TEXT NOT NULL,
                   generation_id TEXT,
                   endpoint_id TEXT,
                   lease_id TEXT,
                   epoch_id TEXT,
                   event_type TEXT NOT NULL CHECK (event_type IN ('grant', 'usage', 'release', 'exhaust', 'counter_reset', 'reconcile', 'stale_observation')),
                   bytes INTEGER NOT NULL CHECK (bytes >= 0),
                   consumed_bytes INTEGER NOT NULL CHECK (consumed_bytes >= 0),
                   remaining_bytes INTEGER NOT NULL CHECK (remaining_bytes >= 0),
                   idempotency_key TEXT NOT NULL UNIQUE,
                   details_json TEXT NOT NULL DEFAULT '{}',
                   created_at TEXT NOT NULL
               )""",
            "CREATE INDEX IF NOT EXISTS entitlement_quota_ledger_lookup ON entitlement_quota_ledger(entitlement_key, created_at)",
        ),
        postgres_statements=(
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS consumed_bytes BIGINT NOT NULL DEFAULT 0",
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS quota_exhausted_at TIMESTAMPTZ",
            "ALTER TABLE provisioning_jobs ADD COLUMN IF NOT EXISTS remote_credential_id TEXT",
            "ALTER TABLE provisioning_jobs ADD COLUMN IF NOT EXISTS remote_ownership TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE provisioning_jobs ADD COLUMN IF NOT EXISTS remote_state TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE paid_vpn_keys ADD COLUMN IF NOT EXISTS remote_ownership TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE paid_vpn_keys ADD COLUMN IF NOT EXISTS remote_state TEXT NOT NULL DEFAULT 'unknown'",
            "CREATE INDEX IF NOT EXISTS subscriptions_quota_status ON subscriptions(status, quota_exhausted_at)",
            """CREATE TABLE IF NOT EXISTS credential_generations (
                   generation_id TEXT PRIMARY KEY,
                   entitlement_key TEXT NOT NULL,
                   source_type TEXT NOT NULL CHECK (source_type IN ('paid', 'free')),
                   source_id TEXT NOT NULL,
                   endpoint_id TEXT NOT NULL,
                   protocol TEXT NOT NULL,
                   external_id TEXT NOT NULL,
                   access_url_ciphertext TEXT,
                   generation_no INTEGER NOT NULL CHECK (generation_no > 0),
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('pending', 'active', 'retiring', 'unknown', 'revoked', 'failed')),
                   remote_state TEXT NOT NULL DEFAULT 'unknown' CHECK (remote_state IN ('unknown', 'observed', 'delete_requested', 'revoked_verified')),
                   intent_key TEXT,
                   created_at TIMESTAMPTZ NOT NULL,
                   revoked_at TIMESTAMPTZ,
                   revoke_verified_at TIMESTAMPTZ,
                   UNIQUE(entitlement_key, generation_no),
                   UNIQUE(entitlement_key, endpoint_id, external_id)
               )""",
            "CREATE INDEX IF NOT EXISTS credential_generations_accounting ON credential_generations(entitlement_key, status, endpoint_id)",
            """CREATE TABLE IF NOT EXISTS quota_leases (
                   lease_id TEXT PRIMARY KEY,
                   entitlement_key TEXT NOT NULL,
                   generation_id TEXT NOT NULL REFERENCES credential_generations(generation_id),
                   endpoint_id TEXT NOT NULL,
                   lease_bytes BIGINT NOT NULL CHECK (lease_bytes > 0),
                   used_bytes BIGINT NOT NULL DEFAULT 0 CHECK (used_bytes >= 0),
                   expires_at TIMESTAMPTZ NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released', 'expired', 'exhausted')),
                   created_at TIMESTAMPTZ NOT NULL,
                   released_at TIMESTAMPTZ,
                   UNIQUE(generation_id, status)
               )""",
            "CREATE INDEX IF NOT EXISTS quota_leases_entitlement ON quota_leases(entitlement_key, status, expires_at)",
            """CREATE TABLE IF NOT EXISTS entitlement_usage_epochs (
                   epoch_id TEXT PRIMARY KEY,
                   entitlement_key TEXT NOT NULL,
                   generation_id TEXT NOT NULL REFERENCES credential_generations(generation_id),
                   endpoint_id TEXT NOT NULL,
                   source_external_id TEXT NOT NULL,
                   epoch_no INTEGER NOT NULL CHECK (epoch_no > 0),
                   last_remote_bytes BIGINT NOT NULL CHECK (last_remote_bytes >= 0),
                   credited_bytes BIGINT NOT NULL DEFAULT 0 CHECK (credited_bytes >= 0),
                   reset_count INTEGER NOT NULL DEFAULT 0 CHECK (reset_count >= 0),
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'reset', 'closed')),
                   last_observed_at TIMESTAMPTZ NOT NULL,
                   created_at TIMESTAMPTZ NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL,
                   UNIQUE(entitlement_key, generation_id, endpoint_id, source_external_id, epoch_no)
               )""",
            "CREATE INDEX IF NOT EXISTS entitlement_usage_epochs_lookup ON entitlement_usage_epochs(entitlement_key, generation_id, endpoint_id, source_external_id, status)",
            """CREATE TABLE IF NOT EXISTS entitlement_usage_samples (
                   sample_id TEXT PRIMARY KEY,
                   epoch_id TEXT NOT NULL REFERENCES entitlement_usage_epochs(epoch_id),
                   entitlement_key TEXT NOT NULL,
                   generation_id TEXT NOT NULL REFERENCES credential_generations(generation_id),
                   endpoint_id TEXT NOT NULL,
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
            "CREATE INDEX IF NOT EXISTS entitlement_usage_samples_lookup ON entitlement_usage_samples(entitlement_key, observed_at)",
            """CREATE TABLE IF NOT EXISTS entitlement_quota_ledger (
                   entry_id TEXT PRIMARY KEY,
                   entitlement_key TEXT NOT NULL,
                   generation_id TEXT,
                   endpoint_id TEXT,
                   lease_id TEXT,
                   epoch_id TEXT,
                   event_type TEXT NOT NULL CHECK (event_type IN ('grant', 'usage', 'release', 'exhaust', 'counter_reset', 'reconcile', 'stale_observation')),
                   bytes BIGINT NOT NULL CHECK (bytes >= 0),
                   consumed_bytes BIGINT NOT NULL CHECK (consumed_bytes >= 0),
                   remaining_bytes BIGINT NOT NULL CHECK (remaining_bytes >= 0),
                   idempotency_key TEXT NOT NULL UNIQUE,
                   details_json TEXT NOT NULL DEFAULT '{}',
                   created_at TIMESTAMPTZ NOT NULL
               )""",
            "CREATE INDEX IF NOT EXISTS entitlement_quota_ledger_lookup ON entitlement_quota_ledger(entitlement_key, created_at)",
        ),
    ),
    Migration(
        5,
        "durable_route_failover_decisions",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS route_failover_policies (
                   entitlement_key TEXT PRIMARY KEY,
                   enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
                   failure_threshold INTEGER NOT NULL DEFAULT 3 CHECK (failure_threshold > 0),
                   recovery_threshold INTEGER NOT NULL DEFAULT 2 CHECK (recovery_threshold > 0),
                   cooldown_seconds INTEGER NOT NULL DEFAULT 300 CHECK (cooldown_seconds >= 0),
                   standby_lease_bytes INTEGER NOT NULL DEFAULT 104857600 CHECK (standby_lease_bytes > 0),
                   max_attempts INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS route_failover_state (
                   generation_id TEXT PRIMARY KEY REFERENCES credential_generations(generation_id),
                   failure_streak INTEGER NOT NULL DEFAULT 0 CHECK (failure_streak >= 0),
                   success_streak INTEGER NOT NULL DEFAULT 0 CHECK (success_streak >= 0),
                   last_outcome TEXT,
                   last_observed_at TEXT,
                   cooldown_until TEXT,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS route_observations (
                   observation_id TEXT PRIMARY KEY,
                   generation_id TEXT NOT NULL REFERENCES credential_generations(generation_id),
                   entitlement_key TEXT NOT NULL,
                   endpoint_id TEXT NOT NULL,
                   network_bucket TEXT NOT NULL,
                   outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failure')),
                   latency_ms INTEGER,
                   reason TEXT,
                   observed_at TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   UNIQUE(generation_id, endpoint_id, network_bucket, outcome, observed_at)
               )""",
            "CREATE INDEX IF NOT EXISTS route_observations_lookup ON route_observations(generation_id, observed_at)",
            """CREATE TABLE IF NOT EXISTS failover_decisions (
                   decision_id TEXT PRIMARY KEY,
                   idempotency_key TEXT NOT NULL UNIQUE,
                   entitlement_key TEXT NOT NULL,
                   source_generation_id TEXT NOT NULL REFERENCES credential_generations(generation_id),
                   source_endpoint_id TEXT NOT NULL,
                   target_endpoint_id TEXT NOT NULL,
                   target_generation_id TEXT REFERENCES credential_generations(generation_id),
                   trigger TEXT NOT NULL,
                   network_bucket TEXT NOT NULL,
                   state TEXT NOT NULL DEFAULT 'pending'
                       CHECK (state IN ('pending', 'creating', 'verified', 'committed', 'failed', 'rolled_back')),
                   attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                   next_attempt_at TEXT NOT NULL,
                   locked_at TEXT,
                   last_error TEXT,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   completed_at TEXT
               )""",
            "CREATE INDEX IF NOT EXISTS failover_decisions_due ON failover_decisions(state, next_attempt_at)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS route_failover_policies (
                   entitlement_key TEXT PRIMARY KEY,
                   enabled BOOLEAN NOT NULL DEFAULT FALSE,
                   failure_threshold INTEGER NOT NULL DEFAULT 3 CHECK (failure_threshold > 0),
                   recovery_threshold INTEGER NOT NULL DEFAULT 2 CHECK (recovery_threshold > 0),
                   cooldown_seconds INTEGER NOT NULL DEFAULT 300 CHECK (cooldown_seconds >= 0),
                   standby_lease_bytes BIGINT NOT NULL DEFAULT 104857600 CHECK (standby_lease_bytes > 0),
                   max_attempts INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
                   created_at TIMESTAMPTZ NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS route_failover_state (
                   generation_id TEXT PRIMARY KEY REFERENCES credential_generations(generation_id),
                   failure_streak INTEGER NOT NULL DEFAULT 0 CHECK (failure_streak >= 0),
                   success_streak INTEGER NOT NULL DEFAULT 0 CHECK (success_streak >= 0),
                   last_outcome TEXT,
                   last_observed_at TIMESTAMPTZ,
                   cooldown_until TIMESTAMPTZ,
                   updated_at TIMESTAMPTZ NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS route_observations (
                   observation_id TEXT PRIMARY KEY,
                   generation_id TEXT NOT NULL REFERENCES credential_generations(generation_id),
                   entitlement_key TEXT NOT NULL,
                   endpoint_id TEXT NOT NULL,
                   network_bucket TEXT NOT NULL,
                   outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failure')),
                   latency_ms INTEGER,
                   reason TEXT,
                   observed_at TIMESTAMPTZ NOT NULL,
                   created_at TIMESTAMPTZ NOT NULL,
                   UNIQUE(generation_id, endpoint_id, network_bucket, outcome, observed_at)
               )""",
            "CREATE INDEX IF NOT EXISTS route_observations_lookup ON route_observations(generation_id, observed_at)",
            """CREATE TABLE IF NOT EXISTS failover_decisions (
                   decision_id TEXT PRIMARY KEY,
                   idempotency_key TEXT NOT NULL UNIQUE,
                   entitlement_key TEXT NOT NULL,
                   source_generation_id TEXT NOT NULL REFERENCES credential_generations(generation_id),
                   source_endpoint_id TEXT NOT NULL,
                   target_endpoint_id TEXT NOT NULL,
                   target_generation_id TEXT REFERENCES credential_generations(generation_id),
                   trigger TEXT NOT NULL,
                   network_bucket TEXT NOT NULL,
                   state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'creating', 'verified', 'committed', 'failed', 'rolled_back')),
                   attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                   next_attempt_at TIMESTAMPTZ NOT NULL,
                   locked_at TIMESTAMPTZ,
                   last_error TEXT,
                   created_at TIMESTAMPTZ NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL,
                   completed_at TIMESTAMPTZ
               )""",
            "CREATE INDEX IF NOT EXISTS failover_decisions_due ON failover_decisions(state, next_attempt_at)",
        ),
    ),
    Migration(
        6,
        "usage_provenance_and_remote_lease_proof",
        sqlite_statements=(
            "ALTER TABLE credential_generations ADD COLUMN usage_baseline_provenance TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE credential_generations ADD COLUMN usage_baseline_bytes INTEGER",
        ),
        postgres_statements=(
            "ALTER TABLE credential_generations ADD COLUMN IF NOT EXISTS usage_baseline_provenance TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE credential_generations ADD COLUMN IF NOT EXISTS usage_baseline_bytes BIGINT",
        ),
    ),
    Migration(
        7,
        "opaque_accounts_and_managed_devices",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS accounts (
                   account_id TEXT PRIMARY KEY,
                   status TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'suspended', 'closed')),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS account_identities (
                   account_id TEXT NOT NULL REFERENCES accounts(account_id),
                   identity_type TEXT NOT NULL,
                   identity_value TEXT NOT NULL,
                   verified_at TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   PRIMARY KEY (identity_type, identity_value),
                   UNIQUE (account_id, identity_type, identity_value)
               )""",
            """CREATE TABLE IF NOT EXISTS device_revocation_epochs (
                   account_id TEXT PRIMARY KEY REFERENCES accounts(account_id),
                   epoch INTEGER NOT NULL DEFAULT 0 CHECK (epoch >= 0),
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS pairing_tokens (
                   token_hash TEXT PRIMARY KEY,
                   account_id TEXT NOT NULL REFERENCES accounts(account_id),
                   requested_by INTEGER NOT NULL,
                   status TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'consumed', 'expired', 'revoked')),
                   expires_at TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   consumed_at TEXT
               )""",
            "CREATE INDEX IF NOT EXISTS pairing_tokens_due ON pairing_tokens(status, expires_at)",
            """CREATE TABLE IF NOT EXISTS devices (
                   device_id TEXT PRIMARY KEY,
                   account_id TEXT NOT NULL REFERENCES accounts(account_id),
                   public_key TEXT NOT NULL UNIQUE,
                   label TEXT NOT NULL DEFAULT '',
                   status TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'revoked')),
                   created_at TEXT NOT NULL,
                   last_seen_at TEXT,
                   revoked_at TEXT
               )""",
            "CREATE INDEX IF NOT EXISTS devices_account_status ON devices(account_id, status)",
            """CREATE TABLE IF NOT EXISTS device_sessions (
                   session_id TEXT PRIMARY KEY,
                   device_id TEXT NOT NULL REFERENCES devices(device_id),
                   manifest_version INTEGER NOT NULL DEFAULT 1,
                   created_at TEXT NOT NULL,
                   last_seen_at TEXT NOT NULL,
                   expires_at TEXT NOT NULL,
                   revoked_at TEXT
               )""",
            "CREATE INDEX IF NOT EXISTS device_sessions_due ON device_sessions(device_id, expires_at, revoked_at)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS accounts (
                   account_id TEXT PRIMARY KEY,
                   status TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'suspended', 'closed')),
                   created_at TIMESTAMPTZ NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS account_identities (
                   account_id TEXT NOT NULL REFERENCES accounts(account_id),
                   identity_type TEXT NOT NULL,
                   identity_value TEXT NOT NULL,
                   verified_at TIMESTAMPTZ NOT NULL,
                   created_at TIMESTAMPTZ NOT NULL,
                   PRIMARY KEY (identity_type, identity_value),
                   UNIQUE (account_id, identity_type, identity_value)
               )""",
            """CREATE TABLE IF NOT EXISTS device_revocation_epochs (
                   account_id TEXT PRIMARY KEY REFERENCES accounts(account_id),
                   epoch BIGINT NOT NULL DEFAULT 0 CHECK (epoch >= 0),
                   updated_at TIMESTAMPTZ NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS pairing_tokens (
                   token_hash TEXT PRIMARY KEY,
                   account_id TEXT NOT NULL REFERENCES accounts(account_id),
                   requested_by BIGINT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'consumed', 'expired', 'revoked')),
                   expires_at TIMESTAMPTZ NOT NULL,
                   created_at TIMESTAMPTZ NOT NULL,
                   consumed_at TIMESTAMPTZ
               )""",
            "CREATE INDEX IF NOT EXISTS pairing_tokens_due ON pairing_tokens(status, expires_at)",
            """CREATE TABLE IF NOT EXISTS devices (
                   device_id TEXT PRIMARY KEY,
                   account_id TEXT NOT NULL REFERENCES accounts(account_id),
                   public_key TEXT NOT NULL UNIQUE,
                   label TEXT NOT NULL DEFAULT '',
                   status TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'revoked')),
                   created_at TIMESTAMPTZ NOT NULL,
                   last_seen_at TIMESTAMPTZ,
                   revoked_at TIMESTAMPTZ
               )""",
            "CREATE INDEX IF NOT EXISTS devices_account_status ON devices(account_id, status)",
            """CREATE TABLE IF NOT EXISTS device_sessions (
                   session_id TEXT PRIMARY KEY,
                   device_id TEXT NOT NULL REFERENCES devices(device_id),
                   manifest_version INTEGER NOT NULL DEFAULT 1,
                   created_at TIMESTAMPTZ NOT NULL,
                   last_seen_at TIMESTAMPTZ NOT NULL,
                   expires_at TIMESTAMPTZ NOT NULL,
                   revoked_at TIMESTAMPTZ
               )""",
            "CREATE INDEX IF NOT EXISTS device_sessions_due ON device_sessions(device_id, expires_at, revoked_at)",
        ),
    ),
    Migration(
        8,
        "notification_delivery_leases",
        sqlite_statements=(
            "ALTER TABLE notifications ADD COLUMN lease_owner TEXT",
            "ALTER TABLE notifications ADD COLUMN lease_token TEXT",
            "ALTER TABLE notifications ADD COLUMN lease_expires_at TEXT",
            "CREATE INDEX IF NOT EXISTS notifications_delivery_lease ON notifications(status, next_attempt_at, lease_expires_at)",
        ),
        postgres_statements=(
            "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS lease_owner TEXT",
            "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS lease_token TEXT",
            "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ",
            "CREATE INDEX IF NOT EXISTS notifications_delivery_lease ON notifications(status, next_attempt_at, lease_expires_at)",
        ),
    ),
    Migration(
        9,
        "endpoint_protocol_profiles",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS endpoint_protocol_profiles (
                   profile_id TEXT PRIMARY KEY,
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   protocol TEXT NOT NULL,
                   adapter_type TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'candidate'
                       CHECK (status IN ('candidate', 'enabled', 'degraded', 'disabled', 'retired')),
                   capabilities_json TEXT NOT NULL DEFAULT '{}',
                   verified_at TEXT,
                   last_healthy_at TEXT,
                   created_at TEXT NOT NULL,
                   retired_at TEXT,
                   UNIQUE(endpoint_id, protocol)
               )""",
            "CREATE INDEX IF NOT EXISTS endpoint_protocol_profiles_lookup ON endpoint_protocol_profiles(endpoint_id, status, protocol)",
            """INSERT OR IGNORE INTO endpoint_protocol_profiles
                   (profile_id, endpoint_id, protocol, adapter_type, status,
                    capabilities_json, verified_at, last_healthy_at, created_at)
               SELECT 'outline:' || id, id, 'outline', 'outline', 'enabled',
                      '{}', verified_at, last_healthy_at, created_at
                 FROM vpn_endpoints""",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS endpoint_protocol_profiles (
                   profile_id TEXT PRIMARY KEY,
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   protocol TEXT NOT NULL,
                   adapter_type TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'candidate'
                       CHECK (status IN ('candidate', 'enabled', 'degraded', 'disabled', 'retired')),
                   capabilities_json TEXT NOT NULL DEFAULT '{}',
                   verified_at TIMESTAMPTZ,
                   last_healthy_at TIMESTAMPTZ,
                   created_at TIMESTAMPTZ NOT NULL,
                   retired_at TIMESTAMPTZ,
                   UNIQUE(endpoint_id, protocol)
               )""",
            "CREATE INDEX IF NOT EXISTS endpoint_protocol_profiles_lookup ON endpoint_protocol_profiles(endpoint_id, status, protocol)",
            """INSERT INTO endpoint_protocol_profiles
                   (profile_id, endpoint_id, protocol, adapter_type, status,
                    capabilities_json, verified_at, last_healthy_at, created_at)
               SELECT 'outline:' || id, id, 'outline', 'outline', 'enabled',
                      '{}', NULLIF(verified_at, '')::timestamptz,
                      NULLIF(last_healthy_at, '')::timestamptz,
                      created_at::timestamptz
                 FROM vpn_endpoints
                ON CONFLICT(endpoint_id, protocol) DO NOTHING""",
        ),
    ),
    Migration(
        10,
        "protocol_health_observations",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS endpoint_protocol_observations (
                   observation_id TEXT PRIMARY KEY,
                   profile_id TEXT NOT NULL REFERENCES endpoint_protocol_profiles(profile_id),
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   protocol TEXT NOT NULL,
                   signal TEXT NOT NULL,
                   status TEXT NOT NULL
                       CHECK (status IN ('healthy', 'degraded', 'failed', 'unsupported', 'unknown')),
                   details_json TEXT NOT NULL DEFAULT '{}',
                   latency_ms REAL,
                   observed_at TEXT NOT NULL,
                   expires_at TEXT,
                   source TEXT NOT NULL,
                   created_at TEXT NOT NULL
               )""",
            "CREATE INDEX IF NOT EXISTS endpoint_protocol_observations_lookup ON endpoint_protocol_observations(endpoint_id, protocol, signal, observed_at)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS endpoint_protocol_observations (
                   observation_id TEXT PRIMARY KEY,
                   profile_id TEXT NOT NULL REFERENCES endpoint_protocol_profiles(profile_id),
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   protocol TEXT NOT NULL,
                   signal TEXT NOT NULL,
                   status TEXT NOT NULL
                       CHECK (status IN ('healthy', 'degraded', 'failed', 'unsupported', 'unknown')),
                   details_json TEXT NOT NULL DEFAULT '{}',
                   latency_ms DOUBLE PRECISION,
                   observed_at TIMESTAMPTZ NOT NULL,
                   expires_at TIMESTAMPTZ,
                   source TEXT NOT NULL,
                   created_at TIMESTAMPTZ NOT NULL
               )""",
            "CREATE INDEX IF NOT EXISTS endpoint_protocol_observations_lookup ON endpoint_protocol_observations(endpoint_id, protocol, signal, observed_at)",
        ),
    ),
    Migration(
        11,
        "failover_safety_controls",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS route_failover_controls (
                   scope TEXT NOT NULL CHECK (scope IN ('global', 'region', 'endpoint')),
                   scope_key TEXT NOT NULL,
                   paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
                   max_migrations_per_window INTEGER NOT NULL CHECK (max_migrations_per_window > 0),
                   window_seconds INTEGER NOT NULL CHECK (window_seconds > 0),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   PRIMARY KEY (scope, scope_key)
               )""",
            """INSERT INTO route_failover_controls
               (scope, scope_key, paused, max_migrations_per_window, window_seconds,
                created_at, updated_at)
               VALUES ('global', 'global', 0, 100, 300,
                       '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')
               ON CONFLICT(scope, scope_key) DO NOTHING""",
            """CREATE TABLE IF NOT EXISTS route_failover_control_windows (
                   scope TEXT NOT NULL,
                   scope_key TEXT NOT NULL,
                   window_start TEXT NOT NULL,
                   migration_count INTEGER NOT NULL DEFAULT 0 CHECK (migration_count >= 0),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   PRIMARY KEY (scope, scope_key, window_start),
                   FOREIGN KEY (scope, scope_key)
                       REFERENCES route_failover_controls(scope, scope_key)
               )""",
            "CREATE INDEX IF NOT EXISTS route_failover_control_windows_lookup ON route_failover_control_windows(updated_at)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS route_failover_controls (
                   scope TEXT NOT NULL CHECK (scope IN ('global', 'region', 'endpoint')),
                   scope_key TEXT NOT NULL,
                   paused BOOLEAN NOT NULL DEFAULT FALSE,
                   max_migrations_per_window INTEGER NOT NULL CHECK (max_migrations_per_window > 0),
                   window_seconds INTEGER NOT NULL CHECK (window_seconds > 0),
                   created_at TIMESTAMPTZ NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL,
                   PRIMARY KEY (scope, scope_key)
               )""",
            """INSERT INTO route_failover_controls
               (scope, scope_key, paused, max_migrations_per_window, window_seconds,
                created_at, updated_at)
               VALUES ('global', 'global', FALSE, 100, 300,
                       '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')
               ON CONFLICT(scope, scope_key) DO NOTHING""",
            """CREATE TABLE IF NOT EXISTS route_failover_control_windows (
                   scope TEXT NOT NULL,
                   scope_key TEXT NOT NULL,
                   window_start TIMESTAMPTZ NOT NULL,
                   migration_count BIGINT NOT NULL DEFAULT 0 CHECK (migration_count >= 0),
                   created_at TIMESTAMPTZ NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL,
                   PRIMARY KEY (scope, scope_key, window_start),
                   FOREIGN KEY (scope, scope_key)
                       REFERENCES route_failover_controls(scope, scope_key)
               )""",
            "CREATE INDEX IF NOT EXISTS route_failover_control_windows_lookup ON route_failover_control_windows(updated_at)",
        ),
    ),
    Migration(
        12,
        "versioned_failover_decisions",
        sqlite_statements=(
            "ALTER TABLE route_failover_policies ADD COLUMN policy_version INTEGER NOT NULL DEFAULT 1 CHECK (policy_version > 0)",
            "ALTER TABLE failover_decisions ADD COLUMN policy_version INTEGER NOT NULL DEFAULT 1 CHECK (policy_version > 0)",
        ),
        postgres_statements=(
            "ALTER TABLE route_failover_policies ADD COLUMN IF NOT EXISTS policy_version INTEGER NOT NULL DEFAULT 1 CHECK (policy_version > 0)",
            "ALTER TABLE failover_decisions ADD COLUMN IF NOT EXISTS policy_version INTEGER NOT NULL DEFAULT 1 CHECK (policy_version > 0)",
        ),
    ),
    Migration(
        13,
        "endpoint_health_recovery_cooldown",
        sqlite_statements=(
            "ALTER TABLE vpn_endpoints ADD COLUMN health_state_changed_at TEXT",
        ),
        postgres_statements=(
            "ALTER TABLE vpn_endpoints ADD COLUMN IF NOT EXISTS health_state_changed_at TIMESTAMPTZ",
        ),
    ),
    Migration(
        14,
        "failover_policy_history",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS route_failover_policy_versions (
                   entitlement_key TEXT NOT NULL,
                   policy_version INTEGER NOT NULL CHECK (policy_version > 0),
                   enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                   failure_threshold INTEGER NOT NULL CHECK (failure_threshold > 0),
                   recovery_threshold INTEGER NOT NULL CHECK (recovery_threshold > 0),
                   cooldown_seconds INTEGER NOT NULL CHECK (cooldown_seconds >= 0),
                   standby_lease_bytes INTEGER NOT NULL CHECK (standby_lease_bytes > 0),
                   max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
                   created_at TEXT NOT NULL,
                   PRIMARY KEY (entitlement_key, policy_version)
               )""",
            """INSERT OR IGNORE INTO route_failover_policy_versions
                   (entitlement_key, policy_version, enabled, failure_threshold,
                    recovery_threshold, cooldown_seconds, standby_lease_bytes,
                    max_attempts, created_at)
                SELECT entitlement_key, policy_version, enabled, failure_threshold,
                       recovery_threshold, cooldown_seconds, standby_lease_bytes,
                       max_attempts, updated_at
                  FROM route_failover_policies""",
            "CREATE INDEX IF NOT EXISTS route_failover_policy_versions_lookup ON route_failover_policy_versions(created_at)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS route_failover_policy_versions (
                   entitlement_key TEXT NOT NULL,
                   policy_version INTEGER NOT NULL CHECK (policy_version > 0),
                   enabled BOOLEAN NOT NULL,
                   failure_threshold INTEGER NOT NULL CHECK (failure_threshold > 0),
                   recovery_threshold INTEGER NOT NULL CHECK (recovery_threshold > 0),
                   cooldown_seconds INTEGER NOT NULL CHECK (cooldown_seconds >= 0),
                   standby_lease_bytes BIGINT NOT NULL CHECK (standby_lease_bytes > 0),
                   max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
                   created_at TIMESTAMPTZ NOT NULL,
                   PRIMARY KEY (entitlement_key, policy_version)
               )""",
            """INSERT INTO route_failover_policy_versions
                   (entitlement_key, policy_version, enabled, failure_threshold,
                    recovery_threshold, cooldown_seconds, standby_lease_bytes,
                    max_attempts, created_at)
                SELECT entitlement_key, policy_version, enabled, failure_threshold,
                       recovery_threshold, cooldown_seconds, standby_lease_bytes,
                       max_attempts, updated_at
                  FROM route_failover_policies
                 ON CONFLICT (entitlement_key, policy_version) DO NOTHING""",
            "CREATE INDEX IF NOT EXISTS route_failover_policy_versions_lookup ON route_failover_policy_versions(created_at)",
        ),
    ),
    Migration(
        15,
        "protocol_aware_paid_assignments",
        sqlite_statements=(
            "ALTER TABLE orders ADD COLUMN requested_protocol TEXT",
            "ALTER TABLE subscriptions ADD COLUMN preferred_protocol TEXT",
            "ALTER TABLE endpoint_assignments ADD COLUMN protocol TEXT NOT NULL DEFAULT 'outline' CHECK (length(protocol) > 0)",
        ),
        postgres_statements=(
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS requested_protocol TEXT",
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS preferred_protocol TEXT",
            "ALTER TABLE endpoint_assignments ADD COLUMN IF NOT EXISTS protocol TEXT NOT NULL DEFAULT 'outline' CHECK (char_length(protocol) > 0)",
        ),
    ),
)


def apply_migrations(
    connection: Any,
    *,
    component: str,
    dialect: str,
    migrations: Iterable[Migration],
    applied_at: str | None = None,
) -> None:
    """Apply missing migrations and validate immutable version/name history.

    Phase 2 adopts the existing schema as version 1 for each component. Future
    schema changes belong in this registry and must use idempotent statements.
    """
    connection.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               component TEXT NOT NULL,
               version INTEGER NOT NULL,
               name TEXT NOT NULL,
               applied_at TEXT NOT NULL,
               PRIMARY KEY (component, version)
           )"""
    )
    rows = connection.execute(
        "SELECT version, name FROM schema_migrations WHERE component = ?",
        (component,),
    ).fetchall()
    recorded = {
        int(row["version"] if hasattr(row, "keys") else row[0]): str(
            row["name"] if hasattr(row, "keys") else row[1]
        )
        for row in rows
    }
    ordered = sorted(tuple(migrations), key=lambda migration: migration.version)
    if len({migration.version for migration in ordered}) != len(ordered):
        raise MigrationError(f"Duplicate migration version for {component}")
    if any(migration.version <= 0 for migration in ordered):
        raise MigrationError(f"Migration versions for {component} must be positive")
    known_versions = {migration.version for migration in ordered}
    unknown_versions = sorted(set(recorded) - known_versions)
    if unknown_versions:
        versions = ", ".join(str(version) for version in unknown_versions)
        raise MigrationError(
            f"Database has unknown {component} migration version(s): {versions}"
        )
    timestamp = applied_at or datetime.now(UTC).isoformat()
    for migration in ordered:
        existing_name = recorded.get(migration.version)
        if existing_name is not None:
            if existing_name != migration.name:
                raise MigrationError(
                    f"Migration {component}:{migration.version} was renamed "
                    f"from {existing_name!r} to {migration.name!r}"
                )
            continue
        for statement in migration.statements_for(dialect):
            connection.execute(statement)
        connection.execute(
            """INSERT INTO schema_migrations
               (component, version, name, applied_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(component, version) DO NOTHING""",
            (component, migration.version, migration.name, timestamp),
        )
