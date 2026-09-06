"""Owner and staff-control Telegram command router."""

from __future__ import annotations

from typing import Any

from telegram_command_context import TelegramCommandContext

STAFF_COMMANDS = frozenset(
    {
        "/admin",
        "/owner",
        "/staff",
        "/notifications",
        "/addadmin",
        "/removeadmin",
        "/serverstate",
        "/migratekey",
        "/groupsync",
    }
)


def _handle_staff_home(host: Any, context: TelegramCommandContext) -> None:
    handlers = {
        "/admin": host._send_admin_home,
        "/owner": host._send_owner_home,
        "/staff": host._send_staff_panel,
        "/notifications": host._send_staff_notifications,
    }
    handlers[context.command](context.chat_id, context.telegram_id)


def _handle_admin_membership(host: Any, context: TelegramCommandContext) -> None:
    if len(context.args) != 1:
        host.send(
            context.chat_id,
            f"Usage: {context.command} <Telegram numeric ID>",
        )
        return
    try:
        target_id = int(context.args[0])
        if context.command == "/addadmin":
            staff = host.staff_access.add_admin(target_id, context.telegram_id)
        else:
            host.staff_access.remove_admin(target_id, context.telegram_id)
    except (ValueError, Exception) as exc:
        fallback = (
            "Administrator could not be added."
            if context.command == "/addadmin"
            else "Administrator could not be removed."
        )
        host.send(context.chat_id, str(exc) or fallback)
        return
    host._refresh_staff_scopes()
    message = (
        f"Administrator added: {staff.get('effective_username') or staff['telegram_id']}"
        if context.command == "/addadmin"
        else f"Administrator {target_id} was revoked immediately."
    )
    host.send(context.chat_id, message, host._owner_keyboard())


def _handle_server_state(host: Any, context: TelegramCommandContext) -> None:
    if len(context.args) != 2 or context.args[1].lower() not in {"active", "draining", "retired"}:
        host.send(
            context.chat_id,
            "Usage: /serverstate <server-id> active|draining|retired",
            host._owner_keyboard(),
        )
        return
    try:
        result = host._admin_owner_call(
            context.telegram_id,
            "set_server_lifecycle",
            context.args[0],
            context.args[1].lower(),
            context.telegram_id,
            reason="owner Telegram lifecycle control",
        )
    except Exception as exc:
        host.send(
            context.chat_id,
            str(exc) or "Endpoint lifecycle change was not saved.",
            host._owner_keyboard(),
        )
        return
    host.send(
        context.chat_id,
        f"✅ Endpoint {context.args[0]} is now "
        f"{result.get('lifecycle_state', context.args[1]).title()}. "
        "No provider VM action was performed.",
        host._owner_keyboard(),
    )


def _handle_key_migration(host: Any, context: TelegramCommandContext) -> None:
    if len(context.args) != 3:
        host.send(
            context.chat_id,
            "Usage: /migratekey <source-server> <outline-key-id> <target-server>",
            host._owner_keyboard(),
        )
        return
    try:
        host._admin_owner_call(
            context.telegram_id,
            "queue_endpoint_migration",
            context.args[0],
            context.args[1],
            context.args[2],
            context.telegram_id,
        )
    except Exception as exc:
        host.send(
            context.chat_id,
            str(exc) or "Endpoint migration could not be queued.",
            host._owner_keyboard(),
        )
        return
    host.send(
        context.chat_id,
        "✅ Credential migration queued. A fresh source-usage check will run before "
        "cutover; the old key is deleted only after the replacement is persisted.",
        host._owner_keyboard(),
    )


def _handle_group_sync(host: Any, context: TelegramCommandContext) -> None:
    if host.control_group_id is None:
        host._send_control_group_picker(context.chat_id)
        return
    try:
        group_owner, group_admins = host._control_group_staff()
        preview = host.staff_access.group_sync_preview(
            host.control_group_id,
            context.telegram_id,
            group_owner,
            group_admins,
        )
    except Exception as exc:
        host.send(
            context.chat_id,
            f"Group sync preview unavailable: {str(exc)[:240]}",
            host._owner_keyboard(),
        )
        return
    host.send(
        context.chat_id,
        "🔄 AuriX Group Sync Preview\n\n"
        f"Group creator: {preview.get('group_owner_id') or '-'}\n"
        f"Current owner: {preview.get('current_owner_id') or '-'}\n"
        f"Potential additions: {', '.join(map(str, preview['additions'])) or 'none'}\n"
        f"Review removals: {', '.join(map(str, preview['review_removals'])) or 'none'}\n\n"
        "Nothing was changed. Additions and removals require owner confirmation "
        "from Staff & Access.",
        host._owner_keyboard(),
    )


def dispatch_staff_command(host: Any, context: TelegramCommandContext) -> bool:
    """Handle owner and staff control-plane commands after centralized authorization."""

    command = context.command
    if command not in STAFF_COMMANDS:
        return False
    if command in {"/admin", "/owner", "/staff", "/notifications"}:
        _handle_staff_home(host, context)
    elif command in {"/addadmin", "/removeadmin"}:
        _handle_admin_membership(host, context)
    elif command == "/serverstate":
        _handle_server_state(host, context)
    elif command == "/migratekey":
        _handle_key_migration(host, context)
    else:
        _handle_group_sync(host, context)
    return True
