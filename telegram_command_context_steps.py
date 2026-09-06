"""Small state-boundary steps used while normalizing Telegram commands."""

from __future__ import annotations

import re
import time
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


def consume_customer_input(
    host: Any,
    chat_id: int,
    telegram_id: int,
    raw_text: str,
) -> tuple[bool, str | None]:
    """Restore and consume a pending customer amount conversation."""
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
    if not customer_input:
        return False, None
    if (
        float(customer_input.get("expires_at", 0)) <= time.monotonic()
        or raw_text in host.CUSTOMER_BUTTON_COMMANDS
        or raw_text.startswith("/")
    ):
        host._customer_inputs.pop(telegram_id, None)
        host._clear_interaction_state(telegram_id, "customer_input")
        return False, None
    if customer_input.get("action") != "topup_amount":
        return False, None
    normalized_amount = raw_text.replace(",", "")
    if not normalized_amount.isdigit():
        host.send(
            chat_id,
            "Send a whole MMK amount from 1,000 to 1,000,000. Example: 7500",
        )
        return True, None
    host._customer_inputs.pop(telegram_id, None)
    host._clear_interaction_state(telegram_id, "customer_input")
    return False, f"/topup {normalized_amount}"


def consume_receipt_verification(
    host: Any,
    chat_id: int,
    telegram_id: int,
    raw_text: str,
    menu_navigation: bool,
) -> tuple[bool, str | None]:
    """Restore and consume a pending administrator receipt verification."""
    pending_receipt = host._receipt_verify_inputs.get(telegram_id)
    if pending_receipt is None and host._is_admin(telegram_id) and not raw_text.startswith("/"):
        persisted_receipt = host._load_interaction_state(telegram_id, "receipt_verify")
        if persisted_receipt is not None and isinstance(
            persisted_receipt.get("evidence_id"), str
        ):
            pending_receipt = persisted_receipt["evidence_id"]
            host._receipt_verify_inputs[telegram_id] = pending_receipt
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
            return True, None
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
        return True, None
    return False, pending_receipt


def consume_admin_add(
    host: Any,
    chat_id: int,
    telegram_id: int,
    raw_text: str,
    normalized_text: str | None,
) -> tuple[bool, str | None]:
    """Restore and consume the owner-only add-administrator conversation."""
    persisted_admin_add = None
    if host._is_owner(telegram_id) and telegram_id not in host._admin_add_waiting:
        persisted_admin_add = host._load_interaction_state(telegram_id, "admin_add")
    if not host._is_owner(telegram_id) or not (
        telegram_id in host._admin_add_waiting or persisted_admin_add is not None
    ):
        return False, normalized_text
    host._admin_add_waiting.discard(telegram_id)
    host._clear_interaction_state(telegram_id, "admin_add")
    if raw_text.isdigit():
        return False, f"/addadmin {raw_text}"
    host.send(
        chat_id,
        "That is not a numeric Telegram ID. No access was changed. "
        "Open Staff & Access and try again.",
        host._owner_keyboard(),
    )
    return True, None


def normalize_command_text(
    host: Any,
    telegram_id: int,
    raw_text: str,
    customer_text: str | None,
) -> str:
    """Resolve promo codes and button labels into one command string."""
    normalized_text = customer_text
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,31}", raw_text):
        promo = host.service.giveaway_status(telegram_id, raw_text)
        if promo["exists"]:
            normalized_text = f"/claimpromo {promo['code']}"
    if normalized_text is None:
        normalized_text = host.CUSTOMER_BUTTON_COMMANDS.get(raw_text)
    if normalized_text is None and host._is_admin(telegram_id):
        normalized_text = host.ADMIN_BUTTON_COMMANDS.get(raw_text, raw_text)
    return normalized_text if normalized_text is not None else raw_text
