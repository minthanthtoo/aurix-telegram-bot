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


def dispatch_staff_command(host: Any, context: TelegramCommandContext) -> bool:
    """Handle owner and staff control-plane commands after centralized authorization."""

    command = context.command
    if command not in STAFF_COMMANDS:
        return False
    chat_id = context.chat_id
    telegram_id = context.telegram_id
    args = context.args

    if command == "/admin":
        host._send_admin_home(chat_id, telegram_id)
    elif command == "/owner":
        host._send_owner_home(chat_id, telegram_id)
    elif command == "/staff":
        host._send_staff_panel(chat_id, telegram_id)
    elif command == "/notifications":
        host._send_staff_notifications(chat_id, telegram_id)
    elif command == "/addadmin":
        if len(args) != 1:
            host.send(chat_id, "Usage: /addadmin <Telegram numeric ID>")
        else:
            try:
                staff = host.staff_access.add_admin(int(args[0]), telegram_id)
            except (ValueError, Exception) as exc:
                host.send(chat_id, str(exc) or "Administrator could not be added.")
            else:
                host._refresh_staff_scopes()
                host.send(
                    chat_id,
                    f"Administrator added: {staff.get('effective_username') or staff['telegram_id']}",
                    host._owner_keyboard(),
                )
    elif command == "/removeadmin":
        if len(args) != 1:
            host.send(chat_id, "Usage: /removeadmin <Telegram numeric ID>")
        else:
            try:
                target_id = int(args[0])
                host.staff_access.remove_admin(target_id, telegram_id)
            except (ValueError, Exception) as exc:
                host.send(chat_id, str(exc) or "Administrator could not be removed.")
            else:
                host._refresh_staff_scopes()
                host.send(
                    chat_id,
                    f"Administrator {target_id} was revoked immediately.",
                    host._owner_keyboard(),
                )
    elif command == "/serverstate":
        if len(args) != 2 or args[1].lower() not in {"active", "draining", "retired"}:
            host.send(
                chat_id,
                "Usage: /serverstate <server-id> active|draining|retired",
                host._owner_keyboard(),
            )
        else:
            try:
                result = host._admin_owner_call(
                    telegram_id,
                    "set_server_lifecycle",
                    args[0],
                    args[1].lower(),
                    telegram_id,
                    reason="owner Telegram lifecycle control",
                )
            except Exception as exc:
                host.send(
                    chat_id,
                    str(exc) or "Endpoint lifecycle change was not saved.",
                    host._owner_keyboard(),
                )
            else:
                host.send(
                    chat_id,
                    f"✅ Endpoint {args[0]} is now "
                    f"{result.get('lifecycle_state', args[1]).title()}. "
                    "No provider VM action was performed.",
                    host._owner_keyboard(),
                )
    elif command == "/migratekey":
        if len(args) != 3:
            host.send(
                chat_id,
                "Usage: /migratekey <source-server> <outline-key-id> <target-server>",
                host._owner_keyboard(),
            )
        else:
            try:
                host._admin_owner_call(
                    telegram_id,
                    "queue_endpoint_migration",
                    args[0],
                    args[1],
                    args[2],
                    telegram_id,
                )
            except Exception as exc:
                host.send(
                    chat_id,
                    str(exc) or "Endpoint migration could not be queued.",
                    host._owner_keyboard(),
                )
            else:
                host.send(
                    chat_id,
                    "✅ Credential migration queued. A fresh source-usage check will run before "
                    "cutover; the old key is deleted only after the replacement is persisted.",
                    host._owner_keyboard(),
                )
    elif command == "/groupsync":
        if host.control_group_id is None:
            host._send_control_group_picker(chat_id)
        else:
            try:
                group_owner, group_admins = host._control_group_staff()
                preview = host.staff_access.group_sync_preview(
                    host.control_group_id,
                    telegram_id,
                    group_owner,
                    group_admins,
                )
            except Exception as exc:
                host.send(
                    chat_id,
                    f"Group sync preview unavailable: {str(exc)[:240]}",
                    host._owner_keyboard(),
                )
            else:
                host.send(
                    chat_id,
                    "🔄 AuriX Group Sync Preview\n\n"
                    f"Group creator: {preview.get('group_owner_id') or '-'}\n"
                    f"Current owner: {preview.get('current_owner_id') or '-'}\n"
                    f"Potential additions: {', '.join(map(str, preview['additions'])) or 'none'}\n"
                    f"Review removals: {', '.join(map(str, preview['review_removals'])) or 'none'}\n\n"
                    "Nothing was changed. Additions and removals require owner confirmation "
                    "from Staff & Access.",
                    host._owner_keyboard(),
                )
    return True
