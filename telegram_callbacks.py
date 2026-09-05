"""Compatibility boundary for Telegram callback handling."""

from __future__ import annotations

from typing import Any

from telegram_callback_router import dispatch_callback


class TelegramCallbackMixin:
    """Keep the legacy callback method while routing to the explicit handler."""

    def handle_callback(self, query: dict[str, Any]) -> None:
        dispatch_callback(self, query)
