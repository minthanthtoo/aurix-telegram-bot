"""Promotional campaign administration Telegram command router."""

from __future__ import annotations

from typing import Any

from telegram_command_context import TelegramCommandContext
from telegram_promo_admin_steps import configure_promo, set_promo_state, show_promo

PROMO_ADMIN_COMMANDS = frozenset({"/promo", "/setpromo", "/stoppromo", "/resumepromo"})


def dispatch_promo_admin_command(host: Any, context: TelegramCommandContext) -> bool:
    """Handle promo inspection and mutation after authorization and confirmation."""

    command = context.command
    if command not in PROMO_ADMIN_COMMANDS:
        return False
    chat_id = context.chat_id
    telegram_id = context.telegram_id
    args = context.args

    if command == "/promo":
        show_promo(host, chat_id, telegram_id)
    elif command == "/setpromo":
        configure_promo(host, chat_id, telegram_id, args)
    elif command in {"/stoppromo", "/resumepromo"}:
        set_promo_state(host, chat_id, telegram_id, command, args)
    return True
