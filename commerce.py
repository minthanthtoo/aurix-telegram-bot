#!/usr/bin/env python3
"""Paid-concierge commerce and provisioning state for the AuriX MVP.

The module keeps the first staging deployment small. Its repository boundary is
small enough to migrate to PostgreSQL later without putting payment or Outline
calls in Telegram handlers.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from supabase_storage import NullReceiptStorage

UTC = timezone.utc
JOB_RETRY_DELAY = timedelta(seconds=30)
NOTIFICATION_RETRY_DELAY = timedelta(minutes=1)
QUOTA_WARNING_THRESHOLDS = ((25, 0.25), (10, 0.10), (5, 0.05))


def _latency_log(event: str, started_at: float, **fields: Any) -> None:
    """Emit bounded timing evidence without logging SQL, credentials, or payloads."""
    if os.environ.get("AURIX_LATENCY_LOG", "0").lower() not in {"1", "true", "yes", "on"}:
        return
    duration_ms = (time.perf_counter() - started_at) * 1000
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    suffix = f" {details}" if details else ""
    print(f"latency event={event} duration_ms={duration_ms:.1f}{suffix}", file=sys.stderr)


def _human_bytes(value: int) -> str:
    amount = float(max(0, int(value)))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1024


def _paid_outline_key_name(subscription: Any) -> str:
    raw_identity = subscription["username"] or str(subscription["telegram_id"])
    identity = re.sub(
        r"[^A-Za-z0-9_-]+", "-", str(raw_identity).lstrip("@")
    ).strip("-_")[:48] or str(subscription["telegram_id"])
    quota = subscription["quota_bytes"]
    if quota is not None and int(quota) % (1024**3) == 0:
        tier = f"PAID{int(quota) // (1024**3)}GB"
    else:
        tier = str(subscription["plan_code"]).upper().replace("_", "-")
    duration = f"{int(subscription['duration_days'])}day"
    started = datetime.fromisoformat(subscription["starts_at"]).astimezone(UTC)
    # The short subscription suffix keeps simultaneous purchases for the same
    # user/plan/minute distinguishable on Outline versions without deterministic
    # caller-selected IDs.
    try:
        subscription_id = subscription["id"]
    except (KeyError, IndexError, TypeError):
        subscription_id = None
    suffix = str(subscription_id or "")[:8]
    base = f"{identity}-{tier}-{duration}-{started.strftime('%Y%m%d%H%M')}"
    return f"{base}-{suffix}"[:128] if suffix else base[:128]


class CommerceError(RuntimeError):
    """A safe, user-facing commerce validation error."""


@dataclass(frozen=True)
class Plan:
    code: str
    name: str
    price_minor: int
    currency: str
    quota_bytes: int | None
    duration_days: int


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    plan: Plan
    status: str
    created: bool = True
    plan_conflict: bool = False


@dataclass(frozen=True)
class ApprovalResult:
    order_id: str
    subscription_id: str
    status: str


def _now_text(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def _normalize_reference(value: str) -> str:
    """Normalize a payment reference for comparison without changing display data."""
    return "".join(str(value or "").split()).casefold()


class CommerceDatabase:
    """SQLite repository used for local/staging MVP state."""

    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

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
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(notifications)")
            }
            if "access_url_ciphertext" not in columns:
                connection.execute(
                    "ALTER TABLE notifications ADD COLUMN access_url_ciphertext TEXT"
                )
            if "dead_lettered_at" not in columns:
                connection.execute(
                    "ALTER TABLE notifications ADD COLUMN dead_lettered_at TEXT"
                )
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
            user_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(users)")
            }
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
            raise CommerceError(
                "PostgreSQL support requires the psycopg-pool package"
            ) from exc
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
            rows = connection.execute(
                "SELECT chat_id FROM telegram_command_scopes"
            ).fetchall()
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
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            data_limit_bytes BIGINT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'revoke_failed')),
            last_usage_bytes BIGINT,
            quota_reason TEXT,
            quota_warning_percent INTEGER
        );
        CREATE INDEX IF NOT EXISTS keys_expiry ON keys(status, expires_at);
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
            connection.execute("ALTER TABLE paid_vpn_keys ADD COLUMN IF NOT EXISTS last_usage_bytes BIGINT")
            connection.execute("ALTER TABLE paid_vpn_keys ADD COLUMN IF NOT EXISTS last_usage_observed_at TEXT")
            connection.execute("ALTER TABLE paid_vpn_keys ADD COLUMN IF NOT EXISTS quota_reason TEXT")
            connection.execute("ALTER TABLE paid_vpn_keys ADD COLUMN IF NOT EXISTS quota_warning_percent INTEGER")
            connection.execute("ALTER TABLE keys ADD COLUMN IF NOT EXISTS quota_warning_percent INTEGER")
            connection.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS plan_name TEXT NOT NULL DEFAULT ''")
            connection.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS quota_bytes_snapshot BIGINT")
            connection.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS duration_days_snapshot INTEGER")
            connection.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS refund_status TEXT NOT NULL DEFAULT 'none'")
            connection.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS plan_name TEXT NOT NULL DEFAULT ''")
            connection.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS quota_bytes BIGINT")
            connection.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS duration_days INTEGER")
            connection.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS activated_at TEXT")
            connection.execute("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS dead_lettered_at TEXT")
            connection.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT")
            connection.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_claimed_at TEXT")
            connection.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS normalized_reference TEXT NOT NULL DEFAULT ''")
            connection.execute("UPDATE payments SET normalized_reference = LOWER(REPLACE(provider_reference, ' ', '')) WHERE normalized_reference = ''")
            connection.execute("CREATE INDEX IF NOT EXISTS payments_reference_lookup ON payments(provider, normalized_reference)")
            connection.execute("ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS review_status TEXT NOT NULL DEFAULT 'pending'")
            connection.execute("ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS verified_provider_reference TEXT")
            connection.execute("ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS verified_amount_minor BIGINT")
            connection.execute("ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS verified_currency TEXT")
            connection.execute("ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS reviewed_at TEXT")
            connection.execute("ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS telegram_media_type TEXT NOT NULL DEFAULT 'photo'")
            connection.execute("ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS storage_bucket TEXT")
            connection.execute("ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS storage_path TEXT")
            connection.execute("ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS storage_status TEXT NOT NULL DEFAULT 'not_configured'")
            connection.execute("ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS storage_error TEXT")
            connection.execute("ALTER TABLE payment_evidence ADD COLUMN IF NOT EXISTS stored_at TEXT")
            CommerceDatabase._seed_plans(connection)


class CommerceService:
    """Commerce state machine and idempotent Outline job processor."""

    def __init__(
        self,
        database: Any,
        outline: Any,
        access_url_key: bytes | str,
        allow_legacy_text_approval: bool = False,
        receipt_storage: Any | None = None,
        receipt_storage_required: bool = False,
    ):
        self.database = database
        self.outline = outline
        # Kept only for controlled migration tests. Public deployments must
        # require verified screenshot evidence or a wallet reservation.
        self.allow_legacy_text_approval = bool(allow_legacy_text_approval)
        self.receipt_storage = receipt_storage or NullReceiptStorage()
        self.receipt_storage_required = bool(receipt_storage_required)
        try:
            self.access_url_cipher = Fernet(access_url_key)
        except (TypeError, ValueError) as exc:
            raise ValueError("AURIX_ACCESS_URL_KEY must be a Fernet key") from exc

    def _encrypt_access_url(self, access_url: str) -> str:
        return self.access_url_cipher.encrypt(access_url.encode()).decode()

    def _decrypt_access_url(self, encrypted: str | None) -> str | None:
        if not encrypted:
            return None
        try:
            return self.access_url_cipher.decrypt(encrypted.encode()).decode()
        except (InvalidToken, UnicodeDecodeError, ValueError):
            return None

    @staticmethod
    def _receipt_storage_extension(mime_type: str) -> str:
        normalized = str(mime_type or "").lower().split(";", 1)[0].strip()
        return {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "image/gif": "gif",
        }.get(normalized, "bin")

    @staticmethod
    def _receipt_storage_path(order_id: str, evidence_id: str, mime_type: str) -> str:
        # Order/evidence IDs are generated UUIDs. Keep this defensive because
        # old/imported order IDs may contain unexpected characters.
        safe_order = re.sub(r"[^A-Za-z0-9_-]+", "-", str(order_id)).strip("-_")[:96]
        safe_evidence = re.sub(r"[^A-Za-z0-9_-]+", "-", str(evidence_id)).strip("-_")[:96]
        extension = CommerceService._receipt_storage_extension(mime_type)
        return f"orders/{safe_order or 'unknown'}/{safe_evidence or _new_id()}.{extension}"

    def _storage_is_configured(self) -> bool:
        return bool(getattr(self.receipt_storage, "configured", False))

    def _storage_bucket(self) -> str | None:
        bucket = getattr(self.receipt_storage, "bucket", None)
        return str(bucket) if bucket else None

    @staticmethod
    def _lock_order(connection: Any, order_id: str) -> None:
        """Serialize aggregate mutations on PostgreSQL as well as SQLite.

        SQLite already serializes writers. PostgreSQL needs an explicit row
        lock because payment, receipt, approval and refund requests can arrive
        concurrently from Telegram retries or two administrators.
        """
        if isinstance(connection, _PostgresConnection):
            connection.execute("SELECT id FROM orders WHERE id = ? FOR UPDATE", (order_id,)).fetchone()

    def initialize(self) -> None:
        self.database.initialize()

    def plans(self) -> list[Plan]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT code, name, price_minor, currency, quota_bytes, duration_days
                   FROM plans WHERE active = 1 ORDER BY price_minor"""
            ).fetchall()
        return [Plan(**dict(row)) for row in rows]

    def get_plan(self, code: str) -> Plan:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT code, name, price_minor, currency, quota_bytes, duration_days
                   FROM plans WHERE code = ? AND active = 1""",
                (code,),
            ).fetchone()
        if row is None:
            raise CommerceError("Unknown or inactive plan")
        return Plan(**dict(row))

    @staticmethod
    def _ensure_user(
        connection: sqlite3.Connection,
        telegram_id: int,
        first_name: str,
        username: str | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO users (telegram_id, first_name, username, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                   first_name = excluded.first_name,
                   username = COALESCE(excluded.username, users.username)""",
            (
                telegram_id,
                first_name[:128],
                (username or "").lstrip("@")[:64] or None,
                _now_text(),
            ),
        )

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        action: str,
        target_type: str,
        target_id: str,
        actor_type: str,
        actor_id: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO audit_events
               (actor_type, actor_id, action, target_type, target_id, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                actor_type,
                actor_id,
                action,
                target_type,
                target_id,
                json.dumps(metadata or {}, sort_keys=True),
                _now_text(),
            ),
        )

    def create_order(
        self,
        telegram_id: int,
        first_name: str,
        plan_code: str,
        now: datetime | None = None,
        username: str | None = None,
    ) -> OrderResult:
        plan = self.get_plan(plan_code)
        order_id = _new_id()
        created_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._ensure_user(connection, telegram_id, first_name, username)
            if isinstance(connection, _PostgresConnection):
                connection.execute(
                    "SELECT telegram_id FROM users WHERE telegram_id = ? FOR UPDATE",
                    (telegram_id,),
                ).fetchone()
            existing = connection.execute(
                """SELECT * FROM orders
                   WHERE telegram_id = ?
                     AND status IN ('awaiting_payment', 'payment_submitted')
                     AND COALESCE(refund_status, 'none') != 'refunded'
                   ORDER BY created_at LIMIT 1""",
                (telegram_id,),
            ).fetchone()
            if existing is not None:
                existing_plan = Plan(
                    code=str(existing["plan_code"]),
                    name=str(existing["plan_name"] or plan.name),
                    price_minor=int(existing["amount_minor"]),
                    currency=str(existing["currency"]),
                    quota_bytes=(
                        existing["quota_bytes_snapshot"]
                        if existing["quota_bytes_snapshot"] is not None
                        else plan.quota_bytes
                    ),
                    duration_days=int(
                        existing["duration_days_snapshot"] or plan.duration_days
                    ),
                )
                return OrderResult(
                    str(existing["id"]), existing_plan, str(existing["status"]),
                    False, existing_plan.code != plan.code,
                )
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, plan_name,
                    quota_bytes_snapshot, duration_days_snapshot, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_payment', ?)""",
                (
                    order_id,
                    telegram_id,
                    plan.code,
                    plan.price_minor,
                    plan.currency,
                    plan.name,
                    plan.quota_bytes,
                    plan.duration_days,
                    created_at,
                ),
            )
            self._audit(
                connection,
                "order_created",
                "order",
                order_id,
                "customer",
                str(telegram_id),
                {"plan_code": plan.code, "amount_minor": plan.price_minor},
            )
        return OrderResult(order_id, plan, "awaiting_payment")

    def replace_open_order(
        self,
        telegram_id: int,
        first_name: str,
        plan_code: str,
        now: datetime | None = None,
        username: str | None = None,
        expected_order_id: str | None = None,
    ) -> OrderResult:
        """Replace an untouched open order with a different plan."""
        plan = self.get_plan(plan_code)
        created_at = _now_text(now)
        new_order_id = _new_id()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._ensure_user(connection, telegram_id, first_name, username)
            existing = connection.execute(
                """SELECT * FROM orders
                   WHERE telegram_id = ? AND status IN ('awaiting_payment', 'payment_submitted')
                     AND COALESCE(refund_status, 'none') != 'refunded'
                   ORDER BY created_at LIMIT 1""",
                (telegram_id,),
            ).fetchone()
            if existing is None:
                raise CommerceError("No open order is available to replace")
            self._lock_order(connection, str(existing["id"]))
            if expected_order_id and str(existing["id"]) != str(expected_order_id):
                raise CommerceError("The open order changed; refresh and try again")
            if existing["plan_code"] == plan.code:
                return OrderResult(str(existing["id"]), plan, str(existing["status"]), False)
            evidence_count = connection.execute(
                "SELECT COUNT(*) AS n FROM payment_evidence WHERE order_id = ?",
                (existing["id"],),
            ).fetchone()["n"]
            payment_count = connection.execute(
                "SELECT COUNT(*) AS n FROM payments WHERE order_id = ?",
                (existing["id"],),
            ).fetchone()["n"]
            if evidence_count or payment_count:
                raise CommerceError("This order has payment activity and cannot be replaced; ask staff to review it")
            connection.execute(
                "UPDATE orders SET status = 'cancelled', rejected_at = ? WHERE id = ?",
                (created_at, existing["id"]),
            )
            self._audit(
                connection, "order_replaced", "order", str(existing["id"]),
                "customer", str(telegram_id), {"new_order_id": new_order_id, "plan_code": plan.code},
            )
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, plan_name,
                    quota_bytes_snapshot, duration_days_snapshot, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_payment', ?)""",
                (
                    new_order_id, telegram_id, plan.code, plan.price_minor,
                    plan.currency, plan.name, plan.quota_bytes, plan.duration_days,
                    created_at,
                ),
            )
            self._audit(
                connection, "order_created", "order", new_order_id,
                "customer", str(telegram_id), {"plan_code": plan.code, "replaces_order_id": str(existing["id"])},
            )
        return OrderResult(new_order_id, plan, "awaiting_payment")

    def cancel_order(
        self, telegram_id: int, order_id: str, now: datetime | None = None
    ) -> str:
        """Cancel an empty customer order, or release a wallet reservation."""
        cancelled_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute(
                "SELECT * FROM orders WHERE id = ? AND telegram_id = ?",
                (order_id, telegram_id),
            ).fetchone()
            if order is None:
                raise CommerceError("Order not found")
            if order["status"] in ("cancelled", "rejected"):
                return "already_cancelled"
            if order["status"] == "approved":
                raise CommerceError("An approved order cannot be cancelled")
            evidence_count = connection.execute(
                "SELECT COUNT(*) AS n FROM payment_evidence WHERE order_id = ?",
                (order_id,),
            ).fetchone()["n"]
            payment_count = connection.execute(
                "SELECT COUNT(*) AS n FROM payments WHERE order_id = ?",
                (order_id,),
            ).fetchone()["n"]
            if evidence_count or payment_count:
                raise CommerceError("This order has payment activity; ask staff to reject or refund it")
            connection.execute(
                "UPDATE orders SET status = 'cancelled', rejected_at = ? WHERE id = ?",
                (cancelled_at, order_id),
            )
            self._audit(
                connection, "order_cancelled", "order", order_id,
                "customer", str(telegram_id),
            )
        return "cancelled"

    def expire_open_orders(
        self,
        now: datetime | None = None,
        awaiting_ttl: timedelta = timedelta(hours=24),
    ) -> int:
        """Close only untouched unpaid orders past the customer-facing TTL."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        cutoff = _now_text(current - awaiting_ttl)
        closed = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            rows = connection.execute(
                """SELECT o.id FROM orders o
                   WHERE o.status = 'awaiting_payment' AND o.created_at <= ?
                     AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.id)
                     AND NOT EXISTS (SELECT 1 FROM payment_evidence e WHERE e.order_id = o.id)""",
                (cutoff,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE orders SET status = 'cancelled', rejected_at = ? WHERE id = ?",
                    (_now_text(current), row["id"]),
                )
                self._audit(
                    connection, "order_expired", "order", str(row["id"]),
                    "system", None,
                )
                closed += 1
        return closed

    def release_expired_wallet_reservations(
        self,
        now: datetime | None = None,
        reservation_ttl: timedelta = timedelta(hours=24),
    ) -> int:
        """Release wallet holds that were never approved by staff."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        cutoff = _now_text(current - reservation_ttl)
        released = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            rows = connection.execute(
                """SELECT r.order_id, r.telegram_id, r.amount_minor, r.currency
                   FROM wallet_reservations r JOIN orders o ON o.id = r.order_id
                   WHERE r.status = 'reserved' AND r.created_at <= ?
                     AND o.status = 'payment_submitted'""",
                (cutoff,),
            ).fetchall()
            for row in rows:
                idem = f"release:{row['order_id']}"
                if connection.execute(
                    "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (idem,)
                ).fetchone() is None:
                    connection.execute(
                        "UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ? WHERE telegram_id = ?",
                        (row["amount_minor"], _now_text(current), row["telegram_id"]),
                    )
                    connection.execute(
                        """INSERT INTO wallet_ledger
                           (id, telegram_id, kind, amount_minor, currency, reference_type,
                            reference_id, idempotency_key, created_at)
                           VALUES (?, ?, 'release', ?, ?, 'order', ?, ?, ?)""",
                        (_new_id(), row["telegram_id"], row["amount_minor"], row["currency"], row["order_id"], idem, _now_text(current)),
                    )
                connection.execute(
                    "UPDATE wallet_reservations SET status = 'released', updated_at = ? WHERE order_id = ?",
                    (_now_text(current), row["order_id"]),
                )
                connection.execute(
                    "UPDATE payments SET status = 'rejected' WHERE order_id = ? AND provider = 'wallet' AND status = 'submitted'",
                    (row["order_id"],),
                )
                connection.execute(
                    "UPDATE orders SET status = 'cancelled', rejected_at = ? WHERE id = ? AND status = 'payment_submitted'",
                    (_now_text(current), row["order_id"]),
                )
                connection.execute(
                    """INSERT INTO notifications
                       (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
                       VALUES (?, ?, ?, 'wallet_reservation_expired', ?, 'pending', ?, ?)
                       ON CONFLICT(dedupe_key) DO NOTHING""",
                    (_new_id(), f"wallet-reservation-expired:{row['order_id']}", row["telegram_id"],
                     "Your wallet payment hold expired before approval; the funds were returned to your wallet.",
                     _now_text(current), _now_text(current)),
                )
                self._audit(
                    connection, "wallet_reservation_expired", "order", row["order_id"],
                    "system", None,
                )
                released += 1
        return released

    @staticmethod
    def _order_stage(order: dict[str, Any]) -> str:
        """Derive one customer-facing stage from order/payment/evidence state."""
        status = str(order.get("status") or "")
        subscription = str(order.get("subscription_status") or "")
        payment = str(order.get("payment_status") or "")
        receipt = str(order.get("receipt_status") or "")
        reservation = str(order.get("wallet_reservation_status") or "")
        if str(order.get("refund_status") or "none") == "refunded" or payment == "refunded":
            return "refunded"
        provision = str(order.get("provisioning_status") or "")
        revoke = str(order.get("revocation_status") or "")
        if revoke in ("pending", "running"):
            return "revocation_pending"
        if revoke == "failed":
            return "revocation_failed"
        if status == "approved":
            if provision == "failed":
                return "activation_failed"
            if subscription == "active":
                return "fulfilled"
            if subscription == "pending":
                return "activation_pending"
            return "approved"
        if status in ("rejected", "cancelled"):
            return status
        if receipt == "verified" or payment == "verified":
            return "payment_verified"
        if reservation == "reserved":
            return "wallet_reserved"
        if receipt == "pending" or payment == "submitted":
            return "review_pending"
        return "awaiting_payment"

    def list_user_orders(
        self, telegram_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT o.id, o.plan_code, o.plan_name, o.amount_minor, o.currency,
                          o.status, o.refund_status, o.created_at,
                          (SELECT p.status FROM payments p WHERE p.order_id = o.id
                           ORDER BY p.submitted_at DESC LIMIT 1) AS payment_status,
                          (SELECT e.review_status FROM payment_evidence e WHERE e.order_id = o.id
                           ORDER BY e.submitted_at DESC LIMIT 1) AS receipt_status,
                          (SELECT s.status FROM subscriptions s WHERE s.order_id = o.id
                           LIMIT 1) AS subscription_status,
                          (SELECT s.expires_at FROM subscriptions s WHERE s.order_id = o.id
                           LIMIT 1) AS expires_at,
                          (SELECT j.status FROM provisioning_jobs j JOIN subscriptions s
                           ON s.id = j.subscription_id WHERE s.order_id = o.id
                           AND j.operation = 'provision' LIMIT 1) AS provisioning_status,
                          (SELECT j.status FROM provisioning_jobs j JOIN subscriptions s
                           ON s.id = j.subscription_id WHERE s.order_id = o.id
                           AND j.operation = 'revoke' LIMIT 1) AS revocation_status,
                          (SELECT r.status FROM wallet_reservations r WHERE r.order_id = o.id
                           LIMIT 1) AS wallet_reservation_status
                   FROM orders o WHERE o.telegram_id = ?
                   ORDER BY o.created_at DESC LIMIT ?""",
                (telegram_id, max(1, min(limit, 50))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["stage"] = self._order_stage(item)
            result.append(item)
        return result

    def reconcile_duplicate_open_orders(self) -> dict[str, int]:
        """Cancel only empty historical duplicates, preserving review evidence."""
        cancelled = 0
        manual_conflicts = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            users = connection.execute(
                """SELECT telegram_id FROM orders
                   WHERE status IN ('awaiting_payment', 'payment_submitted')
                   GROUP BY telegram_id HAVING COUNT(*) > 1"""
            ).fetchall()
            for user in users:
                rows = connection.execute(
                    """SELECT o.id, o.created_at,
                              (SELECT COUNT(*) FROM payments p WHERE p.order_id = o.id) AS payments,
                              (SELECT COUNT(*) FROM payment_evidence e WHERE e.order_id = o.id) AS evidence
                       FROM orders o WHERE o.telegram_id = ?
                         AND o.status IN ('awaiting_payment', 'payment_submitted')
                       ORDER BY o.created_at""",
                    (user["telegram_id"],),
                ).fetchall()
                protected = [
                    row for row in rows if int(row["payments"]) or int(row["evidence"])
                ]
                keeper_id = (protected[0] if protected else rows[0])["id"]
                if len(protected) > 1:
                    manual_conflicts += 1
                for row in rows:
                    if row["id"] == keeper_id:
                        continue
                    if int(row["payments"]) or int(row["evidence"]):
                        continue
                    connection.execute(
                        "UPDATE orders SET status = 'cancelled' WHERE id = ?",
                        (row["id"],),
                    )
                    self._audit(
                        connection,
                        "duplicate_empty_order_cancelled",
                        "order",
                        str(row["id"]),
                        "system",
                        None,
                        {"kept_order_id": str(keeper_id)},
                    )
                    cancelled += 1
        return {"cancelled": cancelled, "manual_conflicts": manual_conflicts}

    def order_detail(
        self, order_id: str, requester_id: int, is_admin: bool = False
    ) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT o.*,
                          (SELECT p.status FROM payments p WHERE p.order_id = o.id
                           ORDER BY p.submitted_at DESC LIMIT 1) AS payment_status,
                          (SELECT p.provider FROM payments p WHERE p.order_id = o.id
                           ORDER BY p.submitted_at DESC LIMIT 1) AS payment_provider,
                          (SELECT e.review_status FROM payment_evidence e WHERE e.order_id = o.id
                           ORDER BY e.submitted_at DESC LIMIT 1) AS receipt_status,
                          (SELECT e.id FROM payment_evidence e WHERE e.order_id = o.id
                           ORDER BY e.submitted_at DESC LIMIT 1) AS evidence_id,
                          (SELECT s.status FROM subscriptions s WHERE s.order_id = o.id
                           LIMIT 1) AS subscription_status,
                          (SELECT s.expires_at FROM subscriptions s WHERE s.order_id = o.id
                           LIMIT 1) AS expires_at,
                          (SELECT j.status FROM provisioning_jobs j JOIN subscriptions s
                           ON s.id = j.subscription_id
                           WHERE s.order_id = o.id AND j.operation = 'provision'
                           LIMIT 1) AS provisioning_status,
                          (SELECT j.status FROM provisioning_jobs j JOIN subscriptions s
                           ON s.id = j.subscription_id
                           WHERE s.order_id = o.id AND j.operation = 'revoke'
                           LIMIT 1) AS revocation_status,
                          (SELECT r.status FROM wallet_reservations r WHERE r.order_id = o.id
                           LIMIT 1) AS wallet_reservation_status
                   FROM orders o WHERE o.id = ?""",
                (order_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if not is_admin and int(result["telegram_id"]) != int(requester_id):
            return None
        result["stage"] = self._order_stage(result)
        return result

    def submit_payment(
        self,
        telegram_id: int,
        order_id: str,
        provider: str,
        provider_reference: str,
        now: datetime | None = None,
    ) -> str:
        provider = provider.strip()[:64]
        provider_reference = provider_reference.strip()[:128]
        normalized_reference = _normalize_reference(provider_reference)
        if not provider or not provider_reference:
            raise CommerceError("Payment provider and reference are required")
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if order is None or order["telegram_id"] != telegram_id:
                raise CommerceError("Order not found")
            if order["status"] == "approved":
                raise CommerceError("Order is already approved")
            if order["status"] not in ("awaiting_payment", "payment_submitted"):
                raise CommerceError("Order is not open for payment")
            existing = connection.execute(
                """SELECT provider, provider_reference FROM payments WHERE order_id = ?
                   ORDER BY submitted_at DESC LIMIT 1""",
                (order_id,),
            ).fetchone()
            if existing is not None:
                if (
                    _normalize_reference(existing["provider"]) == _normalize_reference(provider)
                    and _normalize_reference(existing["provider_reference"]) == normalized_reference
                ):
                    return "already_submitted"
                raise CommerceError("A payment reference is already attached to this order")
            try:
                connection.execute(
                    """INSERT INTO payments
                       (id, order_id, provider, provider_reference, normalized_reference, status, submitted_at)
                       VALUES (?, ?, ?, ?, ?, 'submitted', ?)""",
                    (_new_id(), order_id, provider, provider_reference, normalized_reference, _now_text(now)),
                )
            except Exception as exc:
                if self.database.is_integrity_error(exc):
                    raise CommerceError("Payment reference has already been submitted") from exc
                raise
            connection.execute(
                "UPDATE orders SET status = 'payment_submitted' WHERE id = ?",
                (order_id,),
            )
            self._audit(
                connection,
                "payment_submitted",
                "order",
                order_id,
                "customer",
                str(telegram_id),
                {"provider": provider},
            )
        return "submitted"

    def pending_order_for_user(self, telegram_id: int) -> dict[str, Any] | None:
        """Return the oldest open order so a receipt can be sent without text."""
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT * FROM orders
                   WHERE telegram_id = ? AND status IN ('awaiting_payment', 'payment_submitted')
                     AND COALESCE(refund_status, 'none') != 'refunded'
                   ORDER BY created_at LIMIT 1""",
                (telegram_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def submit_receipt(
        self,
        telegram_id: int,
        order_id: str,
        provider: str,
        file_id: str,
        file_unique_id: str | None,
        image_bytes: bytes,
        mime_type: str,
        extraction: dict[str, Any] | None = None,
        now: datetime | None = None,
        telegram_media_type: str = "photo",
    ) -> dict[str, Any]:
        """Persist receipt metadata and upload the raw image out-of-band.

        The database transaction creates an upload-pending evidence row, then
        the object is uploaded without holding a database connection open. A
        second short transaction marks the object stored and moves the order to
        payment review. This keeps network latency out of the database lock and
        makes a lost response safely retryable using the same immutable path.
        """
        if not isinstance(file_id, str) or not file_id.strip():
            raise CommerceError("Receipt file id is missing")
        if not image_bytes or len(image_bytes) > 20 * 1024 * 1024:
            raise CommerceError("Receipt image is empty or too large")
        if telegram_media_type not in ("photo", "document"):
            raise CommerceError("Receipt media type is invalid")
        digest = hashlib.sha256(image_bytes).hexdigest()
        extraction = extraction if isinstance(extraction, dict) else None
        tx_id = extraction.get("transaction_id") if extraction else None
        provider_name = str((extraction or {}).get("provider") or provider).strip()[:64]
        tx_candidate = str(tx_id).strip()[:128] if tx_id else ""
        status = "parsed" if tx_id else "needs_review"
        submitted_at = _now_text(now)
        storage_configured = self._storage_is_configured()
        if self.receipt_storage_required and not storage_configured:
            raise CommerceError("Receipt storage is not configured")
        storage_status = "pending" if storage_configured else "not_configured"
        storage_bucket = self._storage_bucket() if storage_configured else None
        evidence_id: str
        storage_path: str | None = None
        is_new = False
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if order is None or order["telegram_id"] != telegram_id:
                raise CommerceError("Order not found")
            if order["status"] == "approved":
                raise CommerceError("Order is already approved")
            if order["status"] not in ("awaiting_payment", "payment_submitted"):
                raise CommerceError("Order is not open for a receipt")
            existing = connection.execute(
                """SELECT id, extraction_json, extraction_status, review_status,
                          storage_bucket, storage_path, storage_status
                   FROM payment_evidence
                   WHERE order_id = ? AND image_sha256 = ?""",
                (order_id, digest),
            ).fetchone()
            if existing is not None:
                parsed = json.loads(existing["extraction_json"] or "{}")
                result = parsed if isinstance(parsed, dict) else {}
                result["evidence_id"] = existing["id"]
                result["extraction_status"] = existing["extraction_status"]
                result["review_status"] = existing["review_status"]
                result["image_sha256"] = digest
                result["storage_status"] = existing["storage_status"] or "not_configured"
                result["storage_path"] = existing["storage_path"]
                storage_ready = (
                    result["storage_status"] == "stored"
                    if storage_configured
                    else result["storage_status"] in ("stored", "not_configured")
                )
                if storage_ready:
                    # A prior process may have committed the evidence row but
                    # lost the response before moving the order state. Repair
                    # that narrow inconsistency on an idempotent retry.
                    if order["status"] == "awaiting_payment":
                        connection.execute(
                            "UPDATE orders SET status = 'payment_submitted' WHERE id = ?",
                            (order_id,),
                        )
                        self._audit(
                            connection,
                            "receipt_state_recovered",
                            "order",
                            order_id,
                            "customer",
                            str(telegram_id),
                            {"evidence_id": existing["id"]},
                        )
                    return result
                evidence_id = str(existing["id"])
                storage_path = str(existing["storage_path"] or "") or self._receipt_storage_path(
                    order_id, evidence_id, mime_type
                )
                connection.execute(
                    """UPDATE payment_evidence
                       SET storage_bucket = ?, storage_path = ?, storage_status = 'pending',
                           storage_error = NULL
                       WHERE id = ?""",
                    (storage_bucket, storage_path, evidence_id),
                )
            else:
                latest = connection.execute(
                    """SELECT review_status FROM payment_evidence
                       WHERE order_id = ? ORDER BY submitted_at DESC LIMIT 1""",
                    (order_id,),
                ).fetchone()
                if latest is not None and str(latest["review_status"] or "pending") != "rejected":
                    raise CommerceError("A receipt is already awaiting review; wait for staff feedback")
                payment_rows = connection.execute(
                    "SELECT provider, status FROM payments WHERE order_id = ?",
                    (order_id,),
                ).fetchall()
                if any(str(item["provider"] or "").lower() == "wallet" for item in payment_rows):
                    raise CommerceError("This order already uses wallet payment; receipt payment cannot be combined")
                if any(str(item["status"] or "") in ("submitted", "verified", "refunded") for item in payment_rows):
                    raise CommerceError("A payment is already attached to this order")
                # Keep model output as evidence only. Detect a repeated
                # candidate across screenshots without creating an
                # authoritative payment row.
                if tx_candidate:
                    prior_evidence = connection.execute(
                        "SELECT provider, extraction_json FROM payment_evidence WHERE order_id != ?",
                        (order_id,),
                    ).fetchall()
                    for prior in prior_evidence:
                        try:
                            prior_extraction = json.loads(prior["extraction_json"] or "{}")
                        except json.JSONDecodeError:
                            prior_extraction = {}
                        prior_tx = (
                            prior_extraction.get("transaction_id")
                            if isinstance(prior_extraction, dict)
                            else None
                        )
                        if (
                            _normalize_reference(str(prior["provider"]))
                            == _normalize_reference(provider_name or "manual")
                            and _normalize_reference(str(prior_tx or ""))
                            == _normalize_reference(tx_candidate)
                        ):
                            status = "needs_review"
                            flagged = dict(extraction or {})
                            flagged["flags"] = sorted(
                                set(flagged.get("flags") or [])
                                | {"duplicate_transaction_candidate"}
                            )
                            extraction = flagged
                            break
                evidence_id = _new_id()
                storage_path = (
                    self._receipt_storage_path(order_id, evidence_id, mime_type)
                    if storage_configured
                    else None
                )
                connection.execute(
                    """INSERT INTO payment_evidence
                       (id, order_id, telegram_id, provider, telegram_file_id,
                        telegram_file_unique_id, telegram_media_type, image_sha256,
                        mime_type, byte_size, storage_bucket, storage_path,
                        storage_status, extraction_json, extraction_status,
                        submitted_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        evidence_id,
                        order_id,
                        telegram_id,
                        provider_name,
                        file_id[:256],
                        file_unique_id[:256] if file_unique_id else None,
                        telegram_media_type,
                        digest,
                        mime_type[:64],
                        len(image_bytes),
                        storage_bucket,
                        storage_path,
                        storage_status,
                        json.dumps(extraction or {}, sort_keys=True),
                        status,
                        submitted_at,
                    ),
                )
                is_new = True

        if storage_configured:
            assert storage_path is not None
            try:
                uploaded_path = self.receipt_storage.upload(
                    storage_path, image_bytes, mime_type
                )
                uploaded_path = str(uploaded_path or "").strip()
                if not uploaded_path:
                    raise RuntimeError("Receipt storage returned an empty object path")
            except Exception as exc:
                # Preserve the row so a retry can reuse the same object path.
                try:
                    with self.database.connect() as connection:
                        connection.execute(
                            """UPDATE payment_evidence
                               SET storage_status = 'failed', storage_error = ?
                               WHERE id = ?""",
                            (type(exc).__name__[:128], evidence_id),
                        )
                except Exception:
                    pass
                raise CommerceError(
                    "Receipt image could not be saved. Please try again."
                ) from exc
            try:
                storage_path = uploaded_path
                with self.database.connect() as connection:
                    self.database.begin_write(connection)
                    connection.execute(
                        """UPDATE payment_evidence
                           SET storage_bucket = ?, storage_path = ?, storage_status = 'stored',
                               storage_error = NULL, stored_at = ?
                           WHERE id = ?""",
                        (storage_bucket, str(uploaded_path), submitted_at, evidence_id),
                    )
                    connection.execute(
                        "UPDATE orders SET status = 'payment_submitted' WHERE id = ?",
                        (order_id,),
                    )
                    self._audit(
                        connection,
                        "receipt_submitted" if is_new else "receipt_storage_recovered",
                        "order",
                        order_id,
                        "customer",
                        str(telegram_id),
                        {"evidence_id": evidence_id, "extraction_status": status},
                    )
            except Exception:
                # Do not leave a billable orphan if the final metadata commit
                # fails. Deletion is best-effort and the row remains retryable.
                try:
                    self.receipt_storage.delete(storage_path)
                except Exception:
                    pass
                raise
        else:
            # A receipt is a payment submission even when OCR/LLM extraction
            # failed. Approval still requires a human verification decision.
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                connection.execute(
                    "UPDATE orders SET status = 'payment_submitted' WHERE id = ?",
                    (order_id,),
                )
                if is_new:
                    self._audit(
                        connection,
                        "receipt_submitted",
                        "order",
                        order_id,
                        "customer",
                        str(telegram_id),
                        {"evidence_id": evidence_id, "extraction_status": status},
                    )
        result = dict(extraction or {})
        result["evidence_id"] = evidence_id
        result["image_sha256"] = digest
        result["extraction_status"] = status
        result["storage_status"] = "stored" if storage_configured else "not_configured"
        result["storage_path"] = storage_path
        return result

    def list_pending_receipts(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT e.id, e.order_id, e.telegram_id, e.provider, e.image_sha256,
                          e.byte_size, e.storage_bucket, e.storage_path, e.storage_status,
                          e.extraction_json, e.extraction_status, e.submitted_at,
                          o.plan_code, o.amount_minor, o.currency
                   FROM payment_evidence e JOIN orders o ON o.id = e.order_id
                   WHERE e.review_status = 'pending'
                     AND e.storage_status IN ('stored', 'not_configured')
                   ORDER BY e.submitted_at LIMIT ?""",
                (max(1, min(limit, 100)),),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            try:
                item["extraction"] = json.loads(item.pop("extraction_json") or "{}")
            except json.JSONDecodeError:
                item["extraction"] = {}
            results.append(item)
        return results

    def get_receipt(self, evidence_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT e.*, o.plan_code, o.amount_minor, o.currency, o.status AS order_status
                   FROM payment_evidence e JOIN orders o ON o.id = e.order_id
                   WHERE e.id = ?""",
                (evidence_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["extraction"] = json.loads(result.pop("extraction_json") or "{}")
        except json.JSONDecodeError:
            result["extraction"] = {}
        return result

    def verify_receipt(
        self,
        evidence_id: str,
        admin_id: int,
        provider_reference: str,
        verified_amount_minor: int,
        currency: str = "MMK",
        now: datetime | None = None,
    ) -> str:
        """Record a human verification against the receiving account.

        LLM extraction is deliberately excluded from this trust boundary.  The
        reviewer must supply the transaction ID and amount observed in the
        actual receiving account before an order can be approved.
        """
        provider_reference = provider_reference.strip()[:128]
        currency = currency.strip().upper()[:16]
        try:
            verified_amount_minor = int(verified_amount_minor)
        except (TypeError, ValueError) as exc:
            raise CommerceError("Verified payment amount must be an integer") from exc
        if not provider_reference:
            raise CommerceError("Verified transaction ID is required")
        if verified_amount_minor <= 0:
            raise CommerceError("Verified payment amount must be positive")
        reviewed_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            evidence = connection.execute(
                """SELECT e.*, o.amount_minor, o.currency, o.status AS order_status
                   FROM payment_evidence e JOIN orders o ON o.id = e.order_id
                   WHERE e.id = ?""",
                (evidence_id,),
            ).fetchone()
            if evidence is None:
                raise CommerceError("Receipt evidence not found")
            self._lock_order(connection, str(evidence["order_id"]))
            if evidence["order_status"] == "approved":
                return evidence["order_id"]
            if (
                self.receipt_storage_required
                and str(evidence["storage_status"] or "") != "stored"
            ):
                raise CommerceError("Receipt image must be stored before verification")
            if evidence["review_status"] == "verified":
                if (
                    str(evidence["verified_provider_reference"] or "").strip().casefold()
                    == provider_reference.casefold()
                    and int(evidence["verified_amount_minor"] or 0) == verified_amount_minor
                    and str(evidence["verified_currency"] or "").upper() == currency
                ):
                    return evidence["order_id"]
                raise CommerceError("Receipt verification is already recorded")
            if evidence["review_status"] == "rejected":
                raise CommerceError("Receipt was rejected; submit a new screenshot")
            if evidence["order_status"] not in ("awaiting_payment", "payment_submitted"):
                raise CommerceError("Order is not open for receipt verification")
            if currency != str(evidence["currency"]).upper():
                raise CommerceError("Verified payment currency does not match the order")
            if verified_amount_minor < int(evidence["amount_minor"]):
                raise CommerceError("Verified payment amount is below the order total")
            payment = connection.execute(
                """SELECT id FROM payments
                   WHERE order_id = ? AND status IN ('submitted', 'verified')
                   ORDER BY submitted_at DESC LIMIT 1""",
                (evidence["order_id"],),
            ).fetchone()
            provider = str(evidence["provider"] or "manual")[:64]
            normalized_provider = _normalize_reference(provider)
            normalized_reference = _normalize_reference(provider_reference)
            conflicts = connection.execute(
                """SELECT order_id, provider, normalized_reference FROM payments
                   WHERE status IN ('submitted', 'verified')
                     AND normalized_reference = ? AND order_id != ?""",
                (normalized_reference, evidence["order_id"]),
            ).fetchall()
            if any(
                _normalize_reference(str(item["provider"])) == normalized_provider
                for item in conflicts
            ):
                raise CommerceError("This transaction ID has already been submitted for another order")
            try:
                if payment is None:
                    payment_id = _new_id()
                    connection.execute(
                        """INSERT INTO payments
                           (id, order_id, provider, provider_reference, normalized_reference,
                            status, submitted_at, verified_at)
                           VALUES (?, ?, ?, ?, ?, 'verified', ?, ?)""",
                        (
                            payment_id,
                            evidence["order_id"],
                            provider,
                            provider_reference,
                            normalized_reference,
                            reviewed_at,
                            reviewed_at,
                        ),
                    )
                else:
                    payment_id = payment["id"]
                    connection.execute(
                        """UPDATE payments
                           SET provider = ?, provider_reference = ?, normalized_reference = ?,
                               status = 'verified', verified_at = ?
                           WHERE id = ?""",
                        (provider, provider_reference, normalized_reference, reviewed_at, payment_id),
                    )
            except Exception as exc:
                if self.database.is_integrity_error(exc):
                    raise CommerceError("This transaction ID has already been verified") from exc
                raise
            connection.execute(
                """UPDATE payment_evidence
                   SET reviewer_id = ?, review_notes = 'verified against receiving account',
                       review_status = 'verified', verified_provider_reference = ?,
                       verified_amount_minor = ?, verified_currency = ?, reviewed_at = ?
                   WHERE id = ?""",
                (
                    admin_id,
                    provider_reference,
                    verified_amount_minor,
                    currency,
                    reviewed_at,
                    evidence_id,
                ),
            )
            connection.execute(
                "UPDATE orders SET status = 'payment_submitted' WHERE id = ?",
                (evidence["order_id"],),
            )
            self._audit(
                connection,
                "receipt_verified",
                "payment_evidence",
                evidence_id,
                "admin",
                str(admin_id),
                {"amount_minor": verified_amount_minor, "currency": currency},
            )
        return str(evidence["order_id"])

    def reject_receipt(
        self,
        evidence_id: str,
        admin_id: int,
        notes: str = "rejected by admin",
        now: datetime | None = None,
    ) -> str:
        reviewed_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            evidence = connection.execute(
                "SELECT id, order_id, review_status FROM payment_evidence WHERE id = ?",
                (evidence_id,),
            ).fetchone()
            if evidence is None:
                raise CommerceError("Receipt evidence not found")
            self._lock_order(connection, str(evidence["order_id"]))
            if evidence["review_status"] == "verified":
                raise CommerceError("Verified receipt cannot be rejected")
            if evidence["review_status"] == "rejected":
                return str(evidence["order_id"])
            connection.execute(
                """UPDATE payment_evidence SET reviewer_id = ?, review_notes = ?,
                           review_status = 'rejected', reviewed_at = ? WHERE id = ?""",
                (admin_id, (notes or "rejected by admin")[:500], reviewed_at, evidence_id),
            )
            connection.execute(
                "UPDATE payments SET status = 'rejected' WHERE order_id = ? AND status = 'submitted'",
                (evidence["order_id"],),
            )
            self._audit(
                connection, "receipt_rejected", "payment_evidence", evidence_id,
                "admin", str(admin_id), {"notes": (notes or "")[:500]},
            )
        return str(evidence["order_id"])

    def wallet_balance(self, telegram_id: int, currency: str = "MMK") -> int:
        now_text = _now_text()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            user = connection.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
            if user is None:
                self._ensure_user(connection, telegram_id, "")
            existing_wallet = connection.execute(
                "SELECT currency FROM wallets WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
            if existing_wallet is not None and str(existing_wallet["currency"]).upper() != currency.upper():
                raise CommerceError("A user wallet has one supported currency")
            connection.execute(
                """INSERT INTO wallets (telegram_id, currency, balance_minor, created_at, updated_at)
                   VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
                (telegram_id, currency, now_text, now_text),
            )
            row = connection.execute("SELECT balance_minor FROM wallets WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return int(row["balance_minor"] if row else 0)

    def wallet_history(
        self, telegram_id: int, limit: int = 20, currency: str = "MMK"
    ) -> list[dict[str, Any]]:
        """Return immutable wallet events for the owner, newest first."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT kind, amount_minor, currency, reference_type, reference_id,
                          created_at
                   FROM wallet_ledger
                   WHERE telegram_id = ? AND currency = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (telegram_id, currency.upper(), max(1, min(limit, 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def consistency_report(
        self, now: datetime | None = None,
        review_sla: timedelta = timedelta(hours=24),
    ) -> dict[str, int]:
        """Read-only invariant scan for admin operations and deployment checks."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        review_cutoff = _now_text(current - review_sla)
        with self.database.connect() as connection:
            duplicate_open = connection.execute(
                """SELECT COUNT(*) AS n FROM (
                     SELECT telegram_id FROM orders
                     WHERE status IN ('awaiting_payment', 'payment_submitted')
                     GROUP BY telegram_id HAVING COUNT(*) > 1)"""
            ).fetchone()["n"]
            approved_missing_subscription = connection.execute(
                """SELECT COUNT(*) AS n FROM orders o
                   LEFT JOIN subscriptions s ON s.order_id = o.id
                   WHERE o.status = 'approved' AND s.id IS NULL"""
            ).fetchone()["n"]
            approved_missing_job = connection.execute(
                """SELECT COUNT(*) AS n FROM subscriptions s
                   JOIN orders o ON o.id = s.order_id
                   WHERE o.status = 'approved'
                     AND NOT EXISTS (SELECT 1 FROM provisioning_jobs j
                                     WHERE j.subscription_id = s.id AND j.operation = 'provision')"""
            ).fetchone()["n"]
            pending_reviews = connection.execute(
                "SELECT COUNT(*) AS n FROM payment_evidence WHERE review_status = 'pending'"
            ).fetchone()["n"]
            stale_reviews = connection.execute(
                """SELECT COUNT(*) AS n FROM payment_evidence
                   WHERE review_status = 'pending' AND submitted_at <= ?""",
                (review_cutoff,),
            ).fetchone()["n"]
            pending_receipt_uploads = connection.execute(
                "SELECT COUNT(*) AS n FROM payment_evidence WHERE storage_status = 'pending'"
            ).fetchone()["n"]
            failed_receipt_uploads = connection.execute(
                "SELECT COUNT(*) AS n FROM payment_evidence WHERE storage_status = 'failed'"
            ).fetchone()["n"]
            failed_jobs = connection.execute(
                "SELECT COUNT(*) AS n FROM provisioning_jobs WHERE status = 'failed'"
            ).fetchone()["n"]
            pending_revocations = connection.execute(
                "SELECT COUNT(*) AS n FROM provisioning_jobs WHERE operation = 'revoke' AND status IN ('pending', 'running')"
            ).fetchone()["n"]
            failed_revocations = connection.execute(
                "SELECT COUNT(*) AS n FROM provisioning_jobs WHERE operation = 'revoke' AND status = 'failed'"
            ).fetchone()["n"]
            failed_activations = connection.execute(
                "SELECT COUNT(*) AS n FROM provisioning_jobs WHERE operation = 'provision' AND status = 'failed'"
            ).fetchone()["n"]
            dead_notifications = connection.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE dead_lettered_at IS NOT NULL"
            ).fetchone()["n"]
            wallet_mismatches = 0
            wallets = connection.execute("SELECT telegram_id, currency, balance_minor FROM wallets").fetchall()
            for wallet in wallets:
                ledger = connection.execute(
                    """SELECT COALESCE(SUM(CASE WHEN kind IN ('credit', 'release', 'reversal')
                                                THEN amount_minor
                                                WHEN kind = 'reserve' THEN -amount_minor
                                                ELSE 0 END), 0) AS projected
                       FROM wallet_ledger WHERE telegram_id = ? AND currency = ?""",
                    (wallet["telegram_id"], wallet["currency"]),
                ).fetchone()
                if int(ledger["projected"] or 0) != int(wallet["balance_minor"]):
                    wallet_mismatches += 1
        return {
            "duplicate_open_orders": int(duplicate_open),
            "approved_missing_subscription": int(approved_missing_subscription),
            "approved_missing_provision_job": int(approved_missing_job),
            "pending_receipts": int(pending_reviews),
            "stale_receipts": int(stale_reviews),
            "pending_receipt_uploads": int(pending_receipt_uploads),
            "failed_receipt_uploads": int(failed_receipt_uploads),
            "failed_jobs": int(failed_jobs),
            "pending_revocations": int(pending_revocations),
            "failed_revocations": int(failed_revocations),
            "failed_activations": int(failed_activations),
            "dead_notifications": int(dead_notifications),
            "wallet_balance_mismatches": int(wallet_mismatches),
        }

    def credit_wallet(self, telegram_id: int, amount_minor: int, reference_id: str, admin_id: int, currency: str = "MMK") -> str:
        if amount_minor <= 0:
            raise CommerceError("Wallet credit must be positive")
        now_text = _now_text()
        idem = f"credit:{reference_id}"
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._ensure_user(connection, telegram_id, "")
            connection.execute(
                """INSERT INTO wallets (telegram_id, currency, balance_minor, created_at, updated_at)
                   VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
                (telegram_id, currency, now_text, now_text),
            )
            existing = connection.execute("SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (idem,)).fetchone()
            if existing is not None:
                return "already_credited"
            connection.execute(
                "UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ? WHERE telegram_id = ?",
                (amount_minor, now_text, telegram_id),
            )
            connection.execute(
                """INSERT INTO wallet_ledger
                   (id, telegram_id, kind, amount_minor, currency, reference_type,
                    reference_id, idempotency_key, created_at)
                   VALUES (?, ?, 'credit', ?, ?, 'payment', ?, ?, ?)""",
                (_new_id(), telegram_id, amount_minor, currency, reference_id, idem, now_text),
            )
            self._audit(connection, "wallet_credited", "user", str(telegram_id), "admin", str(admin_id), {"amount_minor": amount_minor, "reference_id": reference_id})
        return "credited"

    def pay_order_with_wallet(self, telegram_id: int, order_id: str, now: datetime | None = None) -> str:
        """Reserve wallet funds and submit an idempotent wallet payment."""
        now_text = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if order is None or order["telegram_id"] != telegram_id:
                raise CommerceError("Order not found")
            if order["status"] == "approved":
                return "already_approved"
            if order["status"] not in ("awaiting_payment", "payment_submitted"):
                raise CommerceError("Order is not open for wallet payment")
            evidence = connection.execute(
                "SELECT review_status FROM payment_evidence WHERE order_id = ? ORDER BY submitted_at DESC LIMIT 1",
                (order_id,),
            ).fetchone()
            if evidence is not None and str(evidence["review_status"] or "pending") != "rejected":
                raise CommerceError("This order already has a receipt; wallet payment cannot be combined")
            payments = connection.execute(
                "SELECT provider, status FROM payments WHERE order_id = ?",
                (order_id,),
            ).fetchall()
            if any(str(item["provider"] or "").lower() != "wallet" and str(item["status"] or "") in ("submitted", "verified") for item in payments):
                raise CommerceError("A receipt payment is already attached to this order")
            self._ensure_user(connection, telegram_id, "")
            connection.execute(
                """INSERT INTO wallets (telegram_id, currency, balance_minor, created_at, updated_at)
                   VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
                (telegram_id, order["currency"], now_text, now_text),
            )
            idem = f"reserve:{order_id}"
            existing = connection.execute("SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (idem,)).fetchone()
            if existing is not None:
                return "already_reserved"
            updated = connection.execute(
                """UPDATE wallets SET balance_minor = balance_minor - ?, updated_at = ?
                   WHERE telegram_id = ? AND balance_minor >= ?""",
                (order["amount_minor"], now_text, telegram_id, order["amount_minor"]),
            )
            if getattr(updated, "rowcount", 1) == 0:
                raise CommerceError("Insufficient wallet balance")
            connection.execute(
                """INSERT INTO wallet_ledger
                   (id, telegram_id, kind, amount_minor, currency, reference_type,
                    reference_id, idempotency_key, created_at)
                   VALUES (?, ?, 'reserve', ?, ?, 'order', ?, ?, ?)""",
                (_new_id(), telegram_id, order["amount_minor"], order["currency"], order_id, idem, now_text),
            )
            connection.execute(
                """INSERT INTO wallet_reservations
                   (id, telegram_id, order_id, amount_minor, currency, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?)""",
                (_new_id(), telegram_id, order_id, order["amount_minor"], order["currency"], now_text, now_text),
            )
            connection.execute(
                """INSERT INTO payments
                   (id, order_id, provider, provider_reference, normalized_reference, status, submitted_at)
                   VALUES (?, ?, 'wallet', ?, ?, 'submitted', ?)""",
                (_new_id(), order_id, f"wallet:{order_id}", _normalize_reference(f"wallet:{order_id}"), now_text),
            )
            connection.execute("UPDATE orders SET status = 'payment_submitted' WHERE id = ?", (order_id,))
            self._audit(connection, "wallet_reserved", "order", order_id, "customer", str(telegram_id), {"amount_minor": order["amount_minor"]})
        return "reserved"

    def list_pending_orders(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT o.id, o.telegram_id, o.plan_code, o.amount_minor, o.currency,
                          o.status, o.created_at,
                          (SELECT p.provider FROM payments p WHERE p.order_id = o.id
                           ORDER BY p.submitted_at DESC LIMIT 1) AS provider,
                          (SELECT p.provider_reference FROM payments p WHERE p.order_id = o.id
                           ORDER BY p.submitted_at DESC LIMIT 1) AS provider_reference,
                          (SELECT e.review_status FROM payment_evidence e WHERE e.order_id = o.id
                           ORDER BY e.submitted_at DESC LIMIT 1) AS receipt_status,
                          (SELECT r.status FROM wallet_reservations r WHERE r.order_id = o.id
                           LIMIT 1) AS wallet_reservation_status
                   FROM orders o
                   WHERE o.status IN ('awaiting_payment', 'payment_submitted')
                     AND COALESCE(o.refund_status, 'none') != 'refunded'
                   ORDER BY o.created_at LIMIT ?""",
                (max(1, min(limit, 100)),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["stage"] = self._order_stage(item)
            result.append(item)
        return result

    def approve_order(
        self,
        order_id: str,
        admin_id: int,
        now: datetime | None = None,
    ) -> ApprovalResult:
        starts_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if order is None:
                raise CommerceError("Order not found")
            if order["status"] == "approved":
                subscription = connection.execute(
                    "SELECT id FROM subscriptions WHERE order_id = ?", (order_id,)
                ).fetchone()
                if subscription is None:
                    raise CommerceError("Approved order has no subscription record")
                return ApprovalResult(order_id, subscription["id"], "already_approved")
            if order["status"] != "payment_submitted":
                raise CommerceError("Order has no submitted payment for review")
            evidence = connection.execute(
                """SELECT review_status, verified_amount_minor, verified_currency,
                          storage_status
                   FROM payment_evidence WHERE order_id = ?
                   ORDER BY submitted_at DESC LIMIT 1""",
                (order_id,),
            ).fetchone()
            wallet_reservation = connection.execute(
                """SELECT id, amount_minor, currency, status
                   FROM wallet_reservations WHERE order_id = ? LIMIT 1""",
                (order_id,),
            ).fetchone()
            if evidence is not None and evidence["review_status"] != "verified":
                if wallet_reservation is None or wallet_reservation["status"] != "reserved":
                    raise CommerceError("Receipt must be verified against the receiving account first")
            if (
                evidence is not None
                and self.receipt_storage_required
                and str(evidence["storage_status"] or "") != "stored"
            ):
                raise CommerceError("Receipt image must be stored before approval")
            payment = connection.execute(
                """SELECT id, provider, status FROM payments WHERE order_id = ?
                   AND status IN ('submitted', 'verified')
                   ORDER BY submitted_at DESC LIMIT 1""",
                (order_id,),
            ).fetchone()
            if payment is None:
                raise CommerceError("Payment record is missing")
            wallet_payment = str(payment["provider"] or "") == "wallet"
            if evidence is not None and payment["status"] != "verified":
                raise CommerceError("Receipt payment has not been verified")
            if wallet_payment and (
                wallet_reservation is None
                or wallet_reservation["status"] != "reserved"
                or int(wallet_reservation["amount_minor"]) < int(order["amount_minor"])
                or str(wallet_reservation["currency"]).upper() != str(order["currency"]).upper()
            ):
                raise CommerceError("Wallet reservation is missing or no longer valid")
            if evidence is None and not wallet_payment and not self.allow_legacy_text_approval:
                raise CommerceError("Verified receipt evidence is required before approval")
            if evidence is not None and wallet_payment:
                raise CommerceError("An order cannot combine a wallet payment and receipt evidence")
            plan = connection.execute(
                "SELECT duration_days, quota_bytes, name FROM plans WHERE code = ?", (order["plan_code"],)
            ).fetchone()
            if plan is None:
                raise CommerceError("Plan record is missing")
            duration_days = int(order["duration_days_snapshot"] or plan["duration_days"])
            plan_name = str(order["plan_name"] or plan["name"])
            quota_bytes = order["quota_bytes_snapshot"] if order["quota_bytes_snapshot"] is not None else plan["quota_bytes"]
            # Each approved paid order represents an independent entitlement
            # and may provision its own key. A customer can therefore buy
            # multiple plans/devices at once; renewal is not serialized behind
            # an existing subscription.
            effective_start = datetime.fromisoformat(starts_at)
            expires_at = (
                effective_start + timedelta(days=duration_days)
            ).isoformat()
            subscription_id = _new_id()
            if payment["status"] == "submitted":
                connection.execute(
                    """UPDATE payments SET status = 'verified', verified_at = ?
                       WHERE id = ?""",
                    (starts_at, payment["id"]),
                )
            connection.execute(
                """UPDATE orders SET status = 'approved', approved_at = ? WHERE id = ?""",
                (starts_at, order_id),
            )
            connection.execute(
                """INSERT INTO subscriptions
                   (id, order_id, telegram_id, plan_code, starts_at, expires_at,
                    plan_name, quota_bytes, duration_days, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    subscription_id,
                    order_id,
                    order["telegram_id"],
                    order["plan_code"],
                    effective_start.isoformat(),
                    expires_at,
                    plan_name,
                    quota_bytes,
                    duration_days,
                ),
            )
            # Record money movement as immutable ledger events. External
            # deposits are credited and immediately reserved/captured. Wallet
            # payments already have a reservation; approval only captures it.
            wallet_now = starts_at
            connection.execute(
                """INSERT INTO wallets (telegram_id, currency, balance_minor, created_at, updated_at)
                   VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
                (order["telegram_id"], order["currency"], wallet_now, wallet_now),
            )
            payment_id = payment["id"]
            credit_amount = int(order["amount_minor"])
            if evidence is not None:
                if str(evidence["verified_currency"]).upper() != str(order["currency"]).upper():
                    raise CommerceError("Verified receipt currency does not match the order")
                credit_amount = int(evidence["verified_amount_minor"])
            if not wallet_payment:
                credit_idem = f"credit:{payment_id}"
                credit_exists = connection.execute(
                    "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (credit_idem,)
                ).fetchone()
                if credit_exists is None:
                    connection.execute(
                        "UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ? WHERE telegram_id = ?",
                        (credit_amount, wallet_now, order["telegram_id"]),
                    )
                    connection.execute(
                        """INSERT INTO wallet_ledger
                           (id, telegram_id, kind, amount_minor, currency, reference_type,
                            reference_id, idempotency_key, created_at)
                           VALUES (?, ?, 'credit', ?, ?, 'payment', ?, ?, ?)""",
                        (_new_id(), order["telegram_id"], credit_amount, order["currency"], payment_id, credit_idem, wallet_now),
                    )
                reserve_idem = f"reserve:{order_id}"
                reserve_exists = connection.execute(
                    "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (reserve_idem,)
                ).fetchone()
                if reserve_exists is None:
                    updated = connection.execute(
                        "UPDATE wallets SET balance_minor = balance_minor - ?, updated_at = ? WHERE telegram_id = ? AND balance_minor >= ?",
                        (order["amount_minor"], wallet_now, order["telegram_id"], order["amount_minor"]),
                    )
                    if getattr(updated, "rowcount", 1) == 0:
                        raise CommerceError("Verified payment credit is insufficient for this order")
                    connection.execute(
                        """INSERT INTO wallet_ledger
                           (id, telegram_id, kind, amount_minor, currency, reference_type,
                            reference_id, idempotency_key, created_at)
                           VALUES (?, ?, 'reserve', ?, ?, 'order', ?, ?, ?)""",
                        (_new_id(), order["telegram_id"], order["amount_minor"], order["currency"], order_id, reserve_idem, wallet_now),
                    )
                    connection.execute(
                        """INSERT INTO wallet_reservations
                           (id, telegram_id, order_id, amount_minor, currency, status, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?)""",
                        (_new_id(), order["telegram_id"], order_id, order["amount_minor"], order["currency"], wallet_now, wallet_now),
                    )
            capture_idem = f"capture:{order_id}"
            if connection.execute("SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (capture_idem,)).fetchone() is None:
                # Capture is a state transition; the reserve already reduced
                # available balance, so capture must not deduct again.
                connection.execute(
                    """INSERT INTO wallet_ledger
                       (id, telegram_id, kind, amount_minor, currency, reference_type,
                        reference_id, idempotency_key, created_at)
                       VALUES (?, ?, 'capture', ?, ?, 'order', ?, ?, ?)""",
                    (_new_id(), order["telegram_id"], order["amount_minor"], order["currency"], order_id, capture_idem, wallet_now),
                )
            connection.execute(
                """INSERT INTO wallet_reservations
                   (id, telegram_id, order_id, amount_minor, currency, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'captured', ?, ?)
                   ON CONFLICT(order_id) DO UPDATE SET status = 'captured', updated_at = excluded.updated_at""",
                (_new_id(), order["telegram_id"], order_id, order["amount_minor"], order["currency"], wallet_now, wallet_now),
            )
            connection.execute(
                """INSERT INTO provisioning_jobs
                   (id, subscription_id, operation, status, next_attempt_at, created_at)
                   VALUES (?, ?, 'provision', 'pending', ?, ?)""",
                (_new_id(), subscription_id, effective_start.isoformat(), effective_start.isoformat()),
            )
            self._audit(
                connection,
                "order_approved",
                "order",
                order_id,
                "admin",
                str(admin_id),
                {"subscription_id": subscription_id},
            )
        return ApprovalResult(order_id, subscription_id, "approved")

    def reject_order(self, order_id: str, admin_id: int, now: datetime | None = None) -> str:
        rejected_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute(
                "SELECT status, telegram_id FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if order is None:
                raise CommerceError("Order not found")
            if order["status"] == "rejected":
                return "already_rejected"
            if order["status"] == "approved":
                raise CommerceError("Approved order cannot be rejected here")
            verified_payment = connection.execute(
                "SELECT id FROM payments WHERE order_id = ? AND status = 'verified' LIMIT 1",
                (order_id,),
            ).fetchone()
            verified_receipt = connection.execute(
                "SELECT id FROM payment_evidence WHERE order_id = ? AND review_status = 'verified' LIMIT 1",
                (order_id,),
            ).fetchone()
            if verified_payment is not None or verified_receipt is not None:
                raise CommerceError("Verified payment must be refunded instead of rejected")
            connection.execute(
                "UPDATE orders SET status = 'rejected', rejected_at = ? WHERE id = ?",
                (rejected_at, order_id),
            )
            connection.execute(
                "UPDATE payments SET status = 'rejected' WHERE order_id = ? AND status = 'submitted'",
                (order_id,),
            )
            wallet_payment = connection.execute(
                "SELECT provider FROM payments WHERE order_id = ? ORDER BY submitted_at DESC LIMIT 1",
                (order_id,),
            ).fetchone()
            if wallet_payment is not None and wallet_payment["provider"] == "wallet":
                amount_row = connection.execute("SELECT amount_minor, currency, telegram_id FROM orders WHERE id = ?", (order_id,)).fetchone()
                release_idem = f"release:{order_id}"
                if amount_row is not None and connection.execute("SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (release_idem,)).fetchone() is None:
                    connection.execute("UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ? WHERE telegram_id = ?", (amount_row["amount_minor"], rejected_at, amount_row["telegram_id"]))
                    connection.execute(
                        """INSERT INTO wallet_ledger
                           (id, telegram_id, kind, amount_minor, currency, reference_type,
                            reference_id, idempotency_key, created_at)
                           VALUES (?, ?, 'release', ?, ?, 'order', ?, ?, ?)""",
                        (_new_id(), amount_row["telegram_id"], amount_row["amount_minor"], amount_row["currency"], order_id, release_idem, rejected_at),
                    )
                    connection.execute("UPDATE wallet_reservations SET status = 'released', updated_at = ? WHERE order_id = ?", (rejected_at, order_id))
            connection.execute(
                """UPDATE payment_evidence
                   SET reviewer_id = ?, review_notes = 'rejected by admin',
                       review_status = 'rejected', reviewed_at = ?
                   WHERE order_id = ? AND reviewer_id IS NULL""",
                (admin_id, rejected_at, order_id),
            )
            connection.execute(
                """INSERT INTO notifications
                   (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
                   VALUES (?, ?, ?, 'payment_rejected', ?, 'pending', ?, ?)
                   ON CONFLICT(dedupe_key) DO NOTHING""",
                (
                    _new_id(),
                    f"payment-rejected:{order_id}",
                    order["telegram_id"],
                    "Your AuriX payment/order was rejected. Contact support if you need a review.",
                    rejected_at,
                    rejected_at,
                ),
            )
            self._audit(
                connection,
                "order_rejected",
                "order",
                order_id,
                "admin",
                str(admin_id),
            )
        return "rejected"

    def refund_order(
        self,
        order_id: str,
        admin_id: int,
        reason: str = "refunded by admin",
        now: datetime | None = None,
    ) -> str:
        """Record an idempotent wallet compensation and close paid access.

        This does not claim that an external bank transfer was reversed.  It
        credits the customer's AuriX wallet as a compensating ledger event;
        the operator remains responsible for any off-platform payout.
        """
        refunded_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if order is None:
                raise CommerceError("Order not found")
            if str(order["refund_status"] or "none") == "refunded":
                return "already_refunded"
            payment = connection.execute(
                """SELECT id, provider, status FROM payments
                   WHERE order_id = ? AND status IN ('verified', 'submitted')
                   ORDER BY submitted_at DESC LIMIT 1""",
                (order_id,),
            ).fetchone()
            if payment is None or payment["status"] != "verified":
                raise CommerceError("Only a verified payment can be refunded")
            verified_evidence = connection.execute(
                """SELECT verified_amount_minor, verified_currency FROM payment_evidence
                   WHERE order_id = ? AND review_status = 'verified'
                   ORDER BY reviewed_at DESC LIMIT 1""",
                (order_id,),
            ).fetchone()
            amount = int(order["amount_minor"])
            if str(payment["provider"] or "").lower() != "wallet" and verified_evidence is not None:
                amount = max(amount, int(verified_evidence["verified_amount_minor"] or amount))
            currency = str(order["currency"]).upper()
            now_text = refunded_at
            connection.execute(
                """INSERT INTO wallets (telegram_id, currency, balance_minor, created_at, updated_at)
                   VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
                (order["telegram_id"], currency, now_text, now_text),
            )
            wallet = connection.execute(
                "SELECT currency FROM wallets WHERE telegram_id = ?",
                (order["telegram_id"],),
            ).fetchone()
            if wallet is None or str(wallet["currency"]).upper() != currency:
                raise CommerceError("Wallet currency does not match the order")
            idem = f"reversal:{order_id}"
            existing_reversal = connection.execute(
                "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (idem,)
            ).fetchone()
            if existing_reversal is None:
                connection.execute(
                    "UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ? WHERE telegram_id = ?",
                    (amount, now_text, order["telegram_id"]),
                )
                connection.execute(
                    """INSERT INTO wallet_ledger
                       (id, telegram_id, kind, amount_minor, currency, reference_type,
                        reference_id, idempotency_key, metadata_json, created_at)
                       VALUES (?, ?, 'reversal', ?, ?, 'order', ?, ?, ?, ?)""",
                    (
                        _new_id(), order["telegram_id"], amount, currency, order_id,
                        idem, json.dumps({"reason": (reason or "")[:500], "admin_id": admin_id}, sort_keys=True),
                        now_text,
                    ),
                )
            connection.execute(
                "UPDATE payments SET status = 'refunded' WHERE order_id = ? AND status IN ('verified', 'submitted')",
                (order_id,),
            )
            final_order_status = str(order["status"])
            if final_order_status != "approved":
                final_order_status = "rejected"
            connection.execute(
                "UPDATE orders SET status = ?, refund_status = 'refunded', rejected_at = COALESCE(rejected_at, ?) WHERE id = ?",
                (final_order_status, now_text, order_id),
            )
            subscription = connection.execute(
                "SELECT id, status FROM subscriptions WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if subscription is not None:
                if subscription["status"] == "active":
                    connection.execute(
                        "UPDATE subscriptions SET status = 'revoked' WHERE id = ?",
                        (subscription["id"],),
                    )
                elif subscription["status"] == "pending":
                    connection.execute(
                        "UPDATE subscriptions SET status = 'cancelled' WHERE id = ?",
                        (subscription["id"],),
                    )
                connection.execute(
                    """INSERT INTO provisioning_jobs
                       (id, subscription_id, operation, status, next_attempt_at, created_at)
                       VALUES (?, ?, 'revoke', 'pending', ?, ?)
                       ON CONFLICT(subscription_id, operation) DO NOTHING""",
                    (_new_id(), subscription["id"], now_text, now_text),
                )
            connection.execute(
                """INSERT INTO notifications
                   (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
                   VALUES (?, ?, ?, 'payment_refunded', ?, 'pending', ?, ?)
                   ON CONFLICT(dedupe_key) DO NOTHING""",
                (
                    _new_id(), f"payment-refund-recorded:{order_id}", order["telegram_id"],
                    f"Your AuriX order was refunded with a {amount:,} {currency} wallet credit. Reason: {(reason or 'admin refund')[:300]}",
                    now_text, now_text,
                ),
            )
            self._audit(
                connection,
                "order_refunded",
                "order",
                order_id,
                "admin",
                str(admin_id),
                {"amount_minor": amount, "currency": currency, "reason": (reason or "")[:500]},
            )
        return "refunded"

    def _claim_job(self, operation: str, now: datetime) -> dict[str, Any] | None:
        now_text = _now_text(now)
        stale_before = _now_text(now - timedelta(minutes=5))
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE provisioning_jobs SET status = 'pending', locked_at = NULL
                   WHERE status = 'running' AND locked_at < ?""",
                (stale_before,),
            )
            lock_clause = " FOR UPDATE SKIP LOCKED" if isinstance(connection, _PostgresConnection) else ""
            row = connection.execute(
                """SELECT * FROM provisioning_jobs
                   WHERE operation = ? AND status = 'pending' AND next_attempt_at <= ?
                   ORDER BY created_at LIMIT 1""" + lock_clause,
                (operation, now_text),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """UPDATE provisioning_jobs
                   SET status = 'running', attempts = attempts + 1, locked_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (now_text, row["id"]),
            )
            result = dict(row)
            result["attempts"] = row["attempts"] + 1
            return result

    def _job_done(self, job_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE provisioning_jobs SET status = 'done', locked_at = NULL, last_error = NULL
                   WHERE id = ?""",
                (job_id,),
            )

    def _job_failed(self, job_id: str, error: Exception, now: datetime) -> None:
        safe_error = f"{type(error).__name__}: {str(error)[:500]}"
        next_attempt = _now_text(now + JOB_RETRY_DELAY)
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE provisioning_jobs
                   SET status = CASE WHEN attempts >= 8 THEN 'failed' ELSE 'pending' END,
                       next_attempt_at = ?, locked_at = NULL, last_error = ?
                   WHERE id = ?""",
                (next_attempt, safe_error, job_id),
            )

    def failed_jobs(self, limit: int = 20, include_nonterminal: bool = False) -> list[dict[str, Any]]:
        """Return worker operations needing attention.

        The default remains terminal-only for API compatibility; operators can
        request pending/running retries so a silent revoke failure is visible
        before the eighth attempt.
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT j.id AS job_id, j.operation, j.attempts, j.last_error,
                          j.next_attempt_at, j.status AS job_status, s.order_id, s.telegram_id,
                          s.plan_code, s.status AS subscription_status
                   FROM provisioning_jobs j
                   JOIN subscriptions s ON s.id = j.subscription_id
                   WHERE j.status = 'failed' OR (? = 1 AND j.status IN ('pending', 'running'))
                   ORDER BY j.created_at LIMIT ?""",
                (1 if include_nonterminal else 0, max(1, min(limit, 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def retry_job(self, job_id: str, admin_id: int, now: datetime | None = None) -> str:
        """Requeue one exact failed job (avoids ambiguous order-level retries)."""
        current = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                "SELECT id, operation, subscription_id FROM provisioning_jobs WHERE id = ? AND status = 'failed'",
                (job_id,),
            ).fetchone()
            if row is None:
                raise CommerceError("No terminal worker failure exists for that job")
            connection.execute(
                """UPDATE provisioning_jobs SET status = 'pending', attempts = 0,
                          next_attempt_at = ?, locked_at = NULL, last_error = NULL
                   WHERE id = ? AND status = 'failed'""",
                (current, job_id),
            )
            self._audit(connection, "job_retried", "provisioning_job", job_id,
                        "admin", str(admin_id), {"operation": row["operation"]})
        return str(row["operation"])

    def retry_failed_job(
        self, order_id: str, admin_id: int, now: datetime | None = None,
        operation: str | None = None,
    ) -> str:
        """Requeue one terminal job after an operator has reviewed its error."""
        current = _now_text(now)
        if operation not in (None, "provision", "revoke"):
            raise CommerceError("Unknown worker operation")
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                """SELECT j.id, j.operation, s.id AS subscription_id
                   FROM provisioning_jobs j JOIN subscriptions s
                     ON s.id = j.subscription_id
                   WHERE s.order_id = ? AND j.status = 'failed'
                     AND (? IS NULL OR j.operation = ?)
                   ORDER BY j.created_at DESC LIMIT 1""",
                (order_id, operation, operation),
            ).fetchone()
            if row is None:
                raise CommerceError("No terminal worker failure exists for this order")
            connection.execute(
                """UPDATE provisioning_jobs
                   SET status = 'pending', attempts = 0, next_attempt_at = ?,
                       locked_at = NULL, last_error = NULL
                   WHERE id = ? AND status = 'failed'""",
                (current, row["id"]),
            )
            self._audit(
                connection,
                "job_retried",
                "subscription",
                str(row["subscription_id"]),
                "admin",
                str(admin_id),
                {"operation": row["operation"], "order_id": order_id},
            )
        return str(row["operation"])

    def _find_key(self, name: str) -> dict[str, Any] | None:
        result = self.outline.list_keys()
        if isinstance(result, dict):
            keys = result.get("accessKeys", [])
        else:
            keys = result if isinstance(result, list) else []
        if not isinstance(keys, list):
            raise CommerceError("Outline key inventory has an invalid shape")
        matches = [
            key for key in keys if isinstance(key, dict) and key.get("name") == name
        ]
        if len(matches) > 1:
            raise CommerceError("Outline has multiple keys for one subscription")
        return matches[0] if matches else None

    def _revoke_legacy_free_keys(
        self, telegram_id: int, keep_key_id: str, username: str | None = None
    ) -> None:
        """Remove old free/trial keys when a paid entitlement becomes active."""
        try:
            result = self.outline.list_keys()
        except Exception:
            return
        keys = result.get("accessKeys", []) if isinstance(result, dict) else []
        prefixes = {f"tg-{telegram_id}-", f"{telegram_id}-"}
        if username:
            safe_username = re.sub(
                r"[^A-Za-z0-9_-]+", "-", str(username).lstrip("@")
            ).strip("-_")[:48]
            if safe_username:
                prefixes.add(f"{safe_username}-")
        for item in keys if isinstance(keys, list) else []:
            if not isinstance(item, dict) or str(item.get("id")) == str(keep_key_id):
                continue
            name = str(item.get("name", ""))
            is_new_free = any(name.startswith(prefix) for prefix in prefixes) and (
                "-FREE200MB-" in name
                or "-FREE300MB-" in name
                or "-TRIAL3GB-" in name
                or "-FREE3GB-" in name
            )
            is_legacy_free = name.startswith(f"tg-{telegram_id}-")
            if is_new_free or is_legacy_free:
                try:
                    self.outline.delete_key(str(item["id"]))
                    with self.database.connect() as connection:
                        self.database.begin_write(connection)
                        local = connection.execute(
                            "SELECT id, telegram_id, data_limit_bytes, expires_at FROM keys WHERE outline_key_id = ?",
                            (str(item["id"]),),
                        ).fetchone()
                        if local is not None:
                            connection.execute("UPDATE keys SET status = 'revoked' WHERE id = ?", (local["id"],))
                            connection.execute(
                                """INSERT INTO key_termination_events
                                   (key_id, telegram_id, outline_key_id, reason, quota_bytes,
                                    expires_at, detected_at, remote_state, delete_attempts,
                                    deletion_verified_at)
                                   VALUES (?, ?, ?, 'paid_upgrade_cleanup', ?, ?, ?, 'delete_accepted', 1, ?)
                                   ON CONFLICT(key_id, reason) DO UPDATE SET
                                      remote_state = excluded.remote_state,
                                      delete_attempts = key_termination_events.delete_attempts + 1,
                                      deletion_verified_at = excluded.deletion_verified_at""",
                                (local["id"], local["telegram_id"], str(item["id"]), local["data_limit_bytes"],
                                 local["expires_at"], _now_text(), _now_text()),
                            )
                except Exception as exc:
                    with self.database.connect() as connection:
                        self.database.begin_write(connection)
                        local = connection.execute(
                            "SELECT id, telegram_id, data_limit_bytes, expires_at FROM keys WHERE outline_key_id = ?",
                            (str(item.get("id")),),
                        ).fetchone()
                        if local is not None:
                            connection.execute(
                                """INSERT INTO key_termination_events
                                   (key_id, telegram_id, outline_key_id, reason, quota_bytes,
                                    expires_at, detected_at, remote_state, delete_attempts, last_error)
                                   VALUES (?, ?, ?, 'paid_upgrade_cleanup', ?, ?, ?, 'retrying', 1, ?)
                                   ON CONFLICT(key_id, reason) DO UPDATE SET
                                      remote_state = 'retrying', delete_attempts = key_termination_events.delete_attempts + 1,
                                      last_error = excluded.last_error""",
                                (local["id"], local["telegram_id"], str(item.get("id")), local["data_limit_bytes"],
                                 local["expires_at"], _now_text(), type(exc).__name__[:128]),
                            )

    def _provision(self, job: dict[str, Any], now: datetime) -> None:
        with self.database.connect() as connection:
            subscription = connection.execute(
                """SELECT s.*, p.quota_bytes AS catalog_quota_bytes,
                          p.name AS catalog_plan_name, u.username
                   FROM subscriptions s JOIN plans p ON p.code = s.plan_code
                   JOIN users u ON u.telegram_id = s.telegram_id
                   WHERE s.id = ?""",
                (job["subscription_id"],),
            ).fetchone()
            existing = connection.execute(
                "SELECT * FROM paid_vpn_keys WHERE subscription_id = ?",
                (job["subscription_id"],),
            ).fetchone()
        if subscription is None:
            self._job_done(job["id"])
            return
        desired_quota = subscription["quota_bytes"] if subscription["quota_bytes"] is not None else subscription["catalog_quota_bytes"]
        desired_plan_name = subscription["plan_name"] or subscription["catalog_plan_name"]
        current_dt = (now or datetime.now(UTC)).astimezone(UTC)
        starts_dt = datetime.fromisoformat(subscription["starts_at"])
        expires_dt = datetime.fromisoformat(subscription["expires_at"])
        if current_dt < starts_dt:
            with self.database.connect() as connection:
                connection.execute(
                    """UPDATE provisioning_jobs SET status = 'pending', next_attempt_at = ?, locked_at = NULL
                       WHERE id = ?""",
                    (subscription["starts_at"], job["id"]),
                )
            return
        if subscription["status"] not in ("pending", "active"):
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                connection.execute(
                    "UPDATE subscriptions SET status = 'expired' WHERE id = ? AND status = 'pending'",
                    (subscription["id"],),
                )
                connection.execute(
                    "UPDATE provisioning_jobs SET status = 'done', locked_at = NULL, last_error = 'expired before provision' WHERE id = ?",
                    (job["id"],),
                )
            return
        # Pending entitlements have no expiry clock yet.  Their planned
        # boundary is only a scheduling hint; paid time starts at successful
        # activation below.  Already-active legacy rows retain their stored
        # expiry and are still protected from late provisioning retries.
        if subscription["status"] == "active" and current_dt >= expires_dt:
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                connection.execute(
                    "UPDATE subscriptions SET status = 'expired' WHERE id = ? AND status = 'active'",
                    (subscription["id"],),
                )
                connection.execute(
                    "UPDATE provisioning_jobs SET status = 'done', locked_at = NULL, last_error = 'expired before provision' WHERE id = ?",
                    (job["id"],),
                )
            return
        if existing is not None:
            self._job_done(job["id"])
            return
        key_name = _paid_outline_key_name(subscription)
        key = None
        deterministic_id = f"aurix-{subscription['id']}"
        getter = getattr(self.outline, "get_key", None)
        if callable(getter):
            try:
                key = getter(deterministic_id)
            except Exception:
                key = None
        if key is None:
            key = self._find_key(key_name)
        if key is None:
            legacy_key = self._find_key(f"aurix-sub-{subscription['id']}")
            if legacy_key is not None:
                key = legacy_key
                rename = getattr(self.outline, "rename_key", None)
                if callable(rename):
                    rename(str(key["id"]), key_name)
        created_remote = False
        if key is None:
            deterministic_create = getattr(self.outline, "create_key_with_id", None)
            if callable(deterministic_create):
                try:
                    key = deterministic_create(deterministic_id, key_name, desired_quota)
                except Exception as exc:
                    # A timeout may have created the remote key.  Re-read the
                    # exact id.  Only an explicit unsupported-endpoint status
                    # may fall back to POST; retrying an ambiguous timeout with
                    # POST could create a second billable remote credential.
                    recovered = None
                    if callable(getter):
                        try:
                            recovered = getter(deterministic_id)
                        except Exception:
                            recovered = None
                    if recovered is not None:
                        key = recovered
                    elif getattr(exc, "status", None) in (404, 405, 501):
                        key = self.outline.create_key(key_name, desired_quota)
                    else:
                        raise
            else:
                key = self.outline.create_key(key_name, desired_quota)
            created_remote = True
        try:
            if not isinstance(key, dict) or not key.get("id") or not key.get("accessUrl"):
                raise CommerceError("Outline key response lacks id or accessUrl")
            if desired_quota is not None:
                self.outline.set_data_limit(str(key["id"]), desired_quota)
            created_at = _now_text(now)
            activated_at = current_dt.isoformat()
            activated_expires_at = (
                current_dt + timedelta(days=int(subscription["duration_days"] or 0))
            ).isoformat()
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                connection.execute(
                    """INSERT INTO paid_vpn_keys
                       (id, subscription_id, telegram_id, outline_key_id, access_url,
                        quota_bytes, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'active', ?)""",
                    (
                        _new_id(),
                        subscription["id"],
                        subscription["telegram_id"],
                        str(key["id"]),
                        self._encrypt_access_url(str(key["accessUrl"])),
                        desired_quota,
                        created_at,
                    ),
                )
                connection.execute(
                    """UPDATE subscriptions
                       SET status = 'active', activated_at = ?, starts_at = ?, expires_at = ?
                       WHERE id = ?""",
                    (activated_at, activated_at, activated_expires_at, subscription["id"]),
                )
                connection.execute(
                    """INSERT INTO notifications
                       (id, dedupe_key, telegram_id, kind, text, access_url_ciphertext,
                        status, next_attempt_at, created_at)
                       VALUES (?, ?, ?, 'vpn_ready', ?, ?, 'pending', ?, ?)
                       ON CONFLICT(dedupe_key) DO NOTHING""",
                    (
                        _new_id(),
                        f"vpn-ready:{subscription['id']}",
                        subscription["telegram_id"],
                        f"Your {desired_plan_name} AuriX VPN is ready.\n\nExpires: {activated_expires_at}",
                        self._encrypt_access_url(str(key["accessUrl"])),
                        created_at,
                        created_at,
                    ),
                )
                self._audit(
                    connection,
                    "key_provisioned",
                    "subscription",
                    subscription["id"],
                    "system",
                    None,
                    {"outline_key_id": str(key["id"]), "activated_at": activated_at},
                )
                connection.execute(
                    """UPDATE provisioning_jobs SET status = 'done', locked_at = NULL, last_error = NULL
                       WHERE id = ?""",
                    (job["id"],),
                )
            # A paid account supersedes any free/trial key.  This is best-effort
            # cleanup; the paid key remains authoritative and the next startup
            # reconciliation can retry removal if the inventory call failed.
            self._revoke_legacy_free_keys(
                subscription["telegram_id"], str(key["id"]), subscription["username"]
            )
        except Exception:
            if created_remote and isinstance(key, dict) and key.get("id"):
                try:
                    self.outline.delete_key(str(key["id"]))
                except Exception:
                    pass
            raise

    def _expire(self, now: datetime) -> int:
        now_text = _now_text(now)
        count = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            rows = connection.execute(
                """SELECT id FROM subscriptions
                   WHERE status = 'active' AND expires_at <= ?""",
                (now_text,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE subscriptions SET status = 'expired' WHERE id = ?",
                    (row["id"],),
                )
                self._audit(
                    connection,
                    "subscription_expired",
                    "subscription",
                    row["id"],
                    "system",
                    None,
                    {"detected_at": now_text},
                )
                connection.execute(
                    """INSERT INTO provisioning_jobs
                       (id, subscription_id, operation, status, next_attempt_at, created_at)
                       VALUES (?, ?, 'revoke', 'pending', ?, ?)
                       ON CONFLICT(subscription_id, operation) DO NOTHING""",
                    (_new_id(), row["id"], now_text, now_text),
                )
                count += 1
        return count

    def _revoke(self, job: dict[str, Any], now: datetime) -> None:
        with self.database.connect() as connection:
            key = connection.execute(
                """SELECT k.*, s.status AS subscription_status,
                          o.id AS order_id, o.refund_status
                   FROM paid_vpn_keys k
                   JOIN subscriptions s ON s.id = k.subscription_id
                   JOIN orders o ON o.id = s.order_id
                   WHERE k.subscription_id = ?""",
                (job["subscription_id"],),
            ).fetchone()
        if key is None or key["status"] == "revoked":
            self._job_done(job["id"])
            return
        try:
            self.outline.delete_key(key["outline_key_id"])
            getter = getattr(self.outline, "get_key", None)
            remote_state = "delete_accepted"
            if callable(getter):
                if getter(str(key["outline_key_id"])) is not None:
                    raise CommerceError("Outline key still exists after delete")
                remote_state = "deleted_verified"
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                connection.execute(
                    """UPDATE paid_vpn_keys SET status = 'revoked', revoked_at = ?
                       WHERE id = ?""",
                    (_now_text(now), key["id"]),
                )
                connection.execute(
                    "UPDATE provisioning_jobs SET status = 'done', locked_at = NULL WHERE id = ?",
                    (job["id"],),
                )
                quota_reason = key["quota_reason"] if "quota_reason" in key.keys() else None
                quota_event = connection.execute(
                    """SELECT observed_bytes, quota_bytes, observed_at FROM quota_events
                       WHERE subscription_id = ? AND reason = 'quota'""",
                    (job["subscription_id"],),
                ).fetchone()
                if key["refund_status"] == "refunded":
                    notice = "Your AuriX order was refunded to your wallet and its VPN access was terminated."
                    notice_kind = "payment_refunded"
                elif quota_reason == "quota":
                    usage = (
                        f" Observed usage: {int(quota_event['observed_bytes']):,} / "
                        f"{int(quota_event['quota_bytes']):,} bytes."
                        if quota_event is not None else ""
                    )
                    notice = (
                        "Your AuriX VPN key reached its data limit and was terminated."
                        + usage + " Renew to receive a new key."
                    )
                    notice_kind = "vpn_quota"
                else:
                    notice = "Your AuriX VPN subscription expired and its key was terminated. Renew to restore access."
                    notice_kind = "vpn_expired"
                if remote_state == "deleted_verified":
                    notice += " Outline confirmed the credential is deleted."
                connection.execute(
                    """INSERT INTO notifications
                       (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
                       VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                       ON CONFLICT(dedupe_key) DO NOTHING""",
                    (
                        _new_id(),
                        (
                            f"access-revoked:{key['order_id']}"
                            if key["refund_status"] == "refunded"
                            else f"vpn-{notice_kind}:{job['subscription_id']}"
                        ),
                        key["telegram_id"],
                        notice_kind,
                        notice,
                        _now_text(now),
                        _now_text(now),
                    ),
                )
                self._audit(
                    connection,
                    "key_revoked",
                    "subscription",
                    job["subscription_id"],
                    "system",
                    None,
                    {
                        "outline_key_id": key["outline_key_id"],
                        "reason": (
                            "refund" if key["refund_status"] == "refunded"
                            else (quota_reason or "expiry")
                        ),
                        "remote_state": remote_state,
                        "last_usage_bytes": key["last_usage_bytes"],
                        "quota_bytes": key["quota_bytes"],
                    },
                )
        except Exception:
            # Keep the entitlement marked active until the remote delete is
            # actually confirmed. The job status/attempts are the retry state;
            # exposing ``revoke_failed`` as an access state made customers and
            # operators believe a credential had already been revoked.
            raise

    def process_jobs(self, now: datetime | None = None, max_jobs: int = 10) -> int:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        processed = 0
        # Revokes run first so an expired/quota-exhausted key is removed before
        # a scheduled renewal provisions its replacement.
        while processed < max_jobs:
            job = self._claim_job("revoke", current)
            if job is None:
                break
            try:
                self._revoke(job, current)
            except Exception as exc:
                self._job_failed(job["id"], exc, current)
            processed += 1
        while processed < max_jobs:
            job = self._claim_job("provision", current)
            if job is None:
                break
            try:
                self._provision(job, current)
            except Exception as exc:
                self._job_failed(job["id"], exc, current)
            processed += 1
        return processed

    def expire_and_process(self, now: datetime | None = None) -> int:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        self.release_expired_wallet_reservations(current)
        self.expire_open_orders(current)
        self._expire(current)
        return self.process_jobs(current)

    def queue_quota_warnings(
        self,
        now: datetime | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> int:
        """Queue one Telegram warning as each remaining-quota threshold is crossed."""
        if metrics is None:
            try:
                metrics = self.outline.transfer_metrics()
            except Exception:
                return 0
        by_key = metrics.get("bytesTransferredByUserId", {}) if isinstance(metrics, dict) else {}
        if not isinstance(by_key, dict):
            return 0
        current = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = _now_text(current)
        queued = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            rows = connection.execute(
                """SELECT k.id, k.subscription_id, k.telegram_id, k.outline_key_id,
                          k.quota_bytes, k.status, k.quota_warning_percent,
                          s.plan_code, s.expires_at
                   FROM paid_vpn_keys k JOIN subscriptions s ON s.id = k.subscription_id
                   WHERE k.status = 'active' AND s.status = 'active'
                     AND k.quota_bytes IS NOT NULL"""
            ).fetchall()
            for row in rows:
                try:
                    used = max(0, int(by_key.get(str(row["outline_key_id"]), 0) or 0))
                    quota = int(row["quota_bytes"])
                except (TypeError, ValueError):
                    continue
                if quota <= 0 or used >= quota:
                    continue
                remaining = quota - used
                reached = next(
                    (
                        percent
                        for percent, fraction in reversed(QUOTA_WARNING_THRESHOLDS)
                        if remaining <= quota * fraction
                    ),
                    None,
                )
                if reached is None:
                    continue
                previous = row["quota_warning_percent"]
                if previous is not None and int(previous) <= reached:
                    continue
                dedupe_key = f"quota-warning:paid:{row['subscription_id']}:{reached}"
                try:
                    existing = connection.execute(
                        "SELECT id FROM notifications WHERE dedupe_key = ?",
                        (dedupe_key,),
                    ).fetchone()
                    if existing is None:
                        remaining_percent = remaining * 100 / quota
                        text = (
                            f"Quota warning: your AuriX {row['plan_code']} key has "
                            f"{_human_bytes(remaining)} remaining "
                            f"({remaining_percent:.1f}% of {_human_bytes(quota)}).\n"
                            "This is based on Outline's trailing-30-day usage. "
                            "When no quota remains, the key will be blocked and deleted. "
                            f"Expires: {row['expires_at']}"
                        )
                        connection.execute(
                            """INSERT INTO notifications
                               (id, dedupe_key, telegram_id, kind, text, status,
                                next_attempt_at, created_at)
                               VALUES (?, ?, ?, 'quota_warning', ?, 'pending', ?, ?)""",
                            (_new_id(), dedupe_key, row["telegram_id"], text, now_text, now_text),
                        )
                        queued += 1
                    connection.execute(
                        "UPDATE paid_vpn_keys SET quota_warning_percent = ? WHERE id = ?",
                        (reached, row["id"]),
                    )
                except Exception as exc:
                    if self.database.is_integrity_error(exc):
                        continue
                    raise
        return queued

    def enforce_quotas(
        self,
        now: datetime | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> int:
        """Observe Outline transfer metrics and queue one idempotent hard revoke.

        Outline's per-key data limit is the immediate safety brake.  Metrics are
        only an observation; once ``used >= quota`` is seen we fail closed in
        AuriX and delete the known remote key.  Missing/stale metrics never
        restore or disable a key.
        """
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if metrics is None:
            try:
                metrics = self.outline.transfer_metrics()
            except Exception:
                return 0
        by_key = metrics.get("bytesTransferredByUserId", {}) if isinstance(metrics, dict) else {}
        if not isinstance(by_key, dict):
            return 0
        try:
            self.queue_quota_warnings(current, metrics)
        except Exception as exc:
            # A notification outage must never delay the hard quota revoke.
            print(f"paid quota warning error: {type(exc).__name__}", file=sys.stderr)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT k.id, k.subscription_id, k.outline_key_id, k.quota_bytes,
                          k.status, s.status AS subscription_status
                   FROM paid_vpn_keys k JOIN subscriptions s ON s.id = k.subscription_id
                   WHERE k.status = 'active' AND s.status = 'active' AND k.quota_bytes IS NOT NULL"""
            ).fetchall()
        scheduled = 0
        for row in rows:
            try:
                used = int(by_key.get(str(row["outline_key_id"]), 0) or 0)
            except (TypeError, ValueError):
                continue
            quota = int(row["quota_bytes"])
            if used < quota:
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE paid_vpn_keys SET last_usage_bytes = ?, last_usage_observed_at = ? WHERE id = ?",
                        (used, _now_text(current), row["id"]),
                    )
                continue
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                existing = connection.execute(
                    "SELECT id FROM quota_events WHERE subscription_id = ? AND reason = 'quota'",
                    (row["subscription_id"],),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """INSERT INTO quota_events
                           (id, subscription_id, reason, observed_bytes, quota_bytes, observed_at)
                           VALUES (?, ?, 'quota', ?, ?, ?)""",
                        (_new_id(), row["subscription_id"], used, quota, _now_text(current)),
                    )
                    scheduled += 1
                connection.execute(
                    """UPDATE paid_vpn_keys SET status = 'active',
                              last_usage_bytes = ?, last_usage_observed_at = ?, quota_reason = 'quota'
                       WHERE id = ? AND status = 'active'""",
                    (used, _now_text(current), row["id"]),
                )
                connection.execute(
                    "UPDATE subscriptions SET status = 'revoked' WHERE id = ? AND status = 'active'",
                    (row["subscription_id"],),
                )
                connection.execute(
                    """INSERT INTO provisioning_jobs
                       (id, subscription_id, operation, status, next_attempt_at, created_at)
                       VALUES (?, ?, 'revoke', 'pending', ?, ?)
                       ON CONFLICT(subscription_id, operation) DO NOTHING""",
                    (_new_id(), row["subscription_id"], _now_text(current), _now_text(current)),
                )
        return scheduled

    def capacity_snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        """Return operator-only counts and mapped transfer usage, never access URLs."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        expiring_at = _now_text(current + timedelta(hours=24))
        server = self.outline.server_info()
        outline_version = str(server.get("version", "unknown"))[:64]
        with self.database.connect() as connection:
            counts = connection.execute(
                """SELECT
                       (SELECT COUNT(*) FROM subscriptions WHERE status = 'active') AS active_subscriptions,
                       (SELECT COUNT(*) FROM paid_vpn_keys WHERE status = 'active') AS active_keys,
                       (SELECT COUNT(*) FROM provisioning_jobs WHERE status IN ('pending', 'running')) AS pending_jobs,
                       (SELECT COUNT(*) FROM provisioning_jobs WHERE status = 'failed') AS failed_jobs,
                       (SELECT COUNT(*) FROM subscriptions
                          WHERE status = 'active'
                            AND expires_at <= ?) AS expiring_24h""",
                (expiring_at,),
            ).fetchone()
            key_rows = connection.execute(
                """SELECT outline_key_id, telegram_id, quota_bytes
                   FROM paid_vpn_keys WHERE status = 'active'"""
            ).fetchall()
        metrics = self.outline.transfer_metrics()
        by_key = metrics.get("bytesTransferredByUserId", {})
        if not isinstance(by_key, dict):
            by_key = {}
        usage = []
        for row in key_rows:
            raw_used = by_key.get(str(row["outline_key_id"]), 0)
            try:
                used_bytes = max(0, int(raw_used or 0))
            except (TypeError, ValueError):
                used_bytes = 0
            usage.append(
                {
                    "outline_key_id": row["outline_key_id"],
                    "telegram_id": row["telegram_id"],
                    "quota_bytes": row["quota_bytes"],
                    "used_bytes": used_bytes,
                }
            )
        return {**dict(counts), "outline_version": outline_version, "usage": usage}

    def user_usage(
        self, telegram_id: int, usage_by_key: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Return paid key usage belonging to one Telegram user."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT s.plan_code, s.plan_name, s.status AS subscription_status, s.expires_at, s.starts_at,
                          k.outline_key_id, k.quota_bytes, k.status,
                          k.last_usage_bytes, k.quota_reason, k.created_at,
                          (SELECT j.status FROM provisioning_jobs j WHERE j.subscription_id = s.id
                           AND j.operation = 'revoke' LIMIT 1) AS revocation_status
                   FROM subscriptions s
                   JOIN paid_vpn_keys k ON k.subscription_id = s.id
                   WHERE s.telegram_id = ?
                     AND (k.status IN ('active', 'revoke_failed') OR k.quota_reason = 'quota')
                   ORDER BY k.created_at DESC LIMIT 10""",
                (telegram_id,),
            ).fetchall()
        result = []
        for row in rows:
            key_id = str(row["outline_key_id"])
            observed = key_id in usage_by_key
            raw_used = usage_by_key.get(key_id, row["last_usage_bytes"] or 0)
            try:
                used = max(0, int(raw_used or 0))
            except (TypeError, ValueError):
                used = max(0, int(row["last_usage_bytes"] or 0))
                observed = False
            quota = int(row["quota_bytes"] or 0)
            if quota <= 0:
                continue
            result.append(
                {
                    "tier": row["plan_name"] or row["plan_code"],
                    "used_bytes": used,
                    "quota_bytes": quota,
                    "remaining_bytes": max(0, quota - used),
                    "usage_observed": observed,
                    "expires_at": row["expires_at"],
                    "status": "quota exhausted" if row["quota_reason"] == "quota" else ("revocation failed" if row["revocation_status"] == "failed" else ("revocation pending" if row["subscription_status"] != "active" and row["status"] == "active" else ("revocation pending" if row["status"] == "revoke_failed" else row["status"]))),
                    "created_at": row["created_at"],
                }
            )
        return result

    def user_vpns(self, telegram_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """Return all of a user's paid entitlements without exposing secrets.

        A customer may own multiple active keys (for devices or parallel
        plans). Access URLs are decrypted only for active, non-expired keys.
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT s.id AS subscription_id, s.plan_code, s.status, s.expires_at,
                          s.starts_at, k.access_url, k.quota_bytes, k.status AS key_status
                   FROM subscriptions s LEFT JOIN paid_vpn_keys k ON k.subscription_id = s.id
                   WHERE s.telegram_id = ? AND s.status IN ('pending', 'active', 'expired', 'revoked')
                   ORDER BY CASE s.status WHEN 'active' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                            s.starts_at DESC LIMIT ?""",
                (telegram_id, max(1, min(int(limit), 100))),
            ).fetchall()
        now_text = _now_text()
        results = []
        for row in rows:
            result = dict(row)
            result["access_url"] = self._decrypt_access_url(result.get("access_url"))
            if (
                result.get("status") != "active"
                or result.get("key_status") != "active"
                or str(result.get("expires_at") or "") <= now_text
            ):
                result["access_url"] = None
            results.append(result)
        return results

    def user_vpn(self, telegram_id: int) -> dict[str, Any] | None:
        """Backward-compatible latest/most relevant paid entitlement view."""
        subscriptions = self.user_vpns(telegram_id, limit=1)
        return subscriptions[0] if subscriptions else None

    def pending_notifications(self, now: datetime | None = None, limit: int = 20) -> list[dict[str, Any]]:
        now_text = _now_text(now)
        with self.database.connect() as connection:
            rows = connection.execute(
            """SELECT * FROM notifications
                   WHERE status IN ('pending', 'failed')
                     AND dead_lettered_at IS NULL
                     AND next_attempt_at <= ?
                   ORDER BY created_at LIMIT ?""",
                (now_text, max(1, min(limit, 100))),
            ).fetchall()
        notifications = []
        for row in rows:
            notification = dict(row)
            access_url = self._decrypt_access_url(notification.get("access_url_ciphertext"))
            if access_url:
                notification["text"] += f"\n\nYour Outline key:\n{access_url}"
            elif notification.get("access_url_ciphertext"):
                notification["secret_unavailable"] = True
            notifications.append(notification)
        return notifications

    def mark_notification_sent(self, notification_id: str, now: datetime | None = None) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE notifications SET status = 'sent', sent_at = ? WHERE id = ?""",
                (_now_text(now), notification_id),
            )

    def mark_notification_failed(self, notification_id: str, now: datetime | None = None) -> None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE notifications
                   SET status = 'failed', attempts = attempts + 1,
                       dead_lettered_at = CASE WHEN attempts + 1 >= 8 THEN ? ELSE dead_lettered_at END,
                       next_attempt_at = CASE WHEN attempts + 1 >= 8 THEN '9999-12-31T00:00:00+00:00' ELSE ? END
                   WHERE id = ?""",
                (_now_text(current), _now_text(current + NOTIFICATION_RETRY_DELAY), notification_id),
            )
