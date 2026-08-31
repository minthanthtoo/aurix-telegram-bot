"""SQLite and PostgreSQL repositories for paid commerce state."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from commerce_models import CommerceError, _normalize_reference, _now_text
from migrations import COMMERCE_MIGRATIONS, FREE_ACCESS_MIGRATIONS, apply_migrations
from observability import latency_log as _latency_log
from persistence import open_sqlite_connection


class CommerceDatabase:
    """SQLite repository used for local/staging MVP state."""

    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        return open_sqlite_connection(self.path, busy_timeout_ms=30_000)

    @staticmethod
    def begin_write(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        return isinstance(error, sqlite3.IntegrityError)

    def initialize(self) -> None:
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
                    outline_key_id TEXT NOT NULL UNIQUE,
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

    @staticmethod
    def _seed_plans(connection: Any) -> None:
        plans = (
            ("basic_50gb", "50 GB", 3000, "MMK", 50 * 1024**3, 30),
            ("standard_100gb", "100 GB", 6000, "MMK", 100 * 1024**3, 30),
        )
        for plan in plans:
            connection.execute(
                """INSERT INTO plans
                   (code, name, price_minor, currency, quota_bytes, duration_days)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(code) DO NOTHING""",
                plan,
            )
        connection.execute(
            "UPDATE plans SET name = '50 GB', price_minor = 3000, currency = 'MMK', quota_bytes = ?, duration_days = 30, active = 1 WHERE code = 'basic_50gb'",
            (50 * 1024**3,),
        )
        connection.execute(
            "UPDATE plans SET name = '100 GB', price_minor = 6000, currency = 'MMK', quota_bytes = ?, duration_days = 30, active = 1 WHERE code = 'standard_100gb'",
            (100 * 1024**3,),
        )


class _PostgresConnection:
    """Small qmark-parameter adapter shared by the existing service queries."""

    def __init__(self, connection: Any, context: Any | None = None):
        # ``connection`` is a raw psycopg connection for the adapter tests. In
        # production it is replaced on enter by a psycopg_pool checkout.
        self._connection = connection
        self._context = context
        self._entered_at: float | None = None

    def __enter__(self) -> "_PostgresConnection":
        started_at = time.perf_counter()
        if self._context is not None:
            self._connection = self._context.__enter__()
        else:
            self._connection.__enter__()
        self._entered_at = time.perf_counter()
        _latency_log("postgres_checkout", started_at)
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
        try:
            if self._context is not None:
                return self._context.__exit__(exc_type, exc_value, traceback)
            return self._connection.__exit__(exc_type, exc_value, traceback)
        finally:
            if self._entered_at is not None:
                _latency_log("postgres_transaction", self._entered_at)
                self._entered_at = None

    def execute(self, query: str, params: Any = None) -> Any:
        query = query.replace("?", "%s")
        if params is None:
            return self._connection.execute(query)
        return self._connection.execute(query, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class PostgresCommerceDatabase:
    """PostgreSQL repository for hosted deployments with a durable database."""

    def __init__(self, url: str):
        self.url = url
        self._pool: Any | None = None
        self._pool_lock = threading.Lock()
        self._closed = False

    def _create_pool(self) -> Any:
        try:
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise CommerceError("PostgreSQL support requires the psycopg package") from exc
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise CommerceError("PostgreSQL support requires the psycopg-pool package") from exc
        # A persistent client-side pool removes DNS/TLS/authentication setup
        # from each repository call. Prepared statements stay disabled so the
        # same code remains safe with either Supabase pooler mode.
        started_at = time.perf_counter()
        pool = ConnectionPool(
            conninfo=self.url,
            kwargs={
                "row_factory": dict_row,
                "prepare_threshold": None,
                "connect_timeout": 10,
            },
            min_size=1,
            # The process has two database-using execution paths: Telegram
            # polling/handlers and one maintenance thread.
            max_size=2,
            timeout=10,
            open=False,
        )
        try:
            pool.open(wait=True, timeout=10)
        except Exception:
            pool.close()
            _latency_log("postgres_pool_open", started_at, status="error")
            raise
        _latency_log("postgres_pool_open", started_at, status="ready")
        return pool

    def connect(self) -> _PostgresConnection:
        with self._pool_lock:
            if self._closed:
                raise CommerceError("PostgreSQL connection pool is closed")
            if self._pool is None:
                self._pool = self._create_pool()
            pool = self._pool
        return _PostgresConnection(None, pool.connection())

    def close(self) -> None:
        """Return pooled connections cleanly during process shutdown."""
        with self._pool_lock:
            self._closed = True
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()

    @staticmethod
    def begin_write(connection: _PostgresConnection) -> None:
        # psycopg starts a transaction automatically before the first
        # statement when autocommit is disabled.  Sending an explicit BEGIN
        # here would create a nested BEGIN and Supabase logs status 25001
        # ("there is already a transaction in progress") for every write.
        return None

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        try:
            import psycopg
        except ImportError:
            return False
        return isinstance(error, psycopg.IntegrityError)

    def mark_update_seen(self, update_id: int) -> bool:
        """Durably deduplicate Telegram updates across restarts."""
        with self.connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO telegram_updates (update_id, received_at) VALUES (?, ?)",
                    (int(update_id), _now_text()),
                )
            except Exception as exc:
                if self.is_integrity_error(exc):
                    return False
                raise
        return True

    def list_command_scope_ids(self) -> set[int]:
        """Return chat-specific command scopes previously configured by this bot."""
        with self.connect() as connection:
            rows = connection.execute("SELECT chat_id FROM telegram_command_scopes").fetchall()
        return {int(row["chat_id"]) for row in rows}

    def record_command_scope(self, chat_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO telegram_command_scopes (chat_id, configured_at)
                   VALUES (?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET configured_at = EXCLUDED.configured_at""",
                (int(chat_id), _now_text()),
            )

    def remove_command_scope(self, chat_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM telegram_command_scopes WHERE chat_id = ?",
                (int(chat_id),),
            )

    def create_admin_challenge(
        self,
        token_hash: str,
        admin_id: int,
        chat_id: int,
        command: str,
        args_json: str,
        state_fingerprint: str,
        created_at: str,
        expires_at: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO admin_action_challenges
                   (token_hash, admin_id, chat_id, command, args_json,
                    state_fingerprint, status, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    token_hash,
                    int(admin_id),
                    int(chat_id),
                    command,
                    args_json,
                    state_fingerprint,
                    created_at,
                    expires_at,
                ),
            )

    def consume_admin_challenge(
        self,
        token_hash: str,
        admin_id: int,
        chat_id: int,
        state_fingerprint: str,
        now: str,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT command, args_json, state_fingerprint
                   FROM admin_action_challenges
                   WHERE token_hash = ? AND admin_id = ? AND chat_id = ?
                     AND state_fingerprint = ? AND status = 'pending'
                     AND expires_at > ?""",
                (token_hash, int(admin_id), int(chat_id), state_fingerprint, now),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """UPDATE admin_action_challenges
                   SET status = 'consumed', consumed_at = ?
                   WHERE token_hash = ? AND status = 'pending'""",
                (now, token_hash),
            )
            if getattr(updated, "rowcount", 1) != 1:
                return None
            result = dict(row)
        try:
            result["args"] = json.loads(result.pop("args_json") or "[]")
        except json.JSONDecodeError:
            return None
        return result

    def cancel_admin_challenge(
        self, token_hash: str, admin_id: int, chat_id: int, now: str
    ) -> bool:
        with self.connect() as connection:
            updated = connection.execute(
                """UPDATE admin_action_challenges
                   SET status = 'cancelled', cancelled_at = ?
                   WHERE token_hash = ? AND admin_id = ? AND chat_id = ?
                     AND status = 'pending'""",
                (now, token_hash, int(admin_id), int(chat_id)),
            )
        return getattr(updated, "rowcount", 1) == 1

    def prune_admin_challenges(self, now: str, retention_days: int = 30) -> int:
        cutoff = (
            datetime.fromisoformat(now) - timedelta(days=max(1, int(retention_days)))
        ).isoformat()
        with self.connect() as connection:
            deleted = connection.execute(
                """DELETE FROM admin_action_challenges
                   WHERE (status = 'pending' AND expires_at <= ?)
                      OR (status <> 'pending' AND created_at < ?)""",
                (now, cutoff),
            )
        return int(getattr(deleted, "rowcount", 0) or 0)

    def maintenance_heartbeat(
        self,
        *,
        started_at: str | None = None,
        completed_at: str | None = None,
        success_at: str | None = None,
        stage: str | None = None,
        error: str | None = None,
    ) -> None:
        now = _now_text()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO maintenance_heartbeat
                   (id, last_started_at, last_completed_at, last_success_at,
                    last_stage, last_error, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     last_started_at = COALESCE(EXCLUDED.last_started_at, maintenance_heartbeat.last_started_at),
                     last_completed_at = COALESCE(EXCLUDED.last_completed_at, maintenance_heartbeat.last_completed_at),
                     last_success_at = COALESCE(EXCLUDED.last_success_at, maintenance_heartbeat.last_success_at),
                     last_stage = EXCLUDED.last_stage,
                     last_error = EXCLUDED.last_error,
                     updated_at = EXCLUDED.updated_at""",
                (started_at, completed_at, success_at, stage, error, now),
            )

    def get_maintenance_heartbeat(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM maintenance_heartbeat WHERE id = 1").fetchone()
        return dict(row) if row is not None else None

    def initialize(self) -> None:
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
            outline_key_id TEXT NOT NULL UNIQUE,
            key_type TEXT NOT NULL DEFAULT 'daily_free'
                CHECK (key_type IN ('daily_free', 'monthly_trial', 'paid')),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            data_limit_bytes BIGINT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'revoke_failed')),
            last_usage_bytes BIGINT,
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
            outline_key_id TEXT NOT NULL UNIQUE,
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
            CommerceDatabase._seed_plans(connection)
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
