"""Durable free/trial provisioning intents and Outline key convergence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet
from commerce_models import CommerceError
from connectivity_registry import ConnectivityRegistry
from identity import IdentityService
from ports import OutlineGateway
from quota_alerts import get_quota_alert_preferences, reached_alert, set_quota_alert_preferences
from repositories import RepositoryDatabase
from entitlement_models import ClaimResult, GiveawayResult, OutlineError
from entitlement_support import (
    CLAIM_PERIOD,
    FREE_INTENT_MAX_ATTEMPTS,
    FREE_INTENT_RETRY_DELAY,
    FREE_INTENT_STALE_AFTER,
    GIVEAWAY_CODE,
    GIVEAWAY_LIMIT_BYTES,
    GIVEAWAY_PERIOD,
    GIVEAWAY_WINNER_LIMIT,
    LIMIT_BYTES,
    PUBLIC_LIMIT_BYTES,
    QUOTA_WARNING_THRESHOLDS,
    TRIAL_LIMIT_BYTES,
    TRIAL_PERIOD,
    UTC,
    human_bytes as _human_bytes,
    human_decimal_bytes as _human_decimal_bytes,
    new_id as _new_id,
    outline_key_name as _outline_key_name,
)


def _outline_client(self, server_id: str | None = None) -> OutlineGateway:
    getter = getattr(self.outline, "client", None)
    return getter(server_id) if callable(getter) else self.outline

def _default_server_id(self) -> str:
    return str(getattr(self.outline, "default_server_id", "primary"))

def _table_exists(connection: Any, name: str) -> bool:
    if connection.__class__.__name__ == "_PostgresConnection":
        row = connection.execute(
            "SELECT to_regclass(?) AS table_name", (f"public.{name}",)
        ).fetchone()
        return bool(row and row["table_name"])
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None

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

from entitlement_server_allocation import _select_server_for_tier
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
