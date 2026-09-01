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
        "staff_access_control",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS staff_accounts (
                   telegram_id INTEGER PRIMARY KEY,
                   role TEXT NOT NULL CHECK (role IN ('owner', 'admin')),
                   status TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'revoked')),
                   display_name TEXT,
                   username TEXT,
                   source TEXT NOT NULL,
                   added_by INTEGER,
                   added_at TEXT NOT NULL,
                   revoked_by INTEGER,
                   revoked_at TEXT,
                   last_privileged_action_at TEXT,
                   access_version INTEGER NOT NULL DEFAULT 1
               )""",
            "CREATE INDEX IF NOT EXISTS staff_active_role ON staff_accounts(role, status)",
            """CREATE TABLE IF NOT EXISTS staff_sync_runs (
                   id TEXT PRIMARY KEY,
                   control_group_id INTEGER NOT NULL,
                   requested_by INTEGER,
                   source TEXT NOT NULL,
                   status TEXT NOT NULL CHECK (status IN ('previewed', 'applied', 'failed')),
                   snapshot_json TEXT NOT NULL DEFAULT '{}',
                   created_at TEXT NOT NULL,
                   applied_at TEXT
               )""",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS staff_accounts (
                   telegram_id BIGINT PRIMARY KEY,
                   role TEXT NOT NULL CHECK (role IN ('owner', 'admin')),
                   status TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'revoked')),
                   display_name TEXT,
                   username TEXT,
                   source TEXT NOT NULL,
                   added_by BIGINT,
                   added_at TEXT NOT NULL,
                   revoked_by BIGINT,
                   revoked_at TEXT,
                   last_privileged_action_at TEXT,
                   access_version INTEGER NOT NULL DEFAULT 1
               )""",
            "CREATE INDEX IF NOT EXISTS staff_active_role ON staff_accounts(role, status)",
            """CREATE TABLE IF NOT EXISTS staff_sync_runs (
                   id TEXT PRIMARY KEY,
                   control_group_id BIGINT NOT NULL,
                   requested_by BIGINT,
                   source TEXT NOT NULL,
                   status TEXT NOT NULL CHECK (status IN ('previewed', 'applied', 'failed')),
                   snapshot_json TEXT NOT NULL DEFAULT '{}',
                   created_at TEXT NOT NULL,
                   applied_at TEXT
               )""",
        ),
    ),
    Migration(
        5,
        "staff_control_group_binding",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS staff_control_group (
                   id INTEGER PRIMARY KEY CHECK (id = 1),
                   control_group_id INTEGER NOT NULL CHECK (control_group_id < 0),
                   title TEXT,
                   bound_by INTEGER NOT NULL,
                   bound_at TEXT NOT NULL,
                   source TEXT NOT NULL
               )""",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS staff_control_group (
                   id INTEGER PRIMARY KEY CHECK (id = 1),
                   control_group_id BIGINT NOT NULL CHECK (control_group_id < 0),
                   title TEXT,
                   bound_by BIGINT NOT NULL,
                   bound_at TEXT NOT NULL,
                   source TEXT NOT NULL
               )""",
        ),
    ),
    Migration(
        6,
        "staff_notification_preferences",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS staff_notification_preferences (
                   telegram_id INTEGER NOT NULL REFERENCES staff_accounts(telegram_id),
                   event_type TEXT NOT NULL CHECK (
                       event_type IN ('order_created', 'receipt_submitted', 'rejected')
                   ),
                   enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                   updated_at TEXT NOT NULL,
                   PRIMARY KEY (telegram_id, event_type)
               )""",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS staff_notification_preferences (
                   telegram_id BIGINT NOT NULL REFERENCES staff_accounts(telegram_id),
                   event_type TEXT NOT NULL CHECK (
                       event_type IN ('order_created', 'receipt_submitted', 'rejected')
                   ),
                   enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                   updated_at TEXT NOT NULL,
                   PRIMARY KEY (telegram_id, event_type)
               )""",
        ),
    ),
    Migration(
        7,
        "customer_quota_alert_preferences",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS user_quota_alert_preferences (
                   telegram_id INTEGER PRIMARY KEY,
                   enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                   mode TEXT NOT NULL DEFAULT 'percent'
                       CHECK (mode IN ('percent', 'mb', 'gb')),
                   alert_count INTEGER NOT NULL DEFAULT 3 CHECK (alert_count BETWEEN 1 AND 3),
                   step_value INTEGER NOT NULL DEFAULT 25 CHECK (step_value > 0),
                   version INTEGER NOT NULL DEFAULT 1,
                   updated_at TEXT NOT NULL
               )""",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS user_quota_alert_preferences (
                   telegram_id BIGINT PRIMARY KEY,
                   enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                   mode TEXT NOT NULL DEFAULT 'percent'
                       CHECK (mode IN ('percent', 'mb', 'gb')),
                   alert_count INTEGER NOT NULL DEFAULT 3 CHECK (alert_count BETWEEN 1 AND 3),
                   step_value INTEGER NOT NULL DEFAULT 25 CHECK (step_value > 0),
                   version INTEGER NOT NULL DEFAULT 1,
                   updated_at TEXT NOT NULL
               )""",
        ),
    ),
)

