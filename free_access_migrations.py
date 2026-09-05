"""Component-owned migration registry."""

from __future__ import annotations

from schema_migrations import Migration
from migration_hooks import (
    _rebuild_free_intents_for_server_identity,
    _rebuild_free_keys_for_server_identity,
    _rebuild_staff_notification_preferences_for_key_repairs,
)


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
    Migration(
        8,
        "free_key_server_identity",
        sqlite_statements=(
            "ALTER TABLE keys ADD COLUMN server_id TEXT NOT NULL DEFAULT 'primary'",
            "CREATE UNIQUE INDEX IF NOT EXISTS free_keys_server_external ON keys(server_id, outline_key_id)",
        ),
        sqlite_hook=_rebuild_free_keys_for_server_identity,
        postgres_statements=(
            "ALTER TABLE keys ADD COLUMN IF NOT EXISTS server_id TEXT NOT NULL DEFAULT 'primary'",
            "ALTER TABLE keys DROP CONSTRAINT IF EXISTS keys_outline_key_id_key",
            "CREATE UNIQUE INDEX IF NOT EXISTS free_keys_server_external ON keys(server_id, outline_key_id)",
        ),
    ),
    Migration(
        9,
        "durable_free_provisioning_intents",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS free_provisioning_intents (
                   id TEXT PRIMARY KEY,
                   telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                   kind TEXT NOT NULL CHECK (kind IN ('daily', 'trial', 'promo')),
                   campaign_code TEXT,
                   window_start TEXT,
                   winner_number INTEGER,
                   server_id TEXT NOT NULL,
                   outline_key_id TEXT NOT NULL,
                   key_name TEXT NOT NULL,
                   quota_bytes INTEGER NOT NULL CHECK (quota_bytes > 0),
                   duration_days INTEGER NOT NULL CHECK (duration_days > 0),
                   claim_started_at TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'running', 'done', 'failed', 'cancelled')),
                   attempts INTEGER NOT NULL DEFAULT 0,
                   next_attempt_at TEXT NOT NULL,
                   locked_at TEXT,
                   last_error TEXT,
                   key_id INTEGER,
                   created_at TEXT NOT NULL,
                   completed_at TEXT,
                   UNIQUE (server_id, outline_key_id)
               )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS free_provisioning_claim_slot
               ON free_provisioning_intents(telegram_id, kind, claim_started_at)""",
            """CREATE INDEX IF NOT EXISTS free_provisioning_due
               ON free_provisioning_intents(status, next_attempt_at)""",
            """CREATE UNIQUE INDEX IF NOT EXISTS free_provisioning_promo_user
               ON free_provisioning_intents(campaign_code, telegram_id)
               WHERE kind = 'promo' AND status IN ('pending', 'running', 'done')""",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS free_provisioning_intents (
                   id TEXT PRIMARY KEY,
                   telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                   kind TEXT NOT NULL CHECK (kind IN ('daily', 'trial', 'promo')),
                   campaign_code TEXT,
                   window_start TEXT,
                   winner_number INTEGER,
                   server_id TEXT NOT NULL,
                   outline_key_id TEXT NOT NULL,
                   key_name TEXT NOT NULL,
                   quota_bytes BIGINT NOT NULL CHECK (quota_bytes > 0),
                   duration_days INTEGER NOT NULL CHECK (duration_days > 0),
                   claim_started_at TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'running', 'done', 'failed', 'cancelled')),
                   attempts INTEGER NOT NULL DEFAULT 0,
                   next_attempt_at TEXT NOT NULL,
                   locked_at TEXT,
                   last_error TEXT,
                   key_id BIGINT,
                   created_at TEXT NOT NULL,
                   completed_at TEXT,
                   UNIQUE (server_id, outline_key_id)
               )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS free_provisioning_claim_slot
               ON free_provisioning_intents(telegram_id, kind, claim_started_at)""",
            """CREATE INDEX IF NOT EXISTS free_provisioning_due
               ON free_provisioning_intents(status, next_attempt_at)""",
            """CREATE UNIQUE INDEX IF NOT EXISTS free_provisioning_promo_user
               ON free_provisioning_intents(campaign_code, telegram_id)
               WHERE kind = 'promo' AND status IN ('pending', 'running', 'done')""",
        ),
    ),
    Migration(
        10,
        "free_intent_server_identity",
        sqlite_hook=_rebuild_free_intents_for_server_identity,
        sqlite_statements=(
            "CREATE UNIQUE INDEX IF NOT EXISTS free_provisioning_server_external "
            "ON free_provisioning_intents(server_id, outline_key_id)",
        ),
        postgres_statements=(
            "ALTER TABLE free_provisioning_intents DROP CONSTRAINT IF EXISTS free_provisioning_intents_outline_key_id_key",
            "CREATE UNIQUE INDEX IF NOT EXISTS free_provisioning_server_external ON free_provisioning_intents(server_id, outline_key_id)",
        ),
    ),
    Migration(
        11,
        "managed_key_repair_jobs",
        # The table is intentionally provider-neutral and does not reference
        # commerce-only tables.  This lets a standalone free-access database
        # record a safe repair decision while the shared production database
        # can use the same table for paid and free entitlements.
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
                   next_attempt_at TEXT NOT NULL,
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
    Migration(
        12,
        "staff_key_repair_notifications",
        sqlite_hook=_rebuild_staff_notification_preferences_for_key_repairs,
        postgres_statements=(
            """ALTER TABLE staff_notification_preferences
               DROP CONSTRAINT IF EXISTS staff_notification_preferences_event_type_check""",
            """ALTER TABLE staff_notification_preferences
               ADD CONSTRAINT staff_notification_preferences_event_type_check CHECK (
                   event_type IN ('order_created', 'receipt_submitted', 'rejected', 'key_repairs')
               )""",
        ),
    ),
)
