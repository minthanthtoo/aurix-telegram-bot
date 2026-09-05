"""Compatibility facade for decomposed administrator Telegram views."""

from __future__ import annotations

from telegram_admin_capacity import TelegramAdminCapacityMixin
from telegram_admin_confirmations import TelegramAdminConfirmationMixin
from telegram_admin_navigation import TelegramAdminNavigationMixin
from telegram_admin_orders import TelegramAdminOrderMixin
from telegram_admin_state import TelegramAdminStateMixin
from telegram_transport_support import ADMIN_CONFIRMATION_TTL, UTC


class TelegramAdminMixin(
    TelegramAdminNavigationMixin,
    TelegramAdminCapacityMixin,
    TelegramAdminStateMixin,
    TelegramAdminConfirmationMixin,
    TelegramAdminOrderMixin,
):
    """Stable admin component API over focused panel handlers."""
