"""Dialect-specific commerce schema bootstrap and upgrade entry points."""

from __future__ import annotations

from commerce_models import _normalize_reference
from migrations import COMMERCE_MIGRATIONS, FREE_ACCESS_MIGRATIONS
from commerce_schema_base_migrations import COMMERCE_BASE_MIGRATIONS
from schema_migrations import apply_migrations


def initialize_sqlite(self) -> None:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    with self.connect() as connection:
        apply_migrations(
            connection,
            component="commerce_base",
            dialect="sqlite",
            migrations=COMMERCE_BASE_MIGRATIONS,
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(notifications)")}
        if "access_url_ciphertext" not in columns:
            connection.execute(
                "ALTER TABLE notifications ADD COLUMN access_url_ciphertext TEXT"
            )
        if "dead_lettered_at" not in columns:
            connection.execute("ALTER TABLE notifications ADD COLUMN dead_lettered_at TEXT")
        evidence_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(payment_evidence)")
        }
        for column, definition in (
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
        ):
            if column not in evidence_columns:
                connection.execute(
                    f"ALTER TABLE payment_evidence ADD COLUMN {column} {definition}"
                )
        user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        if "username" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN username TEXT")
        payment_columns = {row[1] for row in connection.execute("PRAGMA table_info(payments)")}
        if "normalized_reference" not in payment_columns:
            connection.execute(
                "ALTER TABLE payments ADD COLUMN normalized_reference TEXT NOT NULL DEFAULT ''"
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
        key_columns = {row[1] for row in connection.execute("PRAGMA table_info(paid_vpn_keys)")}
        for name, definition in (
            ("last_usage_bytes", "INTEGER"),
            ("last_usage_observed_at", "TEXT"),
            ("quota_reason", "TEXT"),
            ("quota_warning_percent", "INTEGER"),
        ):
            if name not in key_columns:
                connection.execute(f"ALTER TABLE paid_vpn_keys ADD COLUMN {name} {definition}")
        order_columns = {row[1] for row in connection.execute("PRAGMA table_info(orders)")}
        for name, definition in (
            ("plan_name", "TEXT NOT NULL DEFAULT ''"),
            ("quota_bytes_snapshot", "INTEGER"),
            ("duration_days_snapshot", "INTEGER"),
            ("refund_status", "TEXT NOT NULL DEFAULT 'none'"),
            ("payment_method", "TEXT"),
        ):
            if name not in order_columns:
                connection.execute(f"ALTER TABLE orders ADD COLUMN {name} {definition}")
        sub_columns = {row[1] for row in connection.execute("PRAGMA table_info(subscriptions)")}
        for name, definition in (
            ("plan_name", "TEXT NOT NULL DEFAULT ''"),
            ("quota_bytes", "INTEGER"),
            ("duration_days", "INTEGER"),
            ("activated_at", "TEXT"),
        ):
            if name not in sub_columns:
                connection.execute(f"ALTER TABLE subscriptions ADD COLUMN {name} {definition}")
        self._seed_plans(connection)
        apply_migrations(
            connection,
            component="commerce",
            dialect="sqlite",
            migrations=COMMERCE_MIGRATIONS,
        )


def initialize_postgres(self) -> None:
    with self.connect() as connection:
        apply_migrations(
            connection,
            component="commerce_base",
            dialect="postgres",
            migrations=COMMERCE_BASE_MIGRATIONS,
        )
        connection.execute(
            "ALTER TABLE paid_vpn_keys ADD COLUMN IF NOT EXISTS last_usage_bytes BIGINT"
        )
        connection.execute(
            "ALTER TABLE paid_vpn_keys ADD COLUMN IF NOT EXISTS last_usage_observed_at TEXT"
        )
        connection.execute(
            "ALTER TABLE paid_vpn_keys ADD COLUMN IF NOT EXISTS quota_reason TEXT"
        )
        connection.execute(
            "ALTER TABLE paid_vpn_keys ADD COLUMN IF NOT EXISTS quota_warning_percent INTEGER"
        )
        connection.execute(
            "ALTER TABLE keys ADD COLUMN IF NOT EXISTS quota_warning_percent INTEGER"
        )
        connection.execute(
            "ALTER TABLE keys ADD COLUMN IF NOT EXISTS key_type TEXT NOT NULL DEFAULT 'daily_free'"
        )
        connection.execute(
            "ALTER TABLE keys ADD COLUMN IF NOT EXISTS last_usage_observed_at TEXT"
        )
        connection.execute(
            """UPDATE keys SET key_type = 'monthly_trial'
               WHERE key_type = 'daily_free' AND data_limit_bytes >= %s""",
            (3 * 1024 * 1024 * 1024,),
        )
        connection.execute(
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS plan_name TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS quota_bytes_snapshot BIGINT"
        )
        connection.execute(
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS duration_days_snapshot INTEGER"
        )
        connection.execute(
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS refund_status TEXT NOT NULL DEFAULT 'none'"
        )
        connection.execute(
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_method TEXT"
        )
        connection.execute(
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS plan_name TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS quota_bytes BIGINT"
        )
        connection.execute(
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS duration_days INTEGER"
        )
        connection.execute(
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS activated_at TEXT"
        )
        connection.execute(
            "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS dead_lettered_at TEXT"
        )
        connection.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT")
        connection.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_claimed_at TEXT")
        connection.execute(
            "ALTER TABLE payments ADD COLUMN IF NOT EXISTS normalized_reference TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            "UPDATE payments SET normalized_reference = LOWER(REPLACE(provider_reference, ' ', '')) WHERE normalized_reference = ''"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS payments_reference_lookup ON payments(provider, normalized_reference)"
        )
        connection.execute(
            "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS review_status TEXT NOT NULL DEFAULT 'pending'"
        )
        connection.execute(
            "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS verified_provider_reference TEXT"
        )
        connection.execute(
            "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS verified_amount_minor BIGINT"
        )
        connection.execute(
            "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS verified_currency TEXT"
        )
        connection.execute(
            "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS reviewed_at TEXT"
        )
        connection.execute(
            "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS telegram_media_type TEXT NOT NULL DEFAULT 'photo'"
        )
        connection.execute(
            "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS storage_bucket TEXT"
        )
        connection.execute(
            "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS storage_path TEXT"
        )
        connection.execute(
            "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS storage_status TEXT NOT NULL DEFAULT 'not_configured'"
        )
        connection.execute(
            "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS storage_error TEXT"
        )
        connection.execute(
            "ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS stored_at TEXT"
        )
        self._seed_plans(connection)
        apply_migrations(
            connection,
            component="free_access",
            dialect="postgres",
            migrations=FREE_ACCESS_MIGRATIONS,
        )
        apply_migrations(
            connection,
            component="commerce",
            dialect="postgres",
            migrations=COMMERCE_MIGRATIONS,
        )
