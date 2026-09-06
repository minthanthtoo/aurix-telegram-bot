"""Transactional reservation phase for promotional giveaway claims."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from entitlement_giveaway_campaign import _GIVEAWAY
from entitlement_models import GiveawayResult
from entitlement_support import (
    GIVEAWAY_CODE,
    GIVEAWAY_LIMIT_BYTES,
    GIVEAWAY_WINNER_LIMIT,
    outline_key_name as _outline_key_name,
)


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
        if normalized_code == GIVEAWAY_CODE:
            _GIVEAWAY.insert_default_campaign(
                connection,
                GIVEAWAY_CODE,
                GIVEAWAY_LIMIT_BYTES,
                GIVEAWAY_WINNER_LIMIT,
                now_text,
            )
        suffix = " FOR UPDATE" if connection.__class__.__name__ == "_PostgresConnection" else ""
        campaign = _GIVEAWAY.campaign_by_code(
            connection, normalized_code, for_update=bool(suffix)
        )
        if campaign is None:
            return GiveawayReservation(
                result=GiveawayResult(
                    "unavailable", reason="Promo code is invalid or unavailable."
                )
            )
        existing = _GIVEAWAY.claim_for_user(connection, campaign["code"], telegram_id)
        if existing is not None:
            return GiveawayReservation(
                result=GiveawayResult(
                    "already_won",
                    code=str(campaign["code"]),
                    quota_bytes=int(campaign["quota_bytes"]),
                    duration_days=int(campaign["duration_days"]),
                    expires_at=datetime.fromisoformat(existing["expires_at"]),
                    winner_number=int(existing["winner_number"]),
                )
            )
        existing_intent = self._latest_free_intent(
            connection, telegram_id, "promo", str(campaign["code"])
        )
        if existing_intent is not None:
            intent_id = str(existing_intent["id"])
            if existing_intent["status"] == "pending":
                _GIVEAWAY.update_intent_next(connection, intent_id, now_text)
            elif existing_intent["status"] == "failed":
                _GIVEAWAY.reset_intent(connection, intent_id, now_text)
            return GiveawayReservation(intent_id=intent_id)

        window_start = self._campaign_window_start(campaign, current)
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
        remaining = max(0, int(campaign["winner_limit"]) - window_claimed)
        total_claimed = _GIVEAWAY.claim_count(connection, campaign["code"])
        pending_campaign = _GIVEAWAY.pending_campaign_count(
            connection, campaign["code"]
        )
        state = self._campaign_state(campaign, current)
        if state != "active":
            return GiveawayReservation(
                result=GiveawayResult(
                    state,
                    code=str(campaign["code"]),
                    quota_bytes=int(campaign["quota_bytes"]),
                    duration_days=int(campaign["duration_days"]),
                    remaining_slots=remaining,
                )
            )
        if remaining <= 0:
            return GiveawayReservation(
                result=GiveawayResult(
                    "full",
                    code=str(campaign["code"]),
                    quota_bytes=int(campaign["quota_bytes"]),
                    duration_days=int(campaign["duration_days"]),
                    remaining_slots=0,
                )
            )
        if self._commerce_tables_exist(connection):
            conflict = _GIVEAWAY.paid_order_conflict(connection, telegram_id)
            if conflict is not None:
                return GiveawayReservation(
                    result=GiveawayResult(
                        "ineligible",
                        remaining_slots=remaining,
                        reason="An open or completed paid order already belongs to this account.",
                    )
                )
        server_id = self._select_server_for_tier(
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
        return GiveawayReservation(intent_id=str(intent["id"]))
