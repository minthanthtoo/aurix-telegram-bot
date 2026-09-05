"""Account, device, and pairing lifecycle handlers."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from identity_accounts_repository import IdentityAccountsRepository
from identity_support import IdentityError, _now_text, _parse_time, _token_hash


class IdentityAccountsMixin:
    accounts_repository = IdentityAccountsRepository()

    def _account_id_in_connection(self, connection: Any, telegram_id: int) -> str | None:
        return self.accounts_repository.account_id(connection, telegram_id)

    def ensure_account(self, telegram_id: int, *, now: str | None = None) -> str:
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            return self.accounts_repository.ensure_account(connection, telegram_id, timestamp)

    def sync_existing_users(self, *, now: str | None = None) -> int:
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            rows = self.accounts_repository.user_ids(connection)
        for row in rows:
            self.ensure_account(int(row["telegram_id"]), now=timestamp)
        return len(rows)

    def account_snapshot(self, telegram_id: int) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = self.accounts_repository.snapshot(connection, telegram_id)
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
            self.accounts_repository.insert_pairing_token(
                connection,
                token_hash=_token_hash(token),
                account_id=account_id,
                telegram_id=int(telegram_id),
                expires_at=expires,
                created_at=timestamp,
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
            return self.accounts_repository.consume_pairing_token(
                connection,
                token=token,
                public_key=public_key,
                label=label,
                timestamp=timestamp,
            )

    def revoke_device(self, telegram_id: int, device_id: str, *, now: str | None = None) -> bool:
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            return self.accounts_repository.revoke_device(
                connection, telegram_id, device_id, timestamp
            )

    def device_auth_record(self, device_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = self.accounts_repository.device_auth(connection, device_id)
        return dict(row) if row is not None else None

    def touch_device(self, device_id: str, *, now: str | None = None) -> bool:
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            return self.accounts_repository.touch_device(connection, device_id, timestamp)

    def create_device_session(
        self,
        device_id: str,
        *,
        manifest_version: int = 1,
        ttl_seconds: int = 86400,
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
            self.accounts_repository.create_session(
                connection,
                session_id=session_id,
                device_id=device_id,
                manifest_version=manifest_version,
                timestamp=timestamp,
                expires_at=expires,
            )
        return session_id

    def acknowledge_device(
        self,
        device_id: str,
        *,
        route_id: str | None,
        outcome: str,
        details: dict[str, Any] | None = None,
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
                pass
        return accepted
