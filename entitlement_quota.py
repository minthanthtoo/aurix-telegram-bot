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
from entitlement_quota_repository import EntitlementQuotaRepository
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


_QUOTA = EntitlementQuotaRepository()


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
        _QUOTA.begin_termination(connection, row, reason, used_bytes, now_text)
    outline = self._outline_client(str(row["server_id"] or self._default_server_id()))
    try:
        outline.delete_key(str(row["outline_key_id"]))
        getter = getattr(outline, "get_key", None)
        verified = callable(getter)
        if verified and getter(str(row["outline_key_id"])) is not None:
            raise OutlineError("Outline key still exists after delete")
    except Exception as exc:
        with self.database.connect() as connection:
            _QUOTA.mark_termination_failed(connection, row["id"], reason, type(exc).__name__)
        return False
    remote_state = "deleted_verified" if verified else "delete_accepted"
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        self._adjust_remote_key_count(
            connection, str(row["server_id"] or self._default_server_id()), -1
        )
        ConnectivityRegistry.revoke_credential(
            connection,
            server_id=str(row["server_id"] or self._default_server_id()),
            external_id=str(row["outline_key_id"]),
            now_text=now_text,
        )
        _QUOTA.finish_termination(
            connection,
            key_id=row["id"],
            reason=reason,
            state=remote_state,
            verified_at=now_text if verified else None,
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
        rows = _QUOTA.enforceable_keys(connection)
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
                    _QUOTA.update_key_usage(connection, row["id"], used, current.isoformat())
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
        rows = _QUOTA.warning_rows(connection)
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
                if not _QUOTA.warning_exists(connection, dedupe_key):
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
                    _QUOTA.insert_warning(
                        connection,
                        notification_id=_new_id(),
                        dedupe_key=dedupe_key,
                        telegram_id=int(row["telegram_id"]),
                        text=text,
                        now_text=now_text,
                    )
                    queued += 1
                _QUOTA.update_warning_percent(connection, row["id"], int(remaining_percent))
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
        rows = _QUOTA.user_usage_rows(connection, telegram_id)
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
            if not _QUOTA.table_exists(connection, "connectivity_migration_jobs"):
                return 0
            row = _QUOTA.migrated_usage(connection, telegram_id)
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
        rows = _QUOTA.expired_keys(connection, now_text)
    revoked = 0
    for row in rows:
        if self._terminate_key(row, "expiry", current):
            revoked += 1
    return revoked

def reconcile_terminations(self, now: datetime | None = None, limit: int = 20) -> int:
    """Retry recorded remote deletions, including paid-upgrade cleanup."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    with self.database.connect() as connection:
        rows = _QUOTA.pending_terminations(connection, max(1, min(int(limit), 100)))
    completed = 0
    for row in rows:
        if self._terminate_key(row, str(row["reason"]), current, row["used_bytes"]):
            completed += 1
    return completed

def pending_termination_notices(self, audience: str) -> list[dict[str, Any]]:
    column = "admin_notice_state" if audience == "admin" else "user_notice_state"
    with self.database.connect() as connection:
        return _QUOTA.termination_notices(connection, column)

def mark_termination_notice(self, event_id: int, audience: str, state: str) -> None:
    column = "admin_notice_state" if audience == "admin" else "user_notice_state"
    with self.database.connect() as connection:
        _QUOTA.mark_termination_notice(connection, column, event_id, state)

def termination_summary(self, limit: int = 20) -> list[dict[str, Any]]:
    with self.database.connect() as connection:
        return _QUOTA.termination_summary(connection, limit)

def quota_alert_preferences(self, telegram_id: int) -> dict[str, Any]:
    return get_quota_alert_preferences(self.database, telegram_id)

def set_quota_alert_preferences(self, telegram_id: int, **changes: Any) -> dict[str, Any]:
    return persist_quota_alert_preferences(self.database, telegram_id, **changes)
