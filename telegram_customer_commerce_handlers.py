"""Focused handlers for customer commerce Telegram commands."""

from __future__ import annotations

import sys
from typing import Any

from commerce import CommerceError
from telegram_command_context import TelegramCommandContext
from telegram_formatting import format_user_datetime


def handle_trial(host: Any, context: TelegramCommandContext) -> None:
    """Keep the historical no-op route owned by this command family."""


def handle_myorders(host: Any, context: TelegramCommandContext) -> None:
    host._send_my_orders(context.chat_id, context.telegram_id)


def handle_order(host: Any, context: TelegramCommandContext) -> None:
    if host.commerce is None or len(context.args) != 1:
        host.send(context.chat_id, "Usage: /order <order-id>")
        return
    host._send_order_detail(
        context.chat_id,
        context.telegram_id,
        context.args[0],
        admin_view=host._is_admin(context.telegram_id),
    )


def handle_plans(host: Any, context: TelegramCommandContext) -> None:
    host._send_plans(context.chat_id, context.telegram_id)


def handle_buy(host: Any, context: TelegramCommandContext) -> None:
    if host.commerce is None:
        host.send(context.chat_id, "Paid plans are not configured in this staging process.")
    elif len(context.args) != 1:
        host.send(context.chat_id, "Usage: /buy <plan-code>\n\nUse /plans first.")
    else:
        try:
            order = host.commerce.create_order(
                context.telegram_id,
                context.first_name,
                context.args[0],
                username=context.username,
            )
        except CommerceError as exc:
            host.send(context.chat_id, str(exc))
        else:
            _present_created_order(host, context, order)


def _present_created_order(host: Any, context: TelegramCommandContext, order: Any) -> None:
    if order.plan_conflict:
        detail = host.commerce.order_detail(order.order_id, context.telegram_id)
        untouched = bool(
            detail
            and detail.get("status") == "awaiting_payment"
            and not detail.get("payment_status")
            and not detail.get("receipt_status")
        )
        if not untouched:
            host._send_order_detail(
                context.chat_id,
                context.telegram_id,
                order.order_id,
                heading="Existing open order",
            )
        else:
            host.send(
                context.chat_id,
                f"You already have an open order for {order.plan.name}. "
                f"Choose whether to replace that untouched order with {context.args[0]}.",
                host._inline_keyboard(
                    [
                        [
                            ("Replace Open Order", f"p:x:{order.order_id}:{context.args[0]}"),
                            ("Keep Existing", f"o:v:{order.order_id}"),
                        ]
                    ]
                ),
            )
    elif not order.created:
        host._send_order_detail(
            context.chat_id,
            context.telegram_id,
            order.order_id,
            heading="Existing open order",
        )
    else:
        host._send_payment_method_chooser(
            context.chat_id,
            context.telegram_id,
            order.order_id,
            heading="✅ Order created",
        )


def handle_paid(host: Any, context: TelegramCommandContext) -> None:
    args = context.args
    if host.commerce is None:
        host.send(context.chat_id, "Paid plans are not configured in this staging process.")
    elif len(args) < 1:
        host.send(context.chat_id, "Usage: /paid <order-id> then send the receipt screenshot")
    elif len(args) == 1:
        host.send(
            context.chat_id,
            f"Now send the receipt screenshot for order {args[0]}. "
            f"You may caption it with /paid {args[0]}.",
        )
    elif not host.allow_text_payment:
        host.send(
            context.chat_id,
            "Text payment references are disabled. Send the receipt screenshot instead.",
        )
    else:
        try:
            result = host.commerce.submit_payment(
                context.telegram_id,
                args[0],
                "manual",
                " ".join(args[1:]) if len(args) > 1 else "pending-receipt",
            )
        except CommerceError as exc:
            host.send(context.chat_id, str(exc))
        else:
            host.send(context.chat_id, f"Payment recorded ({result}). An admin will review it.")
            _notify_payment_admins(host, context, args[0])


def _notify_payment_admins(host: Any, context: TelegramCommandContext, order_id: str) -> None:
    for admin_id in host.admin_ids:
        try:
            host.send(
                admin_id,
                f"Payment submitted for order {order_id} by Telegram user "
                f"{context.telegram_id}.",
            )
        except Exception as exc:
            print(f"admin notification error: {type(exc).__name__}", file=sys.stderr)


