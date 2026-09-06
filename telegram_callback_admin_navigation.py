"""Navigation and confirmation actions from administrator callbacks."""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from typing import Any

from telegram_callback_admin_navigation_steps import execute_navigation, navigation_target

UTC = timezone.utc


def _handle_probe_action(
    host: Any,
    chat_id: int,
    telegram_id: int,
    message_id: Any,
    can_edit_text: bool,
    entity_id: str,
) -> None:
    if entity_id != "enqueue":
        host.send(chat_id, "This probe action is no longer valid.")
        return
    try:
        host._admin_probe_call(telegram_id, "enqueue_due_probes", limit=100)
    except Exception as exc:
        host.send(chat_id, "Probe jobs could not be queued.", host._admin_keyboard(telegram_id))
        print(f"probe enqueue error: {type(exc).__name__}", file=sys.stderr)
        return
    host._show_probes(
        chat_id,
        telegram_id,
        message_id=message_id if can_edit_text else None,
    )


def _handle_confirmation_action(
    host: Any,
    chat_id: int,
    telegram_id: int,
    synthetic: dict[str, Any],
    entity_id: str,
) -> None:
    challenge = host._consume_admin_confirmation(chat_id, telegram_id, entity_id)
    if challenge is None:
        host.send(
            chat_id,
            "This confirmation has expired or was already used. Open the admin panel again.",
            host._admin_keyboard(telegram_id),
        )
        return
    synthetic["text"] = " ".join([challenge["command"], *challenge["args"]])
    synthetic["_admin_confirmed"] = True
    host.handle(synthetic)


def _cancel_confirmation(
    host: Any, chat_id: int, telegram_id: int, entity_id: str
) -> None:
    token_hash = hashlib.sha256(entity_id.encode()).hexdigest()
    store = getattr(host.service, "database", None)
    cancelled = False
    if callable(getattr(store, "cancel_admin_challenge", None)):
        try:
            cancelled = bool(
                store.cancel_admin_challenge(
                    token_hash,
                    int(telegram_id),
                    int(chat_id),
                    datetime.now(UTC).isoformat(),
                )
            )
        except Exception as exc:
            print(
                f"admin confirmation cancel error: {type(exc).__name__}",
                file=sys.stderr,
            )
    else:
        with host._admin_confirmation_lock:
            challenge = host._admin_confirmations.get(entity_id)
            if (
                challenge
                and challenge["chat_id"] == chat_id
                and challenge["telegram_id"] == telegram_id
            ):
                del host._admin_confirmations[entity_id]
                cancelled = True
    host.send(
        chat_id,
        "Confirmation cancelled." if cancelled else "This confirmation is no longer valid.",
        host._admin_keyboard(telegram_id),
    )


def _navigate(
    host: Any,
    query: dict[str, Any],
    chat_id: int,
    telegram_id: int,
    message_id: Any,
    can_edit_text: bool,
    synthetic: dict[str, Any],
    entity_id: str,
) -> None:
    execute_navigation(
        host,
        query,
        chat_id,
        telegram_id,
        message_id,
        can_edit_text,
        synthetic,
        entity_id,
        navigation_target(entity_id),
    )


def dispatch_navigation_action(
    host: Any,
    query: dict[str, Any],
    chat_id: int,
    telegram_id: int,
    message_id: Any,
    can_edit_text: bool,
    synthetic: dict[str, Any],
    action: str,
    entity_id: str,
) -> None:
    if action == "p":
        _handle_probe_action(
            host, chat_id, telegram_id, message_id, can_edit_text, entity_id
        )
    elif action == "k":
        _handle_confirmation_action(host, chat_id, telegram_id, synthetic, entity_id)
    elif action == "d":
        _cancel_confirmation(host, chat_id, telegram_id, entity_id)
    elif action == "n":
        _navigate(
            host,
            query,
            chat_id,
            telegram_id,
            message_id,
            can_edit_text,
            synthetic,
            entity_id,
        )
