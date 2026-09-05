"""SQLite/Postgres migration compatibility hooks."""

from __future__ import annotations

from typing import Any

from commerce_models import _normalize_reference
from schema_migrations import MigrationError


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
