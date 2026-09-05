"""Persistence boundary for accounts, devices, and pairing sessions."""

from __future__ import annotations

import secrets
import uuid
from typing import Any

from identity_support import IdentityError, _token_hash


class IdentityAccountsRepository:
    """SQL operations for account and device lifecycle state."""

    @staticmethod
    def account_id(connection: Any, telegram_id: int) -> str | None:
        row = connection.execute(
            """SELECT account_id FROM account_identities
                WHERE identity_type = 'telegram' AND identity_value = ?""",
            (str(int(telegram_id)),),
        ).fetchone()
        return str(row["account_id"]) if row is not None else None

    @classmethod
    def ensure_account(cls, connection: Any, telegram_id: int, timestamp: str) -> str:
        account_id = cls.account_id(connection, telegram_id)
        if account_id is None:
            candidate = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO accounts (account_id, created_at, updated_at) VALUES (?, ?, ?)",
                (candidate, timestamp, timestamp),
            )
            connection.execute(
                """INSERT INTO device_revocation_epochs (account_id, epoch, updated_at)
                   VALUES (?, 0, ?)""",
                (candidate, timestamp),
            )
            connection.execute(
                """INSERT INTO account_identities
                   (account_id, identity_type, identity_value, verified_at, created_at)
                   VALUES (?, 'telegram', ?, ?, ?)
                   ON CONFLICT(identity_type, identity_value) DO NOTHING""",
                (candidate, str(int(telegram_id)), timestamp, timestamp),
            )
            account_id = cls.account_id(connection, telegram_id)
            if account_id is None:
                raise IdentityError("account identity could not be created")
            if account_id != candidate:
                connection.execute(
                    "DELETE FROM device_revocation_epochs WHERE account_id = ?", (candidate,)
                )
                connection.execute("DELETE FROM accounts WHERE account_id = ?", (candidate,))
        connection.execute(
            """INSERT INTO accounts (account_id, created_at, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(account_id) DO UPDATE SET updated_at = excluded.updated_at""",
            (account_id, timestamp, timestamp),
        )
        connection.execute(
            """INSERT INTO account_identities
               (account_id, identity_type, identity_value, verified_at, created_at)
               VALUES (?, 'telegram', ?, ?, ?)
               ON CONFLICT(identity_type, identity_value) DO UPDATE SET
                 account_id = excluded.account_id, verified_at = excluded.verified_at""",
            (account_id, str(int(telegram_id)), timestamp, timestamp),
        )
        connection.execute(
            """INSERT INTO device_revocation_epochs (account_id, epoch, updated_at)
               VALUES (?, 0, ?) ON CONFLICT(account_id) DO NOTHING""",
            (account_id, timestamp),
        )
        return account_id

    @staticmethod
    def user_ids(connection: Any) -> list[Any]:
        return connection.execute("SELECT telegram_id FROM users ORDER BY telegram_id").fetchall()

    @staticmethod
    def snapshot(connection: Any, telegram_id: int) -> Any:
        return connection.execute(
            """SELECT a.account_id, a.status, a.created_at, a.updated_at,
                      e.epoch AS revocation_epoch
                 FROM accounts a
                 JOIN account_identities i ON i.account_id = a.account_id
                    AND i.identity_type = 'telegram'
                 LEFT JOIN device_revocation_epochs e ON e.account_id = a.account_id
                WHERE i.identity_value = ?""",
            (str(int(telegram_id)),),
        ).fetchone()

    @staticmethod
    def insert_pairing_token(
        connection: Any,
        *,
        token_hash: str,
        account_id: str,
        telegram_id: int,
        expires_at: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO pairing_tokens
               (token_hash, account_id, requested_by, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (token_hash, account_id, telegram_id, expires_at, created_at),
        )

    @classmethod
    def consume_pairing_token(
        cls,
        connection: Any,
        *,
        token: str,
        public_key: str,
        label: str,
        timestamp: str,
    ) -> dict[str, Any]:
        token_hash = _token_hash(token)
        row = connection.execute(
            """SELECT * FROM pairing_tokens
                WHERE token_hash = ? AND status = 'pending' AND expires_at > ?""",
            (token_hash, timestamp),
        ).fetchone()
        if row is None:
            raise IdentityError("pairing token is invalid, expired, or already used")
        account_id = str(row["account_id"])
        if connection.execute(
            "SELECT device_id FROM devices WHERE public_key = ?", (public_key,)
        ).fetchone() is not None:
            raise IdentityError("device public key is already enrolled")
        device_id = f"device-{secrets.token_hex(16)}"
        connection.execute(
            """UPDATE pairing_tokens SET status = 'consumed', consumed_at = ?
                WHERE token_hash = ? AND status = 'pending'""",
            (timestamp, token_hash),
        )
        connection.execute(
            """INSERT INTO devices
               (device_id, account_id, public_key, label, status, created_at)
               VALUES (?, ?, ?, ?, 'active', ?)""",
            (device_id, account_id, public_key, str(label)[:128], timestamp),
        )
        return {"device_id": device_id, "account_id": account_id, "status": "active"}

    @classmethod
    def revoke_device(
        cls, connection: Any, telegram_id: int, device_id: str, timestamp: str
    ) -> bool:
        account_id = cls.account_id(connection, telegram_id)
        if account_id is None:
            return False
        updated = connection.execute(
            """UPDATE devices SET status = 'revoked', revoked_at = ?
                WHERE device_id = ? AND account_id = ? AND status != 'revoked'""",
            (timestamp, str(device_id), account_id),
        )
        if not int(getattr(updated, "rowcount", 0) or 0):
            return False
        connection.execute(
            """UPDATE device_revocation_epochs SET epoch = epoch + 1, updated_at = ?
                WHERE account_id = ?""",
            (timestamp, account_id),
        )
        connection.execute(
            "UPDATE device_sessions SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
            (timestamp, str(device_id)),
        )
        return True

    @staticmethod
    def device_auth(connection: Any, device_id: str) -> Any:
        return connection.execute(
            """SELECT d.device_id, d.account_id, d.public_key, d.status,
                      a.status AS account_status, e.epoch AS revocation_epoch
                 FROM devices d JOIN accounts a ON a.account_id = d.account_id
                 LEFT JOIN device_revocation_epochs e ON e.account_id = a.account_id
                WHERE d.device_id = ?""",
            (str(device_id),),
        ).fetchone()

    @staticmethod
    def touch_device(connection: Any, device_id: str, timestamp: str) -> bool:
        updated = connection.execute(
            "UPDATE devices SET last_seen_at = ? WHERE device_id = ? AND status = 'active'",
            (timestamp, str(device_id)),
        )
        return int(getattr(updated, "rowcount", 0) or 0) == 1

    @staticmethod
    def create_session(
        connection: Any,
        *,
        session_id: str,
        device_id: str,
        manifest_version: int,
        timestamp: str,
        expires_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO device_sessions
               (session_id, device_id, manifest_version, created_at, last_seen_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, str(device_id), int(manifest_version), timestamp, timestamp, expires_at),
        )
