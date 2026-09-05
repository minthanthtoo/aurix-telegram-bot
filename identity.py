"""Opaque account, device pairing, entitlement, and quota-lease primitives."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

UTC = timezone.utc


class IdentityError(RuntimeError):
    """Raised when an account or device lifecycle invariant is violated."""


def _now_text() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def account_id_for_telegram(telegram_id: int) -> str:
    """Return the legacy deterministic fallback used by old callers.

    New persisted accounts are assigned random UUIDs by :meth:`ensure_account`.
    The fallback remains for source compatibility only and is not used for new
    account storage or lookup.
    """
    digest = hashlib.sha256(f"aurix:telegram:{int(telegram_id)}".encode()).digest()[:16]
    return str(uuid.UUID(bytes=digest))


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


class IdentityService:
    def __init__(self, database: Any):
        self.database = database

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
        return self.touch_device(device_id, now=timestamp)

    def ensure_subscription_entitlement(
        self,
        telegram_id: int,
        subscription_id: str,
        *,
        kind: str,
        quota_bytes: int,
        expires_at: str,
        status: str = "active",
        now: str | None = None,
    ) -> str:
        if kind not in {"free", "paid", "trial", "promo"}:
            raise IdentityError("entitlement kind is invalid")
        if status not in {"pending", "active", "expired", "revoked", "cancelled"}:
            raise IdentityError("entitlement status is invalid")
        if int(quota_bytes) <= 0:
            raise IdentityError("entitlement quota must be positive")
        timestamp = str(now or _now_text())
        account_id = self.ensure_account(telegram_id, now=timestamp)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            existing = connection.execute(
                "SELECT entitlement_id, account_id FROM entitlements WHERE subscription_id = ?",
                (str(subscription_id),),
            ).fetchone()
            if existing is not None and str(existing["account_id"]) != account_id:
                raise IdentityError("subscription is already bound to another account")
            entitlement_id = str(existing["entitlement_id"]) if existing else f"entitlement-{secrets.token_hex(16)}"
            connection.execute(
                """INSERT INTO entitlements
                   (entitlement_id, account_id, subscription_id, source_ref, kind, quota_bytes, expires_at, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entitlement_id) DO UPDATE SET
                     account_id = excluded.account_id, kind = excluded.kind,
                     quota_bytes = excluded.quota_bytes, expires_at = excluded.expires_at,
                     status = excluded.status, updated_at = excluded.updated_at""",
                (entitlement_id, account_id, str(subscription_id), f"subscription:{subscription_id}", kind, int(quota_bytes), str(expires_at), status, timestamp, timestamp),
            )
        return entitlement_id

    def ensure_key_entitlement(
        self,
        telegram_id: int,
        *,
        server_id: str,
        local_key_ref: str,
        kind: str,
        quota_bytes: int,
        expires_at: str,
        status: str = "active",
        now: str | None = None,
    ) -> str:
        if kind not in {"free", "trial", "promo"}:
            raise IdentityError("free entitlement kind is invalid")
        if int(quota_bytes) <= 0:
            raise IdentityError("entitlement quota must be positive")
        timestamp = str(now or _now_text())
        account_id = self.ensure_account(telegram_id, now=timestamp)
        source_ref = f"key:{server_id}:{local_key_ref}"
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            existing = connection.execute(
                "SELECT entitlement_id, account_id FROM entitlements WHERE source_ref = ?",
                (source_ref,),
            ).fetchone()
            if existing is not None and str(existing["account_id"]) != account_id:
                raise IdentityError("key entitlement is already bound to another account")
            entitlement_id = str(existing["entitlement_id"]) if existing else f"entitlement-{secrets.token_hex(16)}"
            connection.execute(
                """INSERT INTO entitlements
                   (entitlement_id, account_id, source_ref, kind, quota_bytes, expires_at, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entitlement_id) DO UPDATE SET
                     quota_bytes = excluded.quota_bytes, expires_at = excluded.expires_at,
                     status = excluded.status, updated_at = excluded.updated_at""",
                (entitlement_id, account_id, source_ref, kind, int(quota_bytes), str(expires_at), status, timestamp, timestamp),
            )
        return entitlement_id

    def sync_existing_entitlements(self, *, now: str | None = None) -> dict[str, int]:
        """Backfill the additive model without changing legacy commerce rows."""
        timestamp = str(now or _now_text())
        current_time = _parse_time(timestamp)
        subscriptions = []
        paid_keys = []
        keys = []
        with self.database.connect() as connection:
            subscriptions = connection.execute(
                """SELECT telegram_id, id, plan_code, quota_bytes, expires_at, status
                     FROM subscriptions WHERE quota_bytes IS NOT NULL"""
            ).fetchall()
            paid_keys = connection.execute(
                """SELECT subscription_id, telegram_id, server_id, outline_key_id,
                          quota_bytes, status, created_at
                     FROM paid_vpn_keys
                    WHERE quota_bytes IS NOT NULL"""
            ).fetchall()
            keys = connection.execute(
                """SELECT k.id, k.telegram_id, k.server_id, k.outline_key_id, k.key_type,
                          k.data_limit_bytes, k.expires_at, k.status,
                          g.campaign_code
                     FROM keys k
                     LEFT JOIN giveaway_claims g ON g.key_id = k.id"""
            ).fetchall()
        entitlements_by_subscription: dict[str, str] = {}
        subscription_expiry: dict[str, str] = {}
        subscription_status: dict[str, str] = {}
        for row in subscriptions:
            status = str(row["status"])
            try:
                if status == "active" and _parse_time(str(row["expires_at"])) <= current_time:
                    status = "expired"
            except (TypeError, ValueError, OverflowError):
                status = "expired"
            entitlement_id = self.ensure_subscription_entitlement(
                int(row["telegram_id"]), str(row["id"]), kind="paid",
                quota_bytes=int(row["quota_bytes"]), expires_at=str(row["expires_at"]),
                status=status, now=timestamp,
            )
            entitlements_by_subscription[str(row["id"])] = entitlement_id
            subscription_expiry[str(row["id"])] = str(row["expires_at"])
            subscription_status[str(row["id"])] = status
        kind_map = {"daily_free": "free", "monthly_trial": "trial", "paid": "paid"}
        entitlements_by_key: dict[tuple[str, str], str] = {}
        for row in keys:
            kind = "promo" if row["campaign_code"] else kind_map.get(str(row["key_type"]), "free")
            if kind == "paid":
                continue
            entitlement_id = self.ensure_key_entitlement(
                int(row["telegram_id"]), server_id=str(row["server_id"]),
                local_key_ref=str(row["id"]), kind=kind,
                quota_bytes=int(row["data_limit_bytes"]), expires_at=str(row["expires_at"]),
                status=(
                    "active"
                    if str(row["status"]) == "active"
                    and _parse_time(str(row["expires_at"])) > current_time
                    else "expired"
                ),
                now=timestamp,
            )
            entitlements_by_key[(str(row["server_id"]), str(row["outline_key_id"]))] = entitlement_id
        generations = 0
        leases = 0
        # Registry rows are already rebuilt during CommerceService startup.
        # This pass only connects those durable credentials to the additive
        # identity model, so restart/backfill is safe and does not issue keys.
        with self.database.connect() as connection:
            credential_rows = connection.execute(
                """SELECT credential_id, endpoint_id, external_id, status
                     FROM connectivity_credentials
                    WHERE status = 'active'"""
            ).fetchall()
            credentials_by_key = {
                (str(row["endpoint_id"]), str(row["external_id"])): dict(row)
                for row in credential_rows
            }
            server_endpoints = {
                str(row["outline_server_id"]): str(row["endpoint_id"])
                for row in connection.execute(
                    "SELECT outline_server_id, endpoint_id FROM connectivity_endpoints"
                ).fetchall()
            }
        for row in paid_keys:
            entitlement_id = entitlements_by_subscription.get(str(row["subscription_id"]))
            endpoint_id = server_endpoints.get(str(row["server_id"]))
            credential = (
                credentials_by_key.get((endpoint_id, str(row["outline_key_id"])))
                if endpoint_id
                else None
            )
            if (
                not entitlement_id
                or credential is None
                or str(row["status"]) != "active"
                or subscription_status.get(str(row["subscription_id"])) != "active"
            ):
                continue
            generation_id = self.ensure_generation_for_credential(
                entitlement_id,
                str(credential["endpoint_id"]),
                credential_id=str(credential["credential_id"]),
                now=timestamp,
            )
            generations += 1
            if self._active_lease_for_generation(generation_id) is None:
                self.ensure_generation_lease(
                    entitlement_id,
                    generation_id,
                    str(credential["endpoint_id"]),
                    int(row["quota_bytes"]),
                    subscription_expiry[str(row["subscription_id"])],
                    now=timestamp,
                )
                leases += 1
        for row in keys:
            if str(row["status"]) != "active":
                continue
            try:
                if _parse_time(str(row["expires_at"])) <= current_time:
                    continue
            except (TypeError, ValueError, OverflowError):
                continue
            entitlement_id = entitlements_by_key.get(
                (str(row["server_id"]), str(row["outline_key_id"]))
            )
            endpoint_id = server_endpoints.get(str(row["server_id"]))
            credential = (
                credentials_by_key.get((endpoint_id, str(row["outline_key_id"])))
                if endpoint_id
                else None
            )
            if not entitlement_id or credential is None:
                continue
            generation_id = self.ensure_generation_for_credential(
                entitlement_id,
                str(credential["endpoint_id"]),
                credential_id=str(credential["credential_id"]),
                now=timestamp,
            )
            generations += 1
            if self._active_lease_for_generation(generation_id) is None:
                self.ensure_generation_lease(
                    entitlement_id,
                    generation_id,
                    str(credential["endpoint_id"]),
                    int(row["data_limit_bytes"]),
                    str(row["expires_at"]),
                    now=timestamp,
                )
                leases += 1
        return {
            "subscriptions": len(subscriptions),
            "free_keys": len(keys),
            "generations": generations,
            "leases": leases,
        }

    def ensure_generation_for_credential(
        self,
        entitlement_id: str,
        endpoint_id: str,
        *,
        credential_id: str,
        status: str = "active",
        now: str | None = None,
    ) -> str:
        """Converge one entitlement onto its current credential generation.

        A retry for the same credential is idempotent. A changed credential
        creates a new generation and revokes the previous active generation,
        preserving an auditable cutover boundary for managed clients.
        """
        if status not in {"pending", "active", "revoked", "failed"}:
            raise IdentityError("credential generation status is invalid")
        credential_id = str(credential_id or "").strip()
        if not credential_id:
            raise IdentityError("credential id is required")
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            entitlement = connection.execute(
                "SELECT 1 FROM entitlements WHERE entitlement_id = ?",
                (str(entitlement_id),),
            ).fetchone()
            credential = connection.execute(
                """SELECT endpoint_id FROM connectivity_credentials
                    WHERE credential_id = ? AND endpoint_id = ?""",
                (credential_id, str(endpoint_id)),
            ).fetchone()
            if entitlement is None or credential is None:
                raise IdentityError("generation references an unknown entitlement or credential")
            existing = connection.execute(
                """SELECT generation_id, status FROM credential_generations
                    WHERE entitlement_id = ? AND credential_id = ?
                    ORDER BY generation_no DESC LIMIT 1""",
                (str(entitlement_id), credential_id),
            ).fetchone()
            # A revoked/failed generation is historical state.  Reusing it
            # would make a reissued credential appear to have uninterrupted
            # validity and would weaken cutover/audit semantics.
            if existing is not None and str(existing["status"]) in {"pending", "active"}:
                connection.execute(
                    """UPDATE credential_generations
                          SET status = ?, revoked_at = CASE WHEN ? = 'revoked' THEN COALESCE(revoked_at, ?) ELSE revoked_at END
                        WHERE generation_id = ?""",
                    (status, status, timestamp, str(existing["generation_id"])),
                )
                return str(existing["generation_id"])
            connection.execute(
                """UPDATE credential_generations
                      SET status = 'revoked', revoked_at = COALESCE(revoked_at, ?)
                    WHERE entitlement_id = ? AND status = 'active'""",
                (timestamp, str(entitlement_id)),
            )
            latest = connection.execute(
                "SELECT COALESCE(MAX(generation_no), 0) AS latest FROM credential_generations WHERE entitlement_id = ?",
                (str(entitlement_id),),
            ).fetchone()
            generation_id = f"generation-{secrets.token_hex(16)}"
            connection.execute(
                """INSERT INTO credential_generations
                   (generation_id, entitlement_id, endpoint_id, credential_id,
                    generation_no, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    generation_id,
                    str(entitlement_id),
                    str(endpoint_id),
                    credential_id,
                    int(latest["latest"] or 0) + 1,
                    status,
                    timestamp,
                ),
            )
        return generation_id

    def _active_lease_for_generation(self, generation_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT lease_id FROM quota_leases
                    WHERE generation_id = ? AND status = 'active'
                    ORDER BY created_at DESC LIMIT 1""",
                (str(generation_id),),
            ).fetchone()
        return str(row["lease_id"]) if row is not None else None

    def ensure_generation_lease(
        self,
        entitlement_id: str,
        generation_id: str,
        endpoint_id: str,
        quota_bytes: int,
        entitlement_expires_at: str,
        *,
        now: str | None = None,
    ) -> str:
        """Reserve a bounded first quota block for a live generation."""
        existing = self._active_lease_for_generation(generation_id)
        if existing is not None:
            return existing
        current = _parse_time(str(now or _now_text()))
        expiry = _parse_time(entitlement_expires_at)
        ttl = max(30, min(2_592_000, int((expiry - current).total_seconds())))
        # Leases are deliberately bounded blocks. Large entitlements are
        # renewed with additional blocks as usage is observed rather than
        # reserving the entire customer quota in one transaction.
        lease_bytes = min(int(quota_bytes), 10 * 1024 * 1024 * 1024)
        return self.grant_lease(
            entitlement_id,
            endpoint_id,
            lease_bytes=lease_bytes,
            generation_id=generation_id,
            ttl_seconds=ttl,
            now=current.isoformat(),
        )

    def create_generation(
        self,
        entitlement_id: str,
        endpoint_id: str,
        *,
        credential_id: str | None = None,
        status: str = "active",
        now: str | None = None,
    ) -> str:
        if status not in {"pending", "active", "revoked", "failed"}:
            raise IdentityError("credential generation status is invalid")
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            if connection.execute("SELECT 1 FROM entitlements WHERE entitlement_id = ?", (str(entitlement_id),)).fetchone() is None:
                raise IdentityError("entitlement does not exist")
            if connection.execute("SELECT 1 FROM connectivity_endpoints WHERE endpoint_id = ?", (str(endpoint_id),)).fetchone() is None:
                raise IdentityError("endpoint does not exist")
            row = connection.execute(
                "SELECT COALESCE(MAX(generation_no), 0) AS latest FROM credential_generations WHERE entitlement_id = ?",
                (str(entitlement_id),),
            ).fetchone()
            generation_no = int(row["latest"] or 0) + 1
            generation_id = f"generation-{secrets.token_hex(16)}"
            connection.execute(
                """INSERT INTO credential_generations
                   (generation_id, entitlement_id, endpoint_id, credential_id, generation_no, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (generation_id, str(entitlement_id), str(endpoint_id), credential_id, generation_no, status, timestamp),
            )
        return generation_id

    def revoke_generation(self, generation_id: str, *, now: str | None = None) -> bool:
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            updated = connection.execute(
                """UPDATE credential_generations SET status = 'revoked', revoked_at = ?
                    WHERE generation_id = ? AND status != 'revoked'""",
                (timestamp, str(generation_id)),
            )
        return int(getattr(updated, "rowcount", 0) or 0) == 1

    def grant_lease(
        self,
        entitlement_id: str,
        endpoint_id: str,
        *,
        lease_bytes: int,
        generation_id: str | None = None,
        ttl_seconds: int = 900,
        now: str | None = None,
    ) -> str:
        if int(lease_bytes) <= 0 or int(lease_bytes) > 10 * 1024 * 1024 * 1024:
            raise IdentityError("lease size is invalid")
        if not 30 <= int(ttl_seconds) <= 2_592_000:
            raise IdentityError("lease TTL is outside the allowed range")
        timestamp = str(now or _now_text())
        expires = (_parse_time(timestamp) + timedelta(seconds=int(ttl_seconds))).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            entitlement = connection.execute(
                "SELECT quota_bytes, status, expires_at FROM entitlements WHERE entitlement_id = ?",
                (str(entitlement_id),),
            ).fetchone()
            endpoint = connection.execute(
                "SELECT 1 FROM connectivity_endpoints WHERE endpoint_id = ?", (str(endpoint_id),)
            ).fetchone()
            if entitlement is None or endpoint is None:
                raise IdentityError("lease references an unknown entitlement or endpoint")
            if str(entitlement["status"]) != "active" or _parse_time(str(entitlement["expires_at"])) <= _parse_time(timestamp):
                raise IdentityError("entitlement is not active")
            if generation_id is not None:
                generation = connection.execute(
                    """SELECT generation_id, endpoint_id, status
                         FROM credential_generations
                        WHERE generation_id = ? AND entitlement_id = ?""",
                    (str(generation_id), str(entitlement_id)),
                ).fetchone()
                if generation is None or str(generation["endpoint_id"]) != str(endpoint_id):
                    raise IdentityError("lease generation is unknown")
                if str(generation["status"]) != "active":
                    raise IdentityError("lease generation is not active")
                existing = connection.execute(
                    """SELECT lease_id FROM quota_leases
                        WHERE generation_id = ? AND status = 'active'
                        ORDER BY created_at DESC LIMIT 1""",
                    (str(generation_id),),
                ).fetchone()
                if existing is not None:
                    return str(existing["lease_id"])
            connection.execute(
                """UPDATE quota_leases SET status = 'expired', released_at = COALESCE(released_at, ?)
                    WHERE entitlement_id = ? AND status = 'active' AND expires_at <= ?""",
                (timestamp, str(entitlement_id), timestamp),
            )
            usage = connection.execute(
                """SELECT COALESCE(SUM(used_bytes), 0) AS consumed,
                          COALESCE(SUM(CASE WHEN status = 'active' THEN lease_bytes - used_bytes ELSE 0 END), 0) AS reserved
                     FROM quota_leases WHERE entitlement_id = ?""",
                (str(entitlement_id),),
            ).fetchone()
            available = int(entitlement["quota_bytes"]) - int(usage["consumed"] or 0) - int(usage["reserved"] or 0)
            if int(lease_bytes) > available:
                raise IdentityError("entitlement has insufficient unreserved quota")
            lease_id = f"lease-{secrets.token_hex(16)}"
            connection.execute(
                """INSERT INTO quota_leases
                   (lease_id, entitlement_id, generation_id, endpoint_id, lease_bytes, expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (lease_id, str(entitlement_id), generation_id, str(endpoint_id), int(lease_bytes), expires, timestamp),
            )
        return lease_id

    def record_lease_usage(self, lease_id: str, used_bytes: int, *, now: str | None = None) -> dict[str, Any]:
        if int(used_bytes) < 0 or int(used_bytes) > 10 * 1024 * 1024 * 1024:
            raise IdentityError("reported lease usage is invalid")
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute("SELECT * FROM quota_leases WHERE lease_id = ?", (str(lease_id),)).fetchone()
            if row is None:
                raise IdentityError("lease does not exist")
            previous = int(row["used_bytes"] or 0)
            current = min(max(previous, int(used_bytes)), int(row["lease_bytes"]))
            expired = _parse_time(str(row["expires_at"])) <= _parse_time(timestamp)
            status = (
                "exhausted" if current >= int(row["lease_bytes"])
                else "expired" if expired and str(row["status"]) == "active"
                else str(row["status"])
            )
            connection.execute(
                "UPDATE quota_leases SET used_bytes = ?, status = ?, released_at = CASE WHEN ? = 'exhausted' THEN COALESCE(released_at, ?) ELSE released_at END WHERE lease_id = ?",
                (current, status, status, timestamp, str(lease_id)),
            )
        return {"lease_id": str(lease_id), "used_bytes": current, "status": status}

    def lease_snapshot(self, telegram_id: int) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT q.lease_id, q.entitlement_id, q.endpoint_id, q.lease_bytes,
                          q.used_bytes, q.expires_at, q.status
                     FROM quota_leases q JOIN entitlements e ON e.entitlement_id = q.entitlement_id
                     JOIN account_identities i ON i.account_id = e.account_id
                    WHERE i.identity_type = 'telegram' AND i.identity_value = ?
                    ORDER BY q.created_at""",
                (str(int(telegram_id)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def routes_for_account(self, account_id: str) -> list[dict[str, Any]]:
        """Return non-secret route metadata suitable for a signed manifest."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT g.generation_id, g.entitlement_id, g.generation_no,
                          e.endpoint_id, r.display_name AS region,
                          t.protocol, t.display_name AS transport,
                          g.credential_id
                     FROM credential_generations g
                     JOIN entitlements en ON en.entitlement_id = g.entitlement_id
                     JOIN connectivity_endpoints e ON e.endpoint_id = g.endpoint_id
                     JOIN connectivity_regions r ON r.region_id = e.region_id
                     JOIN connectivity_transports t ON t.transport_id = e.transport_id
                    WHERE en.account_id = ? AND en.status = 'active' AND g.status = 'active'
                      AND e.status IN ('active', 'degraded')
                    ORDER BY r.display_name, g.generation_no""",
                (str(account_id),),
            ).fetchall()
        return [
            {
                "route_id": str(row["generation_id"]),
                "entitlement_id": str(row["entitlement_id"]),
                "endpoint_id": str(row["endpoint_id"]),
                "region": str(row["region"]),
                "protocol": str(row["protocol"]),
                "transport": str(row["transport"]),
                "credential_ref": str(row["credential_id"] or ""),
                "generation": int(row["generation_no"]),
            }
            for row in rows
        ]

    def route_secret_record(self, account_id: str, route_id: str) -> dict[str, Any] | None:
        """Return one account-owned credential record for the device API.

        The ciphertext is intentionally kept behind this narrow method. The
        manifest path never selects it, and callers must decrypt it before
        returning a customer configuration.
        """
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT g.generation_id, g.entitlement_id, g.generation_no,
                          en.kind, en.quota_bytes, en.expires_at,
                          e.endpoint_id, e.outline_server_id,
                          r.display_name AS region, t.protocol,
                          t.display_name AS transport, c.credential_id,
                          c.external_id, c.secret_ciphertext
                     FROM credential_generations g
                     JOIN entitlements en ON en.entitlement_id = g.entitlement_id
                     JOIN connectivity_endpoints e ON e.endpoint_id = g.endpoint_id
                     JOIN connectivity_regions r ON r.region_id = e.region_id
                     JOIN connectivity_transports t ON t.transport_id = e.transport_id
                     JOIN connectivity_credentials c ON c.credential_id = g.credential_id
                    WHERE en.account_id = ? AND en.status = 'active'
                      AND g.generation_id = ? AND g.status = 'active'
                      AND e.status IN ('active', 'degraded')
                      AND c.status = 'active'""",
                (str(account_id), str(route_id)),
            ).fetchone()
        return dict(row) if row is not None else None
