"""Giveaway campaign configuration and status read model."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from entitlement_support import GIVEAWAY_CODE, UTC
from entitlement_giveaway_repository import GiveawayRepository


_GIVEAWAY = GiveawayRepository()


def _campaign_window_start(campaign: Any, now: datetime) -> str:
    frequency = str(campaign["frequency"] or "campaign").lower()
    if frequency == "hourly":
        return now.replace(minute=0, second=0, microsecond=0).isoformat()
    if frequency == "daily":
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return str(campaign["starts_at"] or campaign["created_at"])


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


def _commerce_tables_exist(connection: Any) -> bool:
    return _GIVEAWAY.commerce_tables_exist(connection)


def _intent_tables_exist(connection: Any) -> bool:
    """Return whether the restart-safe free issuance table is available."""
    return _GIVEAWAY.intent_tables_exist(connection)


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
        campaign = (
            _GIVEAWAY.campaign_by_code(connection, normalized)
            if normalized
            else _GIVEAWAY.active_campaign(connection)
        )
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
        claim = _GIVEAWAY.claim_for_user(connection, campaign["code"], telegram_id)
        total_claimed = _GIVEAWAY.claim_count(connection, campaign["code"])
        window_start = self._campaign_window_start(campaign, current)
        window = _GIVEAWAY.giveaway_window(connection, campaign["code"], window_start)
        pending_intent = self._latest_free_intent(
            connection, telegram_id, "promo", str(campaign["code"])
        )
        pending_window = (
            _GIVEAWAY.pending_window_count(connection, campaign["code"], window_start)
            if self._intent_tables_exist(connection)
            else 0
        )
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
        existing = _GIVEAWAY.campaign_by_code(connection, normalized)
        if existing is None:
            _GIVEAWAY.insert_campaign(
                connection,
                code=normalized,
                quota_bytes=quota_bytes,
                duration_days=duration_days,
                winner_limit=winner_limit,
                created_at=now_text,
                starts_at=starts_at.isoformat(),
                ends_at=ends_at.isoformat(),
                frequency=frequency,
            )
        else:
            claim_count = _GIVEAWAY.claim_count(connection, normalized)
            max_window = _GIVEAWAY.max_window_claims(connection, normalized)
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
            _GIVEAWAY.update_campaign(
                connection,
                code=normalized,
                quota_bytes=quota_bytes,
                duration_days=duration_days,
                winner_limit=winner_limit,
                starts_at=starts_at.isoformat(),
                ends_at=ends_at.isoformat(),
                frequency=frequency,
                now_text=now_text,
            )
        _GIVEAWAY.deactivate_other_campaigns(connection, normalized, now_text)
    return self.giveaway_status(0, normalized, now=now)
