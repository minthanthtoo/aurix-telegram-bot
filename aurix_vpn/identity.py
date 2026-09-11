"""Opaque entitlement identity and aggregate quota accounting.

The portal already has ``subscriptions`` and ``keys`` as its commercial
sources of truth.  This module does not introduce a second entitlement model;
it gives those rows stable accounting identities and records every remote
credential generation underneath them.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .commerce_repositories import _PostgresConnection


UTC = timezone.utc
REMOTE_USABLE_GENERATION_STATUSES = ("pending", "active", "retiring", "unknown")
USAGE_BASELINE_PROVENANCES = ("new", "migrated", "unknown")


class IdentityError(RuntimeError):
    """An entitlement, generation, lease, or accounting invariant failed."""


def _now_text(now: datetime | str | None = None) -> str:
    if now is None:
        return datetime.now(UTC).isoformat()
    if isinstance(now, datetime):
        value = now if now.tzinfo else now.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    return str(now)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def _entitlement_parts(entitlement_key: str) -> tuple[str, str]:
    kind, separator, source_id = str(entitlement_key or "").partition(":")
    if not separator or kind not in {"paid", "free"} or not source_id:
        raise IdentityError("entitlement key must be paid:<subscription_id> or free:<key_id>")
    return kind, source_id


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


class IdentityService:
    """Shared identity and quota authority for paid and free entitlements."""

    def __init__(self, database: Any):
        self.database = database

    @staticmethod
    def _table_exists(connection: Any, name: str) -> bool:
        if isinstance(connection, _PostgresConnection):
            row = connection.execute("SELECT to_regclass(?) AS table_name", (f"public.{name}",)).fetchone()
            return bool(row and row["table_name"])
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    @staticmethod
    def _account_id_in_connection(connection: Any, telegram_id: int) -> str | None:
        row = connection.execute(
            """SELECT account_id FROM account_identities
                WHERE identity_type = 'telegram' AND identity_value = ?""",
            (str(int(telegram_id)),),
        ).fetchone()
        return str(row["account_id"]) if row is not None else None

    def ensure_account(self, telegram_id: int, *, now: str | datetime | None = None) -> str:
        """Create or recover the opaque account bound to one Telegram identity."""
        timestamp = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            account_id = self._account_id_in_connection(connection, telegram_id)
            if account_id is None:
                candidate = f"account-{uuid.uuid4().hex}"
                connection.execute(
                    "INSERT INTO accounts (account_id, created_at, updated_at) VALUES (?, ?, ?)",
                    (candidate, timestamp, timestamp),
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
                    connection.execute("DELETE FROM accounts WHERE account_id = ?", (candidate,))
            connection.execute(
                """INSERT INTO accounts (account_id, created_at, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(account_id) DO UPDATE SET updated_at = excluded.updated_at""",
                (account_id, timestamp, timestamp),
            )
            connection.execute(
                """INSERT INTO device_revocation_epochs (account_id, epoch, updated_at)
                   VALUES (?, 0, ?)
                   ON CONFLICT(account_id) DO NOTHING""",
                (account_id, timestamp),
            )
        return account_id

    def sync_existing_users(self, *, now: str | datetime | None = None) -> int:
        timestamp = _now_text(now)
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
                       ON i.account_id = a.account_id AND i.identity_type = 'telegram'
                     LEFT JOIN device_revocation_epochs e ON e.account_id = a.account_id
                    WHERE i.identity_value = ?""",
                (str(int(telegram_id)),),
            ).fetchone()
        return dict(row) if row is not None else None

    def create_pairing_token(
        self,
        telegram_id: int,
        *,
        ttl_seconds: int = 300,
        now: str | datetime | None = None,
    ) -> str:
        if not 30 <= int(ttl_seconds) <= 900:
            raise IdentityError("pairing token TTL is outside the allowed range")
        timestamp = _now_text(now)
        account_id = self.ensure_account(telegram_id, now=timestamp)
        token = secrets.token_urlsafe(32)
        expires = (_parse_time(timestamp) + timedelta(seconds=int(ttl_seconds))).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            status = connection.execute(
                "SELECT status FROM accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
            if status is None or str(status["status"]) != "active":
                raise IdentityError("account is not active")
            connection.execute(
                """INSERT INTO pairing_tokens
                   (token_hash, account_id, requested_by, expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (_token_hash(token), account_id, int(telegram_id), expires, timestamp),
            )
        return token

    def expire_pairing_tokens(self, *, now: str | datetime | None = None) -> int:
        """Bound one-time-token and session-table growth during maintenance."""
        timestamp = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            result = connection.execute(
                """UPDATE pairing_tokens SET status = 'expired'
                    WHERE status = 'pending' AND expires_at <= ?""",
                (timestamp,),
            )
            connection.execute(
                """UPDATE device_sessions SET revoked_at = COALESCE(revoked_at, ?)
                    WHERE revoked_at IS NULL AND expires_at <= ?""",
                (timestamp, timestamp),
            )
        return int(getattr(result, "rowcount", 0) or 0)

    def consume_pairing_token(
        self,
        token: str,
        public_key: str,
        *,
        label: str = "",
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        token = str(token or "").strip()
        public_key = str(public_key or "").strip()
        if not 20 <= len(token) <= 256 or not 16 <= len(public_key) <= 4096:
            raise IdentityError("pairing token or public key is invalid")
        timestamp = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                """SELECT * FROM pairing_tokens
                    WHERE token_hash = ? AND status = 'pending' AND expires_at > ?""",
                (_token_hash(token), timestamp),
            ).fetchone()
            if row is None:
                raise IdentityError("pairing token is invalid, expired, or already used")
            existing = connection.execute(
                "SELECT device_id FROM devices WHERE public_key = ?", (public_key,)
            ).fetchone()
            if existing is not None:
                raise IdentityError("device public key is already enrolled")
            device_id = f"device-{secrets.token_hex(16)}"
            updated = connection.execute(
                """UPDATE pairing_tokens SET status = 'consumed', consumed_at = ?
                    WHERE token_hash = ? AND status = 'pending'""",
                (timestamp, _token_hash(token)),
            )
            if int(getattr(updated, "rowcount", 0) or 0) != 1:
                raise IdentityError("pairing token is invalid, expired, or already used")
            connection.execute(
                """INSERT INTO devices
                   (device_id, account_id, public_key, label, status, created_at)
                   VALUES (?, ?, ?, ?, 'active', ?)""",
                (device_id, str(row["account_id"]), public_key, str(label)[:128], timestamp),
            )
            epoch = connection.execute(
                "SELECT epoch FROM device_revocation_epochs WHERE account_id = ?",
                (str(row["account_id"]),),
            ).fetchone()
        return {
            "device_id": device_id,
            "account_id": str(row["account_id"]),
            "status": "active",
            "revocation_epoch": int(epoch["epoch"] or 0) if epoch is not None else 0,
        }

    def revoke_device(
        self, telegram_id: int, device_id: str, *, now: str | datetime | None = None
    ) -> bool:
        timestamp = _now_text(now)
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
            if int(getattr(updated, "rowcount", 0) or 0) != 1:
                return False
            connection.execute(
                """UPDATE device_revocation_epochs SET epoch = epoch + 1, updated_at = ?
                    WHERE account_id = ?""",
                (timestamp, account_id),
            )
            connection.execute(
                """UPDATE device_sessions SET revoked_at = ?
                    WHERE device_id = ? AND revoked_at IS NULL""",
                (timestamp, str(device_id)),
            )
        return True

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

    def devices_for_account(self, telegram_id: int) -> list[dict[str, Any]]:
        """Return customer-safe device records for one verified Telegram account."""
        with self.database.connect() as connection:
            account_id = self._account_id_in_connection(connection, telegram_id)
            if account_id is None:
                return []
            rows = connection.execute(
                """SELECT d.device_id, d.label, d.status, d.created_at,
                          d.last_seen_at, d.revoked_at, e.epoch AS revocation_epoch
                     FROM devices d
                     LEFT JOIN device_revocation_epochs e ON e.account_id = d.account_id
                    WHERE d.account_id = ? ORDER BY d.created_at DESC""",
                (account_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def admin_counts(self) -> dict[str, int]:
        """Return non-secret counts for the operator Control Center."""
        with self.database.connect() as connection:
            counts = {
                "accounts": int(connection.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]),
                "active_accounts": int(
                    connection.execute("SELECT COUNT(*) AS n FROM accounts WHERE status = 'active'").fetchone()["n"]
                ),
                "devices": int(connection.execute("SELECT COUNT(*) AS n FROM devices").fetchone()["n"]),
                "active_devices": int(
                    connection.execute("SELECT COUNT(*) AS n FROM devices WHERE status = 'active'").fetchone()["n"]
                ),
                "credential_generations": int(
                    connection.execute("SELECT COUNT(*) AS n FROM credential_generations").fetchone()["n"]
                ),
                "active_generations": int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM credential_generations WHERE status IN ('pending', 'active', 'retiring', 'unknown')"
                    ).fetchone()["n"]
                ),
                "active_leases": int(
                    connection.execute("SELECT COUNT(*) AS n FROM quota_leases WHERE status = 'active'").fetchone()["n"]
                ),
            }
        return counts

    def admin_accounts(self, *, query: str = "", limit: int = 100) -> list[dict[str, Any]]:
        """Return account directory rows without Telegram/device secrets."""
        normalized = str(query or "").strip()[:128]
        like = f"%{normalized}%"
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT a.account_id, a.status, a.created_at, a.updated_at,
                          i.identity_value AS telegram_id,
                          (SELECT COUNT(*) FROM devices d WHERE d.account_id = a.account_id) AS device_count,
                          (SELECT COUNT(*) FROM devices d
                             WHERE d.account_id = a.account_id AND d.status = 'active') AS active_device_count,
                          (SELECT COUNT(*) FROM subscriptions s
                             WHERE CAST(s.telegram_id AS TEXT) = i.identity_value
                               AND s.status = 'active') AS active_subscription_count
                     FROM accounts a
                     LEFT JOIN account_identities i
                       ON i.account_id = a.account_id AND i.identity_type = 'telegram'
                    WHERE (? = '' OR a.account_id LIKE ? OR COALESCE(i.identity_value, '') LIKE ?)
                    ORDER BY a.updated_at DESC LIMIT ?""",
                (normalized, like, like, max(1, min(int(limit), 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    def admin_account(self, account_id: str) -> dict[str, Any] | None:
        """Return one account with safe device and route metadata."""
        account_id = str(account_id or "").strip()
        if not account_id or len(account_id) > 128:
            return None
        with self.database.connect() as connection:
            account = connection.execute(
                """SELECT a.account_id, a.status, a.created_at, a.updated_at,
                          i.identity_value AS telegram_id, e.epoch AS revocation_epoch
                     FROM accounts a
                     LEFT JOIN account_identities i
                       ON i.account_id = a.account_id AND i.identity_type = 'telegram'
                     LEFT JOIN device_revocation_epochs e ON e.account_id = a.account_id
                    WHERE a.account_id = ?""",
                (account_id,),
            ).fetchone()
            if account is None:
                return None
            devices = connection.execute(
                """SELECT device_id, label, status, created_at, last_seen_at, revoked_at
                     FROM devices WHERE account_id = ? ORDER BY created_at DESC""",
                (account_id,),
            ).fetchall()
            subscriptions = connection.execute(
                """SELECT s.id AS subscription_id, s.plan_code, s.plan_name,
                          s.status, s.starts_at, s.expires_at,
                          s.quota_bytes, s.consumed_bytes, s.quota_exhausted_at,
                          s.activated_at, k.endpoint_id,
                          k.status AS key_status, k.created_at AS key_created_at
                     FROM subscriptions s
                     LEFT JOIN paid_vpn_keys k ON k.subscription_id = s.id
                    WHERE CAST(s.telegram_id AS TEXT) =
                          (SELECT identity_value FROM account_identities
                            WHERE account_id = ? AND identity_type = 'telegram'
                            LIMIT 1)
                    ORDER BY s.starts_at DESC, s.id DESC""",
                (account_id,),
            ).fetchall()
        result = dict(account)
        result["devices"] = [dict(row) for row in devices]
        # This is entitlement metadata only. Access URLs, provider IDs, and
        # credential material remain outside the operator browser payload.
        result["subscriptions"] = [dict(row) for row in subscriptions]
        result["routes"] = self.routes_for_account(int(account["telegram_id"])) if account["telegram_id"] else []
        return result

    def admin_devices(self, *, query: str = "", status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Return fleet device inventory without public keys or session tokens."""
        normalized = str(query or "").strip()[:128]
        state = str(status or "").strip().lower()
        if state and state not in {"active", "revoked"}:
            raise IdentityError("device status filter is invalid")
        like = f"%{normalized}%"
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT d.device_id, d.account_id, d.label, d.status,
                          d.created_at, d.last_seen_at, d.revoked_at,
                          a.status AS account_status
                     FROM devices d JOIN accounts a ON a.account_id = d.account_id
                    WHERE (? = '' OR d.device_id LIKE ? OR d.label LIKE ? OR d.account_id LIKE ?)
                      AND (? = '' OR d.status = ?)
                    ORDER BY COALESCE(d.last_seen_at, d.created_at) DESC LIMIT ?""",
                (normalized, like, like, like, state, state, max(1, min(int(limit), 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    def admin_generations(
        self, *, protocol: str | None = None, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return credential lifecycle metadata while omitting credential material."""
        normalized_protocol = str(protocol or "").strip().lower()
        normalized_status = str(status or "").strip().lower()
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT generation_id, entitlement_key, source_type, source_id,
                          endpoint_id, protocol, generation_no, status, remote_state,
                          usage_baseline_provenance, usage_baseline_bytes,
                          created_at, revoked_at, revoke_verified_at
                     FROM credential_generations
                    WHERE (? = '' OR LOWER(protocol) = ?)
                      AND (? = '' OR status = ?)
                    ORDER BY created_at DESC LIMIT ?""",
                (
                    normalized_protocol,
                    normalized_protocol,
                    normalized_status,
                    normalized_status,
                    max(1, min(int(limit), 200)),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def touch_device(self, device_id: str, *, now: str | datetime | None = None) -> bool:
        timestamp = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            updated = connection.execute(
                "UPDATE devices SET last_seen_at = ? WHERE device_id = ? AND status = 'active'",
                (timestamp, str(device_id)),
            )
        return int(getattr(updated, "rowcount", 0) or 0) == 1

    def create_device_session(
        self,
        device_id: str,
        *,
        manifest_version: int = 1,
        ttl_seconds: int = 86_400,
        now: str | datetime | None = None,
    ) -> str:
        if not 60 <= int(ttl_seconds) <= 2_592_000:
            raise IdentityError("device session TTL is outside the allowed range")
        record = self.device_auth_record(device_id)
        if record is None or record.get("status") != "active" or record.get("account_status") != "active":
            raise IdentityError("device is not active")
        timestamp = _now_text(now)
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
        self,
        device_id: str,
        *,
        route_id: str | None,
        outcome: str,
        details: dict[str, Any] | None = None,
        now: str | datetime | None = None,
    ) -> bool:
        if outcome not in {"connected", "failed", "disconnected", "probe"}:
            raise IdentityError("device acknowledgement outcome is invalid")
        if details is not None and len(str(details)) > 2048:
            raise IdentityError("device acknowledgement is too large")
        timestamp = _now_text(now)
        accepted = self.touch_device(device_id, now=timestamp)
        if accepted and route_id and outcome in {"connected", "failed"}:
            try:
                # A signed device heartbeat proves possession of the device
                # key, not ownership of an arbitrary route selector. Only
                # feed observations into failover after resolving the route
                # against the device's account.
                auth_record = self.device_auth_record(device_id)
                if auth_record is None or self.route_secret_record(
                    str(auth_record["account_id"]), str(route_id)
                ) is None:
                    return accepted
                from .route_failover import FailoverError, RouteFailoverService

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
                # Heartbeats remain valid when older clients report a display
                # route rather than a generation selector.
                pass
        return accepted

    def _account_telegram_id(self, connection: Any, account_id: str) -> int | None:
        row = connection.execute(
            """SELECT identity_value FROM account_identities
                WHERE account_id = ? AND identity_type = 'telegram'""",
            (str(account_id),),
        ).fetchone()
        try:
            return int(row["identity_value"]) if row is not None else None
        except (TypeError, ValueError):
            return None

    def routes_for_account(self, account_id: str) -> list[dict[str, Any]]:
        """Return safe route metadata; access URLs stay encrypted and out of manifests."""
        timestamp = _now_text()
        rows: list[Any] = []
        with self.database.connect() as connection:
            telegram_id = self._account_telegram_id(connection, account_id)
            if telegram_id is None:
                return []
            rows.extend(
                connection.execute(
                    """SELECT g.generation_id, g.endpoint_id, g.protocol, g.generation_no,
                              COALESCE(v.region, g.endpoint_id) AS region
                         FROM credential_generations g
                         JOIN subscriptions s ON s.id = g.source_id
                         LEFT JOIN vpn_endpoints v ON v.id = g.endpoint_id
                        WHERE g.source_type = 'paid'
                          AND s.telegram_id = ? AND s.status IN ('active', 'pending')
                          AND s.expires_at > ? AND g.status IN ('active', 'retiring')
                          AND g.remote_state = 'observed'""",
                    (telegram_id, timestamp),
                ).fetchall()
            )
            if self._table_exists(connection, "keys"):
                rows.extend(
                    connection.execute(
                        """SELECT g.generation_id, g.endpoint_id, g.protocol, g.generation_no,
                                  COALESCE(v.region, g.endpoint_id) AS region
                             FROM credential_generations g
                             JOIN keys k ON CAST(k.id AS TEXT) = g.source_id
                             LEFT JOIN vpn_endpoints v ON v.id = g.endpoint_id
                            WHERE g.source_type = 'free'
                              AND k.telegram_id = ? AND k.status = 'active'
                              AND k.expires_at > ? AND g.status IN ('active', 'retiring')
                              AND g.remote_state = 'observed'""",
                        (telegram_id, timestamp),
                    ).fetchall()
                )
        return [
            {
                "route_id": str(row["generation_id"]),
                "generation_id": str(row["generation_id"]),
                "endpoint_id": str(row["endpoint_id"]),
                "region": str(row["region"]),
                "protocol": str(row["protocol"]),
                "transport": str(row["protocol"]),
                "generation": int(row["generation_no"]),
                "credential_ref": str(row["generation_id"]),
            }
            for row in rows
        ]

    def route_secret_record(self, account_id: str, route_id: str) -> dict[str, Any] | None:
        """Return one active account-owned generation for authenticated config delivery."""
        timestamp = _now_text()
        with self.database.connect() as connection:
            telegram_id = self._account_telegram_id(connection, account_id)
            if telegram_id is None:
                return None
            route = str(route_id)
            row = connection.execute(
                """SELECT g.generation_id, g.endpoint_id, g.protocol, g.generation_no,
                          g.access_url_ciphertext,
                          COALESCE(v.region, g.endpoint_id) AS region
                     FROM credential_generations g
                     JOIN subscriptions s ON s.id = g.source_id
                     LEFT JOIN vpn_endpoints v ON v.id = g.endpoint_id
                    WHERE g.generation_id = ? AND g.source_type = 'paid'
                      AND s.telegram_id = ? AND s.status IN ('active', 'pending')
                      AND s.expires_at > ? AND g.status IN ('active', 'retiring')
                      AND g.remote_state = 'observed'""",
                (route, telegram_id, timestamp),
            ).fetchone()
            if row is None and self._table_exists(connection, "keys"):
                row = connection.execute(
                    """SELECT g.generation_id, g.endpoint_id, g.protocol, g.generation_no,
                              g.access_url_ciphertext,
                              COALESCE(v.region, g.endpoint_id) AS region
                         FROM credential_generations g
                         JOIN keys k ON CAST(k.id AS TEXT) = g.source_id
                         LEFT JOIN vpn_endpoints v ON v.id = g.endpoint_id
                        WHERE g.generation_id = ? AND g.source_type = 'free'
                          AND k.telegram_id = ? AND k.status = 'active'
                          AND k.expires_at > ? AND g.status IN ('active', 'retiring')
                          AND g.remote_state = 'observed'""",
                    (route, telegram_id, timestamp),
                ).fetchone()
        if row is None:
            return None
        return {
            "generation_id": str(row["generation_id"]),
            "endpoint_id": str(row["endpoint_id"]),
            "region": str(row["region"]),
            "protocol": str(row["protocol"]),
            "transport": str(row["protocol"]),
            "generation_no": int(row["generation_no"]),
            "secret_ciphertext": str(row["access_url_ciphertext"] or ""),
        }

    @staticmethod
    def _lock_source(connection: Any, source_type: str, source_id: str) -> None:
        """Lock the entitlement row before reading its consumed counter.

        This ordering is intentional.  PostgreSQL callers must never read a
        stale ``consumed_bytes`` value and then race another usage writer.
        """
        if not isinstance(connection, _PostgresConnection):
            return
        table = "subscriptions" if source_type == "paid" else "keys"
        connection.execute(f"SELECT id FROM {table} WHERE id = ? FOR UPDATE", (source_id,)).fetchone()

    def _source_row(self, connection: Any, entitlement_key: str) -> dict[str, Any] | None:
        source_type, source_id = _entitlement_parts(entitlement_key)
        if source_type == "paid":
            row = connection.execute(
                """SELECT s.id AS source_id, s.telegram_id, s.status, s.expires_at,
                          COALESCE(s.quota_bytes, p.quota_bytes) AS quota_bytes,
                          COALESCE(s.consumed_bytes, 0) AS consumed_bytes,
                          s.quota_exhausted_at
                     FROM subscriptions s JOIN plans p ON p.code = s.plan_code
                    WHERE s.id = ?""",
                (source_id,),
            ).fetchone()
            return dict(row) | {"source_type": source_type} if row is not None else None
        if not self._table_exists(connection, "keys"):
            return None
        row = connection.execute(
            """SELECT id AS source_id, telegram_id, status, expires_at,
                      data_limit_bytes AS quota_bytes
                 FROM keys WHERE id = ?""",
            (source_id,),
        ).fetchone()
        if row is None:
            return None
        consumed = connection.execute(
            """SELECT COALESCE(SUM(bytes), 0) AS consumed_bytes
                 FROM entitlement_quota_ledger
                WHERE entitlement_key = ? AND event_type = 'usage'""",
            (entitlement_key,),
        ).fetchone()
        value = dict(row)
        value["consumed_bytes"] = int(consumed["consumed_bytes"] or 0) if consumed else 0
        value["quota_exhausted_at"] = None
        value["source_type"] = source_type
        return value

    @staticmethod
    def _append_ledger(
        connection: Any,
        *,
        entitlement_key: str,
        event_type: str,
        bytes_value: int,
        consumed_bytes: int,
        remaining_bytes: int,
        idempotency_key: str,
        now: str,
        generation_id: str | None = None,
        endpoint_id: str | None = None,
        lease_id: str | None = None,
        epoch_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        result = connection.execute(
            """INSERT INTO entitlement_quota_ledger
               (entry_id, entitlement_key, generation_id, endpoint_id, lease_id, epoch_id,
                event_type, bytes, consumed_bytes, remaining_bytes, idempotency_key,
                details_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(idempotency_key) DO NOTHING""",
            (
                f"ledger-{secrets.token_hex(12)}",
                entitlement_key,
                generation_id,
                endpoint_id,
                lease_id,
                epoch_id,
                event_type,
                max(0, int(bytes_value)),
                max(0, int(consumed_bytes)),
                max(0, int(remaining_bytes)),
                idempotency_key,
                json.dumps(details or {}, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
                now,
            ),
        )
        return int(getattr(result, "rowcount", 0) or 0) == 1

    def ensure_subscription_entitlement(
        self,
        telegram_id: int,
        subscription_id: str,
        *,
        kind: str = "paid",
        quota_bytes: int | None = None,
        expires_at: str | None = None,
        status: str = "active",
        now: str | None = None,
    ) -> str:
        if kind not in {"paid", "trial", "promo"}:
            raise IdentityError("subscription entitlement kind is invalid")
        key = f"paid:{str(subscription_id)}"
        with self.database.connect() as connection:
            row = self._source_row(connection, key)
        if row is None or int(row["telegram_id"]) != int(telegram_id):
            raise IdentityError("subscription entitlement does not belong to the account")
        if quota_bytes is not None and int(quota_bytes) <= 0:
            raise IdentityError("entitlement quota must be positive")
        if expires_at is not None and _parse_time(str(expires_at)) <= _parse_time(_now_text(now)):
            raise IdentityError("entitlement expiry must be in the future")
        return key

    def ensure_free_entitlement(
        self, telegram_id: int, key_id: int | str, *, now: str | None = None
    ) -> str:
        key = f"free:{key_id}"
        with self.database.connect() as connection:
            row = self._source_row(connection, key)
        if row is None or int(row["telegram_id"]) != int(telegram_id):
            raise IdentityError("free entitlement does not belong to the account")
        return key

    def ensure_generation_for_credential(
        self,
        entitlement_key: str,
        endpoint_id: str,
        *,
        credential_id: str | None = None,
        external_id: str | None = None,
        protocol: str = "outline",
        access_url_ciphertext: str | None = None,
        status: str = "active",
        remote_state: str = "observed",
        intent_key: str | None = None,
        usage_baseline_provenance: str = "unknown",
        usage_baseline_bytes: int | None = None,
        now: str | None = None,
    ) -> str:
        source_type, source_id = _entitlement_parts(entitlement_key)
        external_id = str(external_id or credential_id or "").strip()
        if not external_id:
            raise IdentityError("credential external id is required")
        if status not in REMOTE_USABLE_GENERATION_STATUSES + ("revoked", "failed"):
            raise IdentityError("credential generation status is invalid")
        provenance = str(usage_baseline_provenance)
        if provenance not in USAGE_BASELINE_PROVENANCES:
            raise IdentityError("usage baseline provenance is invalid")
        if usage_baseline_bytes is not None and int(usage_baseline_bytes) < 0:
            raise IdentityError("usage baseline bytes cannot be negative")
        if provenance == "migrated" and usage_baseline_bytes is None:
            raise IdentityError("migrated usage baseline requires bytes")
        if provenance != "migrated" and usage_baseline_bytes is not None:
            raise IdentityError("only migrated usage baselines may include bytes")
        timestamp = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            if self._source_row(connection, entitlement_key) is None:
                raise IdentityError("generation references an unknown entitlement")
            existing = connection.execute(
                """SELECT generation_id, status, usage_baseline_provenance,
                          usage_baseline_bytes
                     FROM credential_generations
                    WHERE entitlement_key = ? AND endpoint_id = ? AND external_id = ?""",
                (entitlement_key, str(endpoint_id), external_id),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """UPDATE credential_generations
                          SET access_url_ciphertext = COALESCE(?, access_url_ciphertext),
                              protocol = ?, remote_state = CASE
                                  WHEN remote_state = 'observed' AND ? = 'unknown'
                                  THEN remote_state ELSE ? END,
                              status = CASE WHEN status = 'revoked' THEN status ELSE ? END,
                              usage_baseline_provenance = CASE
                                  WHEN usage_baseline_provenance = 'unknown' THEN ?
                                  ELSE usage_baseline_provenance END,
                              usage_baseline_bytes = CASE
                                  WHEN usage_baseline_provenance = 'unknown' THEN ?
                                  ELSE usage_baseline_bytes END
                        WHERE generation_id = ?""",
                    (
                        access_url_ciphertext,
                        str(protocol),
                        str(remote_state),
                        str(remote_state),
                        status,
                        provenance,
                        int(usage_baseline_bytes) if usage_baseline_bytes is not None else None,
                        existing["generation_id"],
                    ),
                )
                return str(existing["generation_id"])
            # A replacement on one endpoint retires the previous generation,
            # but it remains remotely usable and therefore remains billable.
            connection.execute(
                """UPDATE credential_generations SET status = 'retiring'
                    WHERE entitlement_key = ? AND endpoint_id = ? AND status = 'active'""",
                (entitlement_key, str(endpoint_id)),
            )
            latest = connection.execute(
                "SELECT COALESCE(MAX(generation_no), 0) AS latest FROM credential_generations WHERE entitlement_key = ?",
                (entitlement_key,),
            ).fetchone()
            generation_id = f"generation-{uuid.uuid4().hex}"
            connection.execute(
                """INSERT INTO credential_generations
                   (generation_id, entitlement_key, source_type, source_id, endpoint_id,
                   protocol, external_id, access_url_ciphertext, generation_no, status,
                    remote_state, intent_key, usage_baseline_provenance, usage_baseline_bytes,
                    created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    generation_id,
                    entitlement_key,
                    source_type,
                    source_id,
                    str(endpoint_id),
                    str(protocol),
                    external_id,
                    access_url_ciphertext,
                    int(latest["latest"] or 0) + 1,
                    status,
                    str(remote_state),
                    intent_key,
                    provenance,
                    int(usage_baseline_bytes) if usage_baseline_bytes is not None else None,
                    timestamp,
                ),
            )
        return generation_id

    def create_generation(self, entitlement_key: str, endpoint_id: str, **kwargs: Any) -> str:
        return self.ensure_generation_for_credential(entitlement_key, endpoint_id, **kwargs)

    def _active_lease(self, connection: Any, generation_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            """SELECT * FROM quota_leases
                WHERE generation_id = ? AND status = 'active'
                ORDER BY created_at DESC LIMIT 1""",
            (str(generation_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def grant_lease(
        self,
        entitlement_key: str,
        endpoint_id: str,
        *,
        lease_bytes: int,
        generation_id: str | None = None,
        ttl_seconds: int = 900,
        now: str | None = None,
    ) -> str:
        if not 1 <= int(lease_bytes) <= 10 * 1024 * 1024 * 1024:
            raise IdentityError("lease size is invalid")
        if not 30 <= int(ttl_seconds) <= 2_592_000:
            raise IdentityError("lease TTL is invalid")
        source_type, _source_id = _entitlement_parts(entitlement_key)
        timestamp = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            # This is deliberately before _source_row: the consumed counter
            # must be read only after the PostgreSQL entitlement lock exists.
            self._lock_source(connection, source_type, _entitlement_parts(entitlement_key)[1])
            source = self._source_row(connection, entitlement_key)
            if source is None:
                raise IdentityError("lease references an unknown entitlement")
            if str(source["status"]) not in {"active", "pending"}:
                raise IdentityError("entitlement is not active")
            current = _parse_time(timestamp)
            expiry = _parse_time(str(source["expires_at"]))
            if expiry <= current:
                raise IdentityError("entitlement is expired")
            expires = min(current + timedelta(seconds=int(ttl_seconds)), expiry).isoformat()
            if generation_id is not None:
                generation = connection.execute(
                    """SELECT generation_id, endpoint_id, status FROM credential_generations
                        WHERE generation_id = ? AND entitlement_key = ?""",
                    (str(generation_id), entitlement_key),
                ).fetchone()
                if generation is None or str(generation["endpoint_id"]) != str(endpoint_id):
                    raise IdentityError("lease generation is unknown")
                if str(generation["status"]) not in REMOTE_USABLE_GENERATION_STATUSES:
                    raise IdentityError("lease generation is not remotely usable")
                existing = self._active_lease(connection, str(generation_id))
                if existing is not None:
                    return str(existing["lease_id"])
            # A local TTL is only an operational horizon. It is not proof that
            # the remote credential stopped working, so it cannot release the
            # aggregate reservation while the remote generation is usable.
            usage = connection.execute(
                """SELECT COALESCE(SUM(used_bytes), 0) AS consumed,
                          COALESCE(SUM(CASE WHEN status = 'active'
                                            THEN lease_bytes - used_bytes ELSE 0 END), 0) AS reserved
                     FROM quota_leases WHERE entitlement_key = ?""",
                (entitlement_key,),
            ).fetchone()
            consumed = max(int(source["consumed_bytes"] or 0), int(usage["consumed"] or 0))
            available = int(source["quota_bytes"]) - consumed - int(usage["reserved"] or 0)
            if int(lease_bytes) > available:
                raise IdentityError("entitlement has insufficient unreserved quota")
            lease_id = f"lease-{secrets.token_hex(12)}"
            connection.execute(
                """INSERT INTO quota_leases
                   (lease_id, entitlement_key, generation_id, endpoint_id, lease_bytes,
                    expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (lease_id, entitlement_key, generation_id, str(endpoint_id), int(lease_bytes), expires, timestamp),
            )
            self._append_ledger(
                connection,
                entitlement_key=entitlement_key,
                generation_id=generation_id,
                endpoint_id=str(endpoint_id),
                lease_id=lease_id,
                event_type="grant",
                bytes_value=int(lease_bytes),
                consumed_bytes=consumed,
                remaining_bytes=max(0, int(source["quota_bytes"]) - consumed),
                idempotency_key=f"grant:{lease_id}",
                details={"expires_at": expires},
                now=timestamp,
            )
        return lease_id

    def ensure_generation_lease(
        self,
        entitlement_key: str,
        generation_id: str,
        endpoint_id: str,
        quota_bytes: int | None = None,
        entitlement_expires_at: str | None = None,
        *,
        now: str | None = None,
    ) -> str:
        with self.database.connect() as connection:
            existing = self._active_lease(connection, generation_id)
            source = self._source_row(connection, entitlement_key)
        if existing is not None:
            return str(existing["lease_id"])
        if source is None:
            raise IdentityError("unknown entitlement")
        quota = int(quota_bytes or source["quota_bytes"] or 0)
        if quota <= 0:
            raise IdentityError("entitlement quota must be positive")
        expiry = str(entitlement_expires_at or source["expires_at"])
        current = _parse_time(_now_text(now))
        ttl = max(30, min(2_592_000, int((_parse_time(expiry) - current).total_seconds())))
        return self.grant_lease(
            entitlement_key,
            endpoint_id,
            lease_bytes=min(quota, 10 * 1024 * 1024 * 1024),
            generation_id=generation_id,
            ttl_seconds=ttl,
            now=current.isoformat(),
        )

    def transfer_generation_lease(
        self,
        entitlement_key: str,
        source_generation_id: str,
        target_generation_id: str,
        target_endpoint_id: str,
        *,
        now: str | datetime | None = None,
    ) -> str | None:
        """Move one active reservation between generations of one entitlement.

        Failover must not double-reserve an entitlement while the source
        generation remains remotely usable. The source is still accounted for
        after this operation; only the recovery reservation follows the
        verified target. Calling this repeatedly is idempotent.
        """
        timestamp = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            source = connection.execute(
                """SELECT generation_id FROM credential_generations
                    WHERE generation_id = ? AND entitlement_key = ?""",
                (str(source_generation_id), str(entitlement_key)),
            ).fetchone()
            target = connection.execute(
                """SELECT generation_id FROM credential_generations
                    WHERE generation_id = ? AND entitlement_key = ? AND endpoint_id = ?
                      AND status IN ('pending', 'active', 'retiring', 'unknown')""",
                (str(target_generation_id), str(entitlement_key), str(target_endpoint_id)),
            ).fetchone()
            if source is None or target is None:
                raise IdentityError("failover lease references an unknown generation")
            existing_target = connection.execute(
                """SELECT lease_id FROM quota_leases
                    WHERE entitlement_key = ? AND generation_id = ? AND status = 'active'
                    ORDER BY created_at DESC LIMIT 1""",
                (str(entitlement_key), str(target_generation_id)),
            ).fetchone()
            if existing_target is not None:
                return str(existing_target["lease_id"])
            lease = connection.execute(
                """SELECT lease_id FROM quota_leases
                    WHERE entitlement_key = ? AND generation_id = ? AND status = 'active'
                    ORDER BY created_at DESC LIMIT 1""",
                (str(entitlement_key), str(source_generation_id)),
            ).fetchone()
            if lease is None:
                return None
            connection.execute(
                """UPDATE quota_leases
                      SET generation_id = ?, endpoint_id = ?
                    WHERE lease_id = ? AND status = 'active'""",
                (str(target_generation_id), str(target_endpoint_id), str(lease["lease_id"])),
            )
            self._append_ledger(
                connection,
                entitlement_key=str(entitlement_key),
                generation_id=str(target_generation_id),
                endpoint_id=str(target_endpoint_id),
                lease_id=str(lease["lease_id"]),
                event_type="reconcile",
                bytes_value=0,
                consumed_bytes=0,
                remaining_bytes=0,
                idempotency_key=f"lease-transfer:{lease['lease_id']}:{target_generation_id}",
                details={"source_generation_id": str(source_generation_id)},
                now=timestamp,
            )
            return str(lease["lease_id"])

    def recovery_authorization(
        self,
        entitlement_key: str,
        generation_id: str,
        *,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Return the bounded authority for recreating one remote credential.

        A durable generation is not, by itself, permission to recreate a
        provider credential after a restart.  The commercial entitlement must
        still be active and unexpired, and the generation must retain an
        active quota lease.  The lease is the per-generation recovery budget;
        its local TTL is intentionally ignored because expiry is not proof
        that a remote credential stopped working.
        """
        timestamp = _now_text(now)
        source_type, source_id = _entitlement_parts(entitlement_key)
        current = _parse_time(timestamp)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_source(connection, source_type, source_id)
            source = self._source_row(connection, entitlement_key)
            if source is None:
                return {"authorized": False, "reason": "unknown_entitlement"}
            status = str(source["status"] or "")
            try:
                expired = _parse_time(str(source["expires_at"])) <= current
            except (TypeError, ValueError):
                expired = True
            try:
                quota = int(source["quota_bytes"] or 0)
                consumed = int(source["consumed_bytes"] or 0)
            except (TypeError, ValueError):
                return {"authorized": False, "reason": "invalid_quota"}
            remaining_global = max(0, quota - consumed) if quota > 0 else 0
            if status not in {"active", "pending"}:
                return {
                    "authorized": False,
                    "reason": "entitlement_inactive",
                    "status": status,
                    "remaining_bytes": remaining_global,
                }
            if expired:
                return {
                    "authorized": False,
                    "reason": "entitlement_expired",
                    "status": status,
                    "remaining_bytes": remaining_global,
                }
            lease = connection.execute(
                """SELECT lease_bytes, used_bytes, status
                     FROM quota_leases
                    WHERE entitlement_key = ? AND generation_id = ?
                      AND status = 'active'
                    ORDER BY created_at DESC LIMIT 1""",
                (entitlement_key, str(generation_id)),
            ).fetchone()
            if lease is None:
                return {
                    "authorized": False,
                    "reason": "no_active_lease",
                    "status": status,
                    "remaining_bytes": remaining_global,
                }
            try:
                lease_remaining = max(0, int(lease["lease_bytes"]) - int(lease["used_bytes"] or 0))
            except (TypeError, ValueError):
                return {"authorized": False, "reason": "invalid_lease"}
            remaining = min(remaining_global, lease_remaining) if quota > 0 else lease_remaining
            if remaining <= 0:
                return {
                    "authorized": False,
                    "reason": "quota_exhausted",
                    "status": status,
                    "remaining_bytes": 0,
                }
            return {
                "authorized": True,
                "reason": "authorized",
                "status": status,
                "remaining_bytes": remaining,
                "expires_at": str(source["expires_at"]),
            }

    def record_usage(
        self,
        entitlement_key: str,
        generation_id: str,
        remote_bytes: int,
        *,
        endpoint_id: str | None = None,
        source_external_id: str | None = None,
        observed_at: str | None = None,
        counter_mode: str = "reset_on_decrease",
    ) -> dict[str, Any]:
        remote = int(remote_bytes)
        if remote < 0:
            raise IdentityError("remote usage cannot be negative")
        if counter_mode not in {"reset_on_decrease", "rolling_window"}:
            raise IdentityError("usage counter mode is invalid")
        timestamp = _now_text(observed_at)
        source_type, source_id = _entitlement_parts(entitlement_key)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            # Lock first, then read consumed_bytes in _source_row.
            self._lock_source(connection, source_type, source_id)
            source = self._source_row(connection, entitlement_key)
            generation = connection.execute(
                """SELECT * FROM credential_generations
                    WHERE generation_id = ? AND entitlement_key = ?""",
                (str(generation_id), entitlement_key),
            ).fetchone()
            if source is None or generation is None:
                raise IdentityError("usage references an unknown entitlement or generation")
            endpoint = str(endpoint_id or generation["endpoint_id"])
            external = str(source_external_id or generation["external_id"])
            if endpoint != str(generation["endpoint_id"]) or external != str(generation["external_id"]):
                raise IdentityError("usage source does not match its generation")
            if str(generation["status"]) not in REMOTE_USABLE_GENERATION_STATUSES:
                return {"credited_bytes": 0, "consumed_bytes": int(source["consumed_bytes"] or 0), "ignored": "revoked_generation"}
            generation_keys = set(generation.keys()) if hasattr(generation, "keys") else set()
            provenance = str(
                generation["usage_baseline_provenance"]
                if "usage_baseline_provenance" in generation_keys
                else "unknown"
            )
            baseline_value = (
                generation["usage_baseline_bytes"] if "usage_baseline_bytes" in generation_keys else None
            )
            if provenance not in USAGE_BASELINE_PROVENANCES:
                raise IdentityError("usage baseline provenance is invalid")
            if provenance == "migrated" and baseline_value is None:
                raise IdentityError("migrated usage baseline is missing bytes")
            if provenance == "unknown":
                raise IdentityError("usage baseline provenance is unknown")
            epoch = connection.execute(
                """SELECT * FROM entitlement_usage_epochs
                    WHERE entitlement_key = ? AND generation_id = ? AND endpoint_id = ?
                      AND source_external_id = ? AND status = 'active'
                    ORDER BY epoch_no DESC LIMIT 1""",
                (entitlement_key, generation_id, endpoint, external),
            ).fetchone()
            initialized = False
            if epoch is None:
                epoch_id = f"epoch-{secrets.token_hex(12)}"
                epoch_no = 1
                # A newly owned credential has a trusted zero baseline: its
                # first remote counter reading is usage, not reconciliation.
                # Migrated credentials instead use the explicitly evidenced
                # operator baseline.
                baseline = 0 if provenance == "new" else int(baseline_value)
                connection.execute(
                    """INSERT INTO entitlement_usage_epochs
                       (epoch_id, entitlement_key, generation_id, endpoint_id, source_external_id,
                        epoch_no, last_remote_bytes, last_observed_at, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (epoch_id, entitlement_key, generation_id, endpoint, external, epoch_no, baseline, timestamp, timestamp, timestamp),
                )
                self._append_ledger(
                    connection, entitlement_key=entitlement_key, generation_id=generation_id,
                    endpoint_id=endpoint, epoch_id=epoch_id, event_type="reconcile",
                    bytes_value=0, consumed_bytes=int(source["consumed_bytes"] or 0),
                    remaining_bytes=max(0, int(source["quota_bytes"]) - int(source["consumed_bytes"] or 0)),
                    idempotency_key=f"epoch:{epoch_id}",
                    details={
                        "usage_baseline_provenance": provenance,
                        "baseline_bytes": baseline,
                    },
                    now=timestamp,
                )
                epoch = {
                    "epoch_id": epoch_id,
                    "epoch_no": epoch_no,
                    "last_remote_bytes": baseline,
                    "last_observed_at": timestamp,
                }
                initialized = True
            else:
                epoch = dict(epoch)
            counter_reset = False
            rolling_window_decrease = False
            duplicate = connection.execute(
                """SELECT sample_id FROM entitlement_usage_samples
                    WHERE epoch_id = ? AND observed_at = ? AND remote_bytes = ?""",
                (epoch["epoch_id"], timestamp, remote),
            ).fetchone()
            if duplicate is not None:
                return {"credited_bytes": 0, "delta_bytes": 0, "consumed_bytes": int(source["consumed_bytes"] or 0), "epoch_id": epoch["epoch_id"], "duplicate": True}
            if _parse_time(timestamp) < _parse_time(str(epoch["last_observed_at"])):
                self._append_ledger(
                    connection, entitlement_key=entitlement_key, generation_id=generation_id,
                    endpoint_id=endpoint, epoch_id=epoch["epoch_id"], event_type="stale_observation",
                    bytes_value=0, consumed_bytes=int(source["consumed_bytes"] or 0),
                    remaining_bytes=max(0, int(source["quota_bytes"]) - int(source["consumed_bytes"] or 0)),
                    idempotency_key=f"stale:{generation_id}:{timestamp}:{remote}",
                    details={"last_observed_at": epoch["last_observed_at"]}, now=timestamp,
                )
                return {"credited_bytes": 0, "delta_bytes": 0, "consumed_bytes": int(source["consumed_bytes"] or 0), "epoch_id": epoch["epoch_id"], "stale": True}
            if remote < int(epoch["last_remote_bytes"]):
                if counter_mode == "rolling_window":
                    # Outline reports a trailing-window value.  A decrease
                    # means old traffic aged out; it is not new billable
                    # traffic and must not start a fresh credit epoch.
                    rolling_window_decrease = True
                else:
                    connection.execute(
                        """UPDATE entitlement_usage_epochs
                              SET status = 'reset', reset_count = reset_count + 1, updated_at = ?
                            WHERE epoch_id = ? AND status = 'active'""",
                        (timestamp, epoch["epoch_id"]),
                    )
                    connection.execute(
                        """INSERT INTO entitlement_usage_samples
                           (sample_id, epoch_id, entitlement_key, generation_id, endpoint_id,
                            source_external_id, remote_bytes, delta_bytes, accepted, reason,
                            observed_at, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 'counter_reset', ?, ?)""",
                        (f"sample-{secrets.token_hex(12)}", epoch["epoch_id"], entitlement_key, generation_id, endpoint, external, remote, timestamp, timestamp),
                    )
                    self._append_ledger(
                        connection, entitlement_key=entitlement_key, generation_id=generation_id,
                        endpoint_id=endpoint, epoch_id=epoch["epoch_id"], event_type="counter_reset",
                        bytes_value=0, consumed_bytes=int(source["consumed_bytes"] or 0),
                        remaining_bytes=max(0, int(source["quota_bytes"]) - int(source["consumed_bytes"] or 0)),
                        idempotency_key=f"reset:{generation_id}:{timestamp}:{remote}", now=timestamp,
                    )
                    new_epoch_id = f"epoch-{secrets.token_hex(12)}"
                    new_no = int(epoch["epoch_no"]) + 1
                    connection.execute(
                        """INSERT INTO entitlement_usage_epochs
                           (epoch_id, entitlement_key, generation_id, endpoint_id, source_external_id,
                            epoch_no, last_remote_bytes, last_observed_at, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (new_epoch_id, entitlement_key, generation_id, endpoint, external, new_no, 0, timestamp, timestamp, timestamp),
                    )
                    epoch = {
                        "epoch_id": new_epoch_id,
                        "epoch_no": new_no,
                        "last_remote_bytes": 0,
                        "last_observed_at": timestamp,
                    }
                    counter_reset = True
            delta = max(0, remote - int(epoch["last_remote_bytes"]))
            before = int(source["consumed_bytes"] or 0)
            credit = min(delta, max(0, int(source["quota_bytes"]) - before))
            after = before + credit
            reason = (
                "rolling_window_decrease"
                if rolling_window_decrease
                else ("usage" if credit == delta else "quota_cap")
            )
            lease = connection.execute(
                """SELECT lease_id, lease_bytes, used_bytes FROM quota_leases
                    WHERE generation_id = ? AND status IN ('active', 'exhausted')
                    ORDER BY created_at DESC LIMIT 1""",
                (generation_id,),
            ).fetchone()
            if lease is not None and credit:
                connection.execute(
                    """UPDATE quota_leases
                          SET used_bytes = CASE
                              WHEN used_bytes + ? > lease_bytes THEN lease_bytes
                              ELSE used_bytes + ? END
                        WHERE lease_id = ?""",
                    (credit, credit, lease["lease_id"]),
                )
            connection.execute(
                """INSERT INTO entitlement_usage_samples
                   (sample_id, epoch_id, entitlement_key, generation_id, endpoint_id,
                    source_external_id, lease_id, remote_bytes, delta_bytes, accepted,
                    reason, observed_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"sample-{secrets.token_hex(12)}", epoch["epoch_id"], entitlement_key, generation_id, endpoint,
                 external, lease["lease_id"] if lease else None, remote, credit, 1, reason, timestamp, timestamp),
            )
            connection.execute(
                """UPDATE entitlement_usage_epochs
                      SET last_remote_bytes = ?, credited_bytes = credited_bytes + ?,
                          last_observed_at = ?, updated_at = ?
                    WHERE epoch_id = ? AND status = 'active'""",
                (remote, credit, timestamp, timestamp, epoch["epoch_id"]),
            )
            if source_type == "paid" and credit:
                connection.execute(
                    "UPDATE subscriptions SET consumed_bytes = consumed_bytes + ? WHERE id = ?",
                    (credit, source_id),
                )
            self._append_ledger(
                connection, entitlement_key=entitlement_key, generation_id=generation_id,
                endpoint_id=endpoint, lease_id=lease["lease_id"] if lease else None,
                epoch_id=epoch["epoch_id"], event_type="usage", bytes_value=credit,
                consumed_bytes=after, remaining_bytes=max(0, int(source["quota_bytes"]) - after),
                idempotency_key=f"usage:{generation_id}:{epoch['epoch_id']}:{timestamp}:{remote}",
                details={"remote_delta": delta, "reason": reason, "counter_mode": counter_mode}, now=timestamp,
            )
            exhausted = after >= int(source["quota_bytes"])
            if exhausted and source_type == "paid":
                connection.execute(
                    """UPDATE subscriptions SET status = CASE WHEN status = 'active' THEN 'revoked' ELSE status END,
                              quota_exhausted_at = COALESCE(quota_exhausted_at, ?)
                        WHERE id = ?""",
                    (timestamp, source_id),
                )
                connection.execute(
                    "UPDATE quota_leases SET status = 'exhausted' WHERE entitlement_key = ? AND status = 'active'",
                    (entitlement_key,),
                )
                self._append_ledger(
                    connection, entitlement_key=entitlement_key, generation_id=generation_id,
                    endpoint_id=endpoint, epoch_id=epoch["epoch_id"], event_type="exhaust",
                    bytes_value=0, consumed_bytes=after, remaining_bytes=0,
                    idempotency_key=f"exhaust:{entitlement_key}", now=timestamp,
                )
        return {
            "credited_bytes": credit,
            "delta_bytes": delta,
            "consumed_bytes": after,
            "epoch_id": epoch["epoch_id"],
            "exhausted": exhausted,
            "counter_reset": counter_reset,
            "counter_mode": counter_mode,
            "rolling_window_decrease": rolling_window_decrease,
            "initialized": initialized,
        }

    def reconcile_legacy(self, *, now: str | None = None) -> dict[str, int]:
        """Converge legacy paid/free rows into generation and lease records."""
        timestamp = _now_text(now)
        result = {"paid": 0, "free": 0, "generations": 0, "leases": 0, "errors": 0}
        with self.database.connect() as connection:
            paid = connection.execute(
                """SELECT k.*, s.status AS subscription_status, s.expires_at AS entitlement_expires_at
                     FROM paid_vpn_keys k JOIN subscriptions s ON s.id = k.subscription_id"""
            ).fetchall()
            free = (
                connection.execute(
                    """SELECT k.*,
                              (SELECT e.remote_state FROM key_termination_events e
                               WHERE e.key_id = k.id ORDER BY e.detected_at DESC LIMIT 1)
                              AS termination_state
                         FROM keys k
                        WHERE k.status IN ('active', 'revoke_failed', 'revoked')"""
                ).fetchall()
                if self._table_exists(connection, "keys")
                and self._table_exists(connection, "key_termination_events")
                else []
            )
        for row in paid:
            try:
                key = f"paid:{row['subscription_id']}"
                generation = self.ensure_generation_for_credential(
                    key, str(row["endpoint_id"] or "legacy-default"),
                    credential_id=f"paid-key:{row['id']}", external_id=str(row["outline_key_id"]),
                    protocol="outline", access_url_ciphertext=row["access_url"],
                    status="active" if row["status"] == "active" else "unknown",
                    remote_state=("unknown" if str(row["remote_ownership"] or "unknown") in {"unknown", "uncertain"} else "observed"), now=timestamp,
                    usage_baseline_provenance="unknown",
                )
                result["generations"] += 1
                if row["status"] == "active" and row["subscription_status"] == "active":
                    self.ensure_generation_lease(key, generation, str(row["endpoint_id"] or "legacy-default"),
                                                 int(row["quota_bytes"] or 0), str(row["entitlement_expires_at"]), now=timestamp)
                    result["leases"] += 1
                result["paid"] += 1
            except Exception:
                result["errors"] += 1
        for row in free:
            try:
                key = f"free:{row['id']}"
                remote_usable = row["status"] in ("active", "revoke_failed")
                generation = self.ensure_generation_for_credential(
                    key, str(row["endpoint_id"] or "legacy-default"),
                    credential_id=f"free-key:{row['id']}", external_id=str(row["outline_key_id"]),
                    protocol="outline", status="active" if remote_usable else "unknown",
                    remote_state="observed", usage_baseline_provenance="unknown", now=timestamp,
                )
                result["generations"] += 1
                if remote_usable:
                    self.ensure_generation_lease(key, generation, str(row["endpoint_id"] or "legacy-default"),
                                                 int(row["data_limit_bytes"]), str(row["expires_at"]), now=timestamp)
                    result["leases"] += 1
                elif row["termination_state"] == "deleted_verified":
                    self.mark_remote_revoked(generation, verified=True, now=timestamp)
                result["free"] += 1
            except Exception:
                result["errors"] += 1
        return result

    def generations_for_accounting(self, entitlement_key: str | None = None) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM credential_generations
                    WHERE status IN ('pending', 'active', 'retiring', 'unknown')
                      AND (? IS NULL OR entitlement_key = ?)
                    ORDER BY entitlement_key, generation_no""",
                (entitlement_key, entitlement_key),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_revoke_requested(self, generation_id: str, *, now: str | None = None) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE credential_generations SET remote_state = 'delete_requested'
                    WHERE generation_id = ? AND status IN ('pending', 'active', 'retiring', 'unknown')""",
                (generation_id,),
            )

    def mark_remote_revoked(
        self,
        generation_id: str,
        *,
        verified: bool,
        sessions_terminated: bool = True,
        now: str | None = None,
    ) -> bool:
        """Record auth revocation separately from optional session termination."""
        timestamp = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            if not verified:
                updated = connection.execute(
                    """UPDATE credential_generations SET remote_state = 'delete_requested'
                        WHERE generation_id = ? AND status != 'revoked'""",
                    (generation_id,),
                )
                return int(getattr(updated, "rowcount", 0) or 0) == 1
            if sessions_terminated:
                updated = connection.execute(
                    """UPDATE credential_generations
                          SET status = 'revoked', remote_state = 'revoked_verified',
                              revoked_at = COALESCE(revoked_at, ?), revoke_verified_at = ?
                        WHERE generation_id = ? AND status != 'revoked'""",
                    (timestamp, timestamp, generation_id),
                )
                connection.execute(
                    """UPDATE quota_leases SET status = 'released', released_at = COALESCE(released_at, ?)
                        WHERE generation_id = ? AND status = 'active'""",
                    (timestamp, generation_id),
                )
            else:
                updated = connection.execute(
                    """UPDATE credential_generations
                          SET status = CASE WHEN status = 'revoked' THEN status ELSE 'retiring' END,
                              remote_state = 'revoked_verified', revoke_verified_at = ?
                        WHERE generation_id = ? AND status != 'revoked'""",
                    (timestamp, generation_id),
                )
            return int(getattr(updated, "rowcount", 0) or 0) == 1

    def mark_sessions_terminated(self, generation_id: str, *, now: str | None = None) -> bool:
        """Finalize a verified auth revoke after session evidence is available."""
        timestamp = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            updated = connection.execute(
                """UPDATE credential_generations
                      SET status = 'revoked', revoked_at = COALESCE(revoked_at, ?)
                    WHERE generation_id = ? AND remote_state = 'revoked_verified'
                      AND status != 'revoked'""",
                (timestamp, str(generation_id)),
            )
            if int(getattr(updated, "rowcount", 0) or 0) != 1:
                # A stale/replayed acknowledgement must not release a lease
                # when the guarded generation update matched nothing.
                return False
            connection.execute(
                """UPDATE quota_leases SET status = 'released', released_at = COALESCE(released_at, ?)
                    WHERE generation_id = ? AND status = 'active'""",
                (timestamp, str(generation_id)),
            )
        return True


# Explicit alias for callers that describe this component as quota accounting.
QuotaAccountingService = IdentityService
