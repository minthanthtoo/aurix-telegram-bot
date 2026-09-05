"""Account, device, and pairing lifecycle handlers."""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from typing import Any

from identity_support import IdentityError, _now_text, _parse_time, _token_hash


class IdentityAccountsMixin:
    @staticmethod
    def _account_id_in_connection(connection: Any, telegram_id: int) -> str | None:
        row = connection.execute(
            """SELECT account_id FROM account_identities
                WHERE identity_type = 'telegram' AND identity_value = ?""",
            (str(int(telegram_id)),),
        ).fetchone()
        return str(row["account_id"]) if row is not None else None

    def ensure_account(self, telegram_id: int, *, now: str | None = None) -> str:
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            account_id = self._account_id_in_connection(connection, telegram_id)
            if account_id is None:
                candidate = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO accounts (account_id, created_at, updated_at)
                       VALUES (?, ?, ?)""",
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
                account_id = self._account_id_in_connection(connection, telegram_id)
                if account_id is None:
                    raise IdentityError("account identity could not be created")
                if account_id != candidate:
                    # Another writer won the identity race.  The candidate has
                    # no dependent state yet and can be safely removed.
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
                   VALUES (?, 0, ?)
                   ON CONFLICT(account_id) DO NOTHING""",
                (account_id, timestamp),
            )
        return account_id

    def sync_existing_users(self, *, now: str | None = None) -> int:
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            rows = connection.execute("SELECT telegram_id FROM users ORDER BY telegram_id").fetchall()
        for row in rows:
            self.ensure_account(int(row["telegram_id"]), now=timestamp)
        return len(rows)

    def account_snapshot(self, telegram_id: int) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT a.account_id, a.status, a.created_at, a.updated_at,
                          e.epoch AS revocation_epoch
                     FROM accounts a
                     JOIN account_identities i
                       ON i.account_id = a.account_id
                      AND i.identity_type = 'telegram'
                     LEFT JOIN device_revocation_epochs e ON e.account_id = a.account_id
                    WHERE i.identity_value = ?""",
                (str(int(telegram_id)),),
            ).fetchone()
        return dict(row) if row is not None else None

    def create_pairing_token(
        self, telegram_id: int, *, ttl_seconds: int = 300, now: str | None = None
    ) -> str:
        if not 30 <= int(ttl_seconds) <= 900:
            raise IdentityError("pairing token TTL is outside the allowed range")
        timestamp = str(now or _now_text())
        account_id = self.ensure_account(telegram_id, now=timestamp)
        token = secrets.token_urlsafe(32)
        expires = (_parse_time(timestamp) + timedelta(seconds=int(ttl_seconds))).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """INSERT INTO pairing_tokens
                   (token_hash, account_id, requested_by, expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (_token_hash(token), account_id, int(telegram_id), expires, timestamp),
            )
        return token

    def consume_pairing_token(
        self,
        token: str,
        public_key: str,
        *,
        label: str = "",
        now: str | None = None,
    ) -> dict[str, Any]:
        token = str(token or "").strip()
        public_key = str(public_key or "").strip()
        if not 20 <= len(token) <= 256 or not 16 <= len(public_key) <= 4096:
            raise IdentityError("pairing token or public key is invalid")
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                """SELECT * FROM pairing_tokens
                    WHERE token_hash = ? AND status = 'pending' AND expires_at > ?""",
                (_token_hash(token), timestamp),
            ).fetchone()
            if row is None:
                raise IdentityError("pairing token is invalid, expired, or already used")
            account_id = str(row["account_id"])
            existing = connection.execute(
                "SELECT device_id FROM devices WHERE public_key = ?", (public_key,)
            ).fetchone()
            if existing is not None:
                raise IdentityError("device public key is already enrolled")
            device_id = f"device-{secrets.token_hex(16)}"
            connection.execute(
                """UPDATE pairing_tokens
                      SET status = 'consumed', consumed_at = ?
                    WHERE token_hash = ? AND status = 'pending'""",
                (timestamp, _token_hash(token)),
            )
            connection.execute(
                """INSERT INTO devices
                   (device_id, account_id, public_key, label, status, created_at)
                   VALUES (?, ?, ?, ?, 'active', ?)""",
                (device_id, account_id, public_key, str(label)[:128], timestamp),
            )
        return {"device_id": device_id, "account_id": account_id, "status": "active"}

    def revoke_device(self, telegram_id: int, device_id: str, *, now: str | None = None) -> bool:
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            account_id = self._account_id_in_connection(connection, telegram_id)
            if account_id is None:
                return False
            updated = connection.execute(
                """UPDATE devices SET status = 'revoked', revoked_at = ?
                    WHERE device_id = ? AND account_id = ? AND status != 'revoked'""",
                (timestamp, str(device_id), account_id),
            )
            if int(getattr(updated, "rowcount", 0) or 0):
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
        return False

    def device_auth_record(self, device_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT d.device_id, d.account_id, d.public_key, d.status,
                          a.status AS account_status, e.epoch AS revocation_epoch
                     FROM devices d JOIN accounts a ON a.account_id = d.account_id
                     LEFT JOIN device_revocation_epochs e ON e.account_id = d.account_id
                    WHERE d.device_id = ?""",
                (str(device_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def touch_device(self, device_id: str, *, now: str | None = None) -> bool:
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            updated = connection.execute(
                "UPDATE devices SET last_seen_at = ? WHERE device_id = ? AND status = 'active'",
                (timestamp, str(device_id)),
            )
        return int(getattr(updated, "rowcount", 0) or 0) == 1

    def create_device_session(
        self, device_id: str, *, manifest_version: int = 1, ttl_seconds: int = 86400,
        now: str | None = None,
    ) -> str:
        if not 60 <= int(ttl_seconds) <= 2_592_000:
            raise IdentityError("device session TTL is outside the allowed range")
        record = self.device_auth_record(device_id)
        if record is None or record.get("status") != "active" or record.get("account_status") != "active":
            raise IdentityError("device is not active")
        timestamp = str(now or _now_text())
        expires = (_parse_time(timestamp) + timedelta(seconds=int(ttl_seconds))).isoformat()
        session_id = f"session-{secrets.token_hex(16)}"
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """INSERT INTO device_sessions
                   (session_id, device_id, manifest_version, created_at, last_seen_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, str(device_id), int(manifest_version), timestamp, timestamp, expires),
            )
        return session_id

    def acknowledge_device(
        self, device_id: str, *, route_id: str | None, outcome: str, details: dict[str, Any] | None = None,
        now: str | None = None,
    ) -> bool:
        """Record only bounded connection state; never persist credentials or raw IPs."""
        if outcome not in {"connected", "failed", "disconnected", "probe"}:
            raise IdentityError("device acknowledgement outcome is invalid")
        if details is not None and len(str(details)) > 2048:
            raise IdentityError("device acknowledgement is too large")
        timestamp = str(now or _now_text())
        accepted = self.touch_device(device_id, now=timestamp)
        if accepted and route_id and outcome in {"connected", "failed"}:
            # Keep the device API independent from the commerce service while
            # still feeding the same durable failover state machine. A bad
            # route observation must not erase the authenticated device
            # heartbeat, but it is reported to the caller for correction.
            try:
                from route_failover import FailoverError, RouteFailoverService

                bucket = str((details or {}).get("network_bucket") or "default")[:128]
                latency = (details or {}).get("latency_ms")
                RouteFailoverService(self.database).observe(
                    str(route_id),
                    outcome="success" if outcome == "connected" else "failure",
                    network_bucket=bucket,
                    latency_ms=None if latency is None else int(latency),
                    reason=str((details or {}).get("reason") or outcome)[:256],
                    observed_at=timestamp,
                )
            except (FailoverError, TypeError, ValueError):
                # Acknowledgements from older clients may carry a display
                # route identifier instead of a generation. Preserve the
                # authenticated heartbeat; only valid generation selectors
                # participate in failover.
                pass
        return accepted
