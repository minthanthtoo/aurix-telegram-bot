"""Normalize Telegram updates into authorized command requests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from telegram_command_context_steps import (
    consume_admin_add,
    consume_customer_input,
    consume_receipt_verification,
    handle_media_message,
    normalize_command_text,
    private_actor,
)


@dataclass(frozen=True, slots=True)
class TelegramCommandContext:
    """Validated command data passed from the update boundary to routers."""

    message: dict[str, Any]
    chat_id: int
    telegram_id: int
    first_name: str
    username: str | None
    command: str
    args: tuple[str, ...]
    confirmed: bool


def prepare_command(host: Any, message: dict[str, Any]) -> TelegramCommandContext | None:
    """Validate one private update and consume any pending conversation state."""
    actor = private_actor(message)
    if actor is None:
        return None
    chat_id, telegram_id, first_name, username = actor

    host.service.track_user(telegram_id, first_name, username=username)
    if isinstance(message.get("chat_shared"), dict):
        host._handle_control_group_shared(message, chat_id, telegram_id)
        return None
    if message.get("photo") or message.get("document"):
        handle_media_message(host, message, chat_id, telegram_id)
        return None

    text = message.get("text") or ""
    if not isinstance(text, str) or not text.strip():
        return None
    raw_text = text.strip()
    menu_navigation = (
        raw_text in host.CUSTOMER_BUTTON_COMMANDS or raw_text in host.ADMIN_BUTTON_COMMANDS
    )
    stop, customer_text = consume_customer_input(host, chat_id, telegram_id, raw_text)
    if stop:
        return None
    stop, _pending_receipt = consume_receipt_verification(
        host, chat_id, telegram_id, raw_text, menu_navigation
    )
    if stop:
        return None
    normalized_text = customer_text
    stop, normalized_text = consume_admin_add(
        host, chat_id, telegram_id, raw_text, normalized_text
    )
    if stop:
        return None
    normalized_text = normalize_command_text(
        host, telegram_id, raw_text, normalized_text
    )

    parts = normalized_text.split()
    command = parts[0].split("@", 1)[0].lower()
    if command in host.OWNER_ONLY_COMMANDS and not host._is_owner(telegram_id):
        host._send_customer_fallback(chat_id, telegram_id)
        return None
    if command in host.ADMIN_ONLY_COMMANDS and not host._is_admin(telegram_id):
        host._send_customer_fallback(chat_id, telegram_id)
        return None
    return TelegramCommandContext(
        message=message,
        chat_id=chat_id,
        telegram_id=telegram_id,
        first_name=first_name,
        username=username,
        command=command,
        args=tuple(parts[1:]),
        confirmed=message.get("_admin_confirmed") is True,
    )
