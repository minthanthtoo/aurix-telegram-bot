"""Normalize Telegram updates into authorized command requests."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from telegram_command_context_steps import handle_media_message, private_actor


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
    customer_text: str | None = None
    customer_input = host._customer_inputs.get(telegram_id)
    if (
        customer_input is None
        and not raw_text.startswith("/")
        and raw_text not in host.CUSTOMER_BUTTON_COMMANDS
        and raw_text not in host.ADMIN_BUTTON_COMMANDS
    ):
        persisted_input = host._load_interaction_state(telegram_id, "customer_input")
        if persisted_input is not None and isinstance(persisted_input.get("action"), str):
            customer_input = {
                "action": persisted_input["action"],
                "expires_at": time.monotonic() + 600,
            }
            host._customer_inputs[telegram_id] = customer_input
    if customer_input:
        if (
            float(customer_input.get("expires_at", 0)) <= time.monotonic()
            or raw_text in host.CUSTOMER_BUTTON_COMMANDS
            or raw_text.startswith("/")
        ):
            host._customer_inputs.pop(telegram_id, None)
            host._clear_interaction_state(telegram_id, "customer_input")
        elif customer_input.get("action") == "topup_amount":
            normalized_amount = raw_text.replace(",", "")
            if not normalized_amount.isdigit():
                host.send(
                    chat_id,
                    "Send a whole MMK amount from 1,000 to 1,000,000. Example: 7500",
                )
                return None
            host._customer_inputs.pop(telegram_id, None)
            host._clear_interaction_state(telegram_id, "customer_input")
            customer_text = f"/topup {normalized_amount}"

    pending_receipt = host._receipt_verify_inputs.get(telegram_id)
    if pending_receipt is None and host._is_admin(telegram_id) and not raw_text.startswith("/"):
        persisted_receipt = host._load_interaction_state(telegram_id, "receipt_verify")
        if persisted_receipt is not None and isinstance(persisted_receipt.get("evidence_id"), str):
            pending_receipt = persisted_receipt["evidence_id"]
            host._receipt_verify_inputs[telegram_id] = pending_receipt
    menu_navigation = (
        raw_text in host.CUSTOMER_BUTTON_COMMANDS or raw_text in host.ADMIN_BUTTON_COMMANDS
    )
    if pending_receipt and (menu_navigation or raw_text.startswith("/")):
        host._receipt_verify_inputs.pop(telegram_id, None)
        host._clear_interaction_state(telegram_id, "receipt_verify")
        pending_receipt = None
    if pending_receipt and host._is_admin(telegram_id) and not raw_text.startswith("/"):
        values = raw_text.rsplit(maxsplit=1)
        try:
            amount = int(values[1].replace(",", "")) if len(values) == 2 else 0
        except ValueError:
            amount = 0
        if len(values) != 2 or not values[0].strip() or amount <= 0:
            host.send(
                chat_id,
                "Send the receiving-account transaction ID and amount, for example:\n"
                "123456789 3000",
            )
            return None
        host._receipt_verify_inputs.pop(telegram_id, None)
        host._clear_interaction_state(telegram_id, "receipt_verify")
        host._queue_admin_confirmation(
            chat_id,
            telegram_id,
            "/verify",
            [pending_receipt, values[0].strip(), str(amount)],
            "Confirm these details match the actual receiving account.",
            "✅ I Checked · Verify",
            f"a:r:{pending_receipt}",
        )
        return None

    normalized_text = customer_text
    persisted_admin_add = None
    if host._is_owner(telegram_id) and telegram_id not in host._admin_add_waiting:
        persisted_admin_add = host._load_interaction_state(telegram_id, "admin_add")
    if host._is_owner(telegram_id) and (
        telegram_id in host._admin_add_waiting or persisted_admin_add is not None
    ):
        host._admin_add_waiting.discard(telegram_id)
        host._clear_interaction_state(telegram_id, "admin_add")
        if raw_text.isdigit():
            normalized_text = f"/addadmin {raw_text}"
        else:
            host.send(
                chat_id,
                "That is not a numeric Telegram ID. No access was changed. "
                "Open Staff & Access and try again.",
                host._owner_keyboard(),
            )
            return None
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,31}", raw_text):
        promo = host.service.giveaway_status(telegram_id, raw_text)
        if promo["exists"]:
            normalized_text = f"/claimpromo {promo['code']}"
    if normalized_text is None:
        normalized_text = host.CUSTOMER_BUTTON_COMMANDS.get(raw_text)
    if normalized_text is None and host._is_admin(telegram_id):
        normalized_text = host.ADMIN_BUTTON_COMMANDS.get(raw_text, raw_text)
    if normalized_text is None:
        normalized_text = raw_text

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
