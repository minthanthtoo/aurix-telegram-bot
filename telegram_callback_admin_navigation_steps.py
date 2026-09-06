"""Action-specific execution steps for administrator navigation callbacks."""

from __future__ import annotations

from typing import Any

from commerce import CommerceError


_NAVIGATION_TARGETS = {
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
}


def navigation_target(entity_id: str) -> str | None:
    """Return the synthetic command target for a known navigation entity."""
    return _NAVIGATION_TARGETS.get(entity_id)


def execute_navigation(
    host: Any,
    query: dict[str, Any],
    chat_id: int,
    telegram_id: int,
    message_id: Any,
    can_edit_text: bool,
    synthetic: dict[str, Any],
    entity_id: str,
    target: str | None,
) -> None:
    """Execute the selected navigation destination and its special actions."""
    message_target = (query.get("message") or {}).get("message_id")
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
