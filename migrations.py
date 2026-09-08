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
