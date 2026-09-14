"""Numbered, component-scoped database migration registry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import sqlite3
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
    Migration(
        9,
        "durable_endpoint_usage_snapshots",
        sqlite_statements=(
            "ALTER TABLE keys ADD COLUMN last_usage_observed_at TEXT",
            """CREATE TABLE IF NOT EXISTS endpoint_usage_snapshots (
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   external_id TEXT NOT NULL,
                   protocol TEXT NOT NULL DEFAULT 'outline',
                   observed_bytes INTEGER NOT NULL CHECK (observed_bytes >= 0),
                   observed_at TEXT NOT NULL,
                   source TEXT NOT NULL DEFAULT 'maintenance',
                   PRIMARY KEY (endpoint_id, external_id)
               )""",
            "CREATE INDEX IF NOT EXISTS endpoint_usage_snapshots_latest ON endpoint_usage_snapshots(endpoint_id, observed_at)",
        ),
        postgres_statements=(
            "ALTER TABLE keys ADD COLUMN IF NOT EXISTS last_usage_observed_at TEXT",
            """CREATE TABLE IF NOT EXISTS endpoint_usage_snapshots (
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   external_id TEXT NOT NULL,
                   protocol TEXT NOT NULL DEFAULT 'outline',
                   observed_bytes BIGINT NOT NULL CHECK (observed_bytes >= 0),
                   observed_at TEXT NOT NULL,
                   source TEXT NOT NULL DEFAULT 'maintenance',
                   PRIMARY KEY (endpoint_id, external_id)
               )""",
            "CREATE INDEX IF NOT EXISTS endpoint_usage_snapshots_latest ON endpoint_usage_snapshots(endpoint_id, observed_at)",
        ),
    ),
    Migration(
        10,
        "endpoint_usage_snapshot_status",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS endpoint_usage_snapshot_status (
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   protocol TEXT NOT NULL DEFAULT 'outline',
                   status TEXT NOT NULL CHECK (status IN ('healthy', 'failed', 'unknown')),
                   error_type TEXT,
                   observed_at TEXT NOT NULL,
                   source TEXT NOT NULL DEFAULT 'maintenance',
                   PRIMARY KEY (endpoint_id, protocol)
               )""",
            "CREATE INDEX IF NOT EXISTS endpoint_usage_snapshot_status_latest ON endpoint_usage_snapshot_status(observed_at)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS endpoint_usage_snapshot_status (
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   protocol TEXT NOT NULL DEFAULT 'outline',
                   status TEXT NOT NULL CHECK (status IN ('healthy', 'failed', 'unknown')),
                   error_type TEXT,
                   observed_at TEXT NOT NULL,
                   source TEXT NOT NULL DEFAULT 'maintenance',
                   PRIMARY KEY (endpoint_id, protocol)
               )""",
            "CREATE INDEX IF NOT EXISTS endpoint_usage_snapshot_status_latest ON endpoint_usage_snapshot_status(observed_at)",
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
    Migration(
        16,
        "endpoint_key_inventory_reconciliation",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS endpoint_key_inventory (
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   external_id_ciphertext TEXT NOT NULL,
                   classification TEXT NOT NULL
                       CHECK (classification IN ('managed', 'unmanaged')),
                   present INTEGER NOT NULL DEFAULT 1 CHECK (present IN (0, 1)),
                   first_seen_at TEXT NOT NULL,
                   last_seen_at TEXT NOT NULL,
                   source TEXT NOT NULL DEFAULT 'maintenance',
                   PRIMARY KEY (endpoint_id, external_id_ciphertext)
               )""",
            "CREATE INDEX IF NOT EXISTS endpoint_key_inventory_present ON endpoint_key_inventory(endpoint_id, present, last_seen_at)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS endpoint_key_inventory (
                   endpoint_id TEXT NOT NULL REFERENCES vpn_endpoints(id),
                   external_id_ciphertext TEXT NOT NULL,
                   classification TEXT NOT NULL
                       CHECK (classification IN ('managed', 'unmanaged')),
                   present INTEGER NOT NULL DEFAULT 1 CHECK (present IN (0, 1)),
                   first_seen_at TIMESTAMPTZ NOT NULL,
                   last_seen_at TIMESTAMPTZ NOT NULL,
                   source TEXT NOT NULL DEFAULT 'maintenance',
                   PRIMARY KEY (endpoint_id, external_id_ciphertext)
               )""",
            "CREATE INDEX IF NOT EXISTS endpoint_key_inventory_present ON endpoint_key_inventory(endpoint_id, present, last_seen_at)",
        ),
    ),
    Migration(
        17,
        "protocol_scoped_endpoint_key_inventory",
        sqlite_statements=(
            "ALTER TABLE endpoint_key_inventory ADD COLUMN protocol TEXT NOT NULL DEFAULT 'outline'",
            "CREATE INDEX IF NOT EXISTS endpoint_key_inventory_protocol ON endpoint_key_inventory(endpoint_id, protocol, present, last_seen_at)",
        ),
        postgres_statements=(
            "ALTER TABLE endpoint_key_inventory ADD COLUMN IF NOT EXISTS protocol TEXT NOT NULL DEFAULT 'outline'",
            "CREATE INDEX IF NOT EXISTS endpoint_key_inventory_protocol ON endpoint_key_inventory(endpoint_id, protocol, present, last_seen_at)",
        ),
    ),
    Migration(
        18,
        "device_acknowledgement_replay_protection",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS device_request_receipts (
                   device_id TEXT NOT NULL REFERENCES devices(device_id),
                   request_id TEXT NOT NULL,
                   request_kind TEXT NOT NULL CHECK (request_kind IN ('ack')),
                   created_at TEXT NOT NULL,
                   PRIMARY KEY (device_id, request_id)
               )""",
            "CREATE INDEX IF NOT EXISTS device_request_receipts_cleanup ON device_request_receipts(created_at)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS device_request_receipts (
                   device_id TEXT NOT NULL REFERENCES devices(device_id),
                   request_id TEXT NOT NULL,
                   request_kind TEXT NOT NULL CHECK (request_kind IN ('ack')),
                   created_at TIMESTAMPTZ NOT NULL,
                   PRIMARY KEY (device_id, request_id)
               )""",
            "CREATE INDEX IF NOT EXISTS device_request_receipts_cleanup ON device_request_receipts(created_at)",
        ),
    ),
    Migration(
        19,
        "account_access_lifecycle_actions",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS account_access_actions (
                   action_id TEXT PRIMARY KEY,
                   account_id TEXT NOT NULL REFERENCES accounts(account_id),
                   target_status TEXT NOT NULL CHECK (target_status IN ('suspended', 'active')),
                   state TEXT NOT NULL DEFAULT 'pending'
                       CHECK (state IN ('pending', 'completed')),
                   actor_id TEXT NOT NULL,
                   reason TEXT NOT NULL DEFAULT '',
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   completed_at TEXT,
                   last_error TEXT
               )""",
            "CREATE INDEX IF NOT EXISTS account_access_actions_account ON account_access_actions(account_id, created_at)",
            "CREATE INDEX IF NOT EXISTS account_access_actions_pending ON account_access_actions(state, updated_at)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS account_access_actions (
                   action_id TEXT PRIMARY KEY,
                   account_id TEXT NOT NULL REFERENCES accounts(account_id),
                   target_status TEXT NOT NULL CHECK (target_status IN ('suspended', 'active')),
                   state TEXT NOT NULL DEFAULT 'pending'
                       CHECK (state IN ('pending', 'completed')),
                   actor_id TEXT NOT NULL,
                   reason TEXT NOT NULL DEFAULT '',
                   created_at TIMESTAMPTZ NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL,
                   completed_at TIMESTAMPTZ,
                   last_error TEXT
               )""",
            "CREATE INDEX IF NOT EXISTS account_access_actions_account ON account_access_actions(account_id, created_at)",
            "CREATE INDEX IF NOT EXISTS account_access_actions_pending ON account_access_actions(state, updated_at)",
        ),
    ),
)


# The Singapore host may still carry a database created by the previous
# modular release.  That release used the same component namespaces but kept
# extending its registry after this checkout's migration cut.  Keep those
# historical records available only for the explicitly enabled reconciliation
# path below; they are not part of the normal fresh-install registry (and thus
# do not change the normal migration contract or test fixtures).
_LEGACY_COMPATIBILITY_MIGRATIONS = {
    "free_access": (
        Migration(
            11,
            "managed_key_repair_jobs",
            sqlite_statements=(
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
                """CREATE INDEX IF NOT EXISTS managed_key_repairs_due
                   ON managed_key_repair_jobs(status, next_attempt_at)""",
            ),
            postgres_statements=(
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
                       expires_at TEXT NOT NULL,
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
                """CREATE INDEX IF NOT EXISTS managed_key_repairs_due
                   ON managed_key_repair_jobs(status, next_attempt_at)""",
            ),
        ),
        # This historical migration only widened a SQLite CHECK constraint.
        # The live database already contains the widened definition; keeping a
        # no-op compatibility record lets the registry validate it without
        # rebuilding a table during deploy.
        Migration(12, "staff_key_repair_notifications"),
    ),
    "commerce": tuple(
        Migration(version, name)
        for version, name in (
            (20, "managed_key_repair_observations"),
            (21, "durable_usage_snapshots"),
            (22, "fleet_probe_control_loop"),
            (23, "accounts_entitlements_devices_and_leases"),
            (24, "entitlement_source_identity"),
            (25, "aggregate_entitlement_usage_ledger"),
            (26, "service_routes_and_failover_control"),
        )
    ),
}


def _legacy_reconciliation_enabled() -> bool:
    """Return whether a one-time legacy schema reconciliation is allowed.

    The default remains strict so an accidental schema/version mismatch cannot
    silently mutate a database.  Operators explicitly opt in for a controlled
    deployment by setting ``AURIX_ALLOW_LEGACY_MIGRATION_RECONCILE=1``.
    """
    return os.environ.get("AURIX_ALLOW_LEGACY_MIGRATION_RECONCILE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _execute_compatibility_statement(connection: Any, statement: str, dialect: str) -> None:
    """Execute a reconciliation statement, ignoring only duplicate SQLite DDL.

    Current migrations are intentionally idempotent on fresh databases, but a
    legacy SQLite file can already contain a column added by an older registry.
    Duplicate-column/table/index errors are safe to ignore during this explicit
    compatibility pass; all other errors remain fatal and trigger rollback.
    """
    try:
        connection.execute(statement)
    except Exception as exc:
        if dialect != "sqlite" or not isinstance(exc, sqlite3.OperationalError):
            raise
        message = str(exc).lower()
        ignorable = (
            "duplicate column name" in message
            or "already exists" in message
            or "duplicate index" in message
        )
        if not ignorable:
            raise


def _prepare_legacy_table_shapes(connection: Any, component: str, dialect: str) -> None:
    """Isolate legacy tables whose names collide with the current schema.

    The previous release used ``endpoint_assignments`` for connectivity
    profile bindings (``assignment_id``/``profile_id``).  The current release
    uses that name for entitlement leases (``id``/``subscription_id`` or
    ``free_key_id``).  Keeping both under distinct names preserves the old
    records while allowing the new registry to create the shape its queries
    require.  This is deliberately limited to the known SQLite collision.
    """
    if dialect != "sqlite" or component != "commerce":
        return
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'endpoint_assignments'"
    ).fetchone()
    if row is not None:
        columns = {
            str(item[1])
            for item in connection.execute("PRAGMA table_info(endpoint_assignments)").fetchall()
        }
        if "assignment_id" in columns and "id" not in columns:
            legacy_name = "endpoint_assignments_legacy"
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (legacy_name,),
            ).fetchone():
                connection.execute(f"ALTER TABLE endpoint_assignments RENAME TO {legacy_name}")
                print(
                    "Migration compatibility mode: preserved legacy endpoint_assignments "
                    "as endpoint_assignments_legacy"
                )

    # These accounting tables are structurally incompatible (the previous
    # release keyed them by ``entitlement_id`` while the current release uses
    # the stable ``paid:<id>``/``free:<id>`` key).  Archive the old shape and
    # copy its rows into the new tables after the current migrations create
    # them.  The archive is retained in the same database for audit/recovery.
    for table in (
        "credential_generations",
        "quota_leases",
        "entitlement_usage_epochs",
        "entitlement_usage_samples",
        "entitlement_quota_ledger",
        "route_failover_policies",
        "route_failover_state",
        "route_observations",
        "failover_decisions",
    ):
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if row is None:
            continue
        columns = {
            str(item[1])
            for item in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        # ``route_failover_state`` has no entitlement column, but its foreign
        # key points at the legacy credential-generations table.  Archive it
        # alongside the other identity-keyed tables so the new FK points at
        # the current generation table after the copy.
        if "entitlement_key" in columns:
            continue
        if table == "route_failover_state" and "generation_id" not in columns:
            continue
        legacy_name = f"{table}_legacy_v1"
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (legacy_name,),
        ).fetchone():
            continue
        connection.execute(f"ALTER TABLE {table} RENAME TO {legacy_name}")
        print(f"Migration compatibility mode: preserved legacy {table} as {legacy_name}")


def _migrate_legacy_identity_tables(connection: Any, component: str, dialect: str) -> None:
    """Copy archived entitlement accounting rows into the current schema."""
    if dialect != "sqlite" or component != "commerce":
        return
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'credential_generations_legacy_v1'"
    ).fetchone():
        return

    def entitlement_key(entitlement_id: str) -> tuple[str, str, str] | None:
        row = connection.execute(
            "SELECT kind, subscription_id, source_ref FROM entitlements WHERE entitlement_id = ?",
            (entitlement_id,),
        ).fetchone()
        if row is None:
            return None
        subscription_id = str(row[1] or "").strip()
        if subscription_id:
            return (f"paid:{subscription_id}", "paid", subscription_id)
        source_ref = str(row[2] or "").strip()
        source_id = source_ref.rsplit(":", 1)[-1] if source_ref else ""
        if source_id.isdigit():
            return (f"free:{source_id}", "free", source_id)
        return None

    def keys_by_entitlement(table: str) -> dict[str, tuple[str, str, str]]:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone():
            return {}
        rows = connection.execute(f"SELECT DISTINCT entitlement_id FROM {table}").fetchall()
        result: dict[str, tuple[str, str, str]] = {}
        for row in rows:
            value = entitlement_key(str(row[0]))
            if value is not None:
                result[str(row[0])] = value
        return result

    mapping = keys_by_entitlement("credential_generations_legacy_v1")
    for row in connection.execute("SELECT * FROM credential_generations_legacy_v1").fetchall():
        key_info = mapping.get(str(row[1]))
        if key_info is None:
            continue
        key, source_type, source_id = key_info
        credential = None
        credential_id = str(row[3] or "").strip()
        if credential_id and connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'connectivity_credentials'"
        ).fetchone():
            credential = connection.execute(
                "SELECT external_id, secret_ciphertext FROM connectivity_credentials WHERE credential_id = ?",
                (credential_id,),
            ).fetchone()
        external_id = str((credential[0] if credential else None) or credential_id or row[0])
        secret = (credential[1] if credential else None)
        status = str(row[5] or "unknown")
        if status not in {"pending", "active", "retiring", "unknown", "revoked", "failed"}:
            status = "unknown"
        remote_state = "observed" if status in {"active", "retiring"} else "unknown"
        connection.execute(
            """INSERT OR IGNORE INTO credential_generations
               (generation_id, entitlement_key, source_type, source_id, endpoint_id,
                protocol, external_id, access_url_ciphertext, generation_no, status,
                remote_state, intent_key, usage_baseline_provenance, usage_baseline_bytes,
                created_at, revoked_at)
               VALUES (?, ?, ?, ?, 'legacy-default', 'outline', ?, ?, ?, ?, ?, NULL,
                       'unknown', NULL, ?, ?)""",
            (str(row[0]), key, source_type, source_id, external_id, secret,
             int(row[4] or 1), status, remote_state, str(row[6]), row[7]),
        )

    def copy_table(table: str, archive: str, columns: tuple[str, ...], select_sql: str) -> None:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (archive,)
        ).fetchone():
            return
        for row in connection.execute(select_sql).fetchall():
            info = mapping.get(str(row[1])) if len(row) > 1 else None
            if info is None:
                continue
            values = list(row)
            values[1] = info[0]
            if "endpoint_id" in columns:
                endpoint_index = columns.index("endpoint_id")
                values[endpoint_index] = "legacy-default"
            placeholders = ", ".join("?" for _ in columns)
            connection.execute(
                f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )

    copy_table(
        "quota_leases",
        "quota_leases_legacy_v1",
        ("lease_id", "entitlement_key", "generation_id", "endpoint_id", "lease_bytes",
         "used_bytes", "expires_at", "status", "created_at", "released_at"),
        "SELECT lease_id, entitlement_id, generation_id, endpoint_id, lease_bytes, used_bytes, expires_at, status, created_at, released_at FROM quota_leases_legacy_v1",
    )
    copy_table(
        "entitlement_usage_epochs",
        "entitlement_usage_epochs_legacy_v1",
        ("epoch_id", "entitlement_key", "generation_id", "endpoint_id", "source_external_id",
         "epoch_no", "last_remote_bytes", "credited_bytes", "reset_count", "status",
         "last_observed_at", "created_at", "updated_at"),
        "SELECT epoch_id, entitlement_id, generation_id, endpoint_id, source_external_id, epoch_no, last_remote_bytes, credited_bytes, reset_count, status, last_observed_at, created_at, updated_at FROM entitlement_usage_epochs_legacy_v1",
    )
    copy_table(
        "entitlement_usage_samples",
        "entitlement_usage_samples_legacy_v1",
        ("sample_id", "epoch_id", "entitlement_key", "generation_id", "endpoint_id",
         "source_external_id", "lease_id", "remote_bytes", "delta_bytes", "accepted",
         "reason", "observed_at", "created_at"),
        "SELECT sample_id, epoch_id, entitlement_id, generation_id, endpoint_id, source_external_id, lease_id, remote_bytes, delta_bytes, accepted, reason, observed_at, created_at FROM entitlement_usage_samples_legacy_v1",
    )
    copy_table(
        "entitlement_quota_ledger",
        "entitlement_quota_ledger_legacy_v1",
        ("entry_id", "entitlement_key", "generation_id", "endpoint_id", "lease_id", "epoch_id",
         "event_type", "bytes", "consumed_bytes", "remaining_bytes", "idempotency_key",
         "details_json", "created_at"),
        "SELECT entry_id, entitlement_id, generation_id, endpoint_id, lease_id, epoch_id, event_type, bytes, consumed_bytes, remaining_bytes, idempotency_key, details_json, created_at FROM entitlement_quota_ledger_legacy_v1",
    )

    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'route_failover_policies_legacy_v1'"
    ).fetchone():
        for row in connection.execute(
            "SELECT entitlement_id, enabled, failure_threshold, recovery_threshold, cooldown_seconds, standby_lease_bytes, max_attempts, created_at, updated_at FROM route_failover_policies_legacy_v1"
        ).fetchall():
            info = mapping.get(str(row[0]))
            if info is None:
                continue
            connection.execute(
                """INSERT OR IGNORE INTO route_failover_policies
                   (entitlement_key, enabled, failure_threshold, recovery_threshold,
                    cooldown_seconds, standby_lease_bytes, max_attempts, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (info[0], *row[1:]),
            )

    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'route_failover_state_legacy_v1'"
    ).fetchone():
        connection.execute(
            """INSERT OR IGNORE INTO route_failover_state
               (generation_id, failure_streak, success_streak, last_outcome,
                last_observed_at, cooldown_until, updated_at)
               SELECT generation_id, failure_streak, success_streak, last_outcome,
                      last_observed_at, cooldown_until, updated_at
                 FROM route_failover_state_legacy_v1"""
        )

    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'route_observations_legacy_v1'"
    ).fetchone():
        for row in connection.execute(
            """SELECT observation_id, generation_id, entitlement_id, network_bucket,
                      outcome, latency_ms, reason, observed_at, created_at
                 FROM route_observations_legacy_v1"""
        ).fetchall():
            info = mapping.get(str(row[2]))
            if info is None:
                continue
            connection.execute(
                """INSERT OR IGNORE INTO route_observations
                   (observation_id, generation_id, entitlement_key, endpoint_id,
                    network_bucket, outcome, latency_ms, reason, observed_at, created_at)
                   VALUES (?, ?, ?, 'legacy-default', ?, ?, ?, ?, ?, ?)""",
                (row[0], row[1], info[0], row[3] or "unknown", row[4], row[5], row[6], row[7], row[8]),
            )

    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'failover_decisions_legacy_v1'"
    ).fetchone():
        for row in connection.execute(
            """SELECT decision_id, idempotency_key, entitlement_id,
                      source_generation_id, target_endpoint_id, trigger,
                      network_bucket, state, attempts, next_attempt_at, locked_at,
                      last_error, created_at, updated_at, completed_at
                 FROM failover_decisions_legacy_v1"""
        ).fetchall():
            info = mapping.get(str(row[2]))
            if info is None:
                continue
            connection.execute(
                """INSERT OR IGNORE INTO failover_decisions
                   (decision_id, idempotency_key, entitlement_key, source_generation_id,
                    source_endpoint_id, target_endpoint_id, target_generation_id, trigger,
                    network_bucket, state, attempts, next_attempt_at, locked_at,
                    last_error, created_at, updated_at, completed_at)
                   VALUES (?, ?, ?, ?, 'legacy-default', 'legacy-default', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (row[0], row[1], info[0], row[3], None, row[5], row[6] or "unknown",
                 row[7], row[8], row[9], row[10], row[11], row[12], row[13], row[14]),
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
    reconcile_legacy = _legacy_reconciliation_enabled()
    if reconcile_legacy:
        _prepare_legacy_table_shapes(connection, component, dialect)
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
    ordered_migrations = list(migrations)
    if reconcile_legacy:
        # Include historical tail records only for the explicit compatibility
        # path.  Fresh installs keep the compact current registry unchanged.
        ordered_migrations.extend(_LEGACY_COMPATIBILITY_MIGRATIONS.get(component, ()))
    ordered = sorted(tuple(ordered_migrations), key=lambda migration: migration.version)
    if len({migration.version for migration in ordered}) != len(ordered):
        raise MigrationError(f"Duplicate migration version for {component}")
    if any(migration.version <= 0 for migration in ordered):
        raise MigrationError(f"Migration versions for {component} must be positive")
    known_versions = {migration.version for migration in ordered}
    unknown_versions = sorted(set(recorded) - known_versions)
    if unknown_versions and not reconcile_legacy:
        versions = ", ".join(str(version) for version in unknown_versions)
        raise MigrationError(
            f"Database has unknown {component} migration version(s): {versions}"
        )
    if unknown_versions and reconcile_legacy:
        # Unknown rows are retained as immutable historical evidence.  The
        # current registry is reconciled below and future startup continues to
        # validate the known compatibility tail.
        versions = ", ".join(str(version) for version in unknown_versions)
        print(
            f"Migration compatibility mode: preserving unknown {component} "
            f"version(s): {versions}"
        )
    timestamp = applied_at or datetime.now(UTC).isoformat()
    for migration in ordered:
        existing_name = recorded.get(migration.version)
        if existing_name is not None:
            if existing_name != migration.name:
                if not reconcile_legacy:
                    raise MigrationError(
                        f"Migration {component}:{migration.version} was renamed "
                        f"from {existing_name!r} to {migration.name!r}"
                    )
                # Run the current statements against the legacy schema.  The
                # compatibility executor tolerates only duplicate SQLite DDL,
                # then canonicalizes the metadata once the pass succeeds.
                for statement in migration.statements_for(dialect):
                    _execute_compatibility_statement(connection, statement, dialect)
                connection.execute(
                    "UPDATE schema_migrations SET name = ?, applied_at = ? "
                    "WHERE component = ? AND version = ?",
                    (migration.name, timestamp, component, migration.version),
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
    if reconcile_legacy:
        _migrate_legacy_identity_tables(connection, component, dialect)
