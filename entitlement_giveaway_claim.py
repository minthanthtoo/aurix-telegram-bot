"""Giveaway claim transaction workflow."""

from __future__ import annotations

from datetime import datetime

from entitlement_models import GiveawayResult
from entitlement_support import (
    GIVEAWAY_CODE,
    GIVEAWAY_LIMIT_BYTES,
    GIVEAWAY_WINNER_LIMIT,
    UTC,
    outline_key_name as _outline_key_name,
)
from entitlement_giveaway_campaign import _GIVEAWAY


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
        _GIVEAWAY.upsert_user(connection, telegram_id, first_name, username, now_text)
        self._lock_user(connection, telegram_id)
        if normalized == GIVEAWAY_CODE:
            _GIVEAWAY.insert_default_campaign(
                connection, GIVEAWAY_CODE, GIVEAWAY_LIMIT_BYTES, GIVEAWAY_WINNER_LIMIT, now_text
            )
        suffix = " FOR UPDATE" if connection.__class__.__name__ == "_PostgresConnection" else ""
        campaign = _GIVEAWAY.campaign_by_code(
            connection, normalized, for_update=bool(suffix)
        )
        if campaign is None:
            return GiveawayResult("unavailable", reason="Promo code is invalid or unavailable.")
        existing = _GIVEAWAY.claim_for_user(connection, campaign["code"], telegram_id)
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
                _GIVEAWAY.update_intent_next(connection, intent_id, now_text)
        elif existing_intent is not None and existing_intent["status"] == "failed":
            intent_id = str(existing_intent["id"])
            _GIVEAWAY.reset_intent(connection, intent_id, now_text)
        else:
            window_start = self._campaign_window_start(campaign, current)
            window = _GIVEAWAY.giveaway_window(
                connection, campaign["code"], window_start
            )
            if window is None:
                initial_count = (
                    int(campaign["claimed_count"])
                    if str(campaign["frequency"] or "campaign") == "campaign"
                    else 0
                )
                _GIVEAWAY.insert_window(
                    connection, campaign["code"], window_start, initial_count
                )
                window_claimed = initial_count
            else:
                window_claimed = int(window["claimed_count"])
            pending_window = _GIVEAWAY.pending_window_count(
                connection, campaign["code"], window_start
            )
            window_claimed += pending_window
            remaining = max(0, int(campaign["winner_limit"]) - window_claimed)
            total_claimed = _GIVEAWAY.claim_count(connection, campaign["code"])
            pending_campaign = _GIVEAWAY.pending_campaign_count(
                connection, campaign["code"]
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
                conflict = _GIVEAWAY.paid_order_conflict(connection, telegram_id)
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
        row = _GIVEAWAY.intent_by_id(connection, intent_id)
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
            _GIVEAWAY.intent_by_id(connection, intent_id)
        )
    result = self._intent_result(completed, current, key)
    return result if isinstance(result, GiveawayResult) else GiveawayResult("pending", pending=True)
