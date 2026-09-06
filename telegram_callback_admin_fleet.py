"""Fleet-specific administrator callback actions."""

from __future__ import annotations

from typing import Any

from commerce import CommerceError


def dispatch_fleet_action(
    host: Any,
    query: dict[str, Any],
    chat_id: int,
    telegram_id: int,
    message_id: Any,
    can_edit_text: bool,
    action: str,
    entity_id: str,
) -> None:
    message = query.get("message") or {}
    if action == "S":
        show_server_allocation(host, chat_id, telegram_id, entity_id, message)
    elif action == "I":
        show_remote_inventory(host, chat_id, telegram_id, entity_id, message, can_edit_text)
    elif action == "G":
        show_migration(host, chat_id, telegram_id, entity_id, message_id, can_edit_text)
    elif action == "H":
        confirm_migration(host, chat_id, telegram_id, entity_id)
    elif action == "R":
        review_remote_key(host, chat_id, telegram_id, entity_id, message_id, can_edit_text)
    elif action == "C":
        configure_capacity(host, chat_id, telegram_id, entity_id, message)
    elif action == "L":
        confirm_lifecycle(host, chat_id, telegram_id, entity_id)


def show_server_allocation(host: Any, chat_id: int, telegram_id: int, server_id: str, message: dict[str, Any]) -> None:
    host._show_server_allocation(chat_id, telegram_id, server_id, message_id=message.get("message_id"))


def show_remote_inventory(
    host: Any,
    chat_id: int,
    telegram_id: int,
    entity_id: str,
    message: dict[str, Any],
    can_edit_text: bool,
) -> None:
    try:
        server_id, status, raw_page = entity_id.split(":", 2)
        page = max(0, int(raw_page))
    except (TypeError, ValueError):
        host.send(chat_id, "That remote inventory view is no longer valid.")
        return
    host._show_remote_inventory(
        chat_id,
        telegram_id,
        server_id,
        status=status,
        page=page,
        message_id=message.get("message_id") if can_edit_text else None,
    )


def show_migration(
    host: Any,
    chat_id: int,
    telegram_id: int,
    entity_id: str,
    message_id: Any,
    can_edit_text: bool,
) -> None:
    try:
        source_server_id, mode, raw_value = entity_id.split(":", 2)
        value = max(0, int(raw_value))
    except (TypeError, ValueError):
        host.send(chat_id, "That migration view is no longer valid.")
        return
    if mode == "p":
        host._show_migration_candidates(
            chat_id,
            telegram_id,
            source_server_id,
            page=value,
            message_id=message_id if can_edit_text else None,
        )
    elif mode == "c":
        host._show_migration_targets(
            chat_id,
            telegram_id,
            source_server_id,
            candidate_index=value,
            page=0,
            message_id=message_id if can_edit_text else None,
        )
    else:
        host.send(chat_id, "That migration view is no longer valid.")


def confirm_migration(host: Any, chat_id: int, telegram_id: int, entity_id: str) -> None:
    if not host._is_owner(telegram_id):
        host._send_customer_fallback(chat_id, telegram_id)
        return
    try:
        source_server_id, raw_index, target_server_id, raw_page = entity_id.split("|", 3)
        candidate_index = max(0, int(raw_index))
        page = max(0, int(raw_page))
        candidates = list(host._admin_call(telegram_id, "migratable_credentials", source_server_id) or [])
        candidate = candidates[candidate_index]
        external_id = str(candidate.get("external_id") or "").strip()
        if not external_id:
            raise ValueError("credential identity missing")
    except Exception as exc:
        host.send(chat_id, str(exc) or "That migration target is no longer valid.")
        return
    host._queue_admin_confirmation(
        chat_id,
        telegram_id,
        "/migratekey",
        [source_server_id, external_id, target_server_id],
        "Move this active credential to the selected healthy endpoint?",
        "🔁 Confirm Key Migration",
        cancel_data=f"a:G:{source_server_id}:p:{page}",
    )


def review_remote_key(
    host: Any,
    chat_id: int,
    telegram_id: int,
    entity_id: str,
    message_id: Any,
    can_edit_text: bool,
) -> None:
    if not host._is_owner(telegram_id):
        host._send_customer_fallback(chat_id, telegram_id)
        return
    try:
        server_id, key_id, next_state, raw_page = entity_id.split("|", 3)
        page = max(0, int(raw_page))
        if next_state not in {"unreviewed", "accepted_external"}:
            raise ValueError
        host._admin_owner_call(
            telegram_id,
            "review_remote_key",
            server_id,
            key_id,
            next_state,
            telegram_id,
            note="owner Telegram inventory action",
        )
    except (CommerceError, PermissionError, ValueError) as exc:
        host.send(chat_id, str(exc) or "Remote key review could not be saved.")
        return
    host._show_remote_inventory(
        chat_id,
        telegram_id,
        server_id,
        status="present",
        page=page,
        message_id=message_id if can_edit_text else None,
    )


def configure_capacity(host: Any, chat_id: int, telegram_id: int, entity_id: str, message: dict[str, Any]) -> None:
    try:
        server_id, field, raw_value = entity_id.split("|", 2)
        value = int(raw_value)
    except (ValueError, TypeError):
        host.send(chat_id, "That capacity control is no longer valid.")
        return
    snapshot = host._admin_call(telegram_id, "capacity_snapshot")
    server = next((item for item in snapshot.get("servers", []) if str(item["server_id"]) == server_id), None)
    if server is None:
        host.send(chat_id, "That Outline server is unavailable.")
        return
    if field in {"keys", "reserve", "traffic"}:
        host._admin_call(
            telegram_id,
            "configure_server_capacity",
            server_id,
            telegram_id,
            max_keys=value if field == "keys" else server.get("max_keys"),
            reserved_keys=value if field == "reserve" else int(server.get("reserved_keys") or 0),
            monthly_traffic_bytes=value * 1_000_000_000 if field == "traffic" else server.get("monthly_traffic_bytes"),
        )
    elif field in {"FREE300MB", "FREE3GB", "PROMO"}:
        host._admin_call(telegram_id, "configure_tier_allocation", server_id, field, value, telegram_id)
    else:
        host._admin_call(telegram_id, "configure_plan_allocation", server_id, field, value, telegram_id)
    host._show_server_allocation(chat_id, telegram_id, server_id, message_id=message.get("message_id"))


def confirm_lifecycle(host: Any, chat_id: int, telegram_id: int, entity_id: str) -> None:
    if not host._is_owner(telegram_id):
        host._send_customer_fallback(chat_id, telegram_id)
        return
    try:
        server_id, requested_state = entity_id.split("|", 1)
        requested_state = requested_state.lower()
    except ValueError:
        host.send(chat_id, "That endpoint lifecycle action is no longer valid.")
        return
    if requested_state not in {"active", "draining", "retired"} or not server_id:
        host.send(chat_id, "That endpoint lifecycle action is no longer valid.")
        return
    host._queue_admin_confirmation(
        chat_id,
        telegram_id,
        "/serverstate",
        [server_id, requested_state],
        (
            f"Change endpoint {server_id} to {requested_state}? "
            "This changes AuriX admission only; it never destroys a VM or key."
        ),
        "✅ Confirm Endpoint State",
        cancel_data=f"a:S:{server_id}",
    )
