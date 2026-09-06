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


def _handle_retry(host: Any, context: TelegramCommandContext) -> None:
    if host.commerce is None or len(context.args) not in (1, 2):
        host.send(context.chat_id, "Usage: /retry <order-id> [provision|revoke]")
        return
    try:
        operation = host._admin_call(
            context.telegram_id,
            "retry_failed_job",
            context.args[0],
            context.telegram_id,
            operation=context.args[1] if len(context.args) == 2 else None,
        )
    except CommerceError as exc:
        host.send(context.chat_id, str(exc))
        return
    host.send(
        context.chat_id,
        f"{operation.title()} job requeued for order {context.args[0]}.",
        host._inline_keyboard([[('🔄 Refresh Order', f'a:o:{context.args[0]}')]]),
    )


def _handle_retry_job(host: Any, context: TelegramCommandContext) -> None:
    if host.commerce is None or len(context.args) != 1:
        host.send(context.chat_id, "Usage: /retryjob <job-id>")
        return
    try:
        operation = host._admin_call(
            context.telegram_id, "retry_job", context.args[0], context.telegram_id
        )
    except CommerceError as exc:
        host.send(context.chat_id, str(exc))
        return
    host.send(
        context.chat_id,
        f"{operation.title()} job {context.args[0]} requeued.",
        host._admin_keyboard(context.telegram_id),
    )


def _handle_refund(host: Any, context: TelegramCommandContext) -> None:
    if host.commerce is None or not context.args:
        host.send(context.chat_id, "Usage: /refund <order-id> [reason]")
        return
    try:
        result = host._admin_call(
            context.telegram_id,
            "refund_order",
            context.args[0],
            context.telegram_id,
            " ".join(context.args[1:]) or "refunded by admin",
        )
    except CommerceError as exc:
        host.send(context.chat_id, str(exc))
        return
    host.send(
        context.chat_id,
        f"Order {context.args[0]} {result}; wallet reversal recorded and access revocation queued.",
        host._inline_keyboard([[('🔄 Refresh Order', f'a:o:{context.args[0]}')]]),
    )


def _handle_ledger(host: Any, context: TelegramCommandContext) -> None:
    if host.commerce is None or len(context.args) != 1:
        host.send(context.chat_id, "Usage: /ledger <telegram-id>")
        return
    try:
        customer_id = int(context.args[0])
        balance = host._admin_call(context.telegram_id, "wallet_balance", customer_id)
        history = host._admin_call(
            context.telegram_id,
            "wallet_history",
            customer_id,
            limit=20,
        )
    except (ValueError, CommerceError) as exc:
        host.send(context.chat_id, str(exc) or "Telegram ID must be numeric.")
        return
    lines = [f"Wallet ledger · tg:{customer_id}", f"Balance: {balance:,} MMK"]
    lines.extend(
        f"{format_user_datetime(item['created_at'])} · {item['kind']} "
        f"{int(item['amount_minor']):,} {item['currency']} · {item['reference_id']}"
        for item in history
    )
    host.send(context.chat_id, "\n".join(lines), host._admin_keyboard(context.telegram_id))


def _handle_repair_approval(host: Any, context: TelegramCommandContext) -> None:
    if host.commerce is None:
        host.send(context.chat_id, "Commerce is not configured.")
        return
    if len(context.args) not in (1, 2) or (
        len(context.args) == 2 and context.args[1].lower() != "full"
    ):
        host.send(context.chat_id, "Usage: /approverepair <repair-id> [full]")
        return
    try:
        result = host._admin_owner_call(
            context.telegram_id,
            "approve_managed_key_repair",
            context.args[0],
            context.telegram_id,
            allow_unknown_usage=len(context.args) == 2,
        )
    except (CommerceError, PermissionError) as exc:
        host.send(
            context.chat_id,
            str(exc),
            host._inline_keyboard([[('🧩 Key Repairs', 'a:n:repairs')]]),
        )
        return
    host.send(
        context.chat_id,
        f"✅ Repair {result.get('repair_id') or context.args[0]} queued. "
        "The worker will re-check the endpoint, usage and expiry before changing the key.",
        host._inline_keyboard(
            [[('🧩 Key Repairs', 'a:n:repairs'), ('🏠 Admin Home', 'a:n:admin')]]
        ),
    )


def _handle_order_decision(host: Any, context: TelegramCommandContext) -> None:
    command = context.command
    if host.commerce is None:
        host.send(context.chat_id, "Commerce is not configured.")
        return
    if len(context.args) != 1:
        host.send(context.chat_id, f"Usage: {command} <order-id>")
        return
    try:
        if command == "/approve":
            result = host._admin_call(
                context.telegram_id, "approve_order", context.args[0], context.telegram_id
            )
            outcome = (
                "Wallet top-up approved and credited."
                if result.status in {"wallet_credited", "already_credited"}
                else f"Order {result.order_id} approved; provisioning queued."
            )
            host.send(
                context.chat_id,
                outcome,
                host._inline_keyboard(
                    [[("View Order", f"a:o:{result.order_id}"), ("📥 Orders", "a:n:orders")]]
                ),
            )
        else:
            result = host._admin_call(
                context.telegram_id, "reject_order", context.args[0], context.telegram_id
            )
            host.send(
                context.chat_id,
                f"Order {context.args[0]} {result}.",
                host._inline_keyboard([[('📥 Pending Orders', 'a:n:orders')]]),
            )
    except CommerceError as exc:
        host.send(context.chat_id, str(exc))


def dispatch_approval_command(host: Any, context: TelegramCommandContext) -> bool:
    """Handle explicit administrative retries, financial actions, and repair approval."""

    command = context.command
    if command not in APPROVAL_COMMANDS:
        return False
    handlers = {
        "/retry": _handle_retry,
        "/retryjob": _handle_retry_job,
        "/refund": _handle_refund,
        "/ledger": _handle_ledger,
        "/approverepair": _handle_repair_approval,
        "/approve": _handle_order_decision,
        "/reject": _handle_order_decision,
    }
    handlers[command](host, context)
    return True
