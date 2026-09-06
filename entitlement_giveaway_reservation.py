"""Transactional reservation phase for promotional giveaway claims."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from entitlement_giveaway_campaign import _GIVEAWAY
from entitlement_giveaway_reservation_steps import (
    account_eligibility_result,
    campaign_state_result,
    existing_claim_result,
    insert_reservation,
    load_campaign,
    observe_capacity,
    resume_existing_intent,
)
from entitlement_models import GiveawayResult


@dataclass(frozen=True, slots=True)
class GiveawayReservation:
    intent_id: str | None = None
    result: GiveawayResult | None = None


def reserve_giveaway(
    self,
    *,
    telegram_id: int,
    first_name: str,
    username: str | None,
    normalized_code: str,
    current: datetime,
    now_text: str,
) -> GiveawayReservation:
    """Reserve one deterministic promo intent while holding the user lock."""
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        _GIVEAWAY.upsert_user(connection, telegram_id, first_name, username, now_text)
        self._lock_user(connection, telegram_id)
        campaign = load_campaign(connection, normalized_code, now_text)
        if campaign is None:
            return GiveawayReservation(
                result=GiveawayResult(
                    "unavailable", reason="Promo code is invalid or unavailable."
                )
            )
        existing_result = existing_claim_result(
            campaign, _GIVEAWAY.claim_for_user(connection, campaign["code"], telegram_id)
        )
        if existing_result is not None:
            return GiveawayReservation(result=existing_result)
        existing_intent = resume_existing_intent(
            self,
            connection,
            telegram_id=telegram_id,
            campaign_code=str(campaign["code"]),
            now_text=now_text,
        )
        if existing_intent is not None:
            return GiveawayReservation(intent_id=existing_intent)
        capacity = observe_capacity(connection, campaign=campaign, current=current)
        result = campaign_state_result(campaign, current, capacity.remaining_slots)
        if result is not None:
            return GiveawayReservation(result=result)
        result = account_eligibility_result(
            connection,
            telegram_id=telegram_id,
            remaining_slots=capacity.remaining_slots,
        )
        if result is not None:
            return GiveawayReservation(result=result)
        return GiveawayReservation(
            intent_id=insert_reservation(
                self,
                connection,
                campaign=campaign,
                capacity=capacity,
                telegram_id=telegram_id,
                username=username,
                current=current,
                now_text=now_text,
            )
        )
