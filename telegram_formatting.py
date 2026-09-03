"""Small, dependency-free formatters for Telegram-facing values."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
_CONFIGURED_ZONE = os.environ.get("AURIX_DISPLAY_TIMEZONE", "Asia/Yangon").strip() or "Asia/Yangon"
try:
    DISPLAY_TIMEZONE = ZoneInfo(_CONFIGURED_ZONE)
except ZoneInfoNotFoundError:
    # A bad display-only setting must never prevent the bot from starting.
    DISPLAY_TIMEZONE = UTC
    _CONFIGURED_ZONE = "UTC"


def format_user_datetime(value: object, fallback: str = "-") -> str:
    """Render an ISO timestamp in the configured display zone.

    Database timestamps remain UTC and are never changed by this helper. Naive
    values are treated as UTC because all AuriX persistence writers use UTC.
    Invalid values use the caller's safe fallback instead of raising inside a
    Telegram response path.
    """

    if value is None:
        return fallback
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return fallback
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except (TypeError, ValueError):
            return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    local = parsed.astimezone(DISPLAY_TIMEZONE)
    label = "MMT" if _CONFIGURED_ZONE == "Asia/Yangon" else (local.tzname() or "local")
    return f"{local:%d %b %Y, %H:%M} {label}"
