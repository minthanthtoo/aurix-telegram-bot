"""Giveaway claim orchestration across reservation and execution phases."""

from __future__ import annotations

from datetime import datetime

from entitlement_giveaway_campaign import _GIVEAWAY
from entitlement_giveaway_reservation import reserve_giveaway
from entitlement_models import GiveawayResult
from entitlement_support import GIVEAWAY_CODE, UTC


def claim_giveaway(
    self,
    telegram_id: int,
    first_name: str,
    now: datetime | None = None,
    username: str | None = None,
    code: str | None = None,
) -> GiveawayResult:
    """Reserve and issue one configured promotional entitlement."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    reservation = reserve_giveaway(
        self,
        telegram_id=telegram_id,
        first_name=first_name,
        username=username,
        normalized_code=str(code or GIVEAWAY_CODE).strip().upper(),
        current=current,
        now_text=current.isoformat(),
    )
    if reservation.result is not None:
        return reservation.result
    intent_id = reservation.intent_id
    if intent_id is None:
        return GiveawayResult(
            "unavailable", reason="Promo reservation could not be created."
        )
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
    key = self._execute_free_intent(intent_id, current)
    if key is None:
        return GiveawayResult(
            "pending",
            code=str(intent["campaign_code"]),
            quota_bytes=int(intent["quota_bytes"]),
            duration_days=int(intent["duration_days"]),
            pending=True,
        )
    with self.database.connect() as connection:
        completed = dict(_GIVEAWAY.intent_by_id(connection, intent_id))
    result = self._intent_result(completed, current, key)
    return result if isinstance(result, GiveawayResult) else GiveawayResult("pending", pending=True)
