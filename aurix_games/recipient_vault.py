"""Encrypted private storage for recipient fulfillment inputs.

Customer orders intentionally keep only an opaque recipient fingerprint.  A
later fulfillment worker still needs the exact provider fields, so this vault
stores those fields separately under a runtime Fernet key.  The vault is not a
customer API and never returns data to the browser.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import stat
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken
from persistence import open_sqlite_connection


UTC = timezone.utc


class RecipientVaultError(RuntimeError):
    """A private recipient-vault operation failed safely."""


class RecipientVaultInputError(ValueError):
    """Recipient-vault input is malformed or unsafe."""


class RecipientVault:
    """Mode-0600 SQLite vault with Fernet-encrypted payloads."""

    def __init__(self, path: str | Path, *, encryption_key: bytes | str) -> None:
        self.path = Path(path)
        try:
            raw_key = encryption_key.encode("ascii") if isinstance(encryption_key, str) else encryption_key
            if not isinstance(raw_key, bytes):
                raise TypeError
            self._fernet = Fernet(raw_key)
        except (TypeError, ValueError) as exc:
            raise RecipientVaultInputError("recipient vault key must be a Fernet key") from exc

    def initialize(self) -> None:
        if self.path.is_symlink():
            raise RecipientVaultInputError("recipient vault database must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077:
            raise RecipientVaultInputError("recipient vault directory must be private")
        try:
            with open_sqlite_connection(self.path, timeout_seconds=5) as connection:
                connection.execute("PRAGMA journal_mode = DELETE")
                connection.execute("PRAGMA synchronous = FULL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS game_recipient_vault (
                        order_id TEXT PRIMARY KEY,
                        customer_id TEXT NOT NULL,
                        payload_ciphertext TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TRIGGER IF NOT EXISTS game_recipient_vault_immutable
                        BEFORE UPDATE ON game_recipient_vault
                    BEGIN
                        SELECT RAISE(ABORT, 'recipient vault rows are immutable');
                    END;
                    """
                )
        except sqlite3.Error as exc:
            raise RecipientVaultError("recipient vault initialization failed") from exc
        self.path.chmod(0o600)

    def put(
        self,
        *,
        order_id: str,
        customer_id: str,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> None:
        order = _id(order_id, "order_id")
        customer = _id(customer_id, "customer_id")
        current = _time(now)
        encoded = _encode_payload(payload)
        ciphertext = self._fernet.encrypt(encoded.encode("utf-8")).decode("ascii")
        self.initialize()
        try:
            with open_sqlite_connection(self.path, timeout_seconds=5) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT customer_id, payload_ciphertext FROM game_recipient_vault WHERE order_id = ?",
                    (order,),
                ).fetchone()
                if existing is not None:
                    if existing[0] != customer:
                        raise RecipientVaultInputError("recipient vault owner mismatch")
                    try:
                        existing_payload = self._decrypt(existing[1])
                    except RecipientVaultError:
                        raise
                    if _encode_payload(existing_payload) != encoded:
                        raise RecipientVaultInputError("order already has different recipient fields")
                    return
                connection.execute(
                    """INSERT INTO game_recipient_vault
                       (order_id, customer_id, payload_ciphertext, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (order, customer, ciphertext, current.isoformat(), current.isoformat()),
                )
        except RecipientVaultInputError:
            raise
        except sqlite3.IntegrityError as exc:
            raise RecipientVaultError("recipient vault insert failed") from exc
        except sqlite3.Error as exc:
            raise RecipientVaultError("recipient vault write failed") from exc

    def get(self, *, order_id: str, customer_id: str) -> dict[str, Any]:
        order = _id(order_id, "order_id")
        customer = _id(customer_id, "customer_id")
        self.initialize()
        try:
            with open_sqlite_connection(self.path, timeout_seconds=5) as connection:
                row = connection.execute(
                    "SELECT customer_id, payload_ciphertext FROM game_recipient_vault WHERE order_id = ?",
                    (order,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise RecipientVaultError("recipient vault read failed") from exc
        if row is None or row[0] != customer:
            raise RecipientVaultError("recipient vault record was not found")
        return self._decrypt(row[1])

    def _decrypt(self, ciphertext: object) -> dict[str, Any]:
        try:
            if not isinstance(ciphertext, str):
                raise TypeError
            decoded = self._fernet.decrypt(ciphertext.encode("ascii"), ttl=None)
            payload = json.loads(decoded)
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RecipientVaultError("recipient vault payload could not be decrypted") from exc
        if not isinstance(payload, dict):
            raise RecipientVaultError("recipient vault payload is malformed")
        return payload


def _encode_payload(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping) or not value:
        raise RecipientVaultInputError("recipient payload must be a non-empty object")
    try:
        encoded = json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise RecipientVaultInputError("recipient payload must be finite JSON") from exc
    if len(encoded) > 16 * 1024:
        raise RecipientVaultInputError("recipient payload is too large")
    return encoded


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise RecipientVaultInputError(f"{label} is invalid")
    if any(char in value for char in "\r\n"):
        raise RecipientVaultInputError(f"{label} is invalid")
    return value.strip()


def _time(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RecipientVaultInputError("time must be timezone-aware")
    return value.astimezone(UTC)


__all__ = ["RecipientVault", "RecipientVaultError", "RecipientVaultInputError"]
