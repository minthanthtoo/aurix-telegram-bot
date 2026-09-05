"""Dialect-specific commerce schema bootstrap and upgrade entry points."""

from __future__ import annotations

from commerce_models import _normalize_reference
from migrations import COMMERCE_MIGRATIONS, FREE_ACCESS_MIGRATIONS
from schema_migrations import apply_migrations


def initialize_sqlite(self) -> None:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    with self.connect() as connection:
        connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                first_name TEXT NOT NULL DEFAULT '',
                username TEXT,
                last_claim_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS plans (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                price_minor INTEGER NOT NULL CHECK (price_minor >= 0),
                currency TEXT NOT NULL,
                quota_bytes INTEGER CHECK (quota_bytes IS NULL OR quota_bytes > 0),
                duration_days INTEGER NOT NULL CHECK (duration_days > 0),
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                plan_code TEXT NOT NULL REFERENCES plans(code),
                amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
                currency TEXT NOT NULL,
                plan_name TEXT NOT NULL DEFAULT '',
                quota_bytes_snapshot INTEGER,
                duration_days_snapshot INTEGER,
                payment_method TEXT,
                status TEXT NOT NULL CHECK (status IN (
                    'awaiting_payment', 'payment_submitted', 'approved',
                    'rejected', 'cancelled'
                )),
                refund_status TEXT NOT NULL DEFAULT 'none',
                created_at TEXT NOT NULL,
                approved_at TEXT,
                rejected_at TEXT
            );
            CREATE INDEX IF NOT EXISTS orders_review
                ON orders(status, created_at);
            CREATE TABLE IF NOT EXISTS payments (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL REFERENCES orders(id),
                provider TEXT NOT NULL,
                provider_reference TEXT NOT NULL,
                normalized_reference TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK (status IN (
                    'submitted', 'verified', 'rejected', 'refunded'
                )),
                submitted_at TEXT NOT NULL,
                verified_at TEXT,
                UNIQUE(provider, provider_reference)
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                plan_code TEXT NOT NULL REFERENCES plans(code),
                starts_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                plan_name TEXT NOT NULL DEFAULT '',
                quota_bytes INTEGER,
                duration_days INTEGER,
                activated_at TEXT,
                status TEXT NOT NULL CHECK (status IN (
                    'pending', 'active', 'expired', 'revoked', 'cancelled'
                )),
                CHECK (expires_at > starts_at)
            );
            CREATE INDEX IF NOT EXISTS subscriptions_expiry
                ON subscriptions(status, expires_at);
            CREATE TABLE IF NOT EXISTS paid_vpn_keys (
                id TEXT PRIMARY KEY,
                subscription_id TEXT NOT NULL UNIQUE REFERENCES subscriptions(id),
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                outline_key_id TEXT NOT NULL,
                access_url TEXT NOT NULL,
                quota_bytes INTEGER,
                status TEXT NOT NULL CHECK (status IN (
                    'active', 'revoked', 'revoke_failed'
                )),
                quota_warning_percent INTEGER,
                created_at TEXT NOT NULL,
                revoked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS provisioning_jobs (
                id TEXT PRIMARY KEY,
                subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
                operation TEXT NOT NULL CHECK (operation IN ('provision', 'revoke')),
                status TEXT NOT NULL CHECK (status IN (
                    'pending', 'running', 'done', 'failed'
                )),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                locked_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(subscription_id, operation)
            );
            CREATE INDEX IF NOT EXISTS provisioning_due
                ON provisioning_jobs(status, next_attempt_at);
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY,
                dedupe_key TEXT NOT NULL UNIQUE,
                telegram_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                access_url_ciphertext TEXT,
                status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                dead_lettered_at TEXT
            );
            CREATE INDEX IF NOT EXISTS notifications_due
                ON notifications(status, next_attempt_at);
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_type TEXT NOT NULL,
                actor_id TEXT,
                action TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payment_evidence (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL REFERENCES orders(id),
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                provider TEXT NOT NULL,
                telegram_file_id TEXT NOT NULL,
                telegram_file_unique_id TEXT,
                telegram_media_type TEXT NOT NULL DEFAULT 'photo'
                    CHECK (telegram_media_type IN ('photo', 'document')),
                image_sha256 TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                storage_bucket TEXT,
                storage_path TEXT,
                storage_status TEXT NOT NULL DEFAULT 'not_configured',
                storage_error TEXT,
                stored_at TEXT,
                extraction_json TEXT,
                extraction_status TEXT NOT NULL CHECK (extraction_status IN ('parsed', 'needs_review', 'invalid')),
                submitted_at TEXT NOT NULL,
                reviewer_id INTEGER,
                review_notes TEXT,
                review_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (review_status IN ('pending', 'verified', 'rejected')),
                verified_provider_reference TEXT,
                verified_amount_minor INTEGER,
                verified_currency TEXT,
                reviewed_at TEXT,
                UNIQUE(order_id, image_sha256)
            );
            CREATE INDEX IF NOT EXISTS payment_evidence_review
                ON payment_evidence(extraction_status, submitted_at);
            CREATE TABLE IF NOT EXISTS wallets (
                telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id),
                currency TEXT NOT NULL DEFAULT 'MMK',
                balance_minor INTEGER NOT NULL DEFAULT 0 CHECK (balance_minor >= 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wallet_ledger (
                id TEXT PRIMARY KEY,
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                kind TEXT NOT NULL CHECK (kind IN ('credit', 'reserve', 'capture', 'release', 'reversal')),
                amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
                currency TEXT NOT NULL,
                reference_type TEXT NOT NULL,
                reference_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wallet_reservations (
                id TEXT PRIMARY KEY,
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
                amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
                currency TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('reserved', 'captured', 'released')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quota_events (
                id TEXT PRIMARY KEY,
                subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
                reason TEXT NOT NULL,
                observed_bytes INTEGER NOT NULL,
                quota_bytes INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                UNIQUE(subscription_id, reason)
            );
            """
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
    schema = """
    CREATE TABLE IF NOT EXISTS users (
        telegram_id BIGINT PRIMARY KEY,
        first_name TEXT NOT NULL DEFAULT '',
        username TEXT,
        last_claim_at TEXT,
        trial_claimed_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS keys (
        id BIGSERIAL PRIMARY KEY,
        telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
        outline_key_id TEXT NOT NULL,
        key_type TEXT NOT NULL DEFAULT 'daily_free'
            CHECK (key_type IN ('daily_free', 'monthly_trial', 'paid')),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        data_limit_bytes BIGINT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'revoke_failed')),
        last_usage_bytes BIGINT,
        last_usage_observed_at TEXT,
        quota_reason TEXT,
        quota_warning_percent INTEGER
    );
    CREATE INDEX IF NOT EXISTS keys_expiry ON keys(status, expires_at);
    CREATE TABLE IF NOT EXISTS maintenance_heartbeat (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        last_started_at TEXT,
        last_completed_at TEXT,
        last_success_at TEXT,
        last_stage TEXT,
        last_error TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS telegram_updates (
        update_id BIGINT PRIMARY KEY,
        received_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS key_termination_events (
        id BIGSERIAL PRIMARY KEY,
        key_id BIGINT NOT NULL REFERENCES keys(id),
        telegram_id BIGINT NOT NULL,
        outline_key_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        used_bytes BIGINT,
        quota_bytes BIGINT NOT NULL,
        expires_at TEXT NOT NULL,
        detected_at TEXT NOT NULL,
        remote_state TEXT NOT NULL,
        delete_attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        deletion_verified_at TEXT,
        user_notice_state TEXT,
        admin_notice_state TEXT,
        UNIQUE(key_id, reason)
    );
    CREATE INDEX IF NOT EXISTS key_termination_pending
        ON key_termination_events(remote_state, detected_at);
    CREATE TABLE IF NOT EXISTS plans (
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        price_minor BIGINT NOT NULL CHECK (price_minor >= 0),
        currency TEXT NOT NULL,
        quota_bytes BIGINT CHECK (quota_bytes IS NULL OR quota_bytes > 0),
        duration_days INTEGER NOT NULL CHECK (duration_days > 0),
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
    );
    CREATE TABLE IF NOT EXISTS orders (
        id TEXT PRIMARY KEY,
        telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
        plan_code TEXT NOT NULL REFERENCES plans(code),
        amount_minor BIGINT NOT NULL CHECK (amount_minor >= 0),
        currency TEXT NOT NULL,
        plan_name TEXT NOT NULL DEFAULT '',
        quota_bytes_snapshot BIGINT,
        duration_days_snapshot INTEGER,
        payment_method TEXT,
        status TEXT NOT NULL CHECK (status IN (
            'awaiting_payment', 'payment_submitted', 'approved',
            'rejected', 'cancelled'
        )),
        refund_status TEXT NOT NULL DEFAULT 'none',
        created_at TEXT NOT NULL,
        approved_at TEXT,
        rejected_at TEXT
    );
    CREATE INDEX IF NOT EXISTS orders_review ON orders(status, created_at);
    CREATE TABLE IF NOT EXISTS payments (
        id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL REFERENCES orders(id),
        provider TEXT NOT NULL,
        provider_reference TEXT NOT NULL,
        normalized_reference TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK (status IN (
            'submitted', 'verified', 'rejected', 'refunded'
        )),
        submitted_at TEXT NOT NULL,
        verified_at TEXT,
        UNIQUE(provider, provider_reference)
    );
    CREATE TABLE IF NOT EXISTS subscriptions (
        id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
        telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
        plan_code TEXT NOT NULL REFERENCES plans(code),
        starts_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        plan_name TEXT NOT NULL DEFAULT '',
        quota_bytes BIGINT,
        duration_days INTEGER,
        activated_at TEXT,
        status TEXT NOT NULL CHECK (status IN (
            'pending', 'active', 'expired', 'revoked', 'cancelled'
        )),
        CHECK (expires_at > starts_at)
    );
    CREATE INDEX IF NOT EXISTS subscriptions_expiry ON subscriptions(status, expires_at);
    CREATE TABLE IF NOT EXISTS paid_vpn_keys (
        id TEXT PRIMARY KEY,
        subscription_id TEXT NOT NULL UNIQUE REFERENCES subscriptions(id),
        telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
        outline_key_id TEXT NOT NULL,
        access_url TEXT NOT NULL,
        quota_bytes BIGINT,
        status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'revoke_failed')),
        quota_warning_percent INTEGER,
        created_at TEXT NOT NULL,
        revoked_at TEXT
    );
    CREATE TABLE IF NOT EXISTS provisioning_jobs (
        id TEXT PRIMARY KEY,
        subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
        operation TEXT NOT NULL CHECK (operation IN ('provision', 'revoke')),
        status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL,
        locked_at TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(subscription_id, operation)
    );
    CREATE INDEX IF NOT EXISTS provisioning_due ON provisioning_jobs(status, next_attempt_at);
    CREATE TABLE IF NOT EXISTS notifications (
        id TEXT PRIMARY KEY,
        dedupe_key TEXT NOT NULL UNIQUE,
        telegram_id BIGINT NOT NULL,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        access_url_ciphertext TEXT,
        status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        sent_at TEXT,
        dead_lettered_at TEXT
    );
    CREATE INDEX IF NOT EXISTS notifications_due ON notifications(status, next_attempt_at);
    CREATE TABLE IF NOT EXISTS telegram_command_scopes (
        chat_id BIGINT PRIMARY KEY,
        configured_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS admin_action_challenges (
        token_hash TEXT PRIMARY KEY,
        admin_id BIGINT NOT NULL,
        chat_id BIGINT NOT NULL,
        command TEXT NOT NULL,
        args_json TEXT NOT NULL,
        state_fingerprint TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'consumed', 'cancelled')),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT,
        cancelled_at TEXT
    );
    CREATE INDEX IF NOT EXISTS admin_action_challenges_expiry
        ON admin_action_challenges(status, expires_at);
    CREATE TABLE IF NOT EXISTS audit_events (
        id BIGSERIAL PRIMARY KEY,
        actor_type TEXT NOT NULL,
        actor_id TEXT,
        action TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS payment_evidence (
        id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL REFERENCES orders(id),
        telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
        provider TEXT NOT NULL,
        telegram_file_id TEXT NOT NULL,
        telegram_file_unique_id TEXT,
        telegram_media_type TEXT NOT NULL DEFAULT 'photo'
            CHECK (telegram_media_type IN ('photo', 'document')),
        image_sha256 TEXT NOT NULL,
        mime_type TEXT NOT NULL,
        byte_size BIGINT NOT NULL,
        storage_bucket TEXT,
        storage_path TEXT,
        storage_status TEXT NOT NULL DEFAULT 'not_configured',
        storage_error TEXT,
        stored_at TEXT,
        extraction_json TEXT,
        extraction_status TEXT NOT NULL CHECK (extraction_status IN ('parsed', 'needs_review', 'invalid')),
        submitted_at TEXT NOT NULL,
        reviewer_id BIGINT,
        review_notes TEXT,
        review_status TEXT NOT NULL DEFAULT 'pending'
            CHECK (review_status IN ('pending', 'verified', 'rejected')),
        verified_provider_reference TEXT,
        verified_amount_minor BIGINT,
        verified_currency TEXT,
        reviewed_at TEXT,
        UNIQUE(order_id, image_sha256)
    );
    CREATE INDEX IF NOT EXISTS payment_evidence_review ON payment_evidence(extraction_status, submitted_at);
    CREATE TABLE IF NOT EXISTS wallets (
        telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id),
        currency TEXT NOT NULL DEFAULT 'MMK',
        balance_minor BIGINT NOT NULL DEFAULT 0 CHECK (balance_minor >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS wallet_ledger (
        id TEXT PRIMARY KEY,
        telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
        kind TEXT NOT NULL CHECK (kind IN ('credit', 'reserve', 'capture', 'release', 'reversal')),
        amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
        currency TEXT NOT NULL,
        reference_type TEXT NOT NULL,
        reference_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS wallet_reservations (
        id TEXT PRIMARY KEY,
        telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
        order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
        amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
        currency TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('reserved', 'captured', 'released')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS quota_events (
        id TEXT PRIMARY KEY,
        subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
        reason TEXT NOT NULL,
        observed_bytes BIGINT NOT NULL,
        quota_bytes BIGINT NOT NULL,
        observed_at TEXT NOT NULL,
        UNIQUE(subscription_id, reason)
    );
    """
    with self.connect() as connection:
        for statement in schema.split(";"):
            statement = statement.strip()
            if statement:
                connection.execute(statement)
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
