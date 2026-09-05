"""Entitlement usage observation, quota enforcement and termination use cases."""

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
from quota_alerts import (
    get_quota_alert_preferences,
    reached_alert,
    set_quota_alert_preferences as persist_quota_alert_preferences,
)
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
        try:
            aggregate_exhausted = self.identity.key_is_exhausted(
                server_id=str(row["server_id"] or self._default_server_id()),
                local_key_ref=str(row["id"]),
            )
        except Exception:
            aggregate_exhausted = False
        if aggregate_exhausted:
            # Aggregate entitlement state is authoritative even when this
            # endpoint's latest metrics call is unavailable. Do not let a
            # telemetry outage keep a globally exhausted key alive.
            if self._terminate_key(row, "quota", current, int(row["data_limit_bytes"])):
                revoked += 1
            continue
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
    return persist_quota_alert_preferences(self.database, telegram_id, **changes)
