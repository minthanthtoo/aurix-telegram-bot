"""Versioned compatibility/backfill migration for legacy commerce schemas."""

from __future__ import annotations

from typing import Any

from commerce_models import _normalize_reference
from schema_migrations import Migration


def _sqlite_columns(connection: Any, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _sqlite_add_missing(
    connection: Any, table: str, definitions: tuple[tuple[str, str], ...]
) -> None:
    columns = _sqlite_columns(connection, table)
    for name, definition in definitions:
        if name not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _upgrade_sqlite_legacy_schema(connection: Any) -> None:
    _sqlite_add_missing(
        connection,
        "notifications",
        (
            ("access_url_ciphertext", "TEXT"),
            ("dead_lettered_at", "TEXT"),
        ),
    )
    _sqlite_add_missing(
        connection,
        "payment_evidence",
        (
            ("review_status", "TEXT NOT NULL DEFAULT 'pending'"),
            ("verified_provider_reference", "TEXT"),
            ("verified_amount_minor", "INTEGER"),
            ("verified_currency", "TEXT"),
            ("reviewed_at", "TEXT"),
            ("telegram_media_type", "TEXT NOT NULL DEFAULT 'photo'"),
            ("storage_bucket", "TEXT"),
            ("storage_path", "TEXT"),
            ("storage_status", "TEXT NOT NULL DEFAULT 'not_configured'"),
            ("storage_error", "TEXT"),
            ("stored_at", "TEXT"),
        ),
    )
    _sqlite_add_missing(connection, "users", (("username", "TEXT"),))
    _sqlite_add_missing(
        connection,
        "payments",
        (("normalized_reference", "TEXT NOT NULL DEFAULT ''"),),
    )
    for payment in connection.execute(
        "SELECT id, provider_reference FROM payments WHERE normalized_reference = ''"
    ).fetchall():
        connection.execute(
            "UPDATE payments SET normalized_reference = ? WHERE id = ?",
            (_normalize_reference(payment["provider_reference"]), payment["id"]),
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS payments_reference_lookup "
        "ON payments(provider, normalized_reference)"
    )
    _sqlite_add_missing(
        connection,
        "paid_vpn_keys",
        (
            ("last_usage_bytes", "INTEGER"),
            ("last_usage_observed_at", "TEXT"),
            ("quota_reason", "TEXT"),
            ("quota_warning_percent", "INTEGER"),
        ),
    )
    _sqlite_add_missing(
        connection,
        "orders",
        (
            ("plan_name", "TEXT NOT NULL DEFAULT ''"),
            ("quota_bytes_snapshot", "INTEGER"),
            ("duration_days_snapshot", "INTEGER"),
            ("refund_status", "TEXT NOT NULL DEFAULT 'none'"),
            ("payment_method", "TEXT"),
        ),
    )
    _sqlite_add_missing(
        connection,
        "subscriptions",
        (
            ("plan_name", "TEXT NOT NULL DEFAULT ''"),
            ("quota_bytes", "INTEGER"),
            ("duration_days", "INTEGER"),
            ("activated_at", "TEXT"),
        ),
    )


COMMERCE_SCHEMA_COMPATIBILITY_POSTGRES = (
    "ALTER TABLE paid_vpn_keys ADD COLUMN IF NOT EXISTS last_usage_bytes BIGINT",
    "ALTER TABLE paid_vpn_keys ADD COLUMN IF NOT EXISTS last_usage_observed_at TEXT",
    "ALTER TABLE paid_vpn_keys ADD COLUMN IF NOT EXISTS quota_reason TEXT",
    "ALTER TABLE paid_vpn_keys ADD COLUMN IF NOT EXISTS quota_warning_percent INTEGER",
    "ALTER TABLE keys ADD COLUMN IF NOT EXISTS quota_warning_percent INTEGER",
    "ALTER TABLE keys ADD COLUMN IF NOT EXISTS key_type TEXT NOT NULL DEFAULT 'daily_free'",
    "ALTER TABLE keys ADD COLUMN IF NOT EXISTS last_usage_observed_at TEXT",
    """UPDATE keys SET key_type = 'monthly_trial'
       WHERE key_type = 'daily_free' AND data_limit_bytes >= 3221225472""",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS plan_name TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS quota_bytes_snapshot BIGINT",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS duration_days_snapshot INTEGER",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS refund_status TEXT NOT NULL DEFAULT 'none'",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_method TEXT",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS plan_name TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS quota_bytes BIGINT",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS duration_days INTEGER",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS activated_at TEXT",
    "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS dead_lettered_at TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_claimed_at TEXT",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS normalized_reference TEXT NOT NULL DEFAULT ''",
    "UPDATE payments SET normalized_reference = LOWER(REPLACE(provider_reference, ' ', '')) WHERE normalized_reference = ''",
    "CREATE INDEX IF NOT EXISTS payments_reference_lookup ON payments(provider, normalized_reference)",
    "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS review_status TEXT NOT NULL DEFAULT 'pending'",
    "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS verified_provider_reference TEXT",
    "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS verified_amount_minor BIGINT",
    "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS verified_currency TEXT",
    "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS reviewed_at TEXT",
    "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS telegram_media_type TEXT NOT NULL DEFAULT 'photo'",
    "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS storage_bucket TEXT",
    "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS storage_path TEXT",
    "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS storage_status TEXT NOT NULL DEFAULT 'not_configured'",
    "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS storage_error TEXT",
    "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS stored_at TEXT",
)


COMMERCE_SCHEMA_COMPATIBILITY_MIGRATIONS = (
    Migration(
        1,
        "legacy_schema_compatibility",
        postgres_statements=COMMERCE_SCHEMA_COMPATIBILITY_POSTGRES,
        sqlite_hook=_upgrade_sqlite_legacy_schema,
    ),
)
