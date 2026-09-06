"""Focused steps for transactional giveaway reservation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from entitlement_giveaway_campaign import (
    _GIVEAWAY,
    _campaign_state,
    _campaign_window_start,
    _commerce_tables_exist,
)
from entitlement_models import GiveawayResult
from entitlement_support import (
    GIVEAWAY_CODE,
    GIVEAWAY_LIMIT_BYTES,
    GIVEAWAY_WINNER_LIMIT,
    outline_key_name as _outline_key_name,
)


@dataclass(frozen=True, slots=True)
class GiveawayCapacity:
    """Capacity and winner counters observed while the user lock is held."""

    window_start: str
    remaining_slots: int
    total_claimed: int
    pending_campaign: int


def load_campaign(connection: Any, normalized_code: str, now_text: str) -> Any:
    """Ensure the default campaign exists and return the requested campaign."""
    if normalized_code == GIVEAWAY_CODE:
        _GIVEAWAY.insert_default_campaign(
            connection,
            GIVEAWAY_CODE,
            GIVEAWAY_LIMIT_BYTES,
            GIVEAWAY_WINNER_LIMIT,
            now_text,
        )
    suffix = " FOR UPDATE" if connection.__class__.__name__ == "_PostgresConnection" else ""
    return _GIVEAWAY.campaign_by_code(
        connection, normalized_code, for_update=bool(suffix)
    )


def existing_claim_result(campaign: Any, existing: Any) -> GiveawayResult | None:
    """Map an already-issued claim to the public reservation result."""
    if existing is None:
        return None
    return GiveawayResult(
        "already_won",
        code=str(campaign["code"]),
        quota_bytes=int(campaign["quota_bytes"]),
        duration_days=int(campaign["duration_days"]),
        expires_at=datetime.fromisoformat(existing["expires_at"]),
        winner_number=int(existing["winner_number"]),
    )


def resume_existing_intent(
    service: Any,
    connection: Any,
    *,
    telegram_id: int,
    campaign_code: str,
    now_text: str,
) -> str | None:
    """Resume a pending/failed reservation without allocating a new slot."""
    existing_intent = service._latest_free_intent(
        connection, telegram_id, "promo", campaign_code
    )
    if existing_intent is None:
        return None
    intent_id = str(existing_intent["id"])
    if existing_intent["status"] == "pending":
        _GIVEAWAY.update_intent_next(connection, intent_id, now_text)
    elif existing_intent["status"] == "failed":
        _GIVEAWAY.reset_intent(connection, intent_id, now_text)
    return intent_id


def observe_capacity(
    connection: Any,
    *,
    campaign: Any,
    current: datetime,
) -> GiveawayCapacity:
    """Create/read the current winner window and count durable reservations."""
    window_start = _campaign_window_start(campaign, current)
    window = _GIVEAWAY.giveaway_window(connection, campaign["code"], window_start)
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
    window_claimed += _GIVEAWAY.pending_window_count(
        connection, campaign["code"], window_start
    )
    return GiveawayCapacity(
        window_start=window_start,
        remaining_slots=max(0, int(campaign["winner_limit"]) - window_claimed),
        total_claimed=_GIVEAWAY.claim_count(connection, campaign["code"]),
        pending_campaign=_GIVEAWAY.pending_campaign_count(
            connection, campaign["code"]
        ),
    )


def campaign_state_result(
    campaign: Any, current: datetime, remaining_slots: int
) -> GiveawayResult | None:
    """Map schedule and capacity state to a terminal reservation result."""
    state = _campaign_state(campaign, current)
    if state != "active":
        return GiveawayResult(
            state,
            code=str(campaign["code"]),
            quota_bytes=int(campaign["quota_bytes"]),
            duration_days=int(campaign["duration_days"]),
            remaining_slots=remaining_slots,
        )
    if remaining_slots <= 0:
        return GiveawayResult(
            "full",
            code=str(campaign["code"]),
            quota_bytes=int(campaign["quota_bytes"]),
            duration_days=int(campaign["duration_days"]),
            remaining_slots=0,
        )
    return None


def account_eligibility_result(
    connection: Any,
    *,
    telegram_id: int,
    remaining_slots: int,
) -> GiveawayResult | None:
    """Apply account-level restrictions after campaign capacity is known."""
    if not _commerce_tables_exist(connection):
        return None
    conflict = _GIVEAWAY.paid_order_conflict(connection, telegram_id)
    if conflict is None:
        return None
    return GiveawayResult(
        "ineligible",
        remaining_slots=remaining_slots,
        reason="An open or completed paid order already belongs to this account.",
    )


def insert_reservation(
    service: Any,
    connection: Any,
    *,
    campaign: Any,
    capacity: GiveawayCapacity,
    telegram_id: int,
    username: str | None,
    current: datetime,
    now_text: str,
) -> str:
    """Persist one deterministic free-intent reservation."""
    server_id = service._select_server_for_tier(
        connection,
        "PROMO",
        int(campaign["quota_bytes"]),
        current,
        telegram_id=telegram_id,
    )
    key_name = _outline_key_name(
        telegram_id,
        username,
        f"PROMO-{campaign['code']}",
        f"{int(campaign['duration_days'])}day",
        current,
    )
    intent = service._insert_free_intent(
        connection,
        telegram_id=telegram_id,
        kind="promo",
        campaign_code=str(campaign["code"]),
        window_start=capacity.window_start,
        winner_number=capacity.total_claimed + capacity.pending_campaign + 1,
        server_id=server_id,
        outline_key_id=service._deterministic_slot_id(
            "promo", str(campaign["code"]), str(telegram_id)
        ),
        key_name=key_name,
        quota_bytes=int(campaign["quota_bytes"]),
        duration_days=int(campaign["duration_days"]),
        claim_started_at=now_text,
    )
    return str(intent["id"])
