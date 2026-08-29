#!/usr/bin/env python3
"""AuriX Telegram VPN commerce bot with free, trial, and paid entitlements."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import signal
import sqlite3
import ssl
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from commerce import (
    CommerceDatabase,
    CommerceError,
    CommerceService,
    PostgresCommerceDatabase,
)
from receipt_llm import OpenAICompatibleReceiptExtractor, ReceiptExtractionError, ReceiptLLMUnavailable
from supabase_storage import NullReceiptStorage, SupabaseReceiptStorage

UTC = timezone.utc
LEGACY_LIMIT_BYTES = 100 * 1024 * 1024
PUBLIC_LIMIT_BYTES = 300 * 1024 * 1024
LIMIT_BYTES = PUBLIC_LIMIT_BYTES
TRIAL_LIMIT_BYTES = 3 * 1024**3
CLAIM_PERIOD = timedelta(hours=24)
TRIAL_PERIOD = timedelta(days=30)
DEFAULT_MAINTENANCE_INTERVAL_SECONDS = 60.0
ADMIN_CONFIRMATION_TTL = timedelta(minutes=5)
# Warn once as the observed trailing-30-day allowance crosses these remaining
# percentages. Outline itself enforces the hard limit; these messages make the
# approaching cutoff visible before the key is removed.
QUOTA_WARNING_THRESHOLDS = ((25, 0.25), (10, 0.10), (5, 0.05))


def _latency_log(event: str, started_at: float, **fields: Any) -> None:
    """Emit bounded timing evidence without logging secrets or request payloads."""
    if os.environ.get("AURIX_LATENCY_LOG", "0").lower() not in {"1", "true", "yes", "on"}:
        return
    duration_ms = (time.perf_counter() - started_at) * 1000
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    suffix = f" {details}" if details else ""
    print(f"latency event={event} duration_ms={duration_ms:.1f}{suffix}", file=sys.stderr)


def _outline_key_name(
    telegram_id: int,
    username: str | None,
    tier: str,
    duration: str,
    started_at: datetime,
) -> str:
    """Build a human-readable, non-secret Outline key name."""
    identity = (username or "").strip().lstrip("@") or str(telegram_id)
    identity = re.sub(r"[^A-Za-z0-9_-]+", "-", identity).strip("-_")
    identity = identity[:48] or str(telegram_id)
    timestamp = started_at.astimezone(UTC).strftime("%Y%m%d%H%M")
    return f"{identity}-{tier}-{duration}-{timestamp}"[:128]


def _human_bytes(value: int) -> str:
    amount = float(max(0, int(value)))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1024


def _new_id() -> str:
    return uuid.uuid4().hex


class OutlineError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ClaimResult:
    access_url: str | None = None
    expires_at: datetime | None = None
    next_claim_at: datetime | None = None


class AdminOperations:
    """Authorization boundary for privileged commerce operations.

    Telegram remains the presentation/transport layer; all admin commerce
    calls made by it pass through this object so a future admin transport can
    reuse the same allowlist check instead of trusting a UI decision.
    """

    COMMERCE_OPERATIONS = frozenset({
        "list_pending_orders", "list_pending_receipts", "get_receipt", "order_detail",
        "verify_receipt", "reject_receipt", "approve_order", "reject_order",
        "refund_order", "retry_failed_job", "retry_job", "failed_jobs",
        "consistency_report", "capacity_snapshot", "wallet_balance",
        "wallet_history",
    })
    SERVICE_OPERATIONS = frozenset({"termination_summary", "pending_termination_notices"})

    def __init__(
        self,
        commerce: CommerceService | None,
        admin_ids: set[int],
        service: Any | None = None,
    ):
        self.commerce = commerce
        self.admin_ids = admin_ids
        self.service = service

    def require_admin(self, telegram_id: int) -> None:
        if int(telegram_id) not in self.admin_ids:
            raise PermissionError("administrator access required")

    def call(self, telegram_id: int, operation: str, *args: Any, **kwargs: Any) -> Any:
        self.require_admin(telegram_id)
        if self.commerce is None:
            raise CommerceError("Commerce is not configured.")
        if operation not in self.COMMERCE_OPERATIONS:
            raise CommerceError("That administrator operation is unavailable.")
        method = getattr(self.commerce, operation, None)
        if not callable(method):
            raise CommerceError("That administrator operation is unavailable.")
        return method(*args, **kwargs)

    def call_service(
        self, telegram_id: int, operation: str, *args: Any, **kwargs: Any
    ) -> Any:
        self.require_admin(telegram_id)
        if self.service is None:
            raise CommerceError("Service is not configured.")
        if operation not in self.SERVICE_OPERATIONS:
            raise CommerceError("That administrator operation is unavailable.")
        method = getattr(self.service, operation, None)
        if not callable(method):
            raise CommerceError("That administrator operation is unavailable.")
        return method(*args, **kwargs)


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def begin_write(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

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
                    trial_claimed_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    outline_key_id TEXT NOT NULL UNIQUE,
                    key_type TEXT NOT NULL DEFAULT 'daily_free'
                        CHECK (key_type IN ('daily_free', 'monthly_trial', 'paid')),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    data_limit_bytes INTEGER NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('active', 'revoked', 'revoke_failed')),
                    quota_warning_percent INTEGER
                );
                CREATE INDEX IF NOT EXISTS keys_expiry
                    ON keys(status, expires_at);
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
                    update_id INTEGER PRIMARY KEY,
                    received_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS key_termination_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_id INTEGER NOT NULL REFERENCES keys(id),
                    telegram_id INTEGER NOT NULL,
                    outline_key_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    used_bytes INTEGER,
                    quota_bytes INTEGER NOT NULL,
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
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    telegram_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    access_url_ciphertext TEXT,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'sent', 'failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    dead_lettered_at TEXT
                );
                CREATE INDEX IF NOT EXISTS notifications_due
                    ON notifications(status, next_attempt_at);
                CREATE TABLE IF NOT EXISTS telegram_command_scopes (
                    chat_id INTEGER PRIMARY KEY,
                    configured_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_action_challenges (
                    token_hash TEXT PRIMARY KEY,
                    admin_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
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
                """
            )
            user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
            if "trial_claimed_at" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN trial_claimed_at TEXT")
            if "username" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN username TEXT")
            key_columns = {row[1] for row in connection.execute("PRAGMA table_info(keys)")}
            if "key_type" not in key_columns:
                connection.execute(
                    "ALTER TABLE keys ADD COLUMN key_type TEXT NOT NULL DEFAULT 'daily_free'"
                )
            connection.execute(
                """UPDATE keys SET key_type = 'monthly_trial'
                   WHERE key_type = 'daily_free' AND data_limit_bytes >= ?""",
                (TRIAL_LIMIT_BYTES,),
            )
            if "last_usage_bytes" not in key_columns:
                connection.execute("ALTER TABLE keys ADD COLUMN last_usage_bytes INTEGER")
            if "quota_reason" not in key_columns:
                connection.execute("ALTER TABLE keys ADD COLUMN quota_reason TEXT")
            if "quota_warning_percent" not in key_columns:
                connection.execute("ALTER TABLE keys ADD COLUMN quota_warning_percent INTEGER")

    def maintenance_heartbeat(
        self,
        *,
        started_at: str | None = None,
        completed_at: str | None = None,
        success_at: str | None = None,
        stage: str | None = None,
        error: str | None = None,
    ) -> None:
        """Persist the latest housekeeping lifecycle for health checks."""
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO maintenance_heartbeat
                   (id, last_started_at, last_completed_at, last_success_at,
                    last_stage, last_error, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     last_started_at = COALESCE(excluded.last_started_at, maintenance_heartbeat.last_started_at),
                     last_completed_at = COALESCE(excluded.last_completed_at, maintenance_heartbeat.last_completed_at),
                     last_success_at = COALESCE(excluded.last_success_at, maintenance_heartbeat.last_success_at),
                     last_stage = excluded.last_stage,
                     last_error = excluded.last_error,
                     updated_at = excluded.updated_at""",
                (started_at, completed_at, success_at, stage, error, now),
            )

    def get_maintenance_heartbeat(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM maintenance_heartbeat WHERE id = 1"
            ).fetchone()
        return dict(row) if row is not None else None

    def mark_update_seen(self, update_id: int) -> bool:
        """Durably dedupe Telegram updates across restarts."""
        with self.connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO telegram_updates (update_id, received_at) VALUES (?, ?)",
                    (int(update_id), datetime.now(UTC).isoformat()),
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
        return {int(row[0]) for row in rows}

    def record_command_scope(self, chat_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO telegram_command_scopes (chat_id, configured_at)
                   VALUES (?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET configured_at = excluded.configured_at""",
                (int(chat_id), datetime.now(UTC).isoformat()),
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
            self.begin_write(connection)
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

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        return isinstance(error, sqlite3.IntegrityError)


class OutlineClient:
    def __init__(self, api_url: str, cert_sha256: str):
        parsed = urllib.parse.urlsplit(api_url.rstrip("/"))
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("OUTLINE_API_URL must be an https URL")
        fingerprint = cert_sha256.lower().replace(":", "")
        if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ValueError("OUTLINE_CERT_SHA256 must be 64 hexadecimal characters")
        self.host = parsed.hostname
        self.port = parsed.port or 443
        self.base_path = parsed.path.rstrip("/")
        self.fingerprint = fingerprint

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        accepted_statuses: tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        # Outline commonly uses a self-signed certificate. Trust only exact pinned cert.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        started_at = time.perf_counter()
        connection = http.client.HTTPSConnection(self.host, self.port, context=context, timeout=15)
        payload = json.dumps(body).encode() if body is not None else None
        response_status: int | None = None
        try:
            connection.connect()
            certificate = connection.sock.getpeercert(binary_form=True)
            actual = hashlib.sha256(certificate).hexdigest()
            if not hmac.compare_digest(actual, self.fingerprint):
                raise OutlineError("Outline TLS certificate fingerprint mismatch")
            connection.request(
                method,
                self.base_path + path,
                body=payload,
                headers={"Content-Type": "application/json"} if payload else {},
            )
            response = connection.getresponse()
            response_status = response.status
            raw = response.read()
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise OutlineError(f"Outline request failed: {exc}") from exc
        finally:
            connection.close()
            _latency_log(
                "outline_request",
                started_at,
                method=method,
                resource=path.split("/", 2)[1] if path.startswith("/") and "/" in path[1:] else path,
                status=response_status or "error",
            )
        if response.status not in accepted_statuses:
            raise OutlineError(
                f"Outline returned HTTP {response.status}", status=response.status
            )
        if response.status == 404:
            return None
        return json.loads(raw) if raw else None

    def server_info(self) -> dict[str, Any]:
        result = self._request("GET", "/server")
        if not isinstance(result, dict):
            raise OutlineError("Outline server response is not an object")
        return result

    def transfer_metrics(self) -> dict[str, Any]:
        result = self._request("GET", "/metrics/transfer")
        if not isinstance(result, dict):
            raise OutlineError("Outline transfer metrics response is not an object")
        return result

    def create_key(self, name: str, limit_bytes: int | None) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name}
        if limit_bytes is not None:
            body["limit"] = {"bytes": limit_bytes}
        result = self._request("POST", "/access-keys", body)
        if not isinstance(result, dict) or not result.get("id") or not result.get("accessUrl"):
            raise OutlineError("Outline create response lacks id or accessUrl")
        return result

    def list_keys(self) -> dict[str, Any]:
        result = self._request("GET", "/access-keys")
        if not isinstance(result, dict) or not isinstance(result.get("accessKeys"), list):
            raise OutlineError("Outline list response lacks accessKeys")
        return result

    def get_key(self, key_id: str) -> dict[str, Any] | None:
        result = self._request(
            "GET",
            f"/access-keys/{urllib.parse.quote(key_id, safe='')}",
            accepted_statuses=(200, 404),
        )
        if result is None:
            return None
        if not isinstance(result, dict) or not result.get("id"):
            raise OutlineError("Outline key response lacks id")
        return result

    def set_data_limit(self, key_id: str, limit_bytes: int) -> None:
        self._request(
            "PUT",
            f"/access-keys/{urllib.parse.quote(key_id, safe='')}/data-limit",
            {"limit": {"bytes": limit_bytes}},
        )

    def delete_key(self, key_id: str) -> None:
        # A 404 is converged state when revoking a key already known to AuriX.
        self._request(
            "DELETE",
            f"/access-keys/{urllib.parse.quote(key_id, safe='')}",
            accepted_statuses=(200, 204, 404),
        )

    def create_key_with_id(self, key_id: str, name: str, limit_bytes: int | None) -> dict[str, Any]:
        """Create a caller-selected key where the deployed API supports it."""
        body: dict[str, Any] = {"name": name}
        if limit_bytes is not None:
            body["limit"] = {"bytes": limit_bytes}
        result = self._request(
            "PUT", f"/access-keys/{urllib.parse.quote(key_id, safe='')}", body
        )
        if not isinstance(result, dict) or not result.get("id") or not result.get("accessUrl"):
            raise OutlineError("Outline deterministic create response lacks id or accessUrl")
        return result

    def delete_data_limit(self, key_id: str) -> None:
        self._request(
            "DELETE",
            f"/access-keys/{urllib.parse.quote(key_id, safe='')}/data-limit",
            accepted_statuses=(200, 204, 404),
        )

    def rename_key(self, key_id: str, name: str) -> None:
        self._request(
            "PUT",
            f"/access-keys/{urllib.parse.quote(key_id, safe='')}/name",
            {"name": name[:128]},
        )


