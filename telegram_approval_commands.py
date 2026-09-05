"""Administrative approval and recovery mutation Telegram router."""

from __future__ import annotations

from typing import Any

from commerce import CommerceError
from telegram_command_context import TelegramCommandContext
from telegram_formatting import format_user_datetime

APPROVAL_COMMANDS = frozenset(
    {
        "/retry",
        "/retryjob",
        "/refund",
        "/ledger",
        "/approverepair",
        "/approve",
        "/reject",
    }
)


def dispatch_approval_command(host: Any, context: TelegramCommandContext) -> bool:
    """Handle explicit administrative retries, financial actions, and repair approval."""

    command = context.command
    if command not in APPROVAL_COMMANDS:
        return False
    chat_id = context.chat_id
    telegram_id = context.telegram_id
    args = context.args

    if command == "/retry":
        if host.commerce is None or len(args) not in (1, 2):
            host.send(chat_id, "Usage: /retry <order-id> [provision|revoke]")
        else:
            try:
                operation = host._admin_call(
                    telegram_id,
                    "retry_failed_job",
                    args[0],
                    telegram_id,
                    operation=args[1] if len(args) == 2 else None,
                )
            except CommerceError as exc:
                host.send(chat_id, str(exc))
            else:
                host.send(
                    chat_id,
                    f"{operation.title()} job requeued for order {args[0]}.",
                    host._inline_keyboard([[('🔄 Refresh Order', f'a:o:{args[0]}')]]),
                )
    elif command == "/retryjob":
        if host.commerce is None or len(args) != 1:
            host.send(chat_id, "Usage: /retryjob <job-id>")
        else:
            try:
                operation = host._admin_call(telegram_id, "retry_job", args[0], telegram_id)
            except CommerceError as exc:
                host.send(chat_id, str(exc))
            else:
                host.send(
                    chat_id,
                    f"{operation.title()} job {args[0]} requeued.",
                    host._admin_keyboard(telegram_id),
                )
    elif command == "/refund":
        if host.commerce is None or not args:
            host.send(chat_id, "Usage: /refund <order-id> [reason]")
        else:
            try:
                result = host._admin_call(
                    telegram_id,
                    "refund_order",
                    args[0],
                    telegram_id,
                    " ".join(args[1:]) or "refunded by admin",
                )
            except CommerceError as exc:
                host.send(chat_id, str(exc))
            else:
                host.send(
                    chat_id,
                    f"Order {args[0]} {result}; wallet reversal recorded and access revocation queued.",
                    host._inline_keyboard([[('🔄 Refresh Order', f'a:o:{args[0]}')]]),
                )
    elif command == "/ledger":
        if host.commerce is None or len(args) != 1:
            host.send(chat_id, "Usage: /ledger <telegram-id>")
        else:
            try:
                customer_id = int(args[0])
                balance = host._admin_call(telegram_id, "wallet_balance", customer_id)
                history = host._admin_call(
                    telegram_id,
                    "wallet_history",
                    customer_id,
                    limit=20,
                )
            except (ValueError, CommerceError) as exc:
                host.send(chat_id, str(exc) or "Telegram ID must be numeric.")
            else:
                lines = [f"Wallet ledger · tg:{customer_id}", f"Balance: {balance:,} MMK"]
                lines.extend(
                    f"{format_user_datetime(item['created_at'])} · {item['kind']} "
                    f"{int(item['amount_minor']):,} {item['currency']} · {item['reference_id']}"
                    for item in history
                )
                host.send(chat_id, "\n".join(lines), host._admin_keyboard(telegram_id))
    elif command == "/approverepair":
        if host.commerce is None:
            host.send(chat_id, "Commerce is not configured.")
        elif len(args) not in (1, 2) or (len(args) == 2 and args[1].lower() != "full"):
            host.send(chat_id, "Usage: /approverepair <repair-id> [full]")
        else:
            try:
                result = host._admin_owner_call(
                    telegram_id,
                    "approve_managed_key_repair",
                    args[0],
                    telegram_id,
                    allow_unknown_usage=len(args) == 2,
                )
            except (CommerceError, PermissionError) as exc:
                host.send(
                    chat_id,
                    str(exc),
                    host._inline_keyboard([[('🧩 Key Repairs', 'a:n:repairs')]]),
                )
            else:
                host.send(
                    chat_id,
                    f"✅ Repair {result.get('repair_id') or args[0]} queued. "
                    "The worker will re-check the endpoint, usage and expiry before changing the key.",
                    host._inline_keyboard(
                        [[('🧩 Key Repairs', 'a:n:repairs'), ('🏠 Admin Home', 'a:n:admin')]]
                    ),
                )
    elif command in ("/approve", "/reject"):
        if host.commerce is None:
            host.send(chat_id, "Commerce is not configured.")
        elif len(args) != 1:
            host.send(chat_id, f"Usage: {command} <order-id>")
        else:
            try:
                if command == "/approve":
                    result = host._admin_call(
                        telegram_id,
                        "approve_order",
                        args[0],
                        telegram_id,
                    )
                    outcome = (
                        "Wallet top-up approved and credited."
                        if result.status in {"wallet_credited", "already_credited"}
                        else f"Order {result.order_id} approved; provisioning queued."
                    )
                    host.send(
                        chat_id,
                        outcome,
                        host._inline_keyboard(
                            [[("View Order", f"a:o:{result.order_id}"), ("📥 Orders", "a:n:orders")]]
                        ),
                    )
                else:
                    result = host._admin_call(
                        telegram_id,
                        "reject_order",
                        args[0],
                        telegram_id,
                    )
                    host.send(
                        chat_id,
                        f"Order {args[0]} {result}.",
                        host._inline_keyboard([[('📥 Pending Orders', 'a:n:orders')]]),
                    )
            except CommerceError as exc:
                host.send(chat_id, str(exc))
    return True
