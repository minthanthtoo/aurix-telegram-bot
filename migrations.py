"""Numbered, component-scoped database migration registry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from commerce_models import _normalize_reference


UTC = timezone.utc


class MigrationError(RuntimeError):
    """Raised when recorded migration history disagrees with the code registry."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sqlite_statements: tuple[str, ...] = ()
    postgres_statements: tuple[str, ...] = ()
    sqlite_hook: Callable[[Any], None] | None = None

    def statements_for(self, dialect: str) -> tuple[str, ...]:
        if dialect == "sqlite":
            return self.sqlite_statements
        if dialect == "postgres":
            return self.postgres_statements
        raise MigrationError(f"Unsupported migration dialect: {dialect}")


def _has_legacy_global_unique(connection: Any, table: str, column: str) -> bool:
    for index in connection.execute(f"PRAGMA index_list({table})").fetchall():
        if not bool(index[2]):
            continue
        columns = [
            row[2]
            for row in connection.execute(f"PRAGMA index_info({index[1]})").fetchall()
        ]
        if columns == [column]:
            return True
    return False


def _rebuild_free_keys_for_server_identity(connection: Any) -> None:
    """Remove a legacy SQLite global Outline-ID unique constraint safely."""
    if not _has_legacy_global_unique(connection, "keys", "outline_key_id"):
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """CREATE TABLE keys_server_scoped (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                   outline_key_id TEXT NOT NULL,
                   key_type TEXT NOT NULL DEFAULT 'daily_free'
                     CHECK (key_type IN ('daily_free', 'monthly_trial', 'paid')),
                   created_at TEXT NOT NULL,
                   expires_at TEXT NOT NULL,
                   data_limit_bytes INTEGER NOT NULL,
                   status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'revoke_failed')),
                   last_usage_bytes INTEGER,
                   last_usage_observed_at TEXT,
                   quota_reason TEXT,
                   quota_warning_percent INTEGER,
                   server_id TEXT NOT NULL DEFAULT 'primary'
               )"""
        )
        connection.execute(
            """INSERT INTO keys_server_scoped
               (id, telegram_id, outline_key_id, key_type, created_at, expires_at,
                data_limit_bytes, status, last_usage_bytes, last_usage_observed_at,
                quota_reason, quota_warning_percent, server_id)
               SELECT id, telegram_id, outline_key_id, key_type, created_at, expires_at,
                      data_limit_bytes, status, last_usage_bytes, last_usage_observed_at,
                      quota_reason, quota_warning_percent, server_id FROM keys"""
        )
        connection.execute("DROP TABLE keys")
        connection.execute("ALTER TABLE keys_server_scoped RENAME TO keys")
        connection.execute("CREATE INDEX keys_expiry ON keys(status, expires_at)")
        connection.execute(
            "CREATE UNIQUE INDEX free_keys_server_external ON keys(server_id, outline_key_id)"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise MigrationError("Free key identity migration broke a foreign-key reference")


def _rebuild_paid_keys_for_server_identity(connection: Any) -> None:
    """Remove a legacy SQLite global paid Outline-ID unique constraint safely."""
    if not _has_legacy_global_unique(connection, "paid_vpn_keys", "outline_key_id"):
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """CREATE TABLE paid_vpn_keys_server_scoped (
                   id TEXT PRIMARY KEY,
                   subscription_id TEXT NOT NULL UNIQUE REFERENCES subscriptions(id),
                   telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                   outline_key_id TEXT NOT NULL,
                   access_url TEXT NOT NULL,
                   quota_bytes INTEGER,
                   status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'revoke_failed')),
                   quota_warning_percent INTEGER,
                   created_at TEXT NOT NULL,
                   revoked_at TEXT,
                   last_usage_bytes INTEGER,
                   last_usage_observed_at TEXT,
                   quota_reason TEXT,
                   server_id TEXT
               )"""
        )
        connection.execute(
            """INSERT INTO paid_vpn_keys_server_scoped
               (id, subscription_id, telegram_id, outline_key_id, access_url,
                quota_bytes, status, quota_warning_percent, created_at, revoked_at,
                last_usage_bytes, last_usage_observed_at, quota_reason, server_id)
               SELECT id, subscription_id, telegram_id, outline_key_id, access_url,
                      quota_bytes, status, quota_warning_percent, created_at, revoked_at,
                      last_usage_bytes, last_usage_observed_at, quota_reason, server_id
               FROM paid_vpn_keys"""
        )
        connection.execute("DROP TABLE paid_vpn_keys")
        connection.execute(
            "ALTER TABLE paid_vpn_keys_server_scoped RENAME TO paid_vpn_keys"
        )
        connection.execute(
            """CREATE UNIQUE INDEX paid_keys_server_external
               ON paid_vpn_keys(server_id, outline_key_id)"""
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise MigrationError("Paid key identity migration broke a foreign-key reference")


def _rebuild_staff_notification_preferences_for_key_repairs(connection: Any) -> None:
    """Extend the SQLite event check without leaving a half-rebuilt table.

    SQLite cannot alter a CHECK constraint in place. Keep the rebuild inside
    one explicit transaction so a process interruption rolls back atomically;
    this matters because the bot may be restarted during a deploy.
    """
    table = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'staff_notification_preferences'"
    ).fetchone()
    if table is None:
        return
    ddl = str(table[0] or "").lower()
    if "key_repairs" in ddl:
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """CREATE TABLE staff_notification_preferences_v12 (
                   telegram_id INTEGER NOT NULL REFERENCES staff_accounts(telegram_id),
                   event_type TEXT NOT NULL CHECK (
                       event_type IN ('order_created', 'receipt_submitted', 'rejected', 'key_repairs')
                   ),
                   enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                   updated_at TEXT NOT NULL,
                   PRIMARY KEY (telegram_id, event_type)
               )"""
        )
        connection.execute(
            """INSERT INTO staff_notification_preferences_v12
               (telegram_id, event_type, enabled, updated_at)
               SELECT telegram_id, event_type, enabled, updated_at
                 FROM staff_notification_preferences"""
        )
        connection.execute("DROP TABLE staff_notification_preferences")
        connection.execute(
            "ALTER TABLE staff_notification_preferences_v12 RENAME TO staff_notification_preferences"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _rebuild_free_intents_for_server_identity(connection: Any) -> None:
    """Replace the legacy global intent ID uniqueness with server scope."""
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'free_provisioning_intents'"
    ).fetchone():
        return
    has_global_unique = False
    for index in connection.execute("PRAGMA index_list(free_provisioning_intents)").fetchall():
        if not bool(index[2]):
            continue
        columns = [
            row[2]
            for row in connection.execute(f"PRAGMA index_info({index[1]})").fetchall()
        ]
        if columns == ["outline_key_id"]:
            has_global_unique = True
            break
    if not has_global_unique:
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """CREATE TABLE free_provisioning_intents_server_scoped (
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
               )"""
        )
        connection.execute(
            """INSERT INTO free_provisioning_intents_server_scoped
               SELECT id, telegram_id, kind, campaign_code, window_start, winner_number,
                      server_id, outline_key_id, key_name, quota_bytes, duration_days,
                      claim_started_at, status, attempts, next_attempt_at, locked_at,
                      last_error, key_id, created_at, completed_at
                 FROM free_provisioning_intents"""
        )
        connection.execute("DROP TABLE free_provisioning_intents")
        connection.execute(
            "ALTER TABLE free_provisioning_intents_server_scoped RENAME TO free_provisioning_intents"
        )
        connection.execute(
            """CREATE UNIQUE INDEX free_provisioning_claim_slot
               ON free_provisioning_intents(telegram_id, kind, claim_started_at)"""
        )
        connection.execute(
            """CREATE INDEX free_provisioning_due
               ON free_provisioning_intents(status, next_attempt_at)"""
        )
        connection.execute(
            """CREATE UNIQUE INDEX free_provisioning_promo_user
               ON free_provisioning_intents(campaign_code, telegram_id)
               WHERE kind = 'promo' AND status IN ('pending', 'running', 'done')"""
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _add_normalized_payment_reference_guard(connection: Any) -> None:
    """Make provider/reference deduplication atomic after legacy backfill.

    Older databases only enforced the raw provider reference.  That allowed
    the same transaction to be submitted again with harmless-looking spacing
    or case changes.  Existing collisions are deliberately not
    auto-merged: payment evidence is immutable and an operator must decide
    which records are legitimate before the stronger constraint is enabled.
    """
    # Recompute every legacy value, not only blank columns.  Earlier releases
    # used a narrower SQL normalizer on some databases and could leave tabs or
    # other Unicode whitespace in an apparently populated value.
    for payment in connection.execute(
        "SELECT id, provider_reference, normalized_reference FROM payments"
    ).fetchall():
        normalized = _normalize_reference(payment["provider_reference"])
        if str(payment["normalized_reference"] or "") != normalized:
            connection.execute(
                "UPDATE payments SET normalized_reference = ? WHERE id = ?",
                (normalized, payment["id"]),
            )
    duplicate = connection.execute(
        """SELECT 1
           FROM payments
           WHERE normalized_reference <> ''
           GROUP BY lower(provider), normalized_reference
           HAVING COUNT(*) > 1
           LIMIT 1"""
    ).fetchone()
    if duplicate is not None:
        raise MigrationError(
            "Duplicate normalized payment references require manual reconciliation"
        )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS payments_normalized_reference_unique
           ON payments(lower(provider), normalized_reference)
           WHERE normalized_reference <> ''"""
    )


def _canonicalize_payment_provider_identity(connection: Any) -> None:
    """Canonicalize provider names before relying on the reference index.

    Older rows could contain harmless casing/spacing variants such as
    ``Manual`` and `` manual ``.  Canonicalizing the stored provider makes the
    database constraint and application comparison agree.  Any collision is
    reported before updates begin so no payment record is silently merged.
    """
    rows = connection.execute("SELECT id, provider, normalized_reference FROM payments").fetchall()
    seen: set[tuple[str, str]] = set()
    for payment in rows:
        provider = _normalize_reference(payment["provider"])
        reference = str(payment["normalized_reference"] or "")
        if reference:
            identity = (provider, reference)
            if identity in seen:
                raise MigrationError(
                    "Duplicate normalized payment providers/references require manual reconciliation"
                )
            seen.add(identity)
        if provider and provider != str(payment["provider"] or ""):
            connection.execute(
                "UPDATE payments SET provider = ? WHERE id = ?",
                (provider, payment["id"]),
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
    Migration(
        5,
        "fleet_lifecycle_and_tier_capacity",
        sqlite_statements=(
            "CREATE UNIQUE INDEX IF NOT EXISTS paid_keys_server_external ON paid_vpn_keys(server_id, outline_key_id)",
            """CREATE TABLE IF NOT EXISTS server_tier_allocations (
                   server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   tier_code TEXT NOT NULL,
                   slot_limit INTEGER NOT NULL CHECK (slot_limit >= 0),
                   updated_at TEXT NOT NULL,
                   PRIMARY KEY (server_id, tier_code)
               )""",
            """CREATE TABLE IF NOT EXISTS infrastructure_jobs (
                   id TEXT PRIMARY KEY,
                   operation TEXT NOT NULL,
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
                   server_id TEXT,
                   event_type TEXT NOT NULL,
                   metadata_json TEXT NOT NULL DEFAULT '{}',
                   created_at TEXT NOT NULL
               )""",
        ),
        postgres_statements=(
            "ALTER TABLE paid_vpn_keys DROP CONSTRAINT IF EXISTS paid_vpn_keys_outline_key_id_key",
            "CREATE UNIQUE INDEX IF NOT EXISTS paid_keys_server_external ON paid_vpn_keys(server_id, outline_key_id)",
            """CREATE TABLE IF NOT EXISTS server_tier_allocations (
                   server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   tier_code TEXT NOT NULL,
                   slot_limit INTEGER NOT NULL CHECK (slot_limit >= 0),
                   updated_at TEXT NOT NULL,
                   PRIMARY KEY (server_id, tier_code)
               )""",
            """CREATE TABLE IF NOT EXISTS infrastructure_jobs (
                   id TEXT PRIMARY KEY,
                   operation TEXT NOT NULL,
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
                   server_id TEXT,
                   event_type TEXT NOT NULL,
                   metadata_json TEXT NOT NULL DEFAULT '{}',
                   created_at TEXT NOT NULL
               )""",
        ),
        sqlite_hook=_rebuild_paid_keys_for_server_identity,
    ),
    Migration(
        6,
        "provider_inventory_and_node_identity",
        sqlite_statements=(
            "ALTER TABLE outline_servers ADD COLUMN provider_resource_id TEXT",
            "ALTER TABLE outline_servers ADD COLUMN provider_status TEXT",
            "ALTER TABLE outline_servers ADD COLUMN provider_last_seen_at TEXT",
            "CREATE UNIQUE INDEX IF NOT EXISTS outline_servers_provider_resource ON outline_servers(provider_resource_id) WHERE provider_resource_id IS NOT NULL",
        ),
        postgres_statements=(
            "ALTER TABLE outline_servers ADD COLUMN IF NOT EXISTS provider_resource_id TEXT",
            "ALTER TABLE outline_servers ADD COLUMN IF NOT EXISTS provider_status TEXT",
            "ALTER TABLE outline_servers ADD COLUMN IF NOT EXISTS provider_last_seen_at TEXT",
            "CREATE UNIQUE INDEX IF NOT EXISTS outline_servers_provider_resource ON outline_servers(provider_resource_id) WHERE provider_resource_id IS NOT NULL",
        ),
    ),
    Migration(
        7,
        "remote_key_inventory_audit",
        sqlite_statements=(
            "ALTER TABLE outline_servers ADD COLUMN remote_orphan_key_count INTEGER NOT NULL DEFAULT 0",
            """CREATE TABLE IF NOT EXISTS outline_remote_keys (
                   server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   outline_key_id TEXT NOT NULL,
                   remote_name TEXT,
                   managed INTEGER NOT NULL DEFAULT 0 CHECK (managed IN (0, 1)),
                   status TEXT NOT NULL DEFAULT 'present'
                       CHECK (status IN ('present', 'missing')),
                   first_seen_at TEXT NOT NULL,
                   last_seen_at TEXT NOT NULL,
                   last_usage_bytes INTEGER,
                   PRIMARY KEY (server_id, outline_key_id)
               )""",
            """CREATE INDEX IF NOT EXISTS outline_remote_keys_audit
               ON outline_remote_keys(server_id, status, managed)""",
        ),
        postgres_statements=(
            "ALTER TABLE outline_servers ADD COLUMN IF NOT EXISTS remote_orphan_key_count INTEGER NOT NULL DEFAULT 0",
            """CREATE TABLE IF NOT EXISTS outline_remote_keys (
                   server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   outline_key_id TEXT NOT NULL,
                   remote_name TEXT,
                   managed INTEGER NOT NULL DEFAULT 0 CHECK (managed IN (0, 1)),
                   status TEXT NOT NULL DEFAULT 'present'
                       CHECK (status IN ('present', 'missing')),
                   first_seen_at TEXT NOT NULL,
                   last_seen_at TEXT NOT NULL,
                   last_usage_bytes BIGINT,
                   PRIMARY KEY (server_id, outline_key_id)
               )""",
            """CREATE INDEX IF NOT EXISTS outline_remote_keys_audit
               ON outline_remote_keys(server_id, status, managed)""",
        ),
    ),
    Migration(
        8,
        "scale_observation_history",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS scale_observations (
                   id TEXT PRIMARY KEY,
                   fleet_fingerprint TEXT NOT NULL,
                   observed_at TEXT NOT NULL,
                   status TEXT NOT NULL CHECK (
                       status IN ('stable', 'prepare', 'urgent', 'blocked', 'unconfigured')
                   ),
                   utilization_percent REAL,
                   remaining_slots INTEGER,
                   saleable_capacity INTEGER,
                   traffic_utilization_percent REAL,
                   healthy_server_count INTEGER NOT NULL DEFAULT 0,
                   created_at TEXT NOT NULL,
                   UNIQUE (fleet_fingerprint, observed_at)
               )""",
            """CREATE INDEX IF NOT EXISTS scale_observations_recent
               ON scale_observations(observed_at)""",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS scale_observations (
                   id TEXT PRIMARY KEY,
                   fleet_fingerprint TEXT NOT NULL,
                   observed_at TEXT NOT NULL,
                   status TEXT NOT NULL CHECK (
                       status IN ('stable', 'prepare', 'urgent', 'blocked', 'unconfigured')
                   ),
                   utilization_percent DOUBLE PRECISION,
                   remaining_slots INTEGER,
                   saleable_capacity INTEGER,
                   traffic_utilization_percent DOUBLE PRECISION,
                   healthy_server_count INTEGER NOT NULL DEFAULT 0,
                   created_at TEXT NOT NULL,
                   UNIQUE (fleet_fingerprint, observed_at)
               )""",
            """CREATE INDEX IF NOT EXISTS scale_observations_recent
               ON scale_observations(observed_at)""",
        ),
    ),
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
        if dialect == "sqlite" and migration.sqlite_hook is not None:
            migration.sqlite_hook(connection)
        connection.execute(
            """INSERT INTO schema_migrations
               (component, version, name, applied_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(component, version) DO NOTHING""",
            (component, migration.version, migration.name, timestamp),
        )