class ClaimService:
    def __init__(
        self,
        database: Database,
        outline: Any,
        limit_bytes: int = LIMIT_BYTES,
        trial_limit_bytes: int = TRIAL_LIMIT_BYTES,
    ):
        self.database = database
        self.outline = outline
        self.limit_bytes = int(limit_bytes)
        self.trial_limit_bytes = int(trial_limit_bytes)

    def track_user(
        self,
        telegram_id: int,
        first_name: str,
        now: datetime | None = None,
        username: str | None = None,
    ) -> None:
        now_text = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO users (telegram_id, first_name, username, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                       first_name = excluded.first_name,
                       username = excluded.username""",
                (telegram_id, first_name[:128], (username or "")[:64] or None, now_text),
            )

    def claim(
        self,
        telegram_id: int,
        first_name: str,
        now: datetime | None = None,
        username: str | None = None,
    ) -> ClaimResult:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = now.isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """INSERT INTO users (telegram_id, first_name, username, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                       first_name = excluded.first_name,
                       username = excluded.username""",
                (telegram_id, first_name[:128], (username or "")[:64] or None, now_text),
            )
            user = connection.execute(
                "SELECT last_claim_at FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            if user["last_claim_at"]:
                next_claim = datetime.fromisoformat(user["last_claim_at"]) + CLAIM_PERIOD
                if now < next_claim:
                    return ClaimResult(next_claim_at=next_claim)

            key = self.outline.create_key(
                _outline_key_name(
                    telegram_id, username, "FREE300MB", "24hr", now
                ),
                self.limit_bytes,
            )
            expires_at = now + CLAIM_PERIOD
            try:
                cursor = connection.execute(
                    """INSERT INTO keys
                       (telegram_id, outline_key_id, key_type, created_at, expires_at, data_limit_bytes, status)
                       VALUES (?, ?, 'daily_free', ?, ?, ?, 'active')""",
                    (telegram_id, str(key["id"]), now_text, expires_at.isoformat(), self.limit_bytes),
                )
                connection.execute(
                    """UPDATE users
                       SET last_claim_at = ?
                       WHERE telegram_id = ?""",
                    (now_text, telegram_id),
                )
            except Exception:
                try:
                    self.outline.delete_key(str(key["id"]))
                finally:
                    raise
        return ClaimResult(access_url=str(key["accessUrl"]), expires_at=expires_at)

    def claim_trial(
        self,
        telegram_id: int,
        first_name: str,
        now: datetime | None = None,
        username: str | None = None,
    ) -> ClaimResult:
        """Issue one 3 GiB entitlement per rolling 30 days."""
        now = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = now.isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """INSERT INTO users (telegram_id, first_name, username, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                       first_name = excluded.first_name,
                       username = excluded.username""",
                (telegram_id, first_name[:128], (username or "")[:64] or None, now_text),
            )
            user = connection.execute(
                "SELECT trial_claimed_at FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            if user["trial_claimed_at"]:
                next_claim = datetime.fromisoformat(user["trial_claimed_at"]) + TRIAL_PERIOD
                if now < next_claim:
                    return ClaimResult(next_claim_at=next_claim)
            key = self.outline.create_key(
                _outline_key_name(
                    telegram_id, username, "FREE3GB", "30day", now
                ),
                self.trial_limit_bytes,
            )
            expires_at = now + TRIAL_PERIOD
            try:
                connection.execute(
                    """INSERT INTO keys
                       (telegram_id, outline_key_id, key_type, created_at, expires_at, data_limit_bytes, status)
                       VALUES (?, ?, 'monthly_trial', ?, ?, ?, 'active')""",
                    (telegram_id, str(key["id"]), now_text, expires_at.isoformat(), self.trial_limit_bytes),
                )
                connection.execute(
                    "UPDATE users SET trial_claimed_at = ? WHERE telegram_id = ?",
                    (now_text, telegram_id),
                )
            except Exception:
                try:
                    self.outline.delete_key(str(key["id"]))
                finally:
                    raise
        return ClaimResult(access_url=str(key["accessUrl"]), expires_at=expires_at)

    def _terminate_key(
        self,
        row: Any,
        reason: str,
        now: datetime,
        used_bytes: int | None = None,
    ) -> bool:
        """Record, delete, and (when supported) verify one remote credential."""
        now_text = now.astimezone(UTC).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE keys SET status = 'active', last_usage_bytes = COALESCE(?, last_usage_bytes),
                          quota_reason = CASE WHEN ? = 'quota' THEN 'quota' ELSE quota_reason END
                   WHERE id = ? AND status != 'revoked'""",
                (used_bytes, reason, row["id"]),
            )
            connection.execute(
                """INSERT INTO key_termination_events
                   (key_id, telegram_id, outline_key_id, reason, used_bytes, quota_bytes,
                    expires_at, detected_at, remote_state)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'retrying')
                   ON CONFLICT(key_id, reason) DO UPDATE SET
                       used_bytes = COALESCE(excluded.used_bytes, key_termination_events.used_bytes)""",
                (
                    row["id"], row["telegram_id"], str(row["outline_key_id"]), reason,
                    used_bytes, int(row["data_limit_bytes"]), row["expires_at"], now_text,
                ),
            )
        try:
            self.outline.delete_key(str(row["outline_key_id"]))
            getter = getattr(self.outline, "get_key", None)
            verified = callable(getter)
            if verified and getter(str(row["outline_key_id"])) is not None:
                raise OutlineError("Outline key still exists after delete")
        except Exception as exc:
            with self.database.connect() as connection:
                connection.execute(
                    """UPDATE key_termination_events
                       SET remote_state = CASE
                               WHEN delete_attempts + 1 >= 10 THEN 'escalated'
                               ELSE 'retrying'
                           END,
                           delete_attempts = delete_attempts + 1,
                           last_error = ? WHERE key_id = ? AND reason = ?""",
                    (type(exc).__name__, row["id"], reason),
                )
            return False
        remote_state = "deleted_verified" if verified else "delete_accepted"
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                "UPDATE keys SET status = 'revoked' WHERE id = ?", (row["id"],)
            )
            connection.execute(
                """UPDATE key_termination_events
                   SET remote_state = ?, delete_attempts = delete_attempts + 1,
                       last_error = NULL, deletion_verified_at = ?
                   WHERE key_id = ? AND reason = ?""",
                (remote_state, now_text if verified else None, row["id"], reason),
            )
        return True

    def enforce_quota(
        self,
        now: datetime | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> int:
        """Fail closed and revoke free/trial keys whose Outline metric hit its cap."""
        if metrics is None:
            try:
                metrics = self.outline.transfer_metrics()
            except Exception:
                return 0
        by_key = metrics.get("bytesTransferredByUserId", {}) if isinstance(metrics, dict) else {}
        if not isinstance(by_key, dict):
            return 0
        current = (now or datetime.now(UTC)).astimezone(UTC)
        try:
            self.queue_quota_warnings(current, metrics)
        except Exception as exc:
            # A notification outage must never delay the hard quota revoke.
            print(f"quota warning error: {type(exc).__name__}", file=sys.stderr)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT id, telegram_id, outline_key_id, data_limit_bytes, expires_at FROM keys
                   WHERE status = 'active' OR (status = 'revoke_failed' AND quota_reason = 'quota')"""
            ).fetchall()
        revoked = 0
        for row in rows:
            try:
                used = int(by_key.get(str(row["outline_key_id"]), 0) or 0)
            except (TypeError, ValueError):
                continue
            if used < int(row["data_limit_bytes"]):
                continue
            if self._terminate_key(row, "quota", current, used):
                revoked += 1
        return revoked

    def queue_quota_warnings(
        self,
        now: datetime | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> int:
        """Queue one Telegram warning as each remaining-quota threshold is crossed.

        The warning level is persisted per key, so repeated maintenance passes
        and temporary metric fluctuations cannot spam a customer. The final
        hard stop remains ``enforce_quota`` and never depends on delivery.
        """
        if metrics is None:
            try:
                metrics = self.outline.transfer_metrics()
            except Exception:
                return 0
        by_key = metrics.get("bytesTransferredByUserId", {}) if isinstance(metrics, dict) else {}
        if not isinstance(by_key, dict):
            return 0
        current = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = current.isoformat()
        queued = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            rows = connection.execute(
                """SELECT id, telegram_id, outline_key_id, data_limit_bytes,
                          expires_at, quota_warning_percent
                   FROM keys WHERE status = 'active'"""
            ).fetchall()
            for row in rows:
                try:
                    used = max(0, int(by_key.get(str(row["outline_key_id"]), 0) or 0))
                    quota = int(row["data_limit_bytes"])
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
                dedupe_key = f"quota-warning:free:{row['id']}:{reached}"
                try:
                    existing = connection.execute(
                        "SELECT id FROM notifications WHERE dedupe_key = ?",
                        (dedupe_key,),
                    ).fetchone()
                    if existing is None:
                        if quota == TRIAL_LIMIT_BYTES:
                            tier = "monthly 3 GiB"
                        elif quota == PUBLIC_LIMIT_BYTES:
                            tier = "daily 300 MiB"
                        else:
                            tier = "free"
                        remaining_percent = remaining * 100 / quota
                        text = (
                            f"Quota warning: your AuriX {tier} key has "
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
                        "UPDATE keys SET quota_warning_percent = ? WHERE id = ?",
                        (reached, row["id"]),
                    )
                except Exception as exc:
                    if self.database.is_integrity_error(exc):
                        continue
                    raise
        return queued

    def user_usage(
        self, telegram_id: int, usage_by_key: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Return this user's free/trial key usage without exposing key secrets."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT outline_key_id, created_at, expires_at, data_limit_bytes,
                          status, last_usage_bytes, quota_reason,
                          (SELECT remote_state FROM key_termination_events e
                           WHERE e.key_id = keys.id ORDER BY e.detected_at DESC LIMIT 1) AS termination_state
                   FROM keys
                   WHERE telegram_id = ?
                     AND (status IN ('active', 'revoke_failed') OR quota_reason = 'quota')
                   ORDER BY created_at DESC LIMIT 10""",
                (telegram_id,),
            ).fetchall()
        tiers = {
            300 * 1024**2: "Daily Free 300 MiB",
            3 * 1024**3: "Monthly Free 3 GiB",
        }
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
            quota = int(row["data_limit_bytes"])
            result.append(
                {
                    "tier": tiers.get(quota, "Free access"),
                    "used_bytes": used,
                    "quota_bytes": quota,
                    "remaining_bytes": max(0, quota - used),
                    "usage_observed": observed,
                    "expires_at": row["expires_at"],
                    "status": "quota exhausted" if row["quota_reason"] == "quota" else ("revocation failed" if row["termination_state"] == "escalated" else ("revocation pending" if row["termination_state"] in ("retrying", "delete_accepted") or row["status"] == "revoke_failed" else row["status"])),
                    "created_at": row["created_at"],
                }
            )
        return result

    def revoke_expired(self, now: datetime | None = None) -> int:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = current.isoformat()
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT id, telegram_id, outline_key_id, data_limit_bytes, expires_at FROM keys
                   WHERE status IN ('active', 'revoke_failed') AND expires_at <= ?""",
                (now_text,),
            ).fetchall()
        revoked = 0
        for row in rows:
            if self._terminate_key(row, "expiry", current):
                revoked += 1
        return revoked

    def reconcile_terminations(self, now: datetime | None = None, limit: int = 20) -> int:
        """Retry recorded remote deletions, including paid-upgrade cleanup."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT k.id, k.telegram_id, k.outline_key_id, k.data_limit_bytes,
                          k.expires_at, e.reason, e.used_bytes
                   FROM keys k JOIN key_termination_events e ON e.key_id = k.id
                   WHERE e.remote_state IN ('retrying', 'escalated') AND k.status != 'revoked'
                   ORDER BY e.detected_at LIMIT ?""",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        completed = 0
        for row in rows:
            if self._terminate_key(row, str(row["reason"]), current, row["used_bytes"]):
                completed += 1
        return completed

    def pending_termination_notices(self, audience: str) -> list[dict[str, Any]]:
        column = "admin_notice_state" if audience == "admin" else "user_notice_state"
        with self.database.connect() as connection:
            return [
                dict(row) for row in connection.execute(
                    f"""SELECT * FROM key_termination_events
                        WHERE COALESCE({column}, '') != remote_state
                        ORDER BY detected_at LIMIT 50"""
                ).fetchall()
            ]

    def mark_termination_notice(self, event_id: int, audience: str, state: str) -> None:
        column = "admin_notice_state" if audience == "admin" else "user_notice_state"
        with self.database.connect() as connection:
            connection.execute(
                f"UPDATE key_termination_events SET {column} = ? WHERE id = ?",
                (state, event_id),
            )

    def termination_summary(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute(
                """SELECT * FROM key_termination_events
                   ORDER BY detected_at DESC LIMIT ?""", (limit,)
            ).fetchall()]


class TelegramBot:
    CUSTOMER_BUTTON_COMMANDS = {
        "🎁 Daily 300MB": "/claim",
        "🚀 Monthly 3GB": "/trial",
        "💎 Upgrade 50GB": "/buy basic_50gb",
        "💠 Upgrade 100GB": "/buy standard_100gb",
        "🔐 My VPN": "/myvpn",
        "📊 Status": "/status",
        "📶 Usage": "/usage",
        "💰 Wallet": "/wallet",
        "🧾 My Orders": "/myorders",
        "❓ Help": "/help",
        "🏠 Customer Menu": "/help",
    }
    ADMIN_BUTTON_COMMANDS = {
        "🛠 Admin Panel": "/admin",
        "📥 Pending Orders": "/orders",
        "🧾 Receipt Review": "/receipts",
        "📈 Capacity": "/capacity",
        "🔎 Consistency": "/reconcile",
        "🔁 Failed Jobs": "/failed",
        "🚨 Enforcement": "/enforcement",
        # Retain this mapping for old keyboards, but do not render a global
        # ledger button: ledger access should be scoped to a specific order.
        "💰 Wallet Ledger": "/ledger",
    }
    ADMIN_ONLY_COMMANDS = frozenset(
        {
            "/admin",
            "/orders",
            "/receipts",
            "/capacity",
            "/reconcile",
            "/enforcement",
            "/failed",
            "/retry",
            "/retryjob",
            "/refund",
            "/ledger",
            "/receipt",
            "/rejectreceipt",
            "/verify",
            "/approve",
            "/reject",
        }
    )
    ADMIN_CONFIRMATION_COMMANDS = frozenset(
        {"/retry", "/refund", "/verify", "/rejectreceipt", "/approve", "/reject"}
    )
    UNKNOWN_ACTION_TEXT = "Use the menu to choose an AuriX action."

    def __init__(
        self,
        token: str,
        service: ClaimService,
        commerce: CommerceService | None = None,
        admin_ids: set[int] | None = None,
        trial_ids: set[int] | None = None,
        receipt_extractor: Any | None = None,
        allow_text_payment: bool = True,
        maintenance_interval_seconds: float = DEFAULT_MAINTENANCE_INTERVAL_SECONDS,
        command_scope_cleanup_ids: set[int] | None = None,
    ):
        self.api = f"https://api.telegram.org/bot{token}"
        self.service = service
        self.commerce = commerce
        self.admin_ids = admin_ids or set()
        self.admin_operations = AdminOperations(self.commerce, self.admin_ids, self.service)
        self.trial_ids = trial_ids or set()
        self.receipt_extractor = receipt_extractor or OpenAICompatibleReceiptExtractor()
        self.allow_text_payment = bool(allow_text_payment)
        self.maintenance_interval_seconds = max(1.0, float(maintenance_interval_seconds))
        self.command_scope_cleanup_ids = command_scope_cleanup_ids or set()
        self.offset = 0
        self.running = True
        self._maintenance_stop = threading.Event()
        self._maintenance_thread: threading.Thread | None = None
        self._admin_confirmations: dict[str, dict[str, Any]] = {}
        self._admin_confirmation_lock = threading.Lock()
        self._command_menu_ready = False
        self._command_menu_retry_enabled = hasattr(self.service, "database")
        self._command_menu_configure_attempted = False
        self._maintenance_lock = threading.Lock()
        self._panel_lock = threading.Lock()
        self._panels: dict[str, dict[str, Any]] = {}
        self._maintenance_last_status: dict[str, Any] = {
            "status": "never_run",
            "last_started_at": None,
            "last_completed_at": None,
            "last_success_at": None,
            "last_stage": None,
            "last_error": None,
        }

    def request(self, method: str, payload: dict[str, Any]) -> Any:
        started_at = time.perf_counter()
        request = urllib.request.Request(
            f"{self.api}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
        finally:
            _latency_log("telegram_request", started_at, method=method)
        if not result.get("ok"):
            raise RuntimeError("Telegram API request failed")
        return result["result"]

    def send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.request("sendMessage", payload)

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": text[:4096],
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.request("editMessageText", payload)

    @staticmethod
    def _reply_keyboard(rows: list[list[str]]) -> dict[str, Any]:
        return {
            "keyboard": [[{"text": label} for label in row] for row in rows],
            "resize_keyboard": True,
            "is_persistent": True,
            "input_field_placeholder": "Choose an AuriX action",
        }

    @staticmethod
    def _inline_keyboard(
        rows: list[list[tuple[str, str]]],
    ) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": label, "callback_data": callback_data[:64]}
                    for label, callback_data in row
                ]
                for row in rows
            ]
        }

    def _new_panel(self, chat_id: int, telegram_id: int, view: str) -> str:
        token = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
        with self._panel_lock:
            cutoff = time.monotonic() - 1800
            self._panels = {
                key: value
                for key, value in self._panels.items()
                if float(value.get("updated_at", 0)) >= cutoff
            }
            self._panels[token] = {
                "chat_id": int(chat_id),
                "telegram_id": int(telegram_id),
                "view": view,
                "page": 0,
                "updated_at": time.monotonic(),
                "message_id": None,
                "items": [],
            }
        return token

    def _panel_markup(self, token: str, page: int, pages: int) -> dict[str, Any]:
        rows: list[list[tuple[str, str]]] = []
        state = self._panels[token]
        for index, item in enumerate(state.get("items", [])):
            label = str(item.get("label") or item.get("id") or "Open")[:40]
            rows.append([(label, f"v2:{token}:item:{index}")])
        navigation: list[tuple[str, str]] = []
        if page > 0:
            navigation.append(("◀ Previous", f"v2:{token}:prev"))
        navigation.append((f"{page + 1}/{max(1, pages)}", f"v2:{token}:refresh"))
        if page + 1 < pages:
            navigation.append(("Next ▶", f"v2:{token}:next"))
        rows.append(navigation)
        rows.append([("🔄 Refresh", f"v2:{token}:refresh"), ("🏠 Admin Home", "a:n:admin")])
        return self._inline_keyboard(rows)

    @staticmethod
    def _panel_item(item: dict[str, Any], view: str) -> tuple[str, str]:
        item_id = str(item.get("id") or item.get("job_id") or "-")
        short_id = item_id[:10]
        if view == "orders":
            text = f"#{short_id} · tg:{str(item.get('telegram_id') or '-')[-6:]} · {item.get('plan_code') or '-'}\n{item.get('stage') or item.get('status') or '-'} · {item.get('receipt_status') or 'no receipt'}"
        elif view == "receipts":
            text = f"Receipt {short_id} · order:{str(item.get('order_id') or '-')[:10]}\ntg:{str(item.get('telegram_id') or '-')[-6:]} · {int(item.get('amount_minor') or 0):,} {item.get('currency') or ''}"
        elif view == "failed":
            text = f"{item.get('operation') or '-'} · job:{short_id}\norder:{str(item.get('order_id') or '-')[:10]} · attempts:{item.get('attempts') or 0}"
        else:
            text = f"tg:{str(item.get('telegram_id') or '-')[-6:]} · key:{str(item.get('outline_key_id') or '-')[:12]}\n{item.get('reason') or '-'} · {item.get('remote_state') or '-'}"
        return text[:700], short_id

    def _panel_data(self, telegram_id: int, view: str) -> list[dict[str, Any]]:
        if view == "orders":
            return list(self._admin_call(telegram_id, "list_pending_orders", limit=100) or [])
        if view == "receipts":
            return list(self._admin_call(telegram_id, "list_pending_receipts", limit=100) or [])
        if view == "failed":
            return list(self._admin_call(telegram_id, "failed_jobs", limit=100, include_nonterminal=True) or [])
        if view == "enforcement":
            return list(self._admin_service_call(telegram_id, "termination_summary", limit=100) or [])
        return []

    def _render_panel(self, token: str) -> tuple[str, dict[str, Any]]:
        with self._panel_lock:
            state = self._panels.get(token)
            if state is None:
                raise KeyError(token)
            view = state["view"]
            page = max(0, int(state.get("page", 0)))
            items = list(state.get("all_items", []))
        page_size = 5
        pages = max(1, (len(items) + page_size - 1) // page_size)
        page = min(page, pages - 1)
        current = items[page * page_size : (page + 1) * page_size]
        prepared = []
        blocks = []
        for item in current:
            block, _short = self._panel_item(item, view)
            prepared.append(item)
            blocks.append(block)
        title = {
            "orders": "📥 Pending Orders",
            "receipts": "🧾 Receipt Review",
            "failed": "🔁 Worker Jobs",
            "enforcement": "🚨 Enforcement",
        }.get(view, "AuriX Admin")
        text = f"{title} · {len(items)} open\nPage {page + 1}/{pages} · updated {datetime.now(UTC).strftime('%H:%M UTC')}"
        if blocks:
            text += "\n\n" + "\n\n".join(blocks)
        else:
            text += "\n\nNothing needs attention."
        with self._panel_lock:
            state = self._panels[token]
            state["page"] = page
            state["items"] = prepared
            state["updated_at"] = time.monotonic()
        return text[:4096], self._panel_markup(token, page, pages)

    def _open_admin_panel(self, chat_id: int, telegram_id: int, view: str, message_id: int | None = None) -> None:
        if not self._is_admin(telegram_id):
            self._send_customer_fallback(chat_id, telegram_id)
            return
        token = self._new_panel(chat_id, telegram_id, view)
        items = self._panel_data(telegram_id, view)
        if not items:
            empty = {
                "orders": "No pending orders.",
                "receipts": "No unreviewed receipts.",
                "failed": "No terminal worker failures.",
                "enforcement": "No free/trial termination events recorded.",
            }.get(view, "Nothing needs attention.")
            self.send(chat_id, empty)
            return
        with self._panel_lock:
            self._panels[token]["all_items"] = items
        text, markup = self._render_panel(token)
        if message_id is not None:
            try:
                self.edit_message(chat_id, message_id, text, markup)
                with self._panel_lock:
                    self._panels[token]["message_id"] = int(message_id)
                return
            except Exception:
                pass
        result = self.send(chat_id, text, markup)
        if isinstance(result, dict) and result.get("message_id"):
            with self._panel_lock:
                self._panels[token]["message_id"] = int(result["message_id"])

    def _handle_panel_callback(self, query: dict[str, Any], token: str, action: str, arg: str | None) -> bool:
        user = query.get("from") or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        telegram_id, chat_id = user.get("id"), chat.get("id")
        with self._panel_lock:
            state = self._panels.get(token)
            if state is None or state.get("telegram_id") != telegram_id or state.get("chat_id") != chat_id:
                return False
            if time.monotonic() - float(state.get("updated_at", 0)) > 1800:
                self._panels.pop(token, None)
                return False
            if action == "next":
                state["page"] = int(state.get("page", 0)) + 1
            elif action == "prev":
                state["page"] = max(0, int(state.get("page", 0)) - 1)
            elif action == "refresh":
                pass
            elif action == "item":
                items = state.get("items", [])
                try:
                    item = items[int(arg or "-1")]
                except (ValueError, IndexError):
                    item = None
                if item is not None:
                    view = state["view"]
                    target = item.get("id") or item.get("job_id")
                    if view == "orders":
                        self._send_order_detail(chat_id, telegram_id, str(target), admin_view=True)
                    elif view == "receipts":
                        self.handle({"chat":{"id":chat_id,"type":"private"},"from":{"id":telegram_id},"text":f"/receipt {target}"})
                    elif view == "failed":
                        self.handle({"chat":{"id":chat_id,"type":"private"},"from":{"id":telegram_id},"text":f"/order {item.get('order_id')}"})
                    return True
            state["all_items"] = self._panel_data(telegram_id, state["view"])
            message_id = message.get("message_id") or state.get("message_id")
        text, markup = self._render_panel(token)
        if isinstance(message_id, int):
            try:
                self.edit_message(chat_id, message_id, text, markup)
                return True
            except Exception:
                pass
        self.send(chat_id, text, markup)
        return True

    def _customer_keyboard(self, telegram_id: int) -> dict[str, Any]:
        rows = [
            ["🎁 Daily 300MB", "🚀 Monthly 3GB"],
            ["💎 Upgrade 50GB", "💠 Upgrade 100GB"],
            ["🔐 My VPN"],
            ["📊 Status", "📶 Usage"],
            ["🧾 My Orders", "💰 Wallet"],
            ["❓ Help"],
        ]
        return self._reply_keyboard(rows)

    def _admin_keyboard(self, telegram_id: int) -> dict[str, Any]:
        if not self._is_admin(telegram_id):
            raise PermissionError("admin keyboard requested by non-admin")
        return self._inline_keyboard(
            [
                [("📥 Pending Orders", "a:n:orders"), ("🧾 Receipt Review", "a:n:receipts")],
                [("📈 Capacity", "a:n:capacity"), ("🔎 Consistency", "a:n:reconcile")],
                [("🔁 Failed Jobs", "a:n:failed"), ("🚨 Enforcement", "a:n:enforcement")],
                [("🏠 Customer Menu", "n:menu")],
            ]
        )

    def configure_commands(self) -> None:
        self._command_menu_configure_attempted = True
        customer_commands = [
            {"command": "start", "description": "Open the AuriX menu"},
            {"command": "claim", "description": "Claim free 300 MB for 24 hours"},
            {"command": "trial", "description": "Claim free 3 GB for 30 days"},
            {"command": "buy", "description": "Buy a VPN plan"},
            {"command": "replace", "description": "Replace an untouched open order"},
            {"command": "status", "description": "Check VPN status"},
            {"command": "usage", "description": "Show used and remaining VPN data"},
            {"command": "myvpn", "description": "Show your active VPN key"},
            {"command": "wallet", "description": "Show wallet balance"},
            {"command": "myorders", "description": "Track your recent orders"},
            {"command": "order", "description": "Review one order by ID"},
            {"command": "cancelorder", "description": "Cancel an untouched order"},
            {"command": "whoami", "description": "Show your Telegram ID"},
            {"command": "help", "description": "Show customer help"},
        ]
        errors: list[str] = []

        def set_and_verify(scope: dict[str, Any], commands: list[dict[str, str]], label: str) -> bool:
            try:
                self.request("setMyCommands", {"commands": commands, "scope": scope})
                current = self.request("getMyCommands", {"scope": scope})
                current_names = {
                    str(item.get("command")) for item in current
                } if isinstance(current, list) else set()
                expected = {item["command"] for item in commands}
                if current_names != expected:
                    raise RuntimeError("Telegram returned an unexpected command list")
                return True
            except Exception as exc:
                errors.append(f"{label}: {type(exc).__name__}")
                return False

        set_and_verify({"type": "default"}, customer_commands, "default command scope")

        scope_store = getattr(self.service, "database", None)
        try:
            list_scopes = getattr(scope_store, "list_command_scope_ids", None)
            known_scopes = set(list_scopes()) if callable(list_scopes) else set()
        except Exception as exc:
            known_scopes = set()
            errors.append(f"load command scope state: {type(exc).__name__}")

        stale_scopes = (known_scopes | self.command_scope_cleanup_ids) - self.admin_ids
        for admin_id in sorted(stale_scopes):
            try:
                scope = {"type": "chat", "chat_id": admin_id}
                self.request(
                    "deleteMyCommands",
                    {"scope": scope},
                )
                remaining = self.request("getMyCommands", {"scope": scope})
                if not isinstance(remaining, list) or remaining:
                    raise RuntimeError("Telegram retained commands for removed admin scope")
                if scope_store and hasattr(scope_store, "remove_command_scope"):
                    scope_store.remove_command_scope(admin_id)
            except Exception as exc:
                errors.append(f"remove admin command scope {admin_id}: {type(exc).__name__}")

        admin_commands = customer_commands + [
            {"command": "admin", "description": "Open the admin panel"},
        ]
        for admin_id in self.admin_ids:
            scope = {"type": "chat", "chat_id": admin_id}
            if set_and_verify(scope, admin_commands, f"admin command scope {admin_id}"):
                if scope_store and hasattr(scope_store, "record_command_scope"):
                    try:
                        scope_store.record_command_scope(admin_id)
                    except Exception as exc:
                        errors.append(f"record admin command scope {admin_id}: {type(exc).__name__}")
        if errors:
            self._command_menu_ready = False
            raise RuntimeError("Telegram command menu degraded: " + "; ".join(errors))
        self._command_menu_ready = True

    def send_photo(
        self,
        chat_id: int,
        file_id: str,
        caption: str = "",
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": file_id,
            "caption": caption[:1024],
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.request("sendPhoto", payload)

    def send_document(
        self,
        chat_id: int,
        file_id: str,
        caption: str = "",
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "document": file_id,
            "caption": caption[:1024],
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.request("sendDocument", payload)

    @staticmethod
    def _receipt_review_caption(receipt: dict[str, Any]) -> str:
        extracted = receipt.get("extraction") or {}
        evidence_id = str(receipt["id"])
        return (
            "Receipt awaiting review\n"
            f"Evidence: {evidence_id}\n"
            f"Order: {receipt['order_id']}\n"
            f"Customer: {receipt['telegram_id']}\n"
            f"Expected: {int(receipt['amount_minor']):,} {receipt['currency']}\n"
            f"Extracted transaction: {extracted.get('transaction_id') or '-'}\n\n"
            "Check the receiving account, then use:\n"
            f"/verify {evidence_id} <transaction-id> <amount>"
        )

    def _send_receipt_review(self, chat_id: int, receipt: dict[str, Any]) -> None:
        """Send stored evidence, preferring a private Storage signed URL."""
        evidence_id = str(receipt["id"])
        markup = self._inline_keyboard(
            [
                [("View Order", f"a:o:{receipt['order_id']}")],
                [("🛑 Reject Receipt", f"a:q:{evidence_id}")],
            ]
        )
        file_id = str(receipt["telegram_file_id"])
        storage = getattr(self.commerce, "receipt_storage", None)
        storage_path = receipt.get("storage_path")
        if (
            storage is not None
            and storage_path
            and receipt.get("storage_status") == "stored"
        ):
            try:
                signed = storage.signed_url(str(storage_path), expires_in=300)
                if signed:
                    file_id = str(signed)
            except Exception as exc:
                # Telegram's original file ID remains a compatibility fallback
                # for legacy rows or a temporary Storage outage.
                print(
                    f"receipt storage signed URL error: {type(exc).__name__}",
                    file=sys.stderr,
                )
        caption = self._receipt_review_caption(receipt)
        media_type = receipt.get("telegram_media_type")
        primary = self.send_document if media_type == "document" else self.send_photo
        fallback = self.send_photo if media_type == "document" else self.send_document
        try:
            primary(chat_id, file_id, caption, markup)
        except (RuntimeError, urllib.error.HTTPError):
            # Older rows predate telegram_media_type, and Telegram file IDs can
            # only be reused by the API method matching their original type.
            fallback(chat_id, file_id, caption, markup)

    def _download_telegram_file(self, file_id: str) -> tuple[bytes, str]:
        info = self.request("getFile", {"file_id": file_id})
        file_path = info.get("file_path") if isinstance(info, dict) else None
        if not isinstance(file_path, str) or not file_path:
            raise RuntimeError("Telegram file path was unavailable")
        token = self.api.rsplit("/bot", 1)[-1]
        request = urllib.request.Request(f"https://api.telegram.org/file/bot{token}/{file_path}")
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(20 * 1024 * 1024 + 1)
        if len(data) > 20 * 1024 * 1024:
            raise RuntimeError("Receipt image exceeds Telegram download limit")
        mime = "image/jpeg" if file_path.lower().endswith((".jpg", ".jpeg")) else "image/png"
        return data, mime

    def _pending_order_id(self, telegram_id: int, caption: str = "") -> str | None:
        candidate = caption.split()
        if candidate and candidate[0].startswith("/") and len(candidate) > 1:
            return candidate[1]
        if self.commerce is None:
            return None
        pending = self.commerce.pending_order_for_user(telegram_id)
        return pending["id"] if pending else None

    def _handle_receipt(self, message: dict[str, Any], chat_id: int, telegram_id: int) -> None:
        if self.commerce is None:
            self.send(chat_id, "Paid plans are not configured in this staging process.")
            return
        photos = message.get("photo")
        document = message.get("document")
        file_id = None
        unique_id = None
        mime = "image/jpeg"
        media_type = "photo"
        if isinstance(photos, list) and photos:
            item = photos[-1]
            if isinstance(item, dict):
                file_id = item.get("file_id")
                unique_id = item.get("file_unique_id")
                mime = "image/jpeg"
        elif isinstance(document, dict) and str(document.get("mime_type", "")).startswith("image/"):
            file_id = document.get("file_id")
            unique_id = document.get("file_unique_id")
            mime = str(document.get("mime_type"))[:64]
            media_type = "document"
        if not isinstance(file_id, str):
            return
        order_id = self._pending_order_id(telegram_id, str(message.get("caption") or ""))
        if not order_id:
            self.send(chat_id, "Create an order with /buy basic_50gb, then send its receipt screenshot.")
            return
        try:
            image, mime = self._download_telegram_file(file_id)
            extraction = None
            try:
                extraction = self.receipt_extractor.extract(image, mime)
            except ReceiptLLMUnavailable:
                pass  # retain evidence for a human reviewer
            except ReceiptExtractionError as exc:
                print(f"receipt extraction error: {type(exc).__name__}", file=sys.stderr)
            except Exception as exc:
                # Model/provider output is untrusted; a parser failure must not
                # prevent the evidence record from reaching manual review.
                print(f"receipt extraction error: {type(exc).__name__}", file=sys.stderr)
            result = self.commerce.submit_receipt(
                telegram_id,
                order_id,
                provider="manual",
                file_id=file_id,
                file_unique_id=str(unique_id) if unique_id else None,
                image_bytes=image,
                mime_type=mime,
                extraction=extraction.as_dict() if hasattr(extraction, "as_dict") else extraction,
                telegram_media_type=media_type,
            )
        except (CommerceError, RuntimeError, urllib.error.URLError) as exc:
            self.send(chat_id, str(exc) or "Receipt could not be recorded. Try again later.")
            return
        if result.get("transaction_id"):
            self.send(chat_id, "Receipt received. Transaction ID extracted and queued for staff verification.")
        else:
            self.send(chat_id, "Receipt received for manual review. No payment is activated from the image alone.")
        evidence_id = str(result["evidence_id"])
        for admin_id in self.admin_ids:
            try:
                # Keep receipt images and customer metadata out of persistent
                # Telegram history. Admins open evidence on demand through the
                # authorized review route.
                self.send(
                    admin_id,
                    f"New receipt submitted for order {order_id}. Evidence: {evidence_id}.",
                    self._inline_keyboard(
                        [[
                            ("Open Receipt", f"a:r:{evidence_id}"),
                            ("Open Order", f"a:o:{order_id}"),
                        ]]
                    ),
                )
            except Exception as exc:
                print(f"admin receipt notification error: {type(exc).__name__}", file=sys.stderr)

    def _is_admin(self, telegram_id: int) -> bool:
        return telegram_id in self.admin_ids

    def _trial_allowed(self, telegram_id: int) -> bool:
        """Keep the optional trial allow-list consistent across every entrypoint."""
        return not self.trial_ids or int(telegram_id) in self.trial_ids

    def _free_claim_blocked_by_paid(self, telegram_id: int) -> bool:
        """Block daily claims only for a confirmed, currently usable paid key.

        A stale ``pending`` subscription without a paid key must not consume a
        customer's daily entitlement indefinitely.
        """
        if self.commerce is None:
            return False
        try:
            subscriptions = (
                self.commerce.user_vpns(telegram_id)
                if hasattr(self.commerce, "user_vpns")
                else [self.commerce.user_vpn(telegram_id)]
            )
        except Exception as exc:
            print(f"paid claim guard error: {type(exc).__name__}", file=sys.stderr)
            return False
        now = datetime.now(UTC)
        for subscription in subscriptions or []:
            if not subscription or subscription.get("status") != "active":
                continue
            if subscription.get("key_status") != "active":
                continue
            try:
                if datetime.fromisoformat(str(subscription.get("expires_at"))).astimezone(UTC) <= now:
                    continue
            except (TypeError, ValueError):
                continue
            return True
        return False

    def _admin_call(
        self, telegram_id: int, operation: str, *args: Any, **kwargs: Any
    ) -> Any:
        """Invoke a commerce operation through the admin authorization boundary."""
        return self.admin_operations.call(telegram_id, operation, *args, **kwargs)

    def _admin_service_call(
        self, telegram_id: int, operation: str, *args: Any, **kwargs: Any
    ) -> Any:
        return self.admin_operations.call_service(
            telegram_id, operation, *args, **kwargs
        )

    def _send_customer_fallback(self, chat_id: int, telegram_id: int) -> None:
        """Return a role-neutral response for unknown or unauthorized input."""
        self.send(
            chat_id,
            self.UNKNOWN_ACTION_TEXT,
            self._customer_keyboard(telegram_id),
        )

    def _admin_state_snapshot(
        self, command: str, args: list[str], telegram_id: int
    ) -> dict[str, Any]:
        """Read the state an administrator is about to mutate.

        This is deliberately a read-only snapshot. Domain methods still own
        their invariants and transactions; the snapshot prevents a stale
        confirmation from silently applying to a changed order or receipt.
        """
        target_id = str(args[0]) if args else ""
        snapshot: dict[str, Any] = {
            "command": command,
            "target_id": target_id,
            "state": "unavailable",
        }
        if self.commerce is None or not target_id:
            snapshot["state"] = "missing"
            return snapshot
        try:
            if command == "/retryjob":
                jobs = self._admin_call(telegram_id, "failed_jobs", limit=100, include_nonterminal=True)
                job = next((item for item in jobs if str(item.get("job_id")) == target_id), None)
                if job is None or job.get("job_status") != "failed":
                    snapshot["state"] = "missing"
                else:
                    snapshot.update({"state": "present", "job_id": target_id,
                                     "operation": job.get("operation"), "order_id": job.get("order_id"),
                                     "attempts": job.get("attempts"), "last_error": job.get("last_error")})
            elif command in {"/verify", "/rejectreceipt"}:
                receipt = self._admin_call(telegram_id, "get_receipt", target_id)
                if receipt is None:
                    snapshot["state"] = "missing"
                else:
                    snapshot.update(
                        {
                            "state": "present",
                            "evidence_id": receipt.get("id"),
                            "order_id": receipt.get("order_id"),
                            "telegram_id": receipt.get("telegram_id"),
                            "review_status": receipt.get("review_status"),
                            "storage_status": receipt.get("storage_status"),
                            "amount_minor": receipt.get("amount_minor"),
                            "currency": receipt.get("currency"),
                            "verified_provider_reference": receipt.get(
                                "verified_provider_reference"
                            ),
                            "verified_amount_minor": receipt.get(
                                "verified_amount_minor"
                            ),
                            "verified_currency": receipt.get("verified_currency"),
                        }
                    )
                    order_id = receipt.get("order_id")
                    order = (
                        self._admin_call(
                            telegram_id,
                            "order_detail",
                            str(order_id),
                            telegram_id,
                            is_admin=True,
                        )
                        if order_id
                        else None
                    )
                    if order:
                        snapshot.update(
                            {
                                "order_status": order.get("status"),
                                "payment_status": order.get("payment_status"),
                                "order_amount_minor": order.get("amount_minor"),
                            }
                        )
            else:
                order = self._admin_call(
                    telegram_id,
                    "order_detail",
                    target_id,
                    telegram_id,
                    is_admin=True,
                )
                if order is None:
                    snapshot["state"] = "missing"
                else:
                    snapshot.update(
                        {
                            "state": "present",
                            "order_id": order.get("id"),
                            "telegram_id": order.get("telegram_id"),
                            "plan_code": order.get("plan_code"),
                            "plan_name": order.get("plan_name"),
                            "amount_minor": order.get("amount_minor"),
                            "currency": order.get("currency"),
                            "order_status": order.get("status"),
                            "refund_status": order.get("refund_status"),
                            "payment_status": order.get("payment_status"),
                            "receipt_status": order.get("receipt_status"),
                            "subscription_status": order.get("subscription_status"),
                            "provisioning_status": order.get("provisioning_status"),
                            "wallet_reservation_status": order.get(
                                "wallet_reservation_status"
                            ),
                            "evidence_id": order.get("evidence_id"),
                        }
                    )
                if command == "/retry" and snapshot.get("state") == "present":
                    jobs = self._admin_call(telegram_id, "failed_jobs", limit=100)
                    matching = [
                        job for job in jobs if str(job.get("order_id")) == target_id
                    ]
                    snapshot["failed_job"] = (
                        {
                            "operation": matching[0].get("operation"),
                            "attempts": matching[0].get("attempts"),
                            "last_error": matching[0].get("last_error"),
                        }
                        if matching
                        else None
                    )
        except Exception as exc:
            # A preview must fail closed rather than fabricate financial state.
            snapshot = {
                "command": command,
                "target_id": target_id,
                "state": "unavailable",
                "error_type": type(exc).__name__,
            }
        return snapshot

    def _admin_state_fingerprint(
        self, command: str, args: list[str], telegram_id: int
    ) -> tuple[str, dict[str, Any]]:
        snapshot = self._admin_state_snapshot(command, args, telegram_id)
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode()).hexdigest(), snapshot

    @staticmethod
    def _admin_preview_text(
        command: str, args: list[str], fallback_prompt: str, snapshot: dict[str, Any]
    ) -> str:
        if snapshot.get("state") != "present":
            return (
                fallback_prompt
                + "\n\nCurrent state could not be loaded; it will be rechecked before execution."
            )
        if command == "/retryjob":
            return "\n".join([
                f"Worker job: {snapshot.get('job_id') or args[0]}",
                f"Operation: {snapshot.get('operation') or '-'}",
                f"Order: {snapshot.get('order_id') or '-'}",
                f"Attempts: {snapshot.get('attempts') or 0}",
                f"Failure: {snapshot.get('last_error') or '-'}",
                "Result: requeue this exact failed worker job.",
            ])
        if command in {"/verify", "/rejectreceipt"}:
            target = str(snapshot.get("evidence_id") or args[0])
            lines = [
                f"Evidence: {target}",
                f"Order: {snapshot.get('order_id') or '-'}",
                f"Customer: {snapshot.get('telegram_id') or '-'}",
                f"Current receipt status: {snapshot.get('review_status') or '-'}",
                f"Stored image: {snapshot.get('storage_status') or '-'}",
            ]
            if command == "/verify" and len(args) >= 3:
                lines.extend(
                    [
                        f"Transaction to verify: {args[1]}",
                        f"Amount to verify: {args[2]} {snapshot.get('currency') or ''}".strip(),
                    ]
                )
                lines.append("Verify against the receiving account before confirming.")
            else:
                lines.append("The order remains open so the customer can submit a replacement.")
            return "\n".join(lines)
        target = str(snapshot.get("order_id") or args[0])
        try:
            amount_text = f"{int(snapshot.get('amount_minor') or 0):,}"
        except (TypeError, ValueError):
            amount_text = str(snapshot.get("amount_minor") or "0")
        lines = [
            f"Order: {target}",
            f"Customer: {snapshot.get('telegram_id') or '-'}",
            f"Plan: {snapshot.get('plan_name') or snapshot.get('plan_code') or '-'}",
            f"Amount: {amount_text} {snapshot.get('currency') or ''}".strip(),
            f"Order state: {snapshot.get('order_status') or '-'}",
            f"Payment: {snapshot.get('payment_status') or '-'} · Receipt: {snapshot.get('receipt_status') or '-'}",
        ]
        impact = {
            "/approve": "Result: approve payment and queue VPN provisioning.",
            "/reject": "Result: close the order and notify the customer.",
            "/refund": "Result: credit the wallet and revoke or cancel paid access.",
            "/retry": "Result: requeue the reviewed failed provisioning job.",
        }.get(command)
        if impact:
            lines.append(impact)
        if command == "/retry":
            failed_job = snapshot.get("failed_job") or {}
            lines.append(
                f"Failure: {failed_job.get('operation') or '-'} · attempts: {failed_job.get('attempts') or 0} · {failed_job.get('last_error') or '-'}"
            )
        return "\n".join(lines)

    def _queue_admin_confirmation(
        self,
        chat_id: int,
        telegram_id: int,
        command: str,
        args: list[str],
        prompt: str,
        confirm_label: str = "✅ Confirm",
        cancel_data: str = "a:n:orders",
    ) -> None:
        token = secrets.token_urlsafe(18)
        expires_at = datetime.now(UTC) + ADMIN_CONFIRMATION_TTL
        state_fingerprint, snapshot = self._admin_state_fingerprint(
            command, args, telegram_id
        )
        prompt = self._admin_preview_text(command, args, prompt, snapshot)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        store = getattr(self.service, "database", None)
        durable = all(
            callable(getattr(store, method, None))
            for method in ("create_admin_challenge", "consume_admin_challenge")
        )
        with self._admin_confirmation_lock:
            now = datetime.now(UTC)
            self._admin_confirmations = {
                key: value
                for key, value in self._admin_confirmations.items()
                if value["expires_at"] > now
            }
            if not durable:
                self._admin_confirmations[token] = {
                    "chat_id": int(chat_id),
                    "telegram_id": int(telegram_id),
                    "command": command,
                    "args": list(args),
                    "expires_at": expires_at,
                    "state_fingerprint": state_fingerprint,
                }
        if durable:
            try:
                store.create_admin_challenge(
                    token_hash,
                    int(telegram_id),
                    int(chat_id),
                    command,
                    json.dumps(list(args), separators=(",", ":")),
                    state_fingerprint,
                    datetime.now(UTC).isoformat(),
                    expires_at.isoformat(),
                )
            except Exception as exc:
                print(f"admin confirmation persistence error: {type(exc).__name__}", file=sys.stderr)
                self.send(chat_id, "Administrator confirmation is temporarily unavailable. Try again.", self._admin_keyboard(telegram_id))
                return
        self.send(
            chat_id,
            prompt + f"\n\nThis confirmation expires in {int(ADMIN_CONFIRMATION_TTL.total_seconds() // 60)} minutes.",
            self._inline_keyboard(
                [[(confirm_label, f"a:k:{token}"), ("Cancel", f"a:d:{token}")]]
            ),
        )

    def _consume_admin_confirmation(
        self, chat_id: int, telegram_id: int, token: str
    ) -> dict[str, Any] | None:
        store = getattr(self.service, "database", None)
        if all(
            callable(getattr(store, method, None))
            for method in ("consume_admin_challenge", "create_admin_challenge")
        ):
            # The action is stored with the token, so first inspect the pending
            # record through the store's actor-bound consume operation. The
            # fallback below handles legacy in-memory tokens only.
            try:
                with store.connect() as connection:
                    row = connection.execute(
                        "SELECT command, args_json FROM admin_action_challenges WHERE token_hash = ?",
                        (hashlib.sha256(token.encode()).hexdigest(),),
                    ).fetchone()
                if row is None:
                    return None
                command = str(row["command"] if isinstance(row, dict) else row[0])
                raw_args = row["args_json"] if isinstance(row, dict) else row[1]
                args = json.loads(raw_args or "[]")
                if not isinstance(args, list):
                    return None
                current_fingerprint, current_snapshot = self._admin_state_fingerprint(
                    command, [str(value) for value in args], telegram_id
                )
                if current_snapshot.get("state") != "present":
                    return None
                return store.consume_admin_challenge(
                    hashlib.sha256(token.encode()).hexdigest(),
                    int(telegram_id),
                    int(chat_id),
                    current_fingerprint,
                    datetime.now(UTC).isoformat(),
                )
            except Exception as exc:
                print(f"admin confirmation consume error: {type(exc).__name__}", file=sys.stderr)
                return None
        with self._admin_confirmation_lock:
            challenge = self._admin_confirmations.get(token)
            if challenge is None:
                return None
            if (
                challenge["chat_id"] != int(chat_id)
                or challenge["telegram_id"] != int(telegram_id)
                or challenge["expires_at"] <= datetime.now(UTC)
            ):
                return None
            del self._admin_confirmations[token]
            current_fingerprint, current_snapshot = self._admin_state_fingerprint(
                challenge["command"], challenge["args"], telegram_id
            )
            if current_snapshot.get("state") != "present":
                return None
            if current_fingerprint != challenge.get("state_fingerprint"):
                return None
            return challenge

    @staticmethod
    def _order_summary(order: dict[str, Any]) -> str:
        return (
            f"{order['id']}\n"
            f"{order.get('plan_name') or order['plan_code']} · "
            f"{int(order['amount_minor']):,} {order['currency']}\n"
            f"Order: {order['status']} · Payment: {order.get('payment_status') or 'not submitted'} · "
            f"Receipt: {order.get('receipt_status') or 'not submitted'} · Stage: {order.get('stage', 'unknown')}"
        )

    @staticmethod
    def _order_detail_text(order: dict[str, Any]) -> str:
        lines = [
            "AuriX Order",
            "",
            f"ID: {order['id']}",
            f"Customer: {order['telegram_id']}",
            f"Plan: {order.get('plan_name') or order['plan_code']}",
            f"Amount: {int(order['amount_minor']):,} {order['currency']}",
            f"Order: {order['status']}",
            f"Refund: {order.get('refund_status') or 'none'}",
            f"Customer stage: {order.get('stage', 'unknown')}",
            f"Payment: {order.get('payment_status') or 'not submitted'}",
            f"Receipt review: {order.get('receipt_status') or 'not submitted'}",
            f"Subscription: {order.get('subscription_status') or 'not created'}",
            f"Provisioning: {order.get('provisioning_status') or 'not queued'}",
            f"Revocation: {order.get('revocation_status') or 'not queued'}",
            f"Created: {order['created_at']}",
        ]
        if order.get("expires_at"):
            lines.append(f"Expires: {order['expires_at']}")
        if order.get("evidence_id"):
            lines.append(f"Evidence ID: {order['evidence_id']}")
        return "\n".join(lines)

    def _order_actions(
        self, order: dict[str, Any], is_admin: bool
    ) -> dict[str, Any]:
        order_id = str(order["id"])
        rows: list[list[tuple[str, str]]] = []
        if is_admin:
            if order.get("evidence_id"):
                rows.append(
                    [("🧾 Open Receipt", f"a:r:{order['evidence_id']}")]
                )
                if order.get("receipt_status") == "pending":
                    rows.append(
                        [("🛑 Reject Receipt", f"a:q:{order['evidence_id']}")]
                    )
            if order.get("status") == "approved" and order.get("provisioning_status") == "failed":
                rows.append([("🔁 Retry Setup", f"a:h:{order_id}")])
            if order.get("revocation_status") in ("pending", "running"):
                rows.append([("⏳ Revocation in progress", f"a:o:{order_id}")])
            elif order.get("revocation_status") == "failed":
                rows.append([("🔁 Retry Revocation", f"a:g:{order_id}")])
            if order.get("telegram_id"):
                rows.append([("💰 View Ledger", f"a:l:{order['telegram_id']}")])
            if (
                order.get("refund_status") != "refunded"
                and (order.get("status") == "approved" or order.get("payment_status") == "verified")
            ):
                rows.append([("💸 Refund", f"a:f:{order_id}")])
            if (
                order.get("status") == "payment_submitted"
                and (
                    order.get("receipt_status") == "verified"
                    or order.get("wallet_reservation_status") == "reserved"
                )
            ):
                rows.append([("✅ Approve", f"a:a:{order_id}")])
            if order.get("status") in ("awaiting_payment", "payment_submitted") and order.get("refund_status") != "refunded":
                if order.get("payment_status") == "verified" or order.get("receipt_status") == "verified":
                    pass
                else:
                    rows.append([("❌ Reject…", f"a:x:{order_id}")])
            rows.append(
                [
                    ("🔄 Refresh", f"a:o:{order_id}"),
                    ("📥 Orders", "a:n:orders"),
                ]
            )
        else:
            if order.get("status") == "awaiting_payment" and not order.get("payment_status") and not order.get("receipt_status"):
                rows.append(
                    [
                        ("📷 Send Receipt", f"o:r:{order_id}"),
                        ("💰 Pay Wallet", f"o:w:{order_id}"),
                    ]
                )
                rows.append([("🗑 Cancel Order", f"o:c:{order_id}")])
            elif order.get("receipt_status") == "rejected":
                rows.append(
                    [("📷 Send Replacement Receipt", f"o:r:{order_id}")]
                )
            if order.get("stage") == "fulfilled":
                rows.append([("🔐 My VPN", "n:myvpn")])
            rows.append(
                [
                    ("🔄 Refresh", f"o:v:{order_id}"),
                    ("🧾 My Orders", "n:myorders"),
                ]
            )
        return self._inline_keyboard(rows)

    def _send_order_detail(
        self, chat_id: int, telegram_id: int, order_id: str, admin_view: bool = False,
        heading: str | None = None,
    ) -> None:
        if self.commerce is None:
            self.send(chat_id, "Order tracking is not configured.")
            return
        is_admin = bool(admin_view and self._is_admin(telegram_id))
        order = self.commerce.order_detail(order_id, telegram_id, is_admin=is_admin)
        if order is None:
            self.send(chat_id, "Order not found.")
            return
        self.send(
            chat_id,
            ((heading + "\n\n") if heading else "") + self._order_detail_text(order),
            self._order_actions(order, is_admin),
        )

    def handle_callback(self, query: dict[str, Any]) -> None:
        query_id = query.get("id")
        user = query.get("from") or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        data = query.get("data")
        if (
            not isinstance(query_id, str)
            or not isinstance(data, str)
            or not isinstance(user.get("id"), int)
            or not isinstance(chat.get("id"), int)
            or chat.get("type") != "private"
            or int(chat.get("id")) != int(user.get("id"))
        ):
            return
        self.request("answerCallbackQuery", {"callback_query_id": query_id})
        telegram_id = int(user["id"])
        chat_id = int(chat["id"])
        if data.startswith("v2:"):
            panel_parts = data.split(":", 3)
            if len(panel_parts) == 3:
                panel_parts.append("")
            if len(panel_parts) == 4 and self._handle_panel_callback(
                query, panel_parts[1], panel_parts[2], panel_parts[3] or None
            ):
                return
            self.send(chat_id, "This panel has expired. Open the admin menu again.")
            return
        first_name = str(user.get("first_name") or "")
        username = user.get("username") if isinstance(user.get("username"), str) else None
        synthetic = {
            "chat": {"id": chat_id, "type": "private"},
            "from": {
                "id": telegram_id,
                "first_name": first_name,
                "username": username,
            },
        }
        if data.startswith("a:") and not self._is_admin(telegram_id):
            self._send_customer_fallback(chat_id, telegram_id)
            return
        navigation = {
            "n:myorders": "/myorders",
            "n:myvpn": "/myvpn",
            "n:plans": "/plans",
            "n:wallet": "/wallet",
            "n:usage": "/usage",
            "n:menu": "/help",
        }
        # Legacy admin navigation buttons may still exist in Telegram message
        # history. Keep them safe and role-gated while no longer generating
        # them for new messages.
        if data == "n:adminorders":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat_id, telegram_id)
            else:
                synthetic["text"] = "/orders"
                self.handle(synthetic)
            return
        if data in navigation:
            synthetic["text"] = navigation[data]
            self.handle(synthetic)
            return
        parts = data.split(":", 2)
        if len(parts) != 3:
            self.send(chat_id, "This button is no longer valid. Refresh the menu.")
            return
        scope, action, entity_id = parts
        if scope == "o" and action == "v":
            self._send_order_detail(chat_id, telegram_id, entity_id)
        elif scope == "o" and action == "r":
            order = self.commerce.order_detail(entity_id, telegram_id) if self.commerce else None
            if order is None:
                self.send(chat_id, "Order not found.")
            else:
                self.send(
                    chat_id,
                    f"Send the receipt screenshot now. Caption it with:\n/paid {entity_id}",
                    self._inline_keyboard(
                        [[("🔄 Refresh Order", f"o:v:{entity_id}")]]
                    ),
                )
        elif scope == "o" and action == "w":
            synthetic["text"] = f"/walletpay {entity_id}"
            self.handle(synthetic)
        elif scope == "o" and action == "c":
            order = self.commerce.order_detail(entity_id, telegram_id) if self.commerce else None
            if order is None:
                self.send(chat_id, "Order not found.")
            else:
                self.send(
                    chat_id,
                    f"Cancel untouched order {entity_id}?",
                    self._inline_keyboard(
                        [[("Confirm Cancel", f"o:x:{entity_id}"), ("Keep Order", f"o:v:{entity_id}")]]
                    ),
                )
        elif scope == "o" and action == "x":
            synthetic["text"] = f"/cancelorder {entity_id}"
            self.handle(synthetic)
        elif scope == "p":
            if action == "b":
                synthetic["text"] = f"/buy {entity_id}"
            elif action == "t":
                synthetic["text"] = "/trial"
            elif action == "r":
                synthetic["text"] = f"/renew {entity_id}" if entity_id else "/renew"
            elif action == "x":
                if ":" in entity_id:
                    source, target_plan = entity_id.split(":", 1)
                    synthetic["text"] = f"/replace {target_plan} {source}"
                else:
                    synthetic["text"] = f"/replace {entity_id}"
            else:
                self.send(chat_id, "This plan action is no longer valid.")
                return
            self.handle(synthetic)
        elif scope == "a":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat_id, telegram_id)
                return
            if action == "k":
                challenge = self._consume_admin_confirmation(chat_id, telegram_id, entity_id)
                if challenge is None:
                    self.send(
                        chat_id,
                        "This confirmation has expired or was already used. Open the admin panel again.",
                        self._admin_keyboard(telegram_id),
                    )
                else:
                    synthetic["text"] = " ".join(
                        [challenge["command"], *challenge["args"]]
                    )
                    synthetic["_admin_confirmed"] = True
                    self.handle(synthetic)
            elif action == "d":
                token_hash = hashlib.sha256(entity_id.encode()).hexdigest()
                store = getattr(self.service, "database", None)
                cancelled = False
                if callable(getattr(store, "cancel_admin_challenge", None)):
                    try:
                        cancelled = bool(
                            store.cancel_admin_challenge(
                                token_hash,
                                int(telegram_id),
                                int(chat_id),
                                datetime.now(UTC).isoformat(),
                            )
                        )
                    except Exception as exc:
                        print(f"admin confirmation cancel error: {type(exc).__name__}", file=sys.stderr)
                else:
                    with self._admin_confirmation_lock:
                        challenge = self._admin_confirmations.get(entity_id)
                        if challenge and challenge["chat_id"] == chat_id and challenge["telegram_id"] == telegram_id:
                            del self._admin_confirmations[entity_id]
                            cancelled = True
                self.send(
                    chat_id,
                    "Confirmation cancelled." if cancelled else "This confirmation is no longer valid.",
                    self._admin_keyboard(telegram_id),
                )
            elif action == "n":
                admin_navigation = {
                    "admin": "/admin",
                    "orders": "/orders",
                    "receipts": "/receipts",
                    "capacity": "/capacity",
                    "reconcile": "/reconcile",
                    "failed": "/failed",
                    "enforcement": "/enforcement",
                }
                target = admin_navigation.get(entity_id)
                if target is None:
                    self.send(chat_id, "This admin action is no longer valid.")
                elif entity_id in {"orders", "receipts", "failed", "enforcement"}:
                    if self.commerce is None and entity_id != "enforcement":
                        self.send(chat_id, "Commerce is not configured.")
                    else:
                        self._open_admin_panel(
                            chat_id,
                            telegram_id,
                            entity_id,
                            message_id=message.get("message_id"),
                        )
                else:
                    synthetic["text"] = target
                    self.handle(synthetic)
            elif action == "o":
                self._send_order_detail(chat_id, telegram_id, entity_id, admin_view=True)
            elif action == "p":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/retryjob",
                    [entity_id],
                    f"Retry worker job {entity_id}?",
                    "Confirm Retry",
                )
            elif action == "h":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/retry",
                    [entity_id, "provision"],
                    f"Retry the failed provisioning job for order {entity_id}?",
                    "Confirm Retry",
                )
            elif action == "g":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/retry",
                    [entity_id, "revoke"],
                    f"Retry the failed revocation job for order {entity_id}?",
                    "Confirm Retry",
                )
            elif action == "l":
                synthetic["text"] = f"/ledger {entity_id}"
                self.handle(synthetic)
            elif action == "f":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/refund",
                    [entity_id],
                    f"Refund order {entity_id}? This credits the customer wallet and revokes paid access.",
                    "Confirm Refund",
                    f"a:o:{entity_id}",
                )
            elif action == "z":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/refund",
                    [entity_id],
                    f"Refund order {entity_id} to the customer wallet and revoke paid access?",
                    "Confirm Refund",
                    f"a:o:{entity_id}",
                )
            elif action == "r":
                synthetic["text"] = f"/receipt {entity_id}"
                self.handle(synthetic)
            elif action == "a":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/approve",
                    [entity_id],
                    f"Approve order {entity_id} and queue VPN provisioning?",
                    "Confirm Approve",
                    f"a:o:{entity_id}",
                )
            elif action == "x":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/reject",
                    [entity_id],
                    f"Reject order {entity_id}? This closes the order and notifies the customer.",
                    "Confirm Reject",
                    f"a:o:{entity_id}",
                )
            elif action == "q":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/rejectreceipt",
                    [entity_id],
                    f"Reject receipt {entity_id}? The order stays open for a replacement screenshot.",
                    "Confirm Reject Receipt",
                    f"a:r:{entity_id}",
                )
            elif action == "y":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/rejectreceipt",
                    [entity_id],
                    f"Reject receipt {entity_id} and request a replacement screenshot?",
                    "Confirm Reject Receipt",
                    f"a:r:{entity_id}",
                )
            elif action == "c":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/reject",
                    [entity_id],
                    f"Reject order {entity_id} and notify the customer?",
                    "Confirm Reject",
                    f"a:o:{entity_id}",
                )
            else:
                self.send(chat_id, "This admin action is no longer valid.")
        else:
            self.send(chat_id, "This button is no longer valid. Refresh the menu.")

    def _send_plans(self, chat_id: int) -> None:
        if self.commerce is None:
            self.send(chat_id, "Paid plans are not configured in this staging process.")
            return
        lines = ["AuriX plans:"]
        lines.append("free_3gb — free every 30 days — 3 GiB / 30 days (use /trial)")
        for plan in self.commerce.plans():
            quota = f"{plan.quota_bytes / 1024**3:g} GB" if plan.quota_bytes else "fair-use"
            lines.append(f"{plan.code} — {plan.price_minor:,} {plan.currency} — {quota} / {plan.duration_days} days")
        lines.append("\nBuy with: /buy <plan-code>")
        self.send(
            chat_id,
            "\n".join(lines),
            self._inline_keyboard(
                [
                    [("💎 50GB · 3,000", "p:b:basic_50gb")],
                    [("💠 100GB · 6,000", "p:b:standard_100gb")],
                    [("🚀 Free Monthly 3GB", "p:t:trial")],
                ]
            ),
        )

    def _send_status(self, chat_id: int, telegram_id: int, include_key: bool = False) -> None:
        if self.commerce is None:
            self.send(chat_id, "Paid subscriptions are not configured in this staging process.")
            return
        if hasattr(self.commerce, "user_vpns"):
            subscriptions = self.commerce.user_vpns(telegram_id)
        else:
            latest = self.commerce.user_vpn(telegram_id)
            subscriptions = [latest] if latest else []
        if not subscriptions:
            self.send(chat_id, "No subscription found. Use /plans to see available plans.")
            return
        subscription = subscriptions[0]
        text = (
            f"Status: {subscription['status']}\n"
            f"Plan: {subscription['plan_code']}\n"
            f"Expires: {subscription['expires_at']}\n"
            f"Paid keys: {sum(1 for item in subscriptions if item.get('key_status') == 'active')}"
        )
        if include_key:
            key_blocks = []
            for item in subscriptions:
                if item.get("access_url") and item.get("key_status") == "active":
                    key_blocks.append(
                        f"{item['plan_code']} · expires {item['expires_at']}\n{item['access_url']}"
                    )
                elif item.get("status") == "pending":
                    key_blocks.append(
                        f"{item['plan_code']} · provisioning pending (expires {item['expires_at']})"
                    )
            text += (
                "\n\nYour paid Outline keys:\n\n" + "\n\n".join(key_blocks)
                if key_blocks
                else "\n\nNo active paid key is available."
            )
        actions = [[("📶 Usage", "n:usage"), ("🧾 My Orders", "n:myorders")]]
        if subscription.get("status") in ("active", "expired", "revoked"):
            actions[0].append(("🔄 Renew", f"p:r:{subscription['plan_code']}"))
        self.send(chat_id, text, self._inline_keyboard(actions))

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(max(0, int(value)))
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        unit = units[0]
        for unit in units:
            if amount < 1024 or unit == units[-1]:
                break
            amount /= 1024
        if unit == "B":
            return f"{int(amount)} {unit}"
        return f"{amount:.2f} {unit}"

    def _send_usage(self, chat_id: int, telegram_id: int) -> None:
        try:
            metrics = self.service.outline.transfer_metrics()
            by_key = metrics.get("bytesTransferredByUserId", {}) if isinstance(metrics, dict) else {}
            if not isinstance(by_key, dict):
                raise ValueError("invalid Outline metrics response")
        except Exception as exc:
            self.send(chat_id, "VPN usage is temporarily unavailable. Please try again shortly.")
            print(f"usage metrics error: {type(exc).__name__}", file=sys.stderr)
            return
        entries = self.service.user_usage(telegram_id, by_key)
        if self.commerce is not None:
            entries.extend(self.commerce.user_usage(telegram_id, by_key))
        entries.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        if not entries:
            self.send(
                chat_id,
                "No VPN key usage is available yet. Claim a free tier or activate a paid plan first.",
                self._inline_keyboard([[("🎁 View Plans", "n:plans")]]),
            )
            return
        blocks = ["📶 Your VPN usage\nOutline transfer accounting (rolling 30-day window)"]
        for entry in entries:
            used = int(entry["used_bytes"])
            quota = int(entry["quota_bytes"])
            remaining = int(entry["remaining_bytes"])
            percent = (used * 100 / quota) if quota else 0.0
            filled = min(10, max(0, int(percent / 10)))
            bar = "█" * filled + "░" * (10 - filled)
            observed_note = "" if entry.get("usage_observed") else " (no traffic recorded yet)"
            blocks.append(
                f"{entry['tier']}\n"
                f"{bar} {percent:.1f}%\n"
                f"Used: {self._format_bytes(used)}{observed_note}\n"
                f"Remaining: {self._format_bytes(remaining)} of {self._format_bytes(quota)}\n"
                f"Expires: {entry['expires_at']}\n"
                f"State: {entry['status']}"
            )
        blocks.append(
            "Traffic is bytes reported by Outline for each key. It is not live speed, "
            "and the window is not a calendar-month reset."
        )
        self.send(
            chat_id,
            "\n\n".join(blocks),
            self._inline_keyboard(
                [[("🔄 Refresh Usage", "n:usage"), ("🔐 My VPN", "n:myvpn")]]
            ),
        )

    def _send_pending_notifications(self) -> None:
        if self.commerce is None:
            return
        for notification in self.commerce.pending_notifications():
            if notification.get("secret_unavailable"):
                self.commerce.mark_notification_failed(notification["id"])
                print("notification secret unavailable", file=sys.stderr)
                continue
            try:
                self.send(notification["telegram_id"], notification["text"])
            except Exception as exc:
                self.commerce.mark_notification_failed(notification["id"])
                print(f"notification error: {type(exc).__name__}", file=sys.stderr)
            else:
                self.commerce.mark_notification_sent(notification["id"])

    def _send_termination_notices(self) -> None:
        for event in self.service.pending_termination_notices("user"):
            reason = "data quota reached" if event["reason"] == "quota" else "24-hour/monthly access expired"
            remote = (
                "Outline confirmed the credential is deleted."
                if event["remote_state"] == "deleted_verified"
                else "Deletion could not be confirmed and has been escalated to the operator; the key remains blocked in AuriX."
                if event["remote_state"] == "escalated"
                else "Remote deletion is being retried; AuriX will not disclose or reactivate this key."
                if event["remote_state"] == "retrying"
                else "Outline accepted the deletion request."
            )
            usage = ""
            if event.get("used_bytes") is not None:
                usage = f"\nObserved usage: {self._format_bytes(int(event['used_bytes']))} / {self._format_bytes(int(event['quota_bytes']))}"
            try:
                self.send(
                    event["telegram_id"],
                    f"VPN access terminated\nReason: {reason}{usage}\nExpired at: {event['expires_at']}\n{remote}",
                )
            except Exception:
                continue
            self.service.mark_termination_notice(event["id"], "user", event["remote_state"])
        if not self.admin_ids:
            return
        for event in self.service.pending_termination_notices("admin"):
            message = (
                f"VPN enforcement | tg:{event['telegram_id']} | key:{event['outline_key_id']}\n"
                f"Reason: {event['reason']} | remote:{event['remote_state']} | attempts:{event['delete_attempts']}\n"
                f"Used/quota: {event.get('used_bytes') or '-'} / {event['quota_bytes']} | "
                f"detected:{event['detected_at']} | error:{event.get('last_error') or '-'}"
            )
            delivered = False
            for admin_id in self.admin_ids:
                try:
                    self.send(admin_id, message)
                    delivered = True
                except Exception:
                    pass
            if delivered:
                self.service.mark_termination_notice(event["id"], "admin", event["remote_state"])

    def _record_maintenance_heartbeat(
        self,
        *,
        started_at: str | None = None,
        completed_at: str | None = None,
        success_at: str | None = None,
        stage: str | None = None,
        error: str | None = None,
    ) -> None:
        """Persist housekeeping health without allowing health reporting to fail it."""
        if started_at is not None:
            self._maintenance_last_status["last_started_at"] = started_at
        if completed_at is not None:
            self._maintenance_last_status["last_completed_at"] = completed_at
        if success_at is not None:
            self._maintenance_last_status["last_success_at"] = success_at
        if stage is not None:
            self._maintenance_last_status["last_stage"] = stage
        self._maintenance_last_status["last_error"] = error
        self._maintenance_last_status["status"] = "ok" if success_at else ("error" if error else "running")
        store = getattr(self.service, "database", None)
        recorder = getattr(store, "maintenance_heartbeat", None)
        if callable(recorder):
            try:
                recorder(
                    started_at=started_at,
                    completed_at=completed_at,
                    success_at=success_at,
                    stage=stage,
                    error=error,
                )
            except Exception as exc:
                print(f"maintenance heartbeat persistence error: {type(exc).__name__}", file=sys.stderr)
        heartbeat_path = os.environ.get("AURIX_MAINTENANCE_HEARTBEAT_PATH")
        if heartbeat_path:
            try:
                Path(heartbeat_path).write_text(
                    json.dumps(self._maintenance_last_status, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            except Exception as exc:
                print(f"maintenance heartbeat file error: {type(exc).__name__}", file=sys.stderr)

    def _run_maintenance(self) -> None:
        """Serialize housekeeping passes across manual and scheduled invocations."""
        if not self._maintenance_lock.acquire(blocking=False):
            return
        try:
            self._run_maintenance_pass()
        finally:
            self._maintenance_lock.release()

    def _run_maintenance_pass(self) -> None:
        """Run one bounded housekeeping pass outside the Telegram poll loop."""
        started_at = time.perf_counter()
        started_text = datetime.now(UTC).isoformat()
        self._record_maintenance_heartbeat(started_at=started_text, stage="starting")
        failures: list[tuple[str, Exception]] = []

        def run_stage(name: str, callback: Any) -> Any:
            self._record_maintenance_heartbeat(stage=name)
            try:
                return callback()
            except Exception as exc:
                failures.append((name, exc))
                print(f"maintenance stage={name} error={type(exc).__name__}: {exc}", file=sys.stderr)
                self._record_maintenance_heartbeat(stage=name, error=f"{type(exc).__name__}: {exc}")
                return None

        if (
            self._command_menu_retry_enabled
            and self._command_menu_configure_attempted
            and not self._command_menu_ready
        ):
            run_stage("command_menu", self.configure_commands)

        metrics_result = run_stage("metrics", self.service.outline.transfer_metrics)
        metrics = metrics_result if isinstance(metrics_result, dict) else {}
        if metrics_result is None:
            _latency_log("maintenance_metrics", started_at, status="error")
        # Quota first preserves the more informative cause when a key is both
        # over quota and past its wall-clock entitlement.
        run_stage("free_quota", lambda: self.service.enforce_quota(metrics=metrics))
        run_stage("free_expiry", self.service.revoke_expired)
        reconcile_terminations = getattr(self.service, "reconcile_terminations", None)
        if callable(reconcile_terminations):
            run_stage("free_revocation_retry", reconcile_terminations)
        run_stage("termination_notices", self._send_termination_notices)
        if self.commerce is not None:
            run_stage("paid_quota", lambda: self.commerce.enforce_quotas(metrics=metrics))
            run_stage("paid_expiry", self.commerce.expire_and_process)
            run_stage("notifications", self._send_pending_notifications)
        challenge_store = getattr(self.service, "database", None)
        prune = getattr(challenge_store, "prune_admin_challenges", None)
        if callable(prune):
            run_stage("challenge_cleanup", lambda: prune(datetime.now(UTC).isoformat()))
        completed_text = datetime.now(UTC).isoformat()
        success_text = completed_text if not failures else None
        error_text = "; ".join(f"{name}: {type(exc).__name__}" for name, exc in failures) or None
        self._record_maintenance_heartbeat(
            completed_at=completed_text,
            success_at=success_text,
            stage="completed",
            error=error_text,
        )
        _latency_log("maintenance", started_at)

    def _maintenance_loop(self) -> None:
        while self.running and not self._maintenance_stop.is_set():
            try:
                self._run_maintenance()
            except Exception as exc:
                print(
                    f"maintenance error: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            if self._maintenance_stop.wait(self.maintenance_interval_seconds):
                break

    def stop(self) -> None:
        self.running = False
        self._maintenance_stop.set()

    def handle(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        if (
            not isinstance(chat, dict)
            or not isinstance(user, dict)
            or chat.get("type") != "private"
            or not isinstance(chat.get("id"), int)
            or not isinstance(user.get("id"), int)
            or int(chat.get("id")) != int(user.get("id"))
        ):
            return
        telegram_id = user["id"]
        first_name = user.get("first_name") or ""
        if not isinstance(first_name, str):
            first_name = str(first_name)
        username = user.get("username")
        if username is not None and not isinstance(username, str):
            username = str(username)
        self.service.track_user(
            telegram_id, first_name, username=username
        )
        if message.get("photo") or message.get("document"):
            self._handle_receipt(message, chat["id"], telegram_id)
            return
        text = message.get("text") or ""
        if not isinstance(text, str) or not text.strip():
            return
        raw_text = text.strip()
        text = self.CUSTOMER_BUTTON_COMMANDS.get(raw_text)
        if text is None and self._is_admin(telegram_id):
            text = self.ADMIN_BUTTON_COMMANDS.get(raw_text, raw_text)
        if text is None:
            text = raw_text
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]
        confirmed = message.get("_admin_confirmed") is True
        if command in self.ADMIN_ONLY_COMMANDS and not self._is_admin(telegram_id):
            self._send_customer_fallback(chat["id"], telegram_id)
            return
        if command in self.ADMIN_CONFIRMATION_COMMANDS and not confirmed:
            # Validate syntax before presenting a challenge, but never mutate
            # commerce state from a directly typed administrative command.
            if command in {"/approve", "/reject"} and len(args) != 1:
                pass
            elif command == "/retry" and len(args) not in (1, 2):
                pass
            elif command == "/refund" and not args:
                pass
            elif command == "/verify" and len(args) != 3:
                pass
            elif command == "/rejectreceipt" and not args:
                pass
            else:
                prompt = {
                    "/approve": lambda: f"Approve order {args[0]} and queue VPN provisioning?",
                    "/reject": lambda: f"Reject order {args[0]} and notify the customer?",
                    "/retry": lambda: f"Retry the failed worker job for order {args[0]}?",
                    "/refund": lambda: f"Refund order {args[0]} to the customer wallet and revoke paid access?",
                    "/verify": lambda: f"Verify receipt {args[0]} for transaction {args[1]} and amount {args[2]}?",
                    "/rejectreceipt": lambda: f"Reject receipt {args[0]} and request a replacement screenshot?",
                }[command]()
                self._queue_admin_confirmation(
                    chat["id"],
                    telegram_id,
                    command,
                    args,
                    prompt,
                    confirm_label={
                        "/approve": "Confirm Approve",
                        "/reject": "Confirm Reject",
                        "/retry": "🔁 Confirm Retry",
                        "/refund": "💸 Confirm Refund",
                        "/verify": "✅ Confirm Verify",
                        "/rejectreceipt": "🛑 Confirm Receipt Rejection",
                    }[command],
                )
                return
        if command in ("/start", "/help"):
            self.send(
                chat["id"],
                "AuriX VPN\n\n"
                "Choose an action below. Everyone can claim 300 MB daily or "
                "3 GB every 30 days, with 50 GB and 100 GB paid upgrades.\n\n"
                "For payment, create an upgrade order and send only the receipt screenshot.",
                self._customer_keyboard(telegram_id),
            )
            if command == "/start":
                if self.trial_ids and telegram_id not in self.trial_ids:
                    return
                if self._free_claim_blocked_by_paid(telegram_id):
                    return
                try:
                    welcome_claim = self.service.claim(
                        telegram_id, first_name, username=username
                    )
                except OutlineError:
                    self.send(chat["id"], "Your free key is temporarily unavailable. Use /claim to retry later.")
                else:
                    if welcome_claim.access_url:
                        self.send(
                            chat["id"],
                            f"Your {self.service.limit_bytes / 1024**2:g} MiB starter key:\n\n"
                            f"{welcome_claim.access_url}\n\n"
                            f"Expires: {welcome_claim.expires_at.strftime('%Y-%m-%d %H:%M UTC')}",
                        )
        elif command == "/whoami":
            access = "\nAdmin access: enabled" if self._is_admin(telegram_id) else ""
            self.send(
                chat["id"],
                f"Your Telegram ID: {telegram_id}{access}",
                self._customer_keyboard(telegram_id),
            )
        elif command == "/admin":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            else:
                summary = ""
                if self.commerce is not None:
                    try:
                        report = self._admin_call(telegram_id, "consistency_report")
                        summary = (
                            f"\n\nQueue: {report.get('pending_receipts', 0)} receipt(s) pending · "
                            f"{report.get('pending_receipt_uploads', 0)} upload(s) pending · "
                            f"{report.get('failed_receipt_uploads', 0)} upload(s) failed · "
                            f"{report.get('failed_jobs', 0)} failed job(s) · "
                            f"{report.get('stale_receipts', 0)} stale review(s) · "
                            f"{report.get('dead_notifications', 0)} dead notification(s)"
                        )
                    except Exception as exc:
                        print(f"admin dashboard error: {type(exc).__name__}", file=sys.stderr)
                self.send(
                    chat["id"],
                    "AuriX Admin\n\n"
                    "Daily flow: Pending Orders → open receipt → verify the transaction "
                    "against your receiving account → Approve.\n"
                    "Use Failed Jobs to retry a reviewed Outline failure, open an order "
                    "to inspect its wallet ledger, and run Consistency before taking "
                    "payment decisions."
                    + summary,
                    self._admin_keyboard(telegram_id),
                )
        elif command == "/myorders":
            if self.commerce is None:
                self.send(chat["id"], "Order tracking is not configured.")
            else:
                orders = self.commerce.list_user_orders(telegram_id)
                if not orders:
                    self.send(
                        chat["id"], "You have no orders yet.", self._customer_keyboard(telegram_id)
                    )
                else:
                    text = "Your recent orders\n\n" + "\n\n".join(
                        self._order_summary(order) for order in orders
                    )
                    rows = [
                        [(f"View {str(order['id'])[:8]}", f"o:v:{order['id']}")]
                        for order in orders
                    ]
                    rows.append([("💎 Upgrade", "n:plans"), ("💰 Wallet", "n:wallet")])
                    self.send(chat["id"], text, self._inline_keyboard(rows))
        elif command == "/order":
            if self.commerce is None or len(args) != 1:
                self.send(chat["id"], "Usage: /order <order-id>")
            else:
                self._send_order_detail(
                    chat["id"], telegram_id, args[0], admin_view=self._is_admin(telegram_id)
                )
        elif command == "/plans":
            self._send_plans(chat["id"])
        elif command in ("/buy", "/upgrade"):
            if self.commerce is None:
                self.send(chat["id"], "Paid plans are not configured in this staging process.")
            elif len(args) != 1:
                self.send(chat["id"], "Usage: /buy <plan-code>\n\nUse /plans first.")
            else:
                try:
                    order = self.commerce.create_order(
                        telegram_id, first_name, args[0], username=username
                    )
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    if order.plan_conflict:
                        detail = self.commerce.order_detail(order.order_id, telegram_id)
                        untouched = bool(
                            detail
                            and detail.get("status") == "awaiting_payment"
                            and not detail.get("payment_status")
                            and not detail.get("receipt_status")
                        )
                        if not untouched:
                            self._send_order_detail(chat["id"], telegram_id, order.order_id, heading="Existing open order")
                            return
                        self.send(
                            chat["id"],
                            f"You already have an open order for {order.plan.name}. Choose whether to replace that untouched order with {args[0]}.",
                            self._inline_keyboard(
                                [[("Replace Open Order", f"p:x:{order.order_id}:{args[0]}"), ("Keep Existing", f"o:v:{order.order_id}")]]
                            ),
                        )
                        return
                    if not order.created:
                        self._send_order_detail(chat["id"], telegram_id, order.order_id, heading="Existing open order")
                        return
                    heading = "Order created"
                    self.send(
                        chat["id"],
                        f"{heading}: {order.order_id}\n"
                        f"Plan: {order.plan.name}\n"
                        f"Amount: {order.plan.price_minor:,} {order.plan.currency}\n\n"
                        f"Pay through the approved channel, then send the receipt screenshot.\n"
                        f"Reply to this order message or caption it with: /paid {order.order_id}",
                        self._inline_keyboard(
                            [[
                                ("📷 Send Receipt", f"o:r:{order.order_id}"),
                                ("💰 Pay Wallet", f"o:w:{order.order_id}"),
                            ], [("View Order", f"o:v:{order.order_id}")]]
                        ),
                    )
        elif command == "/paid":
            if self.commerce is None:
                self.send(chat["id"], "Paid plans are not configured in this staging process.")
            elif len(args) < 1:
                self.send(chat["id"], "Usage: /paid <order-id> then send the receipt screenshot")
            elif len(args) == 1:
                self.send(chat["id"], f"Now send the receipt screenshot for order {args[0]}. You may caption it with /paid {args[0]}.")
            elif not self.allow_text_payment:
                self.send(chat["id"], "Text payment references are disabled. Send the receipt screenshot instead.")
            else:
                try:
                    result = self.commerce.submit_payment(
                        telegram_id,
                        args[0],
                        "manual",
                        " ".join(args[1:]) if len(args) > 1 else "pending-receipt",
                    )
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(chat["id"], f"Payment recorded ({result}). An admin will review it.")
                    for admin_id in self.admin_ids:
                        try:
                            self.send(admin_id, f"Payment submitted for order {args[0]} by Telegram user {telegram_id}.")
                        except Exception as exc:
                            print(f"admin notification error: {type(exc).__name__}", file=sys.stderr)
        elif command in ("/status", "/myvpn"):
            self._send_status(chat["id"], telegram_id, include_key=command == "/myvpn")
        elif command == "/usage":
            self._send_usage(chat["id"], telegram_id)
        elif command == "/renew":
            if self.commerce is None:
                self.send(chat["id"], "Paid plans are not configured in this staging process.")
            else:
                requested_plan = args[0] if args else None
                subscriptions = self.commerce.user_vpns(telegram_id) if hasattr(self.commerce, "user_vpns") else []
                subscription = next((item for item in subscriptions if item.get("plan_code") == requested_plan), None) if requested_plan else self.commerce.user_vpn(telegram_id)
                if subscription is None and requested_plan:
                    self.send(chat["id"], "That plan is not one of your previous plans.")
                    return
                if subscription is None:
                    self.send(chat["id"], "No previous plan found. Use /plans and /buy first.")
                else:
                    try:
                        order = self.commerce.create_order(
                            telegram_id,
                            first_name,
                            requested_plan or subscription["plan_code"],
                            username=username,
                        )
                    except CommerceError as exc:
                        self.send(chat["id"], str(exc))
                    else:
                        heading = "Renewal order created" if order.created else "Existing open order"
                        self.send(chat["id"], f"{heading}: {order.order_id}\nSend /paid {order.order_id} then the receipt screenshot after payment.")
        elif command == "/trial":
            if not self._trial_allowed(telegram_id):
                self.send(chat["id"], "The monthly trial is currently invite-only. Use /claim or /plans instead.")
                return
            if self._free_claim_blocked_by_paid(telegram_id):
                self.send(chat["id"], "Your paid account is already active; the free trial is not needed.")
                return
            try:
                result = self.service.claim_trial(
                    telegram_id, first_name, username=username
                )
            except OutlineError:
                self.send(chat["id"], "Trial service temporarily unavailable. Try again later.")
                return
            if result.access_url:
                self.send(chat["id"], f"Your monthly 3 GiB key:\n\n{result.access_url}\n\nExpires: {result.expires_at.strftime('%Y-%m-%d %H:%M UTC')}")
            else:
                retry = result.next_claim_at.strftime("%Y-%m-%d %H:%M UTC") if result.next_claim_at else "later"
                self.send(chat["id"], f"Monthly 3 GiB already claimed. Come back after {retry}.")
        elif command == "/wallet":
            if self.commerce is None:
                self.send(chat["id"], "Wallet is not configured.")
            else:
                balance = self.commerce.wallet_balance(telegram_id)
                history = self.commerce.wallet_history(telegram_id, limit=5)
                history_text = ""
                if history:
                    history_text = "\n\nRecent wallet events:\n" + "\n".join(
                        f"{item['created_at']} · {item['kind']} {int(item['amount_minor']):,} {item['currency']} · {item['reference_id']}"
                        for item in history
                    )
                self.send(
                    chat["id"],
                    f"Wallet balance: {balance:,} MMK\nWallet credits are posted only after staff verify a receipt.{history_text}",
                    self._inline_keyboard(
                        [[("🧾 My Orders", "n:myorders"), ("💎 Upgrade", "n:plans")]]
                    ),
                )
        elif command == "/walletpay":
            if self.commerce is None:
                self.send(chat["id"], "Wallet is not configured.")
            elif len(args) != 1:
                self.send(chat["id"], "Usage: /walletpay <order-id>")
            else:
                try:
                    result = self.commerce.pay_order_with_wallet(telegram_id, args[0])
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(chat["id"], f"Wallet payment {result}; an admin will review and approve the order.")
        elif command == "/replace":
            if self.commerce is None or len(args) not in (1, 2):
                self.send(chat["id"], "Usage: /replace <plan-code> [expected-order-id]")
            else:
                try:
                    order = self.commerce.replace_open_order(
                        telegram_id, first_name, args[0], username=username,
                        expected_order_id=args[1] if len(args) == 2 else None,
                    )
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(
                        chat["id"],
                        f"Order replaced: {order.order_id}\nPlan: {order.plan.name}\nAmount: {order.plan.price_minor:,} {order.plan.currency}\n\nPay through the approved channel, then send the receipt screenshot.",
                        self._inline_keyboard(
                            [[("📷 Send Receipt", f"o:r:{order.order_id}"), ("View Order", f"o:v:{order.order_id}")]]
                        ),
                    )
        elif command == "/cancelorder":
            if self.commerce is None or len(args) != 1:
                self.send(chat["id"], "Usage: /cancelorder <order-id>")
            else:
                try:
                    result = self.commerce.cancel_order(telegram_id, args[0])
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(chat["id"], f"Order {args[0]} {result}.", self._customer_keyboard(telegram_id))
        elif command == "/receipt":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None or len(args) != 1:
                self.send(chat["id"], "Usage: /receipt <evidence-id> (admin)")
            else:
                receipt = self._admin_call(telegram_id, "get_receipt", args[0])
                if receipt is None:
                    self.send(chat["id"], "Receipt evidence not found.")
                else:
                    try:
                        self._send_receipt_review(chat["id"], receipt)
                    except Exception as exc:
                        print(f"receipt review media error: {type(exc).__name__}", file=sys.stderr)
                        self.send(
                            chat["id"],
                            "Receipt metadata exists, but Telegram no longer accepts its stored "
                            "file ID. Ask the customer to submit the screenshot again.",
                        )
        elif command == "/verify":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            elif len(args) != 3:
                self.send(chat["id"], "Usage: /verify <evidence-id> <transaction-id> <amount>")
            else:
                try:
                    amount = int(args[2].replace(",", ""))
                    order_id = self._admin_call(
                        telegram_id, "verify_receipt", args[0], telegram_id, args[1], amount
                    )
                except (CommerceError, ValueError) as exc:
                    self.send(chat["id"], str(exc) or "Verified amount must be an integer.")
                else:
                    self.send(
                        chat["id"],
                        f"Receipt verified for order {order_id}. Use /approve {order_id} to provision.",
                    )
        elif command == "/rejectreceipt":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None or not args:
                self.send(chat["id"], "Usage: /rejectreceipt <evidence-id> [reason]")
            else:
                try:
                    order_id = self._admin_call(
                        telegram_id,
                        "reject_receipt",
                        args[0],
                        telegram_id,
                        " ".join(args[1:]) or "Receipt rejected; please submit a clearer screenshot.",
                    )
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(
                        chat["id"],
                        f"Receipt rejected for order {order_id}; the customer can submit a replacement.",
                        self._inline_keyboard([[("📥 Orders", "a:n:orders")]]),
                    )
        elif command in ("/orders", "/receipts"):
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            else:
                view = "receipts" if command == "/receipts" else "orders"
                items = self._panel_data(telegram_id, view)
                if not items:
                    self.send(chat["id"], "No unreviewed receipts." if view == "receipts" else "No pending orders.")
                else:
                    self._open_admin_panel(chat["id"], telegram_id, view)
        elif command == "/capacity":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            else:
                try:
                    snapshot = self._admin_call(telegram_id, "capacity_snapshot")
                except Exception as exc:
                    self.send(chat["id"], "Outline capacity metrics are temporarily unavailable.")
                    print(f"capacity error: {type(exc).__name__}", file=sys.stderr)
                else:
                    mapped_usage = sum(item["used_bytes"] for item in snapshot["usage"])
                    self.send(
                        chat["id"],
                        "AuriX capacity\n"
                        f"Outline version: {snapshot['outline_version']}\n"
                        f"Active subscriptions: {snapshot['active_subscriptions']}\n"
                        f"Active keys: {snapshot['active_keys']}\n"
                        f"Mapped transfer (Outline window): {mapped_usage:,} bytes\n"
                        f"Expiring within 24h: {snapshot['expiring_24h']}\n"
                        f"Pending jobs: {snapshot['pending_jobs']}\n"
                        f"Failed jobs: {snapshot['failed_jobs']}",
                    )
        elif command == "/reconcile":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            else:
                report = self._admin_call(telegram_id, "consistency_report")
                issue_keys = {
                    "duplicate_open_orders",
                    "approved_missing_subscription",
                    "approved_missing_provision_job",
                    "stale_receipts",
                    "pending_receipt_uploads",
                    "failed_receipt_uploads",
                    "failed_jobs",
                    "failed_activations",
                    "failed_revocations",
                    "pending_revocations",
                    "dead_notifications",
                    "wallet_balance_mismatches",
                }
                healthy = all(report.get(key, 0) == 0 for key in issue_keys)
                lines = ["AuriX consistency scan", "Status: " + ("OK" if healthy else "ACTION REQUIRED")]
                lines.extend(f"{key.replace('_', ' ').title()}: {value}" for key, value in report.items())
                self.send(chat["id"], "\n".join(lines), self._admin_keyboard(telegram_id))
        elif command == "/enforcement":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            else:
                events = self._admin_service_call(telegram_id, "termination_summary")
                if not events:
                    self.send(chat["id"], "No free/trial termination events recorded.", self._admin_keyboard(telegram_id))
                else:
                    self._open_admin_panel(chat["id"], telegram_id, "enforcement")
        elif command == "/failed":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            else:
                jobs = self._admin_call(telegram_id, "failed_jobs", include_nonterminal=True)
                if not jobs:
                    self.send(chat["id"], "No terminal worker failures.", self._admin_keyboard(telegram_id))
                else:
                    self._open_admin_panel(chat["id"], telegram_id, "failed")
        elif command == "/retry":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None or len(args) not in (1, 2):
                self.send(chat["id"], "Usage: /retry <order-id> [provision|revoke]")
            else:
                try:
                    operation = self._admin_call(
                        telegram_id, "retry_failed_job", args[0], telegram_id,
                        operation=args[1] if len(args) == 2 else None,
                    )
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(
                        chat["id"],
                        f"{operation.title()} job requeued for order {args[0]}.",
                        self._inline_keyboard([[ ("🔄 Refresh Order", f"a:o:{args[0]}") ]]),
                    )
        elif command == "/retryjob":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None or len(args) != 1:
                self.send(chat["id"], "Usage: /retryjob <job-id>")
            else:
                try:
                    operation = self._admin_call(telegram_id, "retry_job", args[0], telegram_id)
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(chat["id"], f"{operation.title()} job {args[0]} requeued.", self._admin_keyboard(telegram_id))
        elif command == "/refund":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None or not args:
                self.send(chat["id"], "Usage: /refund <order-id> [reason]")
            else:
                try:
                    result = self._admin_call(
                        telegram_id,
                        "refund_order",
                        args[0],
                        telegram_id,
                        " ".join(args[1:]) or "refunded by admin",
                    )
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(
                        chat["id"],
                        f"Order {args[0]} {result}; wallet reversal recorded and access revocation queued.",
                        self._inline_keyboard([[ ("🔄 Refresh Order", f"a:o:{args[0]}") ]]),
                    )
        elif command == "/ledger":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None or len(args) != 1:
                self.send(chat["id"], "Usage: /ledger <telegram-id>")
            else:
                try:
                    customer_id = int(args[0])
                    balance = self._admin_call(
                        telegram_id, "wallet_balance", customer_id
                    )
                    history = self._admin_call(
                        telegram_id, "wallet_history", customer_id, limit=20
                    )
                except (ValueError, CommerceError) as exc:
                    self.send(chat["id"], str(exc) or "Telegram ID must be numeric.")
                else:
                    lines = [f"Wallet ledger · tg:{customer_id}", f"Balance: {balance:,} MMK"]
                    lines.extend(
                        f"{item['created_at']} · {item['kind']} {int(item['amount_minor']):,} {item['currency']} · {item['reference_id']}"
                        for item in history
                    )
                    self.send(chat["id"], "\n".join(lines), self._admin_keyboard(telegram_id))
        elif command in ("/approve", "/reject"):
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            elif len(args) != 1:
                self.send(chat["id"], f"Usage: {command} <order-id>")
            else:
                try:
                    if command == "/approve":
                        result = self._admin_call(
                            telegram_id, "approve_order", args[0], telegram_id
                        )
                        self.send(
                            chat["id"],
                            f"Order {result.order_id} approved; provisioning queued.",
                            self._inline_keyboard(
                                [[("View Order", f"a:o:{result.order_id}"), ("📥 Orders", "a:n:orders")]]
                            ),
                        )
                    else:
                        result = self._admin_call(
                            telegram_id, "reject_order", args[0], telegram_id
                        )
                        self.send(
                            chat["id"],
                            f"Order {args[0]} {result}.",
                            self._inline_keyboard(
                                [[("📥 Pending Orders", "a:n:orders")]]
                            ),
                        )
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
        elif command == "/claim":
            if self.trial_ids and telegram_id not in self.trial_ids:
                self.send(chat["id"], "Free staging claims are limited to the configured test accounts.")
                return
            if self._free_claim_blocked_by_paid(telegram_id):
                self.send(chat["id"], "Your paid account is active; free claims are paused until it ends.")
                return
            try:
                result = self.service.claim(
                    telegram_id, first_name, username=username
                )
            except OutlineError:
                self.send(chat["id"], "Service temporarily unavailable. Your claim was not consumed. Try again later.")
                return
            if result.access_url:
                expiry = result.expires_at.strftime("%Y-%m-%d %H:%M UTC")
                amount = self.service.limit_bytes / 1024**2
                self.send(chat["id"], f"Your {amount:g} MiB Outline key:\n\n{result.access_url}\n\nExpires: {expiry}")
            elif result.next_claim_at:
                retry = result.next_claim_at.strftime("%Y-%m-%d %H:%M UTC")
                self.send(chat["id"], f"Already claimed. Come back after {retry}.")
            else:
                self.send(chat["id"], "Claims are unavailable for this account.")
        else:
            self._send_customer_fallback(chat["id"], telegram_id)

    def run(self) -> None:
        self._maintenance_stop.clear()
        maintenance_thread = threading.Thread(
            target=self._maintenance_loop,
            name="aurix-maintenance",
            daemon=True,
        )
        self._maintenance_thread = maintenance_thread
        maintenance_thread.start()
        try:
            while self.running:
                try:
                    updates = self.request(
                        "getUpdates",
                        {
                            "offset": self.offset,
                            "timeout": 20,
                            "allowed_updates": ["message", "callback_query"],
                        },
                    )
                    for update in updates:
                        self.offset = update["update_id"] + 1
                        if not self.service.database.mark_update_seen(update["update_id"]):
                            continue
                        started_at = time.perf_counter()
                        if "message" in update:
                            self.handle(update["message"])
                        elif "callback_query" in update:
                            self.handle_callback(update["callback_query"])
                        _latency_log(
                            "update_handler",
                            started_at,
                            update_id=update["update_id"],
                            kind="message" if "message" in update else "callback",
                        )
                except KeyboardInterrupt:
                    break
                except Exception as exc:
                    print(f"bot loop error: {type(exc).__name__}: {exc}", file=sys.stderr)
                    self._maintenance_stop.wait(5)
        finally:
            self.stop()
            maintenance_thread.join(timeout=5)
            self._maintenance_thread = None


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    api_url = os.environ.get("OUTLINE_API_URL", "")
    fingerprint = os.environ.get("OUTLINE_CERT_SHA256", "")
    access_url_key = os.environ.get("AURIX_ACCESS_URL_KEY", "")
    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", token),
            ("OUTLINE_API_URL", api_url),
            ("OUTLINE_CERT_SHA256", fingerprint),
            ("AURIX_ACCESS_URL_KEY", access_url_key),
        )
        if not value
    ]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))
    # Validate token with getMe before starting
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getMe",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise SystemExit("Telegram getMe failed: " + str(result))
        print(f"Bot authorized: @{result['result'].get('username', 'unknown')}")
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Telegram getMe failed: {exc}")
    commerce_database_url = os.environ.get("COMMERCE_DATABASE_URL", "").strip()
    if commerce_database_url:
        # The free Render profile stores both free entitlements and commerce
        # state in one hosted PostgreSQL database.  This avoids losing claim
        # timestamps and Telegram-update deduplication on an ephemeral web FS.
        database: Any = PostgresCommerceDatabase(commerce_database_url)
        commerce_database: Any = database
    else:
        database = Database(Path(os.environ.get("DATABASE_PATH", "data/bot.db")))
        commerce_database = CommerceDatabase(database.path)
    receipt_storage_required = os.environ.get(
        "RECEIPT_STORAGE_REQUIRED", "0"
    ).strip().lower() in ("1", "true", "yes", "on")
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if bool(supabase_url) != bool(supabase_service_key):
        raise SystemExit(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be configured together"
        )
    if supabase_url and supabase_service_key:
        try:
            receipt_storage: Any = SupabaseReceiptStorage(
                supabase_url,
                supabase_service_key,
                os.environ.get("SUPABASE_RECEIPTS_BUCKET", "payment-receipts"),
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    else:
        if receipt_storage_required:
            raise SystemExit(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for receipt storage"
            )
        receipt_storage = NullReceiptStorage()
    database.initialize()
    outline = OutlineClient(api_url, fingerprint)
    allow_text_payment = os.environ.get("ALLOW_TEXT_PAYMENT_REFERENCES", "0").lower() in ("1", "true", "yes")
    commerce = CommerceService(
        commerce_database,
        outline,
        access_url_key,
        allow_legacy_text_approval=allow_text_payment,
        receipt_storage=receipt_storage,
        receipt_storage_required=receipt_storage_required,
    )
    commerce.initialize()
    order_reconciliation = commerce.reconcile_duplicate_open_orders()
    if order_reconciliation["cancelled"]:
        print(
            f"Reconciled {order_reconciliation['cancelled']} empty duplicate open order(s)."
        )
    if order_reconciliation["manual_conflicts"]:
        print(
            "WARNING: duplicate open orders with payment evidence require manual review.",
            file=sys.stderr,
        )
    try:
        outline_info = outline.server_info()
    except OutlineError as exc:
        raise SystemExit(f"Outline readiness check failed: {exc}") from exc
    print(f"Outline connected: version {outline_info.get('version', 'unknown')}")
    def parse_ids(name: str) -> set[int]:
        try:
            return {int(value.strip()) for value in os.environ.get(name, "").split(",") if value.strip()}
        except ValueError as exc:
            raise SystemExit(f"{name} must be comma-separated Telegram numeric IDs") from exc

    admin_ids = parse_ids("ADMIN_TELEGRAM_IDS")
    command_scope_cleanup_ids = parse_ids("ADMIN_SCOPE_CLEANUP_IDS")
    trial_ids = parse_ids("TRIAL_TELEGRAM_IDS")
    try:
        maintenance_interval_seconds = float(
            os.environ.get(
                "AURIX_MAINTENANCE_INTERVAL_SECONDS",
                str(DEFAULT_MAINTENANCE_INTERVAL_SECONDS),
            )
        )
    except ValueError as exc:
        raise SystemExit("AURIX_MAINTENANCE_INTERVAL_SECONDS must be numeric") from exc
    if maintenance_interval_seconds < 1:
        raise SystemExit("AURIX_MAINTENANCE_INTERVAL_SECONDS must be at least 1")
    if not admin_ids:
        print(
            "WARNING: ADMIN_TELEGRAM_IDS is empty; paid receipt verification and approvals are unavailable.",
            file=sys.stderr,
        )
    receipt_llm_config = [
        os.environ.get("RECEIPT_LLM_BASE_URL", "").strip(),
        os.environ.get("RECEIPT_LLM_MODEL", "").strip(),
        os.environ.get("RECEIPT_LLM_API_KEY", "").strip(),
    ]
    if any(receipt_llm_config) and not all(receipt_llm_config):
        raise SystemExit(
            "RECEIPT_LLM_BASE_URL, RECEIPT_LLM_MODEL, and RECEIPT_LLM_API_KEY must be configured together"
        )
    if not all(receipt_llm_config):
        print(
            "WARNING: receipt vision extraction is disabled; screenshots require manual transaction entry.",
            file=sys.stderr,
        )
    bot = TelegramBot(
        token,
        ClaimService(database, outline, limit_bytes=PUBLIC_LIMIT_BYTES),
        commerce,
        admin_ids,
        trial_ids,
        allow_text_payment=allow_text_payment,
        maintenance_interval_seconds=maintenance_interval_seconds,
        command_scope_cleanup_ids=command_scope_cleanup_ids,
    )
    # Long polling cannot coexist with a previously configured webhook. Keep
    # queued updates while explicitly converging the bot into polling mode.
    try:
        bot.request("deleteWebhook", {"drop_pending_updates": False})
    except Exception as exc:
        raise SystemExit(f"Telegram webhook cleanup failed: {type(exc).__name__}") from exc
    try:
        bot.configure_commands()
    except Exception as exc:
        print(
            f"WARNING: Telegram command menu configuration failed: {type(exc).__name__}",
            file=sys.stderr,
        )
    signal.signal(signal.SIGTERM, lambda *_: bot.stop())
    try:
        bot.run()
    finally:
        close_database = getattr(commerce_database, "close", None)
        if callable(close_database):
            close_database()


if __name__ == "__main__":
    main()
