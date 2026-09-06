"""Small state-boundary steps used while normalizing Telegram commands."""

from __future__ import annotations

from typing import Any


def private_actor(
    message: dict[str, Any],
) -> tuple[int, int, str, str | None] | None:
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    if (
        not isinstance(chat, dict)
        or not isinstance(user, dict)
        or chat.get("type") != "private"
        or not isinstance(chat.get("id"), int)
        or not isinstance(user.get("id"), int)
        or int(chat["id"]) != int(user["id"])
    ):
        return None
    first_name = user.get("first_name") or ""
    if not isinstance(first_name, str):
        first_name = str(first_name)
    username = user.get("username")
    if username is not None and not isinstance(username, str):
        username = str(username)
    return int(chat["id"]), int(user["id"]), first_name, username


def handle_media_message(
    host: Any,
    message: dict[str, Any],
    chat_id: int,
    telegram_id: int,
) -> None:
    persisted_receipt_test = None
    if host._is_admin(telegram_id) and telegram_id not in host._receipt_test_waiting:
        persisted_receipt_test = host._load_interaction_state(telegram_id, "receipt_test")
        if persisted_receipt_test is not None:
            host._receipt_test_providers[telegram_id] = str(
                persisted_receipt_test.get("provider") or ""
            )
    if host._is_admin(telegram_id) and (
        telegram_id in host._receipt_test_waiting or persisted_receipt_test is not None
    ):
        host._receipt_test_waiting.discard(telegram_id)
        host._clear_interaction_state(telegram_id, "receipt_test")
        host._handle_receipt_diagnostic(message, chat_id, telegram_id)
        return
    host._handle_receipt(message, chat_id, telegram_id)
