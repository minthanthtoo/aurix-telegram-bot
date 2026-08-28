#!/usr/bin/env python3
"""AuriX Telegram VPN commerce bot with free, trial, and paid entitlements."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import re
import signal
import sqlite3
import ssl
import sys
import time
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

UTC = timezone.utc
LEGACY_LIMIT_BYTES = 100 * 1024 * 1024
PUBLIC_LIMIT_BYTES = 300 * 1024 * 1024
LIMIT_BYTES = PUBLIC_LIMIT_BYTES
TRIAL_LIMIT_BYTES = 3 * 1024**3
CLAIM_PERIOD = timedelta(hours=24)
TRIAL_PERIOD = timedelta(days=30)


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


class OutlineError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ClaimResult:
    access_url: str | None = None
    expires_at: datetime | None = None
    next_claim_at: datetime | None = None


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

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
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    data_limit_bytes INTEGER NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('active', 'revoked', 'revoke_failed'))
                );
                CREATE INDEX IF NOT EXISTS keys_expiry
                    ON keys(status, expires_at);
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
                """
            )
            user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
            if "trial_claimed_at" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN trial_claimed_at TEXT")
            if "username" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN username TEXT")
            key_columns = {row[1] for row in connection.execute("PRAGMA table_info(keys)")}
            if "last_usage_bytes" not in key_columns:
                connection.execute("ALTER TABLE keys ADD COLUMN last_usage_bytes INTEGER")
            if "quota_reason" not in key_columns:
                connection.execute("ALTER TABLE keys ADD COLUMN quota_reason TEXT")

    def mark_update_seen(self, update_id: int) -> bool:
        """Durably dedupe Telegram updates across restarts."""
        with self.connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO telegram_updates (update_id, received_at) VALUES (?, ?)",
                    (int(update_id), datetime.now(UTC).isoformat()),
                )
            except sqlite3.IntegrityError:
                return False
        return True


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
        connection = http.client.HTTPSConnection(self.host, self.port, context=context, timeout=15)
        payload = json.dumps(body).encode() if body is not None else None
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
            raw = response.read()
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise OutlineError(f"Outline request failed: {exc}") from exc
        finally:
            connection.close()
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
            connection.execute("BEGIN IMMEDIATE")
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
                       (telegram_id, outline_key_id, created_at, expires_at, data_limit_bytes, status)
                       VALUES (?, ?, ?, ?, ?, 'active')""",
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
            connection.execute("BEGIN IMMEDIATE")
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
                       (telegram_id, outline_key_id, created_at, expires_at, data_limit_bytes, status)
                       VALUES (?, ?, ?, ?, ?, 'active')""",
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
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE keys SET status = 'revoke_failed', last_usage_bytes = COALESCE(?, last_usage_bytes),
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
            connection.execute("BEGIN IMMEDIATE")
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

    def enforce_quota(self, now: datetime | None = None) -> int:
        """Fail closed and revoke free/trial keys whose Outline metric hit its cap."""
        try:
            metrics = self.outline.transfer_metrics()
        except Exception:
            return 0
        by_key = metrics.get("bytesTransferredByUserId", {}) if isinstance(metrics, dict) else {}
        if not isinstance(by_key, dict):
            return 0
        current = (now or datetime.now(UTC)).astimezone(UTC)
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

    def user_usage(
        self, telegram_id: int, usage_by_key: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Return this user's free/trial key usage without exposing key secrets."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT outline_key_id, created_at, expires_at, data_limit_bytes,
                          status, last_usage_bytes, quota_reason
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
                    "status": "quota exhausted" if row["quota_reason"] == "quota" else row["status"],
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
    BUTTON_COMMANDS = {
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
        "🛠 Admin Panel": "/admin",
        "📥 Pending Orders": "/orders",
        "🧾 Receipt Review": "/receipts",
        "📈 Capacity": "/capacity",
        "🔎 Consistency": "/reconcile",
        "🚨 Enforcement": "/enforcement",
        "🏠 Customer Menu": "/start",
    }

    def __init__(
        self,
        token: str,
        service: ClaimService,
        commerce: CommerceService | None = None,
        admin_ids: set[int] | None = None,
        trial_ids: set[int] | None = None,
        receipt_extractor: Any | None = None,
        allow_text_payment: bool = True,
    ):
        self.api = f"https://api.telegram.org/bot{token}"
        self.service = service
        self.commerce = commerce
        self.admin_ids = admin_ids or set()
        self.trial_ids = trial_ids or set()
        self.receipt_extractor = receipt_extractor or OpenAICompatibleReceiptExtractor()
        self.allow_text_payment = bool(allow_text_payment)
        self.offset = 0
        self.running = True

    def request(self, method: str, payload: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            f"{self.api}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise RuntimeError("Telegram API request failed")
        return result["result"]

    def send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.request("sendMessage", payload)

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

    def _customer_keyboard(self, telegram_id: int) -> dict[str, Any]:
        rows = [
            ["🎁 Daily 300MB", "🚀 Monthly 3GB"],
            ["💎 Upgrade 50GB", "💠 Upgrade 100GB"],
            ["🔐 My VPN"],
            ["📊 Status", "📶 Usage"],
            ["🧾 My Orders", "💰 Wallet"],
            ["❓ Help"],
        ]
        if self._is_admin(telegram_id):
            rows[-1].append("🛠 Admin Panel")
        return self._reply_keyboard(rows)

    def _admin_keyboard(self) -> dict[str, Any]:
        return self._reply_keyboard(
            [
                ["📥 Pending Orders", "🧾 Receipt Review"],
                ["📈 Capacity", "🔎 Consistency"],
                ["🔁 Failed Jobs", "🚨 Enforcement"],
                ["💰 Wallet Ledger"],
                ["🏠 Customer Menu"],
            ]
        )

    def configure_commands(self) -> None:
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
        self.request(
            "setMyCommands",
            {"commands": customer_commands, "scope": {"type": "default"}},
        )
        admin_commands = customer_commands + [
            {"command": "admin", "description": "Open the admin panel"},
            {"command": "orders", "description": "List pending orders"},
            {"command": "receipts", "description": "List receipts to review"},
            {"command": "capacity", "description": "Show Outline capacity"},
            {"command": "reconcile", "description": "Check commerce invariants"},
            {"command": "enforcement", "description": "Review key terminations"},
            {"command": "failed", "description": "Review failed worker jobs"},
            {"command": "retry", "description": "Retry a failed worker job"},
            {"command": "ledger", "description": "View a wallet ledger"},
            {"command": "refund", "description": "Refund a verified order"},
            {"command": "receipt", "description": "Open receipt evidence by ID"},
            {"command": "rejectreceipt", "description": "Reject receipt evidence"},
            {"command": "verify", "description": "Verify receipt transaction and amount"},
            {"command": "approve", "description": "Approve a verified order"},
            {"command": "reject", "description": "Reject an order"},
        ]
        for admin_id in self.admin_ids:
            self.request(
                "setMyCommands",
                {
                    "commands": admin_commands,
                    "scope": {"type": "chat", "chat_id": admin_id},
                },
            )

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
            )
        except (CommerceError, RuntimeError, urllib.error.URLError) as exc:
            self.send(chat_id, str(exc) or "Receipt could not be recorded. Try again later.")
            return
        if result.get("transaction_id"):
            self.send(chat_id, "Receipt received. Transaction ID extracted and queued for staff verification.")
        else:
            self.send(chat_id, "Receipt received for manual review. No payment is activated from the image alone.")
        for admin_id in self.admin_ids:
            try:
                self.send(admin_id, f"Receipt submitted for order {order_id} by Telegram user {telegram_id}; verify against the receiving account.")
            except Exception as exc:
                print(f"admin receipt notification error: {type(exc).__name__}", file=sys.stderr)

    def _is_admin(self, telegram_id: int) -> bool:
        return telegram_id in self.admin_ids

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
                rows.append([("🔁 Retry Setup", f"a:p:{order_id}")])
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
            if order.get("status") in ("awaiting_payment", "payment_submitted"):
                rows.append([("❌ Reject…", f"a:x:{order_id}")])
            rows.append(
                [
                    ("🔄 Refresh", f"a:o:{order_id}"),
                    ("📥 Orders", "n:adminorders"),
                ]
            )
        else:
            if order.get("status") == "awaiting_payment":
                rows.append(
                    [
                        ("📷 Send Receipt", f"o:r:{order_id}"),
                        ("💰 Pay Wallet", f"o:w:{order_id}"),
                    ]
                )
                if not order.get("payment_status") and not order.get("receipt_status"):
                    rows.append([("🗑 Cancel Order", f"o:c:{order_id}")])
            elif order.get("stage") == "review_pending" or order.get("receipt_status") == "rejected":
                rows.append(
                    [("📷 Send Replacement Receipt", f"o:r:{order_id}")]
                )
            if order.get("status") == "approved":
                rows.append([("🔐 My VPN", "n:myvpn")])
            rows.append(
                [
                    ("🔄 Refresh", f"o:v:{order_id}"),
                    ("🧾 My Orders", "n:myorders"),
                ]
            )
        return self._inline_keyboard(rows)

    def _send_order_detail(
        self, chat_id: int, telegram_id: int, order_id: str
    ) -> None:
        if self.commerce is None:
            self.send(chat_id, "Order tracking is not configured.")
            return
        is_admin = self._is_admin(telegram_id)
        order = self.commerce.order_detail(order_id, telegram_id, is_admin=is_admin)
        if order is None:
            self.send(chat_id, "Order not found.")
            return
        self.send(
            chat_id,
            self._order_detail_text(order),
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
        ):
            return
        self.request("answerCallbackQuery", {"callback_query_id": query_id})
        telegram_id = int(user["id"])
        chat_id = int(chat["id"])
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
        navigation = {
            "n:myorders": "/myorders",
            "n:adminorders": "/orders",
            "n:myvpn": "/myvpn",
            "n:plans": "/plans",
            "n:wallet": "/wallet",
            "n:usage": "/usage",
        }
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
                synthetic["text"] = "/renew"
            elif action == "x":
                synthetic["text"] = f"/replace {entity_id}"
            else:
                self.send(chat_id, "This plan action is no longer valid.")
                return
            self.handle(synthetic)
        elif scope == "a":
            if not self._is_admin(telegram_id):
                self.send(chat_id, "Admin access required.")
                return
            if action == "o":
                self._send_order_detail(chat_id, telegram_id, entity_id)
            elif action == "p":
                synthetic["text"] = f"/retry {entity_id}"
                self.handle(synthetic)
            elif action == "l":
                synthetic["text"] = f"/ledger {entity_id}"
                self.handle(synthetic)
            elif action == "f":
                self.send(
                    chat_id,
                    f"Refund order {entity_id}? This credits the customer wallet and revokes paid access.",
                    self._inline_keyboard(
                        [[("Confirm Refund", f"a:z:{entity_id}"), ("Keep Order", f"a:o:{entity_id}")]]
                    ),
                )
            elif action == "z":
                synthetic["text"] = f"/refund {entity_id}"
                self.handle(synthetic)
            elif action == "r":
                synthetic["text"] = f"/receipt {entity_id}"
                self.handle(synthetic)
            elif action == "a":
                synthetic["text"] = f"/approve {entity_id}"
                self.handle(synthetic)
            elif action == "x":
                self.send(
                    chat_id,
                    f"Reject order {entity_id}? This closes the order and notifies the customer.",
                    self._inline_keyboard(
                        [[("Confirm Reject", f"a:c:{entity_id}"), ("Keep Order", f"a:o:{entity_id}")]]
                    ),
                )
            elif action == "q":
                self.send(
                    chat_id,
                    f"Reject receipt {entity_id}? The order stays open for a replacement screenshot.",
                    self._inline_keyboard(
                        [[("Confirm Reject Receipt", f"a:y:{entity_id}"), ("Keep Receipt", f"a:r:{entity_id}")]]
                    ),
                )
            elif action == "y":
                synthetic["text"] = f"/rejectreceipt {entity_id}"
                self.handle(synthetic)
            elif action == "c":
                synthetic["text"] = f"/reject {entity_id}"
                self.handle(synthetic)
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
        subscription = self.commerce.user_vpn(telegram_id)
        if subscription is None:
            self.send(chat_id, "No subscription found. Use /plans to see available plans.")
            return
        text = (
            f"Status: {subscription['status']}\n"
            f"Plan: {subscription['plan_code']}\n"
            f"Expires: {subscription['expires_at']}"
        )
        if include_key:
            if subscription.get("access_url") and subscription.get("key_status") == "active":
                text += f"\n\nYour Outline key:\n{subscription['access_url']}"
            elif subscription["status"] == "pending":
                text += "\n\nProvisioning is pending. Please wait for the key delivery message."
            else:
                text += "\n\nNo active key is available."
        actions = [[("📶 Usage", "n:usage"), ("🧾 My Orders", "n:myorders")]]
        if subscription.get("status") == "active":
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

    def handle(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        if (
            not isinstance(chat, dict)
            or not isinstance(user, dict)
            or chat.get("type") != "private"
            or not isinstance(chat.get("id"), int)
            or not isinstance(user.get("id"), int)
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
        text = self.BUTTON_COMMANDS.get(text.strip(), text)
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]
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
                if self.commerce is not None:
                    current_paid = self.commerce.user_vpn(telegram_id)
                    if current_paid and current_paid.get("status") in ("active", "pending"):
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
            access = "enabled" if self._is_admin(telegram_id) else "not enabled"
            self.send(
                chat["id"],
                f"Your Telegram ID: {telegram_id}\nAdmin access: {access}",
                self._customer_keyboard(telegram_id),
            )
        elif command == "/admin":
            if not self._is_admin(telegram_id):
                self.send(
                    chat["id"],
                    f"Admin access is not enabled for this account.\n\n"
                    f"Your Telegram ID is {telegram_id}. Add it to ADMIN_TELEGRAM_IDS and restart the bot.",
                    self._customer_keyboard(telegram_id),
                )
            else:
                summary = ""
                if self.commerce is not None:
                    try:
                        report = self.commerce.consistency_report()
                        summary = (
                            f"\n\nQueue: {report.get('pending_receipts', 0)} receipt(s) pending · "
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
                    "Use Failed Jobs to retry a reviewed Outline failure, Wallet Ledger "
                    "to inspect funds, and Consistency before taking payment decisions."
                    + summary,
                    self._admin_keyboard(),
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
                self._send_order_detail(chat["id"], telegram_id, args[0])
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
                        self.send(
                            chat["id"],
                            f"You already have an open order for {order.plan.name}. Choose whether to replace that untouched order with {args[0]}.",
                            self._inline_keyboard(
                                [[("Replace Open Order", f"p:x:{args[0]}"), ("Keep Existing", f"o:v:{order.order_id}")]]
                            ),
                        )
                        return
                    heading = "Order created" if order.created else "Existing open order"
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
                subscription = self.commerce.user_vpn(telegram_id)
                if subscription is None:
                    self.send(chat["id"], "No previous plan found. Use /plans and /buy first.")
                else:
                    try:
                        order = self.commerce.create_order(
                            telegram_id,
                            first_name,
                            subscription["plan_code"],
                            username=username,
                        )
                    except CommerceError as exc:
                        self.send(chat["id"], str(exc))
                    else:
                        heading = "Renewal order created" if order.created else "Existing open order"
                        self.send(chat["id"], f"{heading}: {order.order_id}\nSend /paid {order.order_id} then the receipt screenshot after payment.")
        elif command == "/trial":
            if self.commerce is not None:
                current_paid = self.commerce.user_vpn(telegram_id)
                if current_paid and current_paid.get("status") in ("active", "pending"):
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
            if self.commerce is None or len(args) != 1:
                self.send(chat["id"], "Usage: /replace <plan-code>")
            else:
                try:
                    order = self.commerce.replace_open_order(
                        telegram_id, first_name, args[0], username=username
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
                self.send(chat["id"], "Admin access required.")
            elif self.commerce is None or len(args) != 1:
                self.send(chat["id"], "Usage: /receipt <evidence-id> (admin)")
            else:
                receipt = self.commerce.get_receipt(args[0])
                if receipt is None:
                    self.send(chat["id"], "Receipt evidence not found.")
                else:
                    extracted = receipt.get("extraction") or {}
                    caption = (
                        f"order:{receipt['order_id']} tg:{receipt['telegram_id']} "
                        f"amount:{receipt['amount_minor']:,} {receipt['currency']} "
                        f"tx:{extracted.get('transaction_id') or '-'}\n"
                        "Verify against the receiving account, then use "
                        f"/verify {args[0]} <transaction-id> <amount>."
                    )
                    self.send_photo(
                        chat["id"],
                        receipt["telegram_file_id"],
                        caption,
                        self._inline_keyboard(
                            [
                                [("View Order", f"a:o:{receipt['order_id']}")],
                                [("🛑 Reject Receipt", f"a:q:{receipt['id']}")],
                            ]
                        ),
                    )
        elif command == "/verify":
            if not self._is_admin(telegram_id):
                self.send(chat["id"], "Admin access required.")
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            elif len(args) != 3:
                self.send(chat["id"], "Usage: /verify <evidence-id> <transaction-id> <amount>")
            else:
                try:
                    amount = int(args[2].replace(",", ""))
                    order_id = self.commerce.verify_receipt(
                        args[0], telegram_id, args[1], amount
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
                self.send(chat["id"], "Admin access required.")
            elif self.commerce is None or not args:
                self.send(chat["id"], "Usage: /rejectreceipt <evidence-id> [reason]")
            else:
                try:
                    order_id = self.commerce.reject_receipt(
                        args[0], telegram_id, " ".join(args[1:]) or "Receipt rejected; please submit a clearer screenshot."
                    )
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(
                        chat["id"],
                        f"Receipt rejected for order {order_id}; the customer can submit a replacement.",
                        self._inline_keyboard([[("📥 Orders", "n:adminorders")]]),
                    )
        elif command in ("/orders", "/receipts"):
            if not self._is_admin(telegram_id):
                self.send(chat["id"], "Admin access required.")
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            else:
                if command == "/receipts":
                    receipts = self.commerce.list_pending_receipts()
                    if not receipts:
                        self.send(chat["id"], "No unreviewed receipts.")
                    else:
                        lines = []
                        for row in receipts:
                            extraction = row.get("extraction") or {}
                            lines.append(
                                f"{row['id']} | order:{row['order_id']} | tg:{row['telegram_id']} | "
                                f"{row['amount_minor']:,} {row['currency']} | tx:{extraction.get('transaction_id') or '-'} | "
                                f"confidence:{extraction.get('confidence', 0)}"
                            )
                        self.send(
                            chat["id"],
                            "\n".join(lines),
                            self._inline_keyboard(
                                [[(f"Open {str(row['id'])[:8]}", f"a:r:{row['id']}")]
                                 for row in receipts]
                            ),
                        )
                    return
                orders = self.commerce.list_pending_orders()
                if not orders:
                    self.send(chat["id"], "No pending orders.")
                else:
                    self.send(
                        chat["id"],
                        "\n".join(
                            f"{row['id']} | tg:{row['telegram_id']} | {row['plan_code']} | {row['amount_minor']:,} {row['currency']} | stage:{row.get('stage', row['status'])} | receipt:{row.get('receipt_status') or '-'} | ref:{row['provider_reference'] or '-'}"
                            for row in orders
                        ),
                        self._inline_keyboard(
                            [[(f"Review {str(row['id'])[:8]}", f"a:o:{row['id']}")]
                             for row in orders]
                        ),
                    )
        elif command == "/capacity":
            if not self._is_admin(telegram_id):
                self.send(chat["id"], "Admin access required.")
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            else:
                try:
                    snapshot = self.commerce.capacity_snapshot()
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
                self.send(chat["id"], "Admin access required.")
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            else:
                report = self.commerce.consistency_report()
                issue_keys = {
                    "duplicate_open_orders",
                    "approved_missing_subscription",
                    "approved_missing_provision_job",
                    "stale_receipts",
                    "failed_jobs",
                    "dead_notifications",
                    "wallet_balance_mismatches",
                }
                healthy = all(report.get(key, 0) == 0 for key in issue_keys)
                lines = ["AuriX consistency scan", "Status: " + ("OK" if healthy else "ACTION REQUIRED")]
                lines.extend(f"{key.replace('_', ' ').title()}: {value}" for key, value in report.items())
                self.send(chat["id"], "\n".join(lines), self._admin_keyboard())
        elif command == "/enforcement":
            if not self._is_admin(telegram_id):
                self.send(chat["id"], "Admin access required.")
            else:
                events = self.service.termination_summary()
                if not events:
                    self.send(chat["id"], "No free/trial termination events recorded.", self._admin_keyboard())
                else:
                    lines = ["Recent free/trial enforcement"]
                    lines.extend(
                        f"tg:{row['telegram_id']} | key:{row['outline_key_id']} | {row['reason']} | "
                        f"{row['remote_state']} | attempts:{row['delete_attempts']} | {row['detected_at']}"
                        for row in events
                    )
                    self.send(chat["id"], "\n".join(lines), self._admin_keyboard())
        elif command == "/failed":
            if not self._is_admin(telegram_id):
                self.send(chat["id"], "Admin access required.")
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            else:
                jobs = self.commerce.failed_jobs()
                if not jobs:
                    self.send(chat["id"], "No terminal worker failures.", self._admin_keyboard())
                else:
                    lines = ["Failed worker jobs"]
                    rows = []
                    for job in jobs:
                        lines.append(
                            f"{job['operation']} | order:{job['order_id']} | tg:{job['telegram_id']} | "
                            f"attempts:{job['attempts']} | {job['last_error'] or '-'}"
                        )
                        rows.append([
                            ("Open Order", f"a:o:{job['order_id']}"),
                            ("Retry", f"a:p:{job['order_id']}"),
                        ])
                    self.send(chat["id"], "\n".join(lines), self._inline_keyboard(rows))
        elif command == "/retry":
            if not self._is_admin(telegram_id):
                self.send(chat["id"], "Admin access required.")
            elif self.commerce is None or len(args) != 1:
                self.send(chat["id"], "Usage: /retry <order-id>")
            else:
                try:
                    operation = self.commerce.retry_failed_job(args[0], telegram_id)
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(
                        chat["id"],
                        f"{operation.title()} job requeued for order {args[0]}.",
                        self._inline_keyboard([[ ("🔄 Refresh Order", f"a:o:{args[0]}") ]]),
                    )
        elif command == "/refund":
            if not self._is_admin(telegram_id):
                self.send(chat["id"], "Admin access required.")
            elif self.commerce is None or not args:
                self.send(chat["id"], "Usage: /refund <order-id> [reason]")
            else:
                try:
                    result = self.commerce.refund_order(
                        args[0], telegram_id, " ".join(args[1:]) or "refunded by admin"
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
                self.send(chat["id"], "Admin access required.")
            elif self.commerce is None or len(args) != 1:
                self.send(chat["id"], "Usage: /ledger <telegram-id>")
            else:
                try:
                    customer_id = int(args[0])
                    balance = self.commerce.wallet_balance(customer_id)
                    history = self.commerce.wallet_history(customer_id, limit=20)
                except (ValueError, CommerceError) as exc:
                    self.send(chat["id"], str(exc) or "Telegram ID must be numeric.")
                else:
                    lines = [f"Wallet ledger · tg:{customer_id}", f"Balance: {balance:,} MMK"]
                    lines.extend(
                        f"{item['created_at']} · {item['kind']} {int(item['amount_minor']):,} {item['currency']} · {item['reference_id']}"
                        for item in history
                    )
                    self.send(chat["id"], "\n".join(lines), self._admin_keyboard())
        elif command in ("/approve", "/reject"):
            if not self._is_admin(telegram_id):
                self.send(chat["id"], "Admin access required.")
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            elif len(args) != 1:
                self.send(chat["id"], f"Usage: {command} <order-id>")
            else:
                try:
                    if command == "/approve":
                        result = self.commerce.approve_order(args[0], telegram_id)
                        self.send(
                            chat["id"],
                            f"Order {result.order_id} approved; provisioning queued.",
                            self._inline_keyboard(
                                [[("View Order", f"a:o:{result.order_id}"), ("📥 Orders", "n:adminorders")]]
                            ),
                        )
                    else:
                        result = self.commerce.reject_order(args[0], telegram_id)
                        self.send(
                            chat["id"],
                            f"Order {args[0]} {result}.",
                            self._inline_keyboard(
                                [[("📥 Pending Orders", "n:adminorders")]]
                            ),
                        )
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
        elif command == "/claim":
            if self.trial_ids and telegram_id not in self.trial_ids:
                self.send(chat["id"], "Free staging claims are limited to the configured test accounts.")
                return
            if self.commerce is not None:
                current_paid = self.commerce.user_vpn(telegram_id)
                if current_paid and current_paid.get("status") in ("active", "pending"):
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
            self.send(chat["id"], "Use the menu for daily 300 MiB, monthly 3 GiB, or paid upgrades.")

    def run(self) -> None:
        while self.running:
            try:
                # Quota first preserves the more informative cause when a key is
                # both over quota and past its wall-clock entitlement.
                self.service.enforce_quota()
                self.service.revoke_expired()
                self._send_termination_notices()
                if self.commerce is not None:
                    self.commerce.enforce_quotas()
                    self.commerce.expire_and_process()
                    self._send_pending_notifications()
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
                    if "message" in update:
                        self.handle(update["message"])
                    elif "callback_query" in update:
                        self.handle_callback(update["callback_query"])
            except KeyboardInterrupt:
                break
            except Exception as exc:
                print(f"bot loop error: {type(exc).__name__}: {exc}", file=sys.stderr)
                time.sleep(5)


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
    database = Database(Path(os.environ.get("DATABASE_PATH", "data/bot.db")))
    database.initialize()
    outline = OutlineClient(api_url, fingerprint)
    commerce_database_url = os.environ.get("COMMERCE_DATABASE_URL", "").strip()
    commerce_database = (
        PostgresCommerceDatabase(commerce_database_url)
        if commerce_database_url
        else CommerceDatabase(database.path)
    )
    allow_text_payment = os.environ.get("ALLOW_TEXT_PAYMENT_REFERENCES", "0").lower() in ("1", "true", "yes")
    commerce = CommerceService(
        commerce_database,
        outline,
        access_url_key,
        allow_legacy_text_approval=allow_text_payment,
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
    trial_ids = parse_ids("TRIAL_TELEGRAM_IDS")
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
    signal.signal(signal.SIGTERM, lambda *_: setattr(bot, "running", False))
    bot.run()


if __name__ == "__main__":
    main()
