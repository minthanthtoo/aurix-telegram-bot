"""Giveaway campaigns and customer free/trial claim use cases."""

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
from entitlement_giveaway_campaign import (
    _campaign_state,
    _campaign_window_start,
    _commerce_tables_exist,
    _GIVEAWAY,
    _intent_tables_exist,
    configure_giveaway,
    giveaway_status,
)


def _lock_user(connection: Any, telegram_id: int) -> None:
    _GIVEAWAY.lock_user(connection, telegram_id)

def _has_active_promo_gift(connection: Any, telegram_id: int, now: datetime) -> bool:
    """Return whether a live campaign and usable gift currently pause other plans."""
    now_text = now.astimezone(UTC).isoformat()
    return _GIVEAWAY.has_active_promo_gift(connection, telegram_id, now_text)

def set_giveaway_active(
    self, code: str, active: bool, now: datetime | None = None
) -> dict[str, Any]:
    normalized = str(code).strip().upper()
    now_text = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        row = _GIVEAWAY.campaign_by_code(connection, normalized)
        if row is None:
            raise ValueError("Promo campaign not found")
        _GIVEAWAY.set_active(connection, normalized, active, now_text)
    return self.giveaway_status(0, str(row["code"]), now=now)

def reconcile_giveaway_limits(self) -> int:
    """Converge already-issued remote promo keys to their stored exact quota."""
    with self.database.connect() as connection:
        rows = _GIVEAWAY.reconciliation_rows(connection)
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

from entitlement_giveaway_claim import claim_giveaway
def track_user(
    self,
    telegram_id: int,
    first_name: str,
    now: datetime | None = None,
    username: str | None = None,
) -> None:
    now_text = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    with self.database.connect() as connection:
        _GIVEAWAY.upsert_user(connection, telegram_id, first_name, username, now_text)
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
        _GIVEAWAY.upsert_user(connection, telegram_id, first_name, username, now_text)
        self._lock_user(connection, telegram_id)
        user = _GIVEAWAY.user_claim_marker(connection, telegram_id, "last_claim_at")
        existing_intent = self._latest_free_intent(connection, telegram_id, "daily")
        if existing_intent is not None and existing_intent["status"] in {
            "pending",
            "running",
            "failed",
        }:
            intent_id = str(existing_intent["id"])
            if existing_intent["status"] == "failed":
                _GIVEAWAY.reset_intent(connection, intent_id, now_text)
            elif existing_intent["status"] == "pending":
                _GIVEAWAY.update_intent_next(connection, intent_id, now_text)
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
        row = _GIVEAWAY.intent_by_id(connection, intent_id)
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
            _GIVEAWAY.intent_by_id(connection, intent_id)
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
        _GIVEAWAY.upsert_user(connection, telegram_id, first_name, username, now_text)
        self._lock_user(connection, telegram_id)
        user = _GIVEAWAY.user_claim_marker(connection, telegram_id, "trial_claimed_at")
        existing_intent = self._latest_free_intent(connection, telegram_id, "trial")
        if existing_intent is not None and existing_intent["status"] in {
            "pending",
            "running",
            "failed",
        }:
            intent_id = str(existing_intent["id"])
            if existing_intent["status"] == "failed":
                _GIVEAWAY.reset_intent(connection, intent_id, now_text)
            elif existing_intent["status"] == "pending":
                _GIVEAWAY.update_intent_next(connection, intent_id, now_text)
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
        row = _GIVEAWAY.intent_by_id(connection, intent_id)
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
            _GIVEAWAY.intent_by_id(connection, intent_id)
        )
    result = self._intent_result(completed, current, key)
    return result if isinstance(result, ClaimResult) else ClaimResult(pending=True)