def handle_renew(host: Any, context: TelegramCommandContext) -> None:
    if host.commerce is None:
        host.send(context.chat_id, "Paid plans are not configured in this staging process.")
        return
    requested_plan = context.args[0] if context.args else None
    subscriptions = (
        host.commerce.user_vpns(context.telegram_id)
        if hasattr(host.commerce, "user_vpns")
        else []
    )
    subscription = (
        next((item for item in subscriptions if item.get("plan_code") == requested_plan), None)
        if requested_plan
        else host.commerce.user_vpn(context.telegram_id)
    )
    if subscription is None and requested_plan:
        host.send(context.chat_id, "That plan is not one of your previous plans.")
    elif subscription is None:
        host.send(context.chat_id, "No previous plan found. Use /plans and /buy first.")
    else:
        try:
            order = host.commerce.create_order(
                context.telegram_id,
                context.first_name,
                requested_plan or subscription["plan_code"],
                username=context.username,
            )
        except CommerceError as exc:
            host.send(context.chat_id, str(exc))
        else:
            heading = "Renewal order created" if order.created else "Existing open order"
            host._send_payment_method_chooser(
                context.chat_id, context.telegram_id, order.order_id, heading=heading
            )


def handle_wallet(host: Any, context: TelegramCommandContext) -> None:
    if host.commerce is None:
        host.send(context.chat_id, "Wallet is not configured.")
        return
    balance = host.commerce.wallet_balance(context.telegram_id)
    history = host.commerce.wallet_history(context.telegram_id, limit=5)
    history_text = ""
    if history:
        history_text = "\n\nRecent wallet events:\n" + "\n".join(
            f"{format_user_datetime(item['created_at'])} · {item['kind']} "
            f"{int(item['amount_minor']):,} {item['currency']} · {item['reference_id']}"
            for item in history
        )
    host.send(
        context.chat_id,
        f"💰 AuriX Wallet\n\nBalance: {balance:,} MMK\n"
        f"Top-ups are credited only after receipt verification.{history_text}",
        host._inline_keyboard([[('➕ Top up wallet', 't:a:menu')]]),
    )


def handle_topup(host: Any, context: TelegramCommandContext) -> None:
    args = context.args
    if host.commerce is None:
        host.send(context.chat_id, "Wallet is not configured.")
    elif not args:
        host.send(
            context.chat_id,
            "Choose a wallet top-up amount, or tap Other amount and type your own.",
            host._topup_amount_keyboard(),
        )
    elif len(args) != 1:
        host.send(context.chat_id, "Choose one top-up amount.", host._topup_amount_keyboard())
    else:
        try:
            order = host.commerce.create_wallet_topup(
                context.telegram_id,
                context.first_name,
                int(args[0].replace(",", "")),
                username=context.username,
            )
        except (ValueError, CommerceError) as exc:
            host.send(context.chat_id, str(exc) or "Top-up amount is invalid.")
        else:
            if not order.created and order.plan_conflict:
                host.send(
                    context.chat_id,
                    "Finish or cancel your current open order before starting this top-up.",
                    host._inline_keyboard([[('Open current order', f"o:v:{order.order_id}")]]),
                )
            else:
                heading = "Wallet top-up created" if order.created else "Existing wallet top-up"
                host._send_payment_method_chooser(
                    context.chat_id, context.telegram_id, order.order_id, heading=heading
                )


def handle_walletpay(host: Any, context: TelegramCommandContext) -> None:
    if host.commerce is None:
        host.send(context.chat_id, "Wallet is not configured.")
    elif len(context.args) != 1:
        host.send(context.chat_id, "Usage: /walletpay <order-id>")
    else:
        try:
            result = host.commerce.pay_order_with_wallet(
                context.telegram_id, context.args[0]
            )
        except CommerceError as exc:
            host.send(context.chat_id, str(exc))
        else:
            host.send(
                context.chat_id,
                f"Wallet payment {result}; an admin will review and approve the order.",
            )


def handle_replace(host: Any, context: TelegramCommandContext) -> None:
    if host.commerce is None or len(context.args) not in (1, 2):
        host.send(context.chat_id, "Usage: /replace <plan-code> [expected-order-id]")
        return
    try:
        order = host.commerce.replace_open_order(
            context.telegram_id,
            context.first_name,
            context.args[0],
            username=context.username,
            expected_order_id=context.args[1] if len(context.args) == 2 else None,
        )
    except CommerceError as exc:
        host.send(context.chat_id, str(exc))
    else:
        host.send(
            context.chat_id,
            f"Order replaced: {order.order_id}\nPlan: {order.plan.name}\n"
            f"Amount: {order.plan.price_minor:,} {order.plan.currency}\n\n"
            "Pay through the approved channel, then send the receipt screenshot.",
            host._inline_keyboard(
                [[("📷 Send Receipt", f"o:r:{order.order_id}"), ("View Order", f"o:v:{order.order_id}")]]
            ),
        )


def handle_cancelorder(host: Any, context: TelegramCommandContext) -> None:
    if host.commerce is None or len(context.args) != 1:
        host.send(context.chat_id, "Usage: /cancelorder <order-id>")
        return
    try:
        result = host.commerce.cancel_order(context.telegram_id, context.args[0])
    except CommerceError as exc:
        host.send(context.chat_id, str(exc))
    else:
        host.send(
            context.chat_id,
            f"Order {context.args[0]} {result}.",
            host._customer_keyboard(context.telegram_id),
        )
