"""Navigation and confirmation actions from administrator callbacks."""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from typing import Any

from commerce import CommerceError

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
    message = query.get("message") or {}
    message_target = message.get("message_id")
    target = {
        "admin": "/admin",
        "owner": "/owner",
        "staff": "/staff",
        "groupsync": "/groupsync",
        "receiptsystem": "/receiptsystem",
        "notifications": "/notifications",
        "orders": "/orders",
        "receipts": "/receipts",
        "capacity": "/capacity",
        "probes": "/probes",
        "prepare": "/capacity",
        "reconcile": "/reconcile",
        "failed": "/failed",
        "repairs": "/repairs",
        "migrations": "/migrations",
        "failover": "/failover",
        "enforcement": "/enforcement",
        "promo": "/promo",
    }.get(entity_id)
    if target is None:
        host.send(chat_id, "This admin action is no longer valid.")
    elif entity_id == "receiptsystem":
        host._send_receipt_system(
            chat_id,
            telegram_id,
            message_id=message_id if can_edit_text else None,
        )
    elif entity_id == "admin":
        host._send_admin_home(
            chat_id,
            telegram_id,
            message_id=message_id if can_edit_text else None,
        )
    elif entity_id == "owner":
        host._send_owner_home(
            chat_id,
            telegram_id,
            message_id=message_id if can_edit_text else None,
        )
    elif entity_id == "notifications":
        host._send_staff_notifications(
            chat_id,
            telegram_id,
            message_id=message_id if can_edit_text else None,
        )
    elif entity_id == "staff":
        host._send_staff_panel(
            chat_id,
            telegram_id,
            message_id=message_id if can_edit_text else None,
        )
    elif entity_id in {
        "orders",
        "receipts",
        "failed",
        "repairs",
        "migrations",
        "failover",
        "enforcement",
    }:
        if host.commerce is None and entity_id != "enforcement":
            host.send(chat_id, "Commerce is not configured.")
        else:
            host._open_admin_panel(
                chat_id,
                telegram_id,
                entity_id,
                message_id=message_target,
            )
    elif entity_id == "capacity":
        host._show_capacity(chat_id, telegram_id, message_id=message_target)
    elif entity_id == "prepare":
        try:
            host._admin_call(
                telegram_id,
                "queue_infrastructure_provision",
                telegram_id,
            )
            host.send(
                chat_id,
                "✅ Provisioning request queued. The infrastructure worker will "
                "re-check capacity, budget and provider state before any change.",
                host._inline_keyboard([[('📈 Capacity', 'a:n:capacity')]]),
            )
            host._show_capacity(
                chat_id,
                telegram_id,
                message_id=message_target,
            )
        except (CommerceError, ValueError, RuntimeError) as exc:
            host.send(chat_id, str(exc) or "Provisioning request was not queued.")
    else:
        synthetic["text"] = target
        host.handle(synthetic)


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
