"""Free and trial entitlement domain service and compatibility value types."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet

from ports import OutlineGateway
from commerce_models import CommerceError
from connectivity_registry import ConnectivityRegistry
from identity import IdentityService
from quota_alerts import (
    get_quota_alert_preferences,
    reached_alert,
    set_quota_alert_preferences,
)
from repositories import RepositoryDatabase

UTC = timezone.utc

# Warn once as the observed trailing-30-day allowance crosses these remaining
# percentages. Outline itself enforces the hard limit; these messages make the
# approaching cutoff visible before the key is removed.

PUBLIC_LIMIT_BYTES = 300_000_000


LIMIT_BYTES = PUBLIC_LIMIT_BYTES


TRIAL_LIMIT_BYTES = 3_000_000_000


CLAIM_PERIOD = timedelta(hours=24)


TRIAL_PERIOD = timedelta(days=30)


GIVEAWAY_CODE = "100GBFREE"


GIVEAWAY_LIMIT_BYTES = 100_000_000_000


GIVEAWAY_PERIOD = timedelta(days=30)


GIVEAWAY_WINNER_LIMIT = 5


QUOTA_WARNING_THRESHOLDS = ((25, 0.25), (10, 0.10), (5, 0.05))


# Free/trial/promo issuance uses the same bounded retry posture as paid
# provisioning, but keeps its own table because these entitlements do not have
# a paid subscription foreign key.  The intent is committed before any remote
# Outline call; a restart can therefore finish or retry it without issuing a
# second deterministic key.
FREE_INTENT_RETRY_DELAY = timedelta(minutes=1)
FREE_INTENT_STALE_AFTER = timedelta(minutes=5)
FREE_INTENT_MAX_ATTEMPTS = 8


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
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if amount < 1000 or unit == "TB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1000


def _human_decimal_bytes(value: int) -> str:
    amount = float(max(0, int(value)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1000 or unit == "TB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1000


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
    denied_reason: str | None = None
    pending: bool = False


@dataclass(frozen=True)
class GiveawayResult:
    outcome: str
    code: str | None = None
    quota_bytes: int | None = None
    duration_days: int | None = None
    access_url: str | None = None
    expires_at: datetime | None = None
    winner_number: int | None = None
    remaining_slots: int = 0
    reason: str | None = None
    pending: bool = False


class ClaimService:
    def __init__(
        self,
        database: RepositoryDatabase,
        outline: OutlineGateway,
        limit_bytes: int = LIMIT_BYTES,
        trial_limit_bytes: int = TRIAL_LIMIT_BYTES,
        probe_service: Any | None = None,
        access_url_key: bytes | str | None = None,
    ):
        self.database = database
        self.outline = outline
        self.limit_bytes = int(limit_bytes)
        self.trial_limit_bytes = int(trial_limit_bytes)
        self.identity = IdentityService(database)
        self.probe_service = probe_service
        try:
            self.access_url_cipher = Fernet(access_url_key) if access_url_key else None
        except (TypeError, ValueError) as exc:
            raise ValueError("access_url_key must be a Fernet key") from exc

    def _encrypt_access_url(self, access_url: str) -> str | None:
        """Encrypt an access URL for generic device delivery when configured.

        Legacy/free rows intentionally do not store plaintext URLs.  Keeping
        the cipher optional preserves the local test/staging compatibility
        path, while hosted runtime always supplies the same durable key used
        by paid-key notifications.
        """
        if self.access_url_cipher is None:
            return None
        return self.access_url_cipher.encrypt(str(access_url).encode()).decode()

    def _outline_client(self, server_id: str | None = None) -> OutlineGateway:
        getter = getattr(self.outline, "client", None)
        return getter(server_id) if callable(getter) else self.outline

    def _default_server_id(self) -> str:
        return str(getattr(self.outline, "default_server_id", "primary"))

    @staticmethod
    def _table_exists(connection: Any, name: str) -> bool:
        if connection.__class__.__name__ == "_PostgresConnection":
            row = connection.execute(
                "SELECT to_regclass(?) AS table_name", (f"public.{name}",)
            ).fetchone()
            return bool(row and row["table_name"])
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone() is not None

    @staticmethod
    def _server_tables_exist(connection: Any) -> bool:
        if connection.__class__.__name__ == "_PostgresConnection":
            row = connection.execute(
                "SELECT to_regclass('public.outline_servers') AS table_name"
            ).fetchone()
            return bool(row and row["table_name"])
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'outline_servers'"
            ).fetchone()
            is not None
        )

    def _select_server_for_tier(
        self,
        connection: Any,
        tier_code: str,
        quota_bytes: int,
        now: datetime,
        *,
        telegram_id: int | None = None,
    ) -> str:
        """Select a fresh, healthy server using shared key and traffic headroom."""
        if not self._server_tables_exist(connection):
            return self._default_server_id()
        if int(connection.execute("SELECT COUNT(*) AS n FROM outline_servers").fetchone()["n"]) == 0:
            # Standalone/local free-access databases have no registered fleet.
            # Their single configured adapter remains the authoritative target.
            return self._default_server_id()
        max_age = max(30, int(os.environ.get("AURIX_SERVER_HEALTH_MAX_AGE_SECONDS", "900")))
        fresh_after = (now - timedelta(seconds=max_age)).astimezone(UTC).isoformat()
        servers = connection.execute(
            """SELECT * FROM outline_servers
               WHERE enabled = 1 AND lifecycle_state = 'active'
                 AND health_status = 'healthy'
                 AND last_synced_at IS NOT NULL AND last_synced_at >= ?
               ORDER BY server_id""",
            (fresh_after,),
        ).fetchall()
        has_tier_allocations = int(
            connection.execute(
                "SELECT COUNT(*) AS n FROM server_tier_allocations WHERE tier_code = ?",
                (tier_code,),
            ).fetchone()["n"]
        ) > 0
        candidates: list[tuple[float, int, float, str]] = []
        for server in servers:
            server_id = str(server["server_id"])
            probe_status = "unknown"
            probe_score = -1.0
            probe = (
                connection.execute(
                    """SELECT status, score, last_observed_at
                         FROM route_health_snapshots WHERE server_id = ?""",
                    (server_id,),
                ).fetchone()
                if self._table_exists(connection, "route_health_snapshots")
                else None
            )
            if probe is not None and probe["last_observed_at"] is not None:
                try:
                    probe_fresh = datetime.fromisoformat(str(probe["last_observed_at"])).astimezone(UTC) >= now - timedelta(seconds=max_age)
                except (TypeError, ValueError, OverflowError):
                    probe_fresh = False
                if probe_fresh:
                    probe_status = str(probe["status"] or "unknown")
                    probe_score = float(probe["score"]) if probe["score"] is not None else -1.0
                    if probe_status == "unreachable":
                        continue
                    if os.environ.get("AURIX_REQUIRE_PROBE_EVIDENCE_FOR_ISSUANCE", "0").strip().lower() in {"1", "true", "yes", "on"} and probe_status == "unknown":
                        continue
            allocation = connection.execute(
                """SELECT slot_limit FROM server_tier_allocations
                   WHERE server_id = ? AND tier_code = ?""",
                (server_id, tier_code),
            ).fetchone()
            if has_tier_allocations and allocation is None:
                continue
            active_tier = connection.execute(
                """SELECT COUNT(*) AS n FROM keys k
                   LEFT JOIN giveaway_claims g ON g.key_id = k.id
                   WHERE k.server_id = ? AND k.status IN ('active', 'revoke_failed')
                     AND CASE
                       WHEN ? = 'FREE300MB' THEN k.key_type = 'daily_free'
                       WHEN ? = 'FREE3GB' THEN k.key_type = 'monthly_trial' AND g.key_id IS NULL
                       ELSE g.key_id IS NOT NULL
                     END""",
                (server_id, tier_code, tier_code),
            ).fetchone()["n"]
            pending_tier = 0
            if self._intent_tables_exist(connection):
                pending_kind = {
                    "FREE300MB": "daily",
                    "FREE3GB": "trial",
                    "PROMO": "promo",
                }.get(tier_code)
                if pending_kind:
                    pending_tier = int(
                        connection.execute(
                            """SELECT COUNT(*) AS n FROM free_provisioning_intents
                               WHERE server_id = ? AND kind = ?
                                 AND status IN ('pending', 'running')""",
                            (server_id, pending_kind),
                        ).fetchone()["n"]
                    )
            active_tier = int(active_tier) + pending_tier
            if allocation is not None and int(active_tier) >= int(allocation["slot_limit"]):
                continue
            remote_keys = int(server["remote_key_count"] or 0) + pending_tier
            max_keys = server["max_keys"]
            usable = None if max_keys is None else max(
                0, int(max_keys) - int(server["reserved_keys"] or 0)
            )
            if usable is not None and remote_keys >= usable:
                continue
            traffic_budget = server["monthly_traffic_bytes"]
            if traffic_budget is not None:
                free_committed = connection.execute(
                    """SELECT COALESCE(SUM(data_limit_bytes), 0) AS n FROM keys
                       WHERE server_id = ? AND status IN ('active', 'revoke_failed')""",
                    (server_id,),
                ).fetchone()["n"]
                paid_committed = connection.execute(
                    """SELECT COALESCE(SUM(COALESCE(quota_bytes, 0)), 0) AS n
                       FROM subscriptions WHERE server_id = ? AND status IN ('pending', 'active')""",
                    (server_id,),
                ).fetchone()["n"]
                if int(free_committed or 0) + int(paid_committed or 0) + quota_bytes > int(
                    traffic_budget
                ):
                    continue
            denominator = (
                int(allocation["slot_limit"])
                if allocation is not None and int(allocation["slot_limit"])
                else (usable or max(1, remote_keys + 1))
            )
            probe_rank = {"healthy": 0, "degraded": 1, "unknown": 2}.get(probe_status, 2)
            candidates.append((int(active_tier) / max(1, denominator), probe_rank, -probe_score, server_id))
        if not candidates:
            raise OutlineError("No healthy VPN server currently has capacity for this tier")
        selected = min(candidates)
        if self._table_exists(connection, "route_decisions"):
            selected_score = -selected[2] if selected[2] >= 0 else None
            connection.execute(
                """INSERT INTO route_decisions
                   (decision_id, telegram_id, entitlement_ref, requested_region,
                    selected_server_id, decision_mode, score, evidence_json, created_at)
                   VALUES (?, ?, NULL, NULL, ?, 'automatic', ?, ?, ?)""",
                (
                    f"decision-{_new_id()}",
                    telegram_id,
                    selected[3],
                    selected_score,
                    json.dumps(
                        {
                            "basis": "capacity_and_fresh_probe",
                            "tier_code": str(tier_code),
                            "probe_rank": selected[1],
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    now.astimezone(UTC).isoformat(),
                ),
            )
        return selected[3]

    def _adjust_remote_key_count(self, connection: Any, server_id: str, delta: int) -> None:
        if not self._server_tables_exist(connection):
            return
        connection.execute(
            """UPDATE outline_servers
               SET remote_key_count = CASE
                     WHEN COALESCE(remote_key_count, 0) + ? < 0 THEN 0
                     ELSE COALESCE(remote_key_count, 0) + ?
                   END
               WHERE server_id = ?""",
            (int(delta), int(delta), server_id),
        )

    def _sync_identity_key(
        self,
        *,
        telegram_id: int,
        local_key_id: int,
        kind: str,
        quota_bytes: int,
        expires_at: str,
        server_id: str,
        external_id: str,
        now: str | None = None,
    ) -> None:
        """Converge a committed free/trial key into managed identity state."""
        timestamp = str(now or datetime.now(UTC).isoformat())
        entitlement_id = self.identity.ensure_key_entitlement(
            int(telegram_id),
            server_id=str(server_id),
            local_key_ref=str(local_key_id),
            kind=str(kind),
            quota_bytes=int(quota_bytes),
            expires_at=str(expires_at),
            status="active",
            now=timestamp,
        )
        with self.database.connect() as connection:
            credential = connection.execute(
                """SELECT c.credential_id, c.endpoint_id
                     FROM connectivity_credentials c
                     JOIN connectivity_endpoints e ON e.endpoint_id = c.endpoint_id
                    WHERE e.outline_server_id = ? AND c.external_id = ?
                      AND c.status = 'active'""",
                (str(server_id), str(external_id)),
            ).fetchone()
        if credential is None:
            return
        generation_id = self.identity.ensure_generation_for_credential(
            entitlement_id,
            str(credential["endpoint_id"]),
            credential_id=str(credential["credential_id"]),
            now=timestamp,
        )
        self.identity.ensure_generation_lease(
            entitlement_id,
            generation_id,
            str(credential["endpoint_id"]),
            int(quota_bytes),
            str(expires_at),
            now=timestamp,
        )

    @staticmethod
    def _deterministic_slot_id(prefix: str, *parts: str) -> str:
        """Return a stable, Outline-safe ID for one entitlement issuance slot.

        Free entitlements use the previous successful claim timestamp as the
        slot seed.  If the remote create succeeds but the local transaction is
        interrupted, a retry therefore reads the same remote key instead of
        issuing a second credential.  The digest keeps timestamps and campaign
        values out of the external ID while remaining deterministic.
        """
        seed = "\x1f".join(str(part) for part in parts)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
        return f"aurix-{prefix}-{digest}"

    def _create_key_idempotent(
        self,
        outline: OutlineGateway,
        *,
        key_id: str,
        name: str,
        limit_bytes: int | None,
    ) -> tuple[dict[str, Any], bool]:
        """Create one remote key exactly once when the adapter supports it.

        Returns ``(key, created_remote)``.  ``created_remote`` is false when a
        pre-existing deterministic key was recovered; callers must not delete
        such a key while compensating for a later local-transaction failure.
        Older Outline-compatible test/adapters without deterministic PUT keep
        the legacy POST behavior, while adapters that explicitly report an
        unsupported endpoint may safely fall back to POST.
        """
        getter = getattr(outline, "get_key", None)
        existing: dict[str, Any] | None = None
        if callable(getter):
            try:
                candidate = getter(key_id)
            except Exception as exc:
                # A transport failure is not evidence that the key is absent;
                # never issue a second POST in that case.
                if getattr(exc, "status", None) not in (404, 405, 501):
                    raise
                candidate = None
            if candidate is not None:
                existing = candidate
        if existing is not None:
            if (
                not isinstance(existing, dict)
                or str(existing.get("id")) != key_id
                or not existing.get("accessUrl")
            ):
                raise OutlineError("Outline deterministic key lacks accessUrl")
            remote_name = str(existing.get("name") or "").strip()
            if remote_name and remote_name != name:
                raise OutlineError("Outline deterministic key belongs to another entitlement")
            if limit_bytes is not None:
                outline.set_data_limit(str(existing["id"]), int(limit_bytes))
            return existing, False

        creator = getattr(outline, "create_key_with_id", None)
        if not callable(creator):
            return outline.create_key(name, limit_bytes), True
        try:
            created = creator(key_id, name, limit_bytes)
            if (
                not isinstance(created, dict)
                or str(created.get("id")) != key_id
                or not created.get("accessUrl")
            ):
                raise OutlineError("Outline deterministic create response lacks accessUrl")
            return created, True
        except Exception as exc:
            # A request can time out after Outline committed the PUT.  A GET of
            # the same ID is the only safe recovery; retrying POST can create a
            # second billable/usable credential.
            if callable(getter):
                recovered = getter(key_id)
                if recovered is not None:
                    if (
                        not isinstance(recovered, dict)
                        or str(recovered.get("id")) != key_id
                        or not recovered.get("accessUrl")
                    ):
                        raise OutlineError("Outline recovered key lacks accessUrl") from exc
                    remote_name = str(recovered.get("name") or "").strip()
                    if remote_name and remote_name != name:
                        raise OutlineError(
                            "Outline recovered key belongs to another entitlement"
                        ) from exc
                    if limit_bytes is not None:
                        outline.set_data_limit(str(recovered["id"]), int(limit_bytes))
                    return recovered, False
            if getattr(exc, "status", None) in (404, 405, 501):
                return outline.create_key(name, limit_bytes), True
            raise

    def _claim_free_intent(self, intent_id: str, now: datetime) -> dict[str, Any] | None:
        """Claim one pending free entitlement intent for remote execution."""
        now_text = now.astimezone(UTC).isoformat()
        stale_before = (now - FREE_INTENT_STALE_AFTER).astimezone(UTC).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE free_provisioning_intents
                   SET status = 'pending', locked_at = NULL
                 WHERE status = 'running' AND locked_at < ?""",
                (stale_before,),
            )
            # PostgreSQL workers can run concurrently.  Lock the selected row
            # while choosing it, and skip rows owned by another worker; the
            # SQLite writer lock already serializes this section.
            lock_clause = " FOR UPDATE SKIP LOCKED" if connection.__class__.__name__ == "_PostgresConnection" else ""
            if intent_id:
                row = connection.execute(
                    """SELECT * FROM free_provisioning_intents
                       WHERE id = ?
                         AND (status = 'pending' OR (status = 'failed' AND attempts < ?))
                         AND next_attempt_at <= ?""" + lock_clause,
                    (str(intent_id), FREE_INTENT_MAX_ATTEMPTS, now_text),
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT * FROM free_provisioning_intents
                       WHERE (status = 'pending' OR (status = 'failed' AND attempts < ?))
                         AND next_attempt_at <= ?
                       ORDER BY created_at LIMIT 1""" + lock_clause,
                    (FREE_INTENT_MAX_ATTEMPTS, now_text),
                ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                """UPDATE free_provisioning_intents
                   SET status = 'running', attempts = attempts + 1, locked_at = ?
                 WHERE id = ? AND (status = 'pending' OR status = 'failed')""",
                (now_text, row["id"]),
            )
            if connection.__class__.__name__ == "_PostgresConnection" and cursor.rowcount != 1:
                # Defensive guard for a database-side status transition.  Do
                # not perform an external call unless this worker owns the row.
                return None
            result = dict(row)
            result["status"] = "running"
            result["attempts"] = int(row["attempts"] or 0) + 1
            result["locked_at"] = now_text
            return result

    def _reset_free_intent(self, intent_id: str, now: datetime) -> None:
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE free_provisioning_intents
                   SET status = 'pending', attempts = 0, next_attempt_at = ?,
                       locked_at = NULL, last_error = NULL
                 WHERE id = ? AND status = 'failed'""",
                (now.astimezone(UTC).isoformat(), str(intent_id)),
            )

    def _free_intent_failed(self, intent_id: str, error: Exception, now: datetime) -> None:
        safe_error = f"{type(error).__name__}: {str(error)[:500]}"
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                "SELECT attempts FROM free_provisioning_intents WHERE id = ?",
                (str(intent_id),),
            ).fetchone()
            attempts = int(row["attempts"] or 0) if row is not None else FREE_INTENT_MAX_ATTEMPTS
            status = "failed"
            next_attempt = (now + FREE_INTENT_RETRY_DELAY).astimezone(UTC).isoformat()
            connection.execute(
                """UPDATE free_provisioning_intents
                   SET status = ?, next_attempt_at = ?, locked_at = NULL, last_error = ?
                 WHERE id = ?""",
                (status, next_attempt, safe_error, str(intent_id)),
            )

    def _free_intent_done(self, intent_id: str, key_id: int | None, now: datetime) -> None:
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE free_provisioning_intents
                   SET status = 'done', locked_at = NULL, last_error = NULL,
                       key_id = ?, completed_at = ?
                 WHERE id = ?""",
                (key_id, now.astimezone(UTC).isoformat(), str(intent_id)),
            )

    def _remote_key_for_intent(self, intent: dict[str, Any]) -> dict[str, Any] | None:
        outline = self._outline_client(str(intent["server_id"]))
        key_id = str(intent["outline_key_id"])
        getter = getattr(outline, "get_key", None)
        if callable(getter):
            try:
                key = getter(key_id)
            except Exception as exc:
                if getattr(exc, "status", None) not in (404, 405, 501):
                    raise
                key = None
            if isinstance(key, dict) and key.get("accessUrl"):
                return key
        listed = outline.list_keys()
        items = listed.get("accessKeys", []) if isinstance(listed, dict) else listed
        if not isinstance(items, list):
            raise OutlineError("Outline returned invalid access key data")
        exact = [
            item
            for item in items
            if isinstance(item, dict)
            and str(item.get("id")) == key_id
            and item.get("accessUrl")
        ]
        if exact:
            return exact[0]
        # Older Outline-compatible adapters may only support POST, which
        # returns a server-selected ID. If the process loses the response
        # before persisting that ID, recover the one uniquely named intent
        # instead of issuing another POST. Multiple matches are ambiguous and
        # must stay in manual review rather than risking the wrong entitlement.
        key_name = str(intent.get("key_name") or "").strip()
        if key_name:
            named = [
                item
                for item in items
                if isinstance(item, dict)
                and str(item.get("name") or "").strip() == key_name
                and item.get("accessUrl")
            ]
            if len(named) > 1:
                raise OutlineError("Multiple Outline keys match the pending entitlement name")
            if named:
                return named[0]
        return None

    def _finalize_free_intent(
        self, intent: dict[str, Any], key: dict[str, Any], now: datetime
    ) -> dict[str, Any]:
        """Persist a successful remote key without repeating the remote effect."""
        remote_id = str(key.get("id") or "")
        if remote_id != str(intent["outline_key_id"]) or not key.get("accessUrl"):
            raise OutlineError("Outline free entitlement response lacks the expected key")
        now_text = now.astimezone(UTC).isoformat()
        encrypted_access_url = self._encrypt_access_url(str(key["accessUrl"]))
        local_key_id: int | None = None
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            existing = connection.execute(
                """SELECT id, telegram_id, status FROM keys
                   WHERE server_id = ? AND outline_key_id = ?""",
                (str(intent["server_id"]), remote_id),
            ).fetchone()
            if existing is not None:
                if int(existing["telegram_id"]) != int(intent["telegram_id"]):
                    raise CommerceError("Outline key is already mapped to another account")
                local_key_id = int(existing["id"])
            else:
                cursor = connection.execute(
                    """INSERT INTO keys
                       (telegram_id, server_id, outline_key_id, key_type, created_at, expires_at,
                        data_limit_bytes, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'active')""",
                    (
                        int(intent["telegram_id"]),
                        str(intent["server_id"]),
                        remote_id,
                        "monthly_trial" if intent["kind"] in {"trial", "promo"} else "daily_free",
                        str(intent["claim_started_at"]),
                        (datetime.fromisoformat(str(intent["claim_started_at"])).astimezone(UTC)
                         + timedelta(days=int(intent["duration_days"]))).isoformat(),
                        int(intent["quota_bytes"]),
                    ),
                )
                local_key_id = int(getattr(cursor, "lastrowid", 0) or 0) or None
                if local_key_id is None:
                    local_key_id = int(
                        connection.execute(
                            """SELECT id FROM keys
                               WHERE server_id = ? AND outline_key_id = ?""",
                            (str(intent["server_id"]), remote_id),
                        ).fetchone()["id"]
                    )
                self._adjust_remote_key_count(connection, str(intent["server_id"]), 1)

            if intent["kind"] == "daily":
                connection.execute(
                    "UPDATE users SET last_claim_at = ? WHERE telegram_id = ?",
                    (str(intent["claim_started_at"]), int(intent["telegram_id"])),
                )
            elif intent["kind"] == "trial":
                connection.execute(
                    "UPDATE users SET trial_claimed_at = ? WHERE telegram_id = ?",
                    (str(intent["claim_started_at"]), int(intent["telegram_id"])),
                )
            else:
                campaign_code = str(intent["campaign_code"])
                claim = connection.execute(
                    """SELECT 1 FROM giveaway_claims
                       WHERE campaign_code = ? AND telegram_id = ?""",
                    (campaign_code, int(intent["telegram_id"])),
                ).fetchone()
                if claim is None:
                    connection.execute(
                        """INSERT INTO giveaway_claims
                           (campaign_code, telegram_id, key_id, winner_number, claimed_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (
                            campaign_code,
                            int(intent["telegram_id"]),
                            local_key_id,
                            int(intent["winner_number"]),
                            str(intent["claim_started_at"]),
                        ),
                    )
                    window = connection.execute(
                        """SELECT claimed_count FROM giveaway_windows
                           WHERE campaign_code = ? AND window_start = ?""",
                        (campaign_code, str(intent["window_start"])),
                    ).fetchone()
                    if window is None:
                        connection.execute(
                            """INSERT INTO giveaway_windows
                               (campaign_code, window_start, claimed_count) VALUES (?, ?, 1)""",
                            (campaign_code, str(intent["window_start"])),
                        )
                    else:
                        connection.execute(
                            """UPDATE giveaway_windows SET claimed_count = claimed_count + 1
                               WHERE campaign_code = ? AND window_start = ?""",
                            (campaign_code, str(intent["window_start"])),
                        )
                    connection.execute(
                        """UPDATE giveaway_campaigns
                           SET claimed_count = CASE WHEN claimed_count < winner_limit
                                                    THEN claimed_count + 1 ELSE claimed_count END,
                               updated_at = ?
                         WHERE code = ?""",
                        (now_text, campaign_code),
                    )
            ConnectivityRegistry.bind_credential(
                connection,
                telegram_id=int(intent["telegram_id"]),
                server_id=str(intent["server_id"]),
                external_id=remote_id,
                secret_ciphertext=encrypted_access_url,
                now_text=now_text,
                profile_kind=(
                    "promo" if intent["kind"] == "promo"
                    else "trial" if intent["kind"] == "trial"
                    else "free"
                ),
            )
            connection.execute(
                """UPDATE free_provisioning_intents
                   SET status = 'done', locked_at = NULL, last_error = NULL,
                       key_id = ?, completed_at = ?
                 WHERE id = ?""",
                (local_key_id, now_text, str(intent["id"])),
            )
        try:
            self._sync_identity_key(
                telegram_id=int(intent["telegram_id"]),
                local_key_id=int(local_key_id or 0),
                kind=(
                    "promo" if intent["kind"] == "promo"
                    else "trial" if intent["kind"] == "trial"
                    else "free"
                ),
                quota_bytes=int(intent["quota_bytes"]),
                expires_at=(
                    datetime.fromisoformat(str(intent["claim_started_at"])).astimezone(UTC)
                    + timedelta(days=int(intent["duration_days"]))
                ).isoformat(),
                server_id=str(intent["server_id"]),
                external_id=remote_id,
                now=now_text,
            )
        except Exception as exc:
            # The free key is already durably committed. Startup backfill is
            # able to repair the additive identity view without reissuing it.
            print(f"identity free-key sync error: {type(exc).__name__}", file=sys.stderr)
        return key

    def _execute_free_intent(
        self,
        intent_id: str,
        now: datetime,
        *,
        claimed: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        intent = claimed or self._claim_free_intent(intent_id, now)
        if intent is None:
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM free_provisioning_intents WHERE id = ?",
                    (str(intent_id),),
                ).fetchone()
            if row is None or str(row["status"]) != "done":
                return None
            return self._remote_key_for_intent(dict(row))
        try:
            outline = self._outline_client(str(intent["server_id"]))
            # A failed local commit after a legacy POST can leave a
            # server-selected key whose ID was never persisted. On retries,
            # reconcile the exact deterministic ID and then the unique
            # human-readable name before creating anything else. The first
            # attempt still avoids an unnecessary list call.
            recovered = None
            if int(intent.get("attempts") or 0) > 1:
                recovered = self._remote_key_for_intent(intent)
            if recovered is not None:
                key, _created_remote = recovered, False
            else:
                key, _created_remote = self._create_key_idempotent(
                    outline,
                    key_id=str(intent["outline_key_id"]),
                    name=str(intent["key_name"]),
                    limit_bytes=int(intent["quota_bytes"]),
                )
            if str(key.get("id") or "") != str(intent["outline_key_id"]):
                # Legacy POST-only adapters cannot choose the remote ID.  Make
                # the observed ID durable before local finalization so a later
                # retry targets the exact key rather than the next POST result.
                with self.database.connect() as connection:
                    self.database.begin_write(connection)
                    connection.execute(
                        """UPDATE free_provisioning_intents SET outline_key_id = ?
                           WHERE id = ? AND status = 'running'""",
                        (str(key.get("id") or ""), str(intent["id"])),
                    )
                intent = dict(intent)
                intent["outline_key_id"] = str(key.get("id") or "")
            return self._finalize_free_intent(intent, key, now)
        except Exception as exc:
            self._free_intent_failed(str(intent["id"]), exc, now)
            raise

    def process_provisioning_intents(
        self, now: datetime | None = None, max_jobs: int = 10
    ) -> int:
        """Finish pending free/trial/promo intents outside the Telegram request path."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        processed = 0
        while processed < max(1, int(max_jobs)):
            intent = self._claim_free_intent("", current)
            if intent is None:
                break
            try:
                self._execute_free_intent(str(intent["id"]), current, claimed=intent)
            except Exception as exc:
                print(f"free entitlement worker error: {type(exc).__name__}", file=sys.stderr)
            processed += 1
        return processed

    def _insert_free_intent(
        self,
        connection: Any,
        *,
        telegram_id: int,
        kind: str,
        campaign_code: str | None,
        window_start: str | None,
        winner_number: int | None,
        server_id: str,
        outline_key_id: str,
        key_name: str,
        quota_bytes: int,
        duration_days: int,
        claim_started_at: str,
    ) -> dict[str, Any]:
        if not self._intent_tables_exist(connection):
            raise OutlineError("Free entitlement durability is not initialized")
        intent_id = _new_id()
        connection.execute(
            """INSERT INTO free_provisioning_intents
               (id, telegram_id, kind, campaign_code, window_start, winner_number,
                server_id, outline_key_id, key_name, quota_bytes, duration_days,
                claim_started_at, status, attempts, next_attempt_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)""",
            (
                intent_id,
                int(telegram_id),
                str(kind),
                campaign_code,
                window_start,
                winner_number,
                str(server_id),
                str(outline_key_id),
                str(key_name),
                int(quota_bytes),
                int(duration_days),
                str(claim_started_at),
                str(claim_started_at),
                str(claim_started_at),
            ),
        )
        row = connection.execute(
            "SELECT * FROM free_provisioning_intents WHERE id = ?", (intent_id,)
        ).fetchone()
        return dict(row)

    def _latest_free_intent(
        self, connection: Any, telegram_id: int, kind: str, campaign_code: str | None = None
    ) -> dict[str, Any] | None:
        if not self._intent_tables_exist(connection):
            return None
        if campaign_code is None:
            row = connection.execute(
                """SELECT * FROM free_provisioning_intents
                   WHERE telegram_id = ? AND kind = ? AND status != 'cancelled'
                   ORDER BY created_at DESC LIMIT 1""",
                (int(telegram_id), str(kind)),
            ).fetchone()
        else:
            row = connection.execute(
                """SELECT * FROM free_provisioning_intents
                   WHERE telegram_id = ? AND kind = ? AND campaign_code = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (int(telegram_id), str(kind), str(campaign_code)),
            ).fetchone()
        return dict(row) if row is not None else None

    def _intent_result(
        self, intent: dict[str, Any], now: datetime, key: dict[str, Any] | None = None
    ) -> ClaimResult | GiveawayResult | None:
        key = key or self._remote_key_for_intent(intent)
        if key is None:
            return None
        expires_at = datetime.fromisoformat(str(intent["claim_started_at"])).astimezone(UTC) + timedelta(
            days=int(intent["duration_days"])
        )
        if intent["kind"] == "promo":
            remaining = 0
            with self.database.connect() as connection:
                campaign = connection.execute(
                    "SELECT winner_limit, claimed_count FROM giveaway_campaigns WHERE code = ?",
                    (str(intent["campaign_code"]),),
                ).fetchone()
                if campaign is not None:
                    remaining = max(0, int(campaign["winner_limit"]) - int(campaign["claimed_count"]))
            return GiveawayResult(
                "won",
                code=str(intent["campaign_code"]),
                quota_bytes=int(intent["quota_bytes"]),
                duration_days=int(intent["duration_days"]),
                access_url=str(key["accessUrl"]),
                expires_at=expires_at,
                winner_number=int(intent["winner_number"] or 0) or None,
                remaining_slots=remaining,
            )
        return ClaimResult(access_url=str(key["accessUrl"]), expires_at=expires_at)

    def collect_metrics(self) -> dict[str, Any]:
        """Return collision-safe per-server metrics, tolerating partial outage."""
        ids = (
            self.outline.server_ids()
            if callable(getattr(self.outline, "server_ids", None))
            else (self._default_server_id(),)
        )
        by_server: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        for server_id in ids:
            try:
                payload = self._outline_client(str(server_id)).transfer_metrics()
                by_key = payload.get("bytesTransferredByUserId", {})
                if not isinstance(by_key, dict):
                    raise OutlineError("Outline returned invalid transfer metrics")
                by_server[str(server_id)] = dict(by_key)
            except Exception as exc:
                errors[str(server_id)] = type(exc).__name__
        return {"byServer": by_server, "errors": errors}

    @staticmethod
    def _usage_for_server(metrics: dict[str, Any], server_id: str) -> dict[str, Any] | None:
        scoped = metrics.get("byServer") if isinstance(metrics, dict) else None
        if isinstance(scoped, dict):
            value = scoped.get(server_id)
            return value if isinstance(value, dict) else None
        if not isinstance(metrics, dict):
            return None
        legacy = metrics.get("bytesTransferredByUserId")
        if isinstance(legacy, dict):
            return legacy
        # Older callers pass the key->bytes map directly.
        return metrics

    @staticmethod
    def _lock_user(connection: Any, telegram_id: int) -> None:
        if connection.__class__.__name__ == "_PostgresConnection":
            connection.execute(
                "SELECT telegram_id FROM users WHERE telegram_id = ? FOR UPDATE",
                (telegram_id,),
            ).fetchone()

    @staticmethod
    def _has_active_promo_gift(connection: Any, telegram_id: int, now: datetime) -> bool:
        """Return whether a live campaign and usable gift currently pause other plans."""
        now_text = now.astimezone(UTC).isoformat()
        return (
            connection.execute(
                """SELECT 1
                   FROM giveaway_claims g
                   JOIN giveaway_campaigns c ON c.code = g.campaign_code
                   JOIN keys k ON k.id = g.key_id
                   WHERE g.telegram_id = ?
                     AND c.active = 1
                     AND (c.starts_at IS NULL OR c.starts_at <= ?)
                     AND (c.ends_at IS NULL OR c.ends_at > ?)
                     AND k.status IN ('active', 'revoke_failed')
                     AND k.expires_at > ?
                     AND k.quota_reason IS NULL
                   LIMIT 1""",
                (telegram_id, now_text, now_text, now_text),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _campaign_window_start(campaign: Any, now: datetime) -> str:
        frequency = str(campaign["frequency"] or "campaign").lower()
        if frequency == "hourly":
            return now.replace(minute=0, second=0, microsecond=0).isoformat()
        if frequency == "daily":
            return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        return str(campaign["starts_at"] or campaign["created_at"])

    @staticmethod
    def _campaign_state(campaign: Any, now: datetime) -> str:
        if not bool(campaign["active"]):
            return "paused"
        starts_at = campaign["starts_at"]
        ends_at = campaign["ends_at"]
        if starts_at and now < datetime.fromisoformat(str(starts_at)).astimezone(UTC):
            return "scheduled"
        if ends_at and now >= datetime.fromisoformat(str(ends_at)).astimezone(UTC):
            return "ended"
        return "active"

    @staticmethod
    def _commerce_tables_exist(connection: Any) -> bool:
        if connection.__class__.__name__ == "_PostgresConnection":
            row = connection.execute("SELECT to_regclass('public.orders') AS table_name").fetchone()
            return bool(row and row["table_name"])
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'orders'"
        ).fetchone()
        return row is not None

    @staticmethod
    def _intent_tables_exist(connection: Any) -> bool:
        """Return whether the restart-safe free issuance table is available."""
        if connection.__class__.__name__ == "_PostgresConnection":
            row = connection.execute(
                "SELECT to_regclass('public.free_provisioning_intents') AS table_name"
            ).fetchone()
            return bool(row and row["table_name"])
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'free_provisioning_intents'"
        ).fetchone()
        return row is not None

    def giveaway_status(
        self,
        telegram_id: int,
        code: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return campaign schedule/capacity and this user's durable gift state."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        normalized = str(code or "").strip().upper()
        with self.database.connect() as connection:
            if normalized:
                campaign = connection.execute(
                    "SELECT * FROM giveaway_campaigns WHERE UPPER(code) = ?", (normalized,)
                ).fetchone()
            else:
                campaign = connection.execute(
                    """SELECT * FROM giveaway_campaigns
                       ORDER BY active DESC, COALESCE(updated_at, created_at) DESC
                       LIMIT 1"""
                ).fetchone()
            if campaign is None:
                return {
                    "exists": False,
                    "code": normalized or GIVEAWAY_CODE,
                    "active": False,
                    "campaign_state": "unavailable",
                    "winner": False,
                    "gift_active": False,
                    "access_lock_active": False,
                    "claimed_count": 0,
                    "window_claimed_count": 0,
                    "winner_limit": 0,
                    "remaining_slots": 0,
                }
            claim = connection.execute(
                """SELECT g.winner_number, g.claimed_at, k.expires_at, k.status,
                          k.quota_reason, k.data_limit_bytes
                   FROM giveaway_claims g JOIN keys k ON k.id = g.key_id
                   WHERE g.campaign_code = ? AND g.telegram_id = ?""",
                (campaign["code"], telegram_id),
            ).fetchone()
            total_claimed = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM giveaway_claims WHERE campaign_code = ?",
                    (campaign["code"],),
                ).fetchone()["n"]
            )
            window_start = self._campaign_window_start(campaign, current)
            window = connection.execute(
                """SELECT claimed_count FROM giveaway_windows
                   WHERE campaign_code = ? AND window_start = ?""",
                (campaign["code"], window_start),
            ).fetchone()
            pending_intent = self._latest_free_intent(
                connection, telegram_id, "promo", str(campaign["code"])
            )
            pending_window = int(
                connection.execute(
                    """SELECT COUNT(*) AS n FROM free_provisioning_intents
                       WHERE campaign_code = ? AND window_start = ?
                         AND status IN ('pending', 'running')""",
                    (campaign["code"], window_start),
                ).fetchone()["n"]
            ) if self._intent_tables_exist(connection) else 0
        frequency = str(campaign["frequency"] or "campaign")
        window_claimed = (
            int(window["claimed_count"])
            if window is not None
            else (total_claimed if frequency == "campaign" else 0)
        )
        window_claimed += pending_window
        winner_limit = int(campaign["winner_limit"])
        state = self._campaign_state(campaign, current)
        gift_active = bool(
            claim is not None
            and claim["status"] in ("active", "revoke_failed")
            and not claim["quota_reason"]
            and datetime.fromisoformat(str(claim["expires_at"])).astimezone(UTC) > current
        )
        result: dict[str, Any] = {
            "exists": True,
            "code": str(campaign["code"]),
            "quota_bytes": int(campaign["quota_bytes"]),
            "duration_days": int(campaign["duration_days"]),
            "frequency": frequency,
            "starts_at": campaign["starts_at"],
            "ends_at": campaign["ends_at"],
            "campaign_state": state,
            "claimed_count": total_claimed,
            "window_claimed_count": window_claimed,
            "winner_limit": winner_limit,
            "remaining_slots": max(0, winner_limit - window_claimed),
            "active": state == "active",
            "winner": claim is not None,
            "pending": bool(
                pending_intent is not None
                and pending_intent.get("status") in {"pending", "running", "failed"}
            ),
            "gift_active": gift_active,
            "access_lock_active": state == "active" and gift_active,
        }
        if claim is not None:
            result.update(dict(claim))
        return result

    def configure_giveaway(
        self,
        *,
        code: str,
        quota_bytes: int,
        duration_days: int,
        winner_limit: int,
        frequency: str,
        starts_at: datetime,
        ends_at: datetime,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Create or update the single owner-selected promo season."""
        normalized = str(code).strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{2,31}", normalized):
            raise ValueError("Promo code must be 3-32 letters, numbers, underscores, or hyphens")
        quota_bytes = int(quota_bytes)
        duration_days = int(duration_days)
        winner_limit = int(winner_limit)
        frequency = str(frequency).strip().lower()
        if not 1_000_000 <= quota_bytes <= 10_000_000_000_000:
            raise ValueError("Promo quota must be between 0.001 GB and 10,000 GB")
        if not 1 <= duration_days <= 365:
            raise ValueError("Promo duration must be between 1 and 365 days")
        if not 1 <= winner_limit <= 100_000:
            raise ValueError("Giveaway count must be between 1 and 100,000")
        if frequency not in {"campaign", "daily", "hourly"}:
            raise ValueError("Frequency must be campaign, daily, or hourly")
        starts_at = starts_at.astimezone(UTC)
        ends_at = ends_at.astimezone(UTC)
        if starts_at >= ends_at:
            raise ValueError("Promo end must be after its start")
        now_text = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            existing = connection.execute(
                "SELECT * FROM giveaway_campaigns WHERE code = ?", (normalized,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO giveaway_campaigns
                       (code, quota_bytes, duration_days, winner_limit, claimed_count,
                        active, created_at, starts_at, ends_at, frequency, updated_at)
                       VALUES (?, ?, ?, ?, 0, 1, ?, ?, ?, ?, ?)""",
                    (
                        normalized,
                        quota_bytes,
                        duration_days,
                        winner_limit,
                        now_text,
                        starts_at.isoformat(),
                        ends_at.isoformat(),
                        frequency,
                        now_text,
                    ),
                )
            else:
                claim_count = int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM giveaway_claims WHERE campaign_code = ?",
                        (normalized,),
                    ).fetchone()["n"]
                )
                max_window = int(
                    connection.execute(
                        """SELECT COALESCE(MAX(claimed_count), 0) AS n
                           FROM giveaway_windows WHERE campaign_code = ?""",
                        (normalized,),
                    ).fetchone()["n"]
                )
                if claim_count:
                    immutable_changed = any(
                        (
                            int(existing["quota_bytes"]) != quota_bytes,
                            int(existing["duration_days"]) != duration_days,
                            str(existing["frequency"] or "campaign") != frequency,
                            str(existing["starts_at"] or "") != starts_at.isoformat(),
                        )
                    )
                    if immutable_changed:
                        raise ValueError(
                            "A claimed promo's quota, duration, frequency, and start are immutable; "
                            "create a new promo code for a new season"
                        )
                if winner_limit < max_window:
                    raise ValueError(
                        f"Giveaway count cannot be below {max_window} claims already made in a window"
                    )
                connection.execute(
                    """UPDATE giveaway_campaigns
                       SET quota_bytes = ?, duration_days = ?, winner_limit = ?, active = 1,
                           starts_at = ?, ends_at = ?, frequency = ?, updated_at = ?
                       WHERE code = ?""",
                    (
                        quota_bytes,
                        duration_days,
                        winner_limit,
                        starts_at.isoformat(),
                        ends_at.isoformat(),
                        frequency,
                        now_text,
                        normalized,
                    ),
                )
            connection.execute(
                "UPDATE giveaway_campaigns SET active = 0, updated_at = ? WHERE code != ? AND active = 1",
                (now_text, normalized),
            )
        return self.giveaway_status(0, normalized, now=now)

    def set_giveaway_active(
        self, code: str, active: bool, now: datetime | None = None
    ) -> dict[str, Any]:
        normalized = str(code).strip().upper()
        now_text = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                "SELECT code FROM giveaway_campaigns WHERE UPPER(code) = ?", (normalized,)
            ).fetchone()
            if row is None:
                raise ValueError("Promo campaign not found")
            if active:
                connection.execute(
                    "UPDATE giveaway_campaigns SET active = 0, updated_at = ? WHERE code != ?",
                    (now_text, row["code"]),
                )
            connection.execute(
                "UPDATE giveaway_campaigns SET active = ?, updated_at = ? WHERE code = ?",
                (1 if active else 0, now_text, row["code"]),
            )
        return self.giveaway_status(0, str(row["code"]), now=now)

    def reconcile_giveaway_limits(self) -> int:
        """Converge already-issued remote promo keys to their stored exact quota."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT k.server_id, k.outline_key_id, c.quota_bytes
                   FROM giveaway_claims g
                   JOIN giveaway_campaigns c ON c.code = g.campaign_code
                   JOIN keys k ON k.id = g.key_id
                   WHERE k.status IN ('active', 'revoke_failed')
                     AND k.quota_reason IS NULL"""
            ).fetchall()
        if not rows:
            return 0
        updated = 0
        grouped: dict[str, list[Any]] = {}
        for row in rows:
            grouped.setdefault(str(row["server_id"] or self._default_server_id()), []).append(row)
        for server_id, server_rows in grouped.items():
            outline = self._outline_client(server_id)
            remote = outline.list_keys()
            items = remote.get("accessKeys", []) if isinstance(remote, dict) else []
            if not isinstance(items, list):
                raise OutlineError("Outline returned invalid access key data")
            existing_ids = {
                str(item.get("id"))
                for item in items
                if isinstance(item, dict) and item.get("id") is not None
            }
            for row in server_rows:
                key_id = str(row["outline_key_id"])
                if key_id not in existing_ids:
                    continue
                outline.set_data_limit(key_id, int(row["quota_bytes"]))
                updated += 1
        return updated

    def claim_giveaway(
        self,
        telegram_id: int,
        first_name: str,
        now: datetime | None = None,
        username: str | None = None,
        code: str | None = None,
    ) -> GiveawayResult:
        """Reserve and issue one configured promotional entitlement.

        The reservation is committed before the Outline call.  If the process
        dies after remote creation, the deterministic intent is recovered by
        the maintenance worker instead of creating a second key.
        """
        current = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = current.isoformat()
        normalized = str(code or GIVEAWAY_CODE).strip().upper()
        intent_id: str | None = None
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
            self._lock_user(connection, telegram_id)
            if normalized == GIVEAWAY_CODE:
                connection.execute(
                    """INSERT INTO giveaway_campaigns
                       (code, quota_bytes, duration_days, winner_limit, claimed_count, active,
                        created_at, frequency, updated_at)
                       VALUES (?, ?, 30, ?, 0, 1, ?, 'campaign', ?)
                       ON CONFLICT(code) DO NOTHING""",
                    (GIVEAWAY_CODE, GIVEAWAY_LIMIT_BYTES, GIVEAWAY_WINNER_LIMIT, now_text, now_text),
                )
            suffix = " FOR UPDATE" if connection.__class__.__name__ == "_PostgresConnection" else ""
            campaign = connection.execute(
                "SELECT * FROM giveaway_campaigns WHERE UPPER(code) = ?" + suffix,
                (normalized,),
            ).fetchone()
            if campaign is None:
                return GiveawayResult("unavailable", reason="Promo code is invalid or unavailable.")
            existing = connection.execute(
                """SELECT g.winner_number, k.expires_at
                   FROM giveaway_claims g JOIN keys k ON k.id = g.key_id
                   WHERE g.campaign_code = ? AND g.telegram_id = ?""",
                (campaign["code"], telegram_id),
            ).fetchone()
            if existing is not None:
                return GiveawayResult(
                    "already_won",
                    code=str(campaign["code"]),
                    quota_bytes=int(campaign["quota_bytes"]),
                    duration_days=int(campaign["duration_days"]),
                    expires_at=datetime.fromisoformat(existing["expires_at"]),
                    winner_number=int(existing["winner_number"]),
                )
            existing_intent = self._latest_free_intent(
                connection, telegram_id, "promo", str(campaign["code"])
            )
            if existing_intent is not None and existing_intent["status"] == "done":
                intent_id = str(existing_intent["id"])
            elif existing_intent is not None and existing_intent["status"] in {"pending", "running"}:
                intent_id = str(existing_intent["id"])
                if existing_intent["status"] == "pending":
                    connection.execute(
                        "UPDATE free_provisioning_intents SET next_attempt_at = ? WHERE id = ?",
                        (now_text, intent_id),
                    )
            elif existing_intent is not None and existing_intent["status"] == "failed":
                intent_id = str(existing_intent["id"])
                connection.execute(
                    "UPDATE free_provisioning_intents SET status = 'pending', attempts = 0, next_attempt_at = ?, locked_at = NULL, last_error = NULL WHERE id = ?",
                    (now_text, intent_id),
                )
            else:
                window_start = self._campaign_window_start(campaign, current)
                window = connection.execute(
                    """SELECT claimed_count FROM giveaway_windows
                       WHERE campaign_code = ? AND window_start = ?""",
                    (campaign["code"], window_start),
                ).fetchone()
                if window is None:
                    initial_count = (
                        int(campaign["claimed_count"])
                        if str(campaign["frequency"] or "campaign") == "campaign"
                        else 0
                    )
                    connection.execute(
                        """INSERT INTO giveaway_windows
                           (campaign_code, window_start, claimed_count) VALUES (?, ?, ?)""",
                        (campaign["code"], window_start, initial_count),
                    )
                    window_claimed = initial_count
                else:
                    window_claimed = int(window["claimed_count"])
                pending_window = int(
                    connection.execute(
                        """SELECT COUNT(*) AS n FROM free_provisioning_intents
                           WHERE campaign_code = ? AND window_start = ?
                             AND status IN ('pending', 'running')""",
                        (campaign["code"], window_start),
                    ).fetchone()["n"]
                )
                window_claimed += pending_window
                remaining = max(0, int(campaign["winner_limit"]) - window_claimed)
                total_claimed = int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM giveaway_claims WHERE campaign_code = ?",
                        (campaign["code"],),
                    ).fetchone()["n"]
                )
                pending_campaign = int(
                    connection.execute(
                        """SELECT COUNT(*) AS n FROM free_provisioning_intents
                           WHERE campaign_code = ? AND status IN ('pending', 'running')""",
                        (campaign["code"],),
                    ).fetchone()["n"]
                )
                state = self._campaign_state(campaign, current)
                if state != "active":
                    return GiveawayResult(
                        state,
                        code=str(campaign["code"]),
                        quota_bytes=int(campaign["quota_bytes"]),
                        duration_days=int(campaign["duration_days"]),
                        remaining_slots=remaining,
                    )
                if remaining <= 0:
                    return GiveawayResult(
                        "full",
                        code=str(campaign["code"]),
                        quota_bytes=int(campaign["quota_bytes"]),
                        duration_days=int(campaign["duration_days"]),
                        remaining_slots=0,
                    )
                if self._commerce_tables_exist(connection):
                    conflict = connection.execute(
                        """SELECT 1 FROM orders
                           WHERE telegram_id = ?
                             AND status IN ('awaiting_payment', 'payment_submitted')
                             AND COALESCE(refund_status, 'none') != 'refunded'
                           UNION ALL
                           SELECT 1 FROM subscriptions
                           WHERE telegram_id = ? AND status IN ('pending', 'active')
                           LIMIT 1""",
                        (telegram_id, telegram_id),
                    ).fetchone()
                    if conflict is not None:
                        return GiveawayResult(
                            "ineligible",
                            remaining_slots=remaining,
                            reason="An open or completed paid order already belongs to this account.",
                        )
                server_id = self._select_server_for_tier(
                    connection, "PROMO", int(campaign["quota_bytes"]), current,
                    telegram_id=telegram_id,
                )
                key_name = _outline_key_name(
                    telegram_id,
                    username,
                    f"PROMO-{campaign['code']}",
                    f"{int(campaign['duration_days'])}day",
                    current,
                )
                intent = self._insert_free_intent(
                    connection,
                    telegram_id=telegram_id,
                    kind="promo",
                    campaign_code=str(campaign["code"]),
                    window_start=window_start,
                    winner_number=total_claimed + pending_campaign + 1,
                    server_id=server_id,
                    outline_key_id=self._deterministic_slot_id(
                        "promo", str(campaign["code"]), str(telegram_id)
                    ),
                    key_name=key_name,
                    quota_bytes=int(campaign["quota_bytes"]),
                    duration_days=int(campaign["duration_days"]),
                    claim_started_at=now_text,
                )
                intent_id = str(intent["id"])
        if intent_id is None:
            return GiveawayResult("unavailable", reason="Promo reservation could not be created.")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM free_provisioning_intents WHERE id = ?", (intent_id,)
            ).fetchone()
        if row is None:
            return GiveawayResult("unavailable", reason="Promo reservation is unavailable.")
        intent = dict(row)
        if intent["status"] == "done":
            result = self._intent_result(intent, current)
            if isinstance(result, GiveawayResult):
                return result
        if intent["status"] == "running":
            return GiveawayResult(
                "pending",
                code=str(intent["campaign_code"]),
                quota_bytes=int(intent["quota_bytes"]),
                duration_days=int(intent["duration_days"]),
                pending=True,
            )
        try:
            key = self._execute_free_intent(intent_id, current)
        except Exception:
            raise
        if key is None:
            return GiveawayResult(
                "pending",
                code=str(intent["campaign_code"]),
                quota_bytes=int(intent["quota_bytes"]),
                duration_days=int(intent["duration_days"]),
                pending=True,
            )
        with self.database.connect() as connection:
            completed = dict(
                connection.execute(
                    "SELECT * FROM free_provisioning_intents WHERE id = ?", (intent_id,)
                ).fetchone()
            )
        result = self._intent_result(completed, current, key)
        return result if isinstance(result, GiveawayResult) else GiveawayResult("pending", pending=True)

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
        self.identity.ensure_account(telegram_id, now=now_text)

    def claim(
        self,
        telegram_id: int,
        first_name: str,
        now: datetime | None = None,
        username: str | None = None,
    ) -> ClaimResult:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = current.isoformat()
        intent_id: str | None = None
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
            self._lock_user(connection, telegram_id)
            user = connection.execute(
                "SELECT last_claim_at FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            existing_intent = self._latest_free_intent(connection, telegram_id, "daily")
            if existing_intent is not None and existing_intent["status"] in {
                "pending",
                "running",
                "failed",
            }:
                intent_id = str(existing_intent["id"])
                if existing_intent["status"] == "failed":
                    connection.execute(
                        """UPDATE free_provisioning_intents
                           SET status = 'pending', attempts = 0, next_attempt_at = ?,
                               locked_at = NULL, last_error = NULL
                         WHERE id = ?""",
                        (now_text, intent_id),
                    )
                elif existing_intent["status"] == "pending":
                    connection.execute(
                        "UPDATE free_provisioning_intents SET next_attempt_at = ? WHERE id = ?",
                        (now_text, intent_id),
                    )
            else:
                if self._has_active_promo_gift(connection, telegram_id, current):
                    return ClaimResult(denied_reason="active_promo")
                if user["last_claim_at"]:
                    next_claim = datetime.fromisoformat(user["last_claim_at"]) + CLAIM_PERIOD
                    if current < next_claim:
                        return ClaimResult(next_claim_at=next_claim)
                server_id = self._select_server_for_tier(
                    connection, "FREE300MB", self.limit_bytes, current,
                    telegram_id=telegram_id,
                )
                key_name = _outline_key_name(
                    telegram_id, username, "FREE300MB", "24hr", current
                )
                intent = self._insert_free_intent(
                    connection,
                    telegram_id=telegram_id,
                    kind="daily",
                    campaign_code=None,
                    window_start=None,
                    winner_number=None,
                    server_id=server_id,
                    outline_key_id=self._deterministic_slot_id(
                        "daily", str(telegram_id), str(user["last_claim_at"] or "first")
                    ),
                    key_name=key_name,
                    quota_bytes=self.limit_bytes,
                    duration_days=1,
                    claim_started_at=now_text,
                )
                intent_id = str(intent["id"])
        if intent_id is None:
            return ClaimResult(denied_reason="unavailable")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM free_provisioning_intents WHERE id = ?", (intent_id,)
            ).fetchone()
        if row is None:
            return ClaimResult(denied_reason="unavailable")
        intent = dict(row)
        if intent["status"] == "done":
            result = self._intent_result(intent, current)
            if isinstance(result, ClaimResult):
                return result
        if intent["status"] == "running":
            return ClaimResult(pending=True)
        key = self._execute_free_intent(intent_id, current)
        if key is None:
            return ClaimResult(pending=True)
        with self.database.connect() as connection:
            completed = dict(
                connection.execute(
                    "SELECT * FROM free_provisioning_intents WHERE id = ?", (intent_id,)
                ).fetchone()
            )
        result = self._intent_result(completed, current, key)
        return result if isinstance(result, ClaimResult) else ClaimResult(pending=True)

    def claim_trial(
        self,
        telegram_id: int,
        first_name: str,
        now: datetime | None = None,
        username: str | None = None,
    ) -> ClaimResult:
        """Reserve and issue one 3 GB entitlement per rolling 30 days."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = current.isoformat()
        intent_id: str | None = None
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
            self._lock_user(connection, telegram_id)
            user = connection.execute(
                "SELECT trial_claimed_at FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            existing_intent = self._latest_free_intent(connection, telegram_id, "trial")
            if existing_intent is not None and existing_intent["status"] in {
                "pending",
                "running",
                "failed",
            }:
                intent_id = str(existing_intent["id"])
                if existing_intent["status"] == "failed":
                    connection.execute(
                        """UPDATE free_provisioning_intents
                           SET status = 'pending', attempts = 0, next_attempt_at = ?,
                               locked_at = NULL, last_error = NULL
                         WHERE id = ?""",
                        (now_text, intent_id),
                    )
                elif existing_intent["status"] == "pending":
                    connection.execute(
                        "UPDATE free_provisioning_intents SET next_attempt_at = ? WHERE id = ?",
                        (now_text, intent_id),
                    )
            else:
                if self._has_active_promo_gift(connection, telegram_id, current):
                    return ClaimResult(denied_reason="active_promo")
                if user["trial_claimed_at"]:
                    next_claim = datetime.fromisoformat(user["trial_claimed_at"]) + TRIAL_PERIOD
                    if current < next_claim:
                        return ClaimResult(next_claim_at=next_claim)
                server_id = self._select_server_for_tier(
                    connection, "FREE3GB", self.trial_limit_bytes, current,
                    telegram_id=telegram_id,
                )
                key_name = _outline_key_name(
                    telegram_id, username, "FREE3GB", "30day", current
                )
                intent = self._insert_free_intent(
                    connection,
                    telegram_id=telegram_id,
                    kind="trial",
                    campaign_code=None,
                    window_start=None,
                    winner_number=None,
                    server_id=server_id,
                    outline_key_id=self._deterministic_slot_id(
                        "monthly", str(telegram_id), str(user["trial_claimed_at"] or "first")
                    ),
                    key_name=key_name,
                    quota_bytes=self.trial_limit_bytes,
                    duration_days=30,
                    claim_started_at=now_text,
                )
                intent_id = str(intent["id"])
        if intent_id is None:
            return ClaimResult(denied_reason="unavailable")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM free_provisioning_intents WHERE id = ?", (intent_id,)
            ).fetchone()
        if row is None:
            return ClaimResult(denied_reason="unavailable")
        intent = dict(row)
        if intent["status"] == "done":
            result = self._intent_result(intent, current)
            if isinstance(result, ClaimResult):
                return result
        if intent["status"] == "running":
            return ClaimResult(pending=True)
        key = self._execute_free_intent(intent_id, current)
        if key is None:
            return ClaimResult(pending=True)
        with self.database.connect() as connection:
            completed = dict(
                connection.execute(
                    "SELECT * FROM free_provisioning_intents WHERE id = ?", (intent_id,)
                ).fetchone()
            )
        result = self._intent_result(completed, current, key)
        return result if isinstance(result, ClaimResult) else ClaimResult(pending=True)

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
                          last_usage_observed_at = COALESCE(?, last_usage_observed_at),
                          quota_reason = CASE WHEN ? = 'quota' THEN 'quota' ELSE quota_reason END
                   WHERE id = ? AND status != 'revoked'""",
                (used_bytes, now_text if used_bytes is not None else None, reason, row["id"]),
            )
            connection.execute(
                """INSERT INTO key_termination_events
                   (key_id, telegram_id, outline_key_id, reason, used_bytes, quota_bytes,
                    expires_at, detected_at, remote_state)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'retrying')
                   ON CONFLICT(key_id, reason) DO UPDATE SET
                       used_bytes = COALESCE(excluded.used_bytes, key_termination_events.used_bytes)""",
                (
                    row["id"],
                    row["telegram_id"],
                    str(row["outline_key_id"]),
                    reason,
                    used_bytes,
                    int(row["data_limit_bytes"]),
                    row["expires_at"],
                    now_text,
                ),
            )
        outline = self._outline_client(str(row["server_id"] or self._default_server_id()))
        try:
            outline.delete_key(str(row["outline_key_id"]))
            getter = getattr(outline, "get_key", None)
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
            connection.execute("UPDATE keys SET status = 'revoked' WHERE id = ?", (row["id"],))
            self._adjust_remote_key_count(
                connection, str(row["server_id"] or self._default_server_id()), -1
            )
            ConnectivityRegistry.revoke_credential(
                connection,
                server_id=str(row["server_id"] or self._default_server_id()),
                external_id=str(row["outline_key_id"]),
                now_text=now_text,
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
            metrics = self.collect_metrics()
        current = (now or datetime.now(UTC)).astimezone(UTC)
        try:
            self.queue_quota_warnings(current, metrics)
        except Exception as exc:
            # A notification outage must never delay the hard quota revoke.
            print(f"quota warning error: {type(exc).__name__}", file=sys.stderr)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT id, telegram_id, server_id, outline_key_id,
                          data_limit_bytes, expires_at FROM keys
                   WHERE status = 'active' OR (status = 'revoke_failed' AND quota_reason = 'quota')"""
            ).fetchall()
        revoked = 0
        for row in rows:
            by_key = self._usage_for_server(
                metrics, str(row["server_id"] or self._default_server_id())
            )
            # Missing metrics are an endpoint outage, never evidence of zero usage.
            if by_key is None:
                continue
            try:
                key_id = str(row["outline_key_id"])
                observed = key_id in by_key
                used = int(by_key.get(key_id, 0) or 0)
            except (TypeError, ValueError):
                continue
            if used < int(row["data_limit_bytes"]):
                if observed:
                    with self.database.connect() as connection:
                        connection.execute(
                            """UPDATE keys
                                  SET last_usage_bytes = ?, last_usage_observed_at = ?
                                WHERE id = ? AND status = 'active'""",
                            (used, current.isoformat(), row["id"]),
                        )
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
            metrics = self.collect_metrics()
        current = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = current.isoformat()
        queued = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            rows = connection.execute(
                """SELECT keys.id, keys.telegram_id, keys.server_id, keys.outline_key_id,
                          keys.data_limit_bytes, keys.expires_at,
                          keys.quota_warning_percent, g.campaign_code
                   FROM keys
                   LEFT JOIN giveaway_claims g ON g.key_id = keys.id
                   WHERE keys.status = 'active'"""
            ).fetchall()
            for row in rows:
                by_key = self._usage_for_server(
                    metrics, str(row["server_id"] or self._default_server_id())
                )
                if by_key is None:
                    continue
                try:
                    used = max(0, int(by_key.get(str(row["outline_key_id"]), 0) or 0))
                    quota = int(row["data_limit_bytes"])
                except (TypeError, ValueError):
                    continue
                if quota <= 0 or used >= quota:
                    continue
                remaining = quota - used
                preferences = get_quota_alert_preferences(self.database, int(row["telegram_id"]))
                reached = reached_alert(preferences, quota, remaining)
                if reached is None:
                    continue
                threshold_bytes, threshold_label = reached
                remaining_percent = remaining * 100 / quota
                dedupe_key = (
                    f"quota-warning:free:{row['id']}:v{preferences.get('version', 1)}:"
                    f"{threshold_bytes}"
                )
                try:
                    existing = connection.execute(
                        "SELECT id FROM notifications WHERE dedupe_key = ?",
                        (dedupe_key,),
                    ).fetchone()
                    if existing is None:
                        if row["campaign_code"]:
                            tier = f"promo {row['campaign_code']}"
                        elif quota == TRIAL_LIMIT_BYTES:
                            tier = "monthly 3 GB"
                        elif quota == PUBLIC_LIMIT_BYTES:
                            tier = "daily 300 MB"
                        else:
                            tier = "free"
                        formatter = _human_decimal_bytes if row["campaign_code"] else _human_bytes
                        text = (
                            f"📶 VPN usage alert: your AuriX {tier} key has "
                            f"{formatter(remaining)} remaining "
                            f"({remaining_percent:.1f}% of {formatter(quota)}).\n"
                            f"Your configured alert level: {threshold_label} remaining.\n"
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
                        (int(remaining_percent), row["id"]),
                    )
                except Exception as exc:
                    if self.database.is_integrity_error(exc):
                        continue
                    raise
        return queued

    def user_usage(
        self,
        telegram_id: int,
        usage_by_key: dict[str, Any],
        access_by_key: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return this user's current free/trial key state for the customer dashboard."""
        access_by_key = access_by_key or {}
        now = datetime.now(UTC)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT keys.server_id, keys.outline_key_id, keys.key_type, keys.created_at,
                          keys.expires_at, keys.data_limit_bytes, keys.status,
                          keys.last_usage_bytes, keys.quota_reason,
                          g.campaign_code,
                          (SELECT remote_state FROM key_termination_events e
                           WHERE e.key_id = keys.id ORDER BY e.detected_at DESC LIMIT 1) AS termination_state,
                          (SELECT r.status FROM managed_key_repair_jobs r
                           WHERE r.kind = 'free'
                             AND r.server_id = keys.server_id
                             AND r.local_key_ref = CAST(keys.id AS TEXT)
                           ORDER BY r.created_at DESC LIMIT 1) AS repair_status,
                          (SELECT r.last_error FROM managed_key_repair_jobs r
                           WHERE r.kind = 'free'
                             AND r.server_id = keys.server_id
                             AND r.local_key_ref = CAST(keys.id AS TEXT)
                           ORDER BY r.created_at DESC LIMIT 1) AS repair_reason
                   FROM keys
                   LEFT JOIN giveaway_claims g ON g.key_id = keys.id
                   WHERE keys.telegram_id = ?
                     AND (keys.status IN ('active', 'revoke_failed') OR keys.quota_reason = 'quota')
                   ORDER BY keys.created_at DESC LIMIT 10""",
                (telegram_id,),
            ).fetchall()
        tiers = {
            300_000_000: "Daily Free 300 MB",
            3_000_000_000: "Monthly Free 3 GB",
        }
        result = []
        for row in rows:
            server_id = str(row["server_id"] or self._default_server_id())
            key_id = str(row["outline_key_id"])
            scoped_usage = self._usage_for_server(usage_by_key, server_id)
            if scoped_usage is None:
                scoped_usage = {}
                observed = False
            else:
                observed = key_id in scoped_usage
            raw_used = scoped_usage.get(key_id, row["last_usage_bytes"] or 0)
            scoped_access: dict[str, str]
            nested_access = access_by_key.get("byServer") if isinstance(access_by_key, dict) else None
            if isinstance(nested_access, dict) and isinstance(nested_access.get(server_id), dict):
                scoped_access = nested_access[server_id]
            else:
                scoped_access = access_by_key
            try:
                used = max(0, int(raw_used or 0))
            except (TypeError, ValueError):
                used = max(0, int(row["last_usage_bytes"] or 0))
                observed = False
            quota = int(row["data_limit_bytes"])
            effective_status = (
                "quota exhausted"
                if row["quota_reason"] == "quota"
                else (
                    "revocation failed"
                    if row["termination_state"] == "escalated"
                    else (
                        "revocation pending"
                        if row["termination_state"] in ("retrying", "delete_accepted")
                        or row["status"] == "revoke_failed"
                        else (
                            "expired"
                            if datetime.fromisoformat(row["expires_at"]).astimezone(UTC) <= now
                            else row["status"]
                        )
                    )
                )
            )
            result.append(
                {
                    "outline_key_id": key_id,
                    "server_id": server_id,
                    "key_type": row["key_type"],
                    "tier": (
                        f"{quota / 1_000_000_000:g} GB Promo · {row['campaign_code']}"
                        if row["campaign_code"]
                        else tiers.get(quota, "Free access")
                    ),
                    "decimal_quota": bool(row["campaign_code"]),
                    "used_bytes": used,
                    "quota_bytes": quota,
                    "remaining_bytes": max(0, quota - used),
                    "usage_observed": observed,
                    "expires_at": row["expires_at"],
                    "status": effective_status,
                    "repair_status": row["repair_status"],
                    "repair_reason": row["repair_reason"],
                    "access_url": scoped_access.get(key_id)
                    if effective_status == "active"
                    else None,
                    "created_at": row["created_at"],
                }
            )
        return result

    def user_migrated_usage(self, telegram_id: int) -> int:
        """Return measured traffic consumed by this account before turnover.

        A migrated credential is replaced in ``keys``; the source usage is
        retained in the shared migration ledger so an account-wide dashboard
        does not make a rotated key look like a fresh quota. Missing legacy
        tables are treated as zero for standalone/free-only databases.
        """
        with self.database.connect() as connection:
            try:
                if connection.__class__.__name__ == "_PostgresConnection":
                    exists = connection.execute(
                        "SELECT to_regclass('public.connectivity_migration_jobs') AS table_name"
                    ).fetchone()
                    if not (exists and exists["table_name"]):
                        return 0
                else:
                    exists = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        ("connectivity_migration_jobs",),
                    ).fetchone()
                    if exists is None:
                        return 0
                row = connection.execute(
                    """SELECT COALESCE(SUM(source_used_bytes), 0) AS used
                         FROM connectivity_migration_jobs
                        WHERE telegram_id = ?
                          AND status IN ('source_delete_pending', 'completed')
                          AND source_used_bytes IS NOT NULL""",
                    (int(telegram_id),),
                ).fetchone()
            except Exception:
                # This is a read-only enhancement. A migration-table outage
                # must not hide the user's current keys or block the bot.
                return 0
        try:
            return max(0, int(row["used"] if row is not None else 0))
        except (KeyError, TypeError, ValueError):
            return 0

    def revoke_expired(self, now: datetime | None = None) -> int:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = current.isoformat()
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT id, telegram_id, server_id, outline_key_id,
                          data_limit_bytes, expires_at FROM keys
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
                """SELECT k.id, k.telegram_id, k.server_id, k.outline_key_id, k.data_limit_bytes,
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
                dict(row)
                for row in connection.execute(
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
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM key_termination_events
                   ORDER BY detected_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            ]

    def quota_alert_preferences(self, telegram_id: int) -> dict[str, Any]:
        return get_quota_alert_preferences(self.database, telegram_id)

    def set_quota_alert_preferences(self, telegram_id: int, **changes: Any) -> dict[str, Any]:
        return set_quota_alert_preferences(self.database, telegram_id, **changes)