COMMERCE_MIGRATIONS = (
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
    Migration(
        4,
        "outline_server_capacity",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS outline_servers (
                   server_id TEXT PRIMARY KEY,
                   label TEXT NOT NULL,
                   enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                   max_keys INTEGER CHECK (max_keys IS NULL OR max_keys > 0),
                   reserved_keys INTEGER NOT NULL DEFAULT 2 CHECK (reserved_keys >= 0),
                   monthly_traffic_bytes INTEGER CHECK (monthly_traffic_bytes IS NULL OR monthly_traffic_bytes > 0),
                   remote_key_count INTEGER,
                   remote_transfer_bytes INTEGER,
                   current_bandwidth_bytes INTEGER,
                   peak_bandwidth_bytes INTEGER,
                   telemetry_experimental INTEGER NOT NULL DEFAULT 0 CHECK (telemetry_experimental IN (0, 1)),
                   health_status TEXT NOT NULL DEFAULT 'unknown',
                   last_error TEXT,
                   last_synced_at TEXT,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS server_plan_allocations (
                   server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   plan_code TEXT NOT NULL REFERENCES plans(code),
                   slot_limit INTEGER NOT NULL CHECK (slot_limit >= 0),
                   updated_at TEXT NOT NULL,
                   PRIMARY KEY (server_id, plan_code)
               )""",
            "ALTER TABLE orders ADD COLUMN server_id TEXT",
            "ALTER TABLE orders ADD COLUMN capacity_reserved_until TEXT",
            "ALTER TABLE subscriptions ADD COLUMN server_id TEXT",
            "ALTER TABLE paid_vpn_keys ADD COLUMN server_id TEXT",
            "CREATE INDEX IF NOT EXISTS orders_capacity_reservation ON orders(server_id, status, capacity_reserved_until)",
            "CREATE INDEX IF NOT EXISTS subscriptions_server_status ON subscriptions(server_id, status)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS outline_servers (
                   server_id TEXT PRIMARY KEY,
                   label TEXT NOT NULL,
                   enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                   max_keys INTEGER CHECK (max_keys IS NULL OR max_keys > 0),
                   reserved_keys INTEGER NOT NULL DEFAULT 2 CHECK (reserved_keys >= 0),
                   monthly_traffic_bytes BIGINT CHECK (monthly_traffic_bytes IS NULL OR monthly_traffic_bytes > 0),
                   remote_key_count INTEGER,
                   remote_transfer_bytes BIGINT,
                   current_bandwidth_bytes BIGINT,
                   peak_bandwidth_bytes BIGINT,
                   telemetry_experimental INTEGER NOT NULL DEFAULT 0 CHECK (telemetry_experimental IN (0, 1)),
                   health_status TEXT NOT NULL DEFAULT 'unknown',
                   last_error TEXT,
                   last_synced_at TEXT,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS server_plan_allocations (
                   server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   plan_code TEXT NOT NULL REFERENCES plans(code),
                   slot_limit INTEGER NOT NULL CHECK (slot_limit >= 0),
                   updated_at TEXT NOT NULL,
                   PRIMARY KEY (server_id, plan_code)
               )""",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS server_id TEXT",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS capacity_reserved_until TEXT",
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS server_id TEXT",
            "ALTER TABLE paid_vpn_keys ADD COLUMN IF NOT EXISTS server_id TEXT",
            "CREATE INDEX IF NOT EXISTS orders_capacity_reservation ON orders(server_id, status, capacity_reserved_until)",
            "CREATE INDEX IF NOT EXISTS subscriptions_server_status ON subscriptions(server_id, status)",
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
        raise MigrationError(f"Database has unknown {component} migration version(s): {versions}")
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
