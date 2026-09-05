"""Shared transport values for Telegram presentation components."""

from __future__ import annotations

from datetime import timedelta, timezone


UTC = timezone.utc
DEFAULT_MAINTENANCE_INTERVAL_SECONDS = 60.0
ADMIN_CONFIRMATION_TTL = timedelta(minutes=5)
INTERACTION_STATE_TTL = timedelta(minutes=10)


class TelegramAPIError(RuntimeError):
    """A bounded, payload-free Telegram Bot API failure."""
