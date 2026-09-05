"""Customer commerce and access Telegram command router."""

from __future__ import annotations

import sys
from typing import Any

from commerce import CommerceError
from telegram_command_context import TelegramCommandContext
from telegram_formatting import format_user_datetime

CUSTOMER_COMMERCE_COMMANDS = frozenset(
    {
        "/myorders",
        "/order",
        "/plans",
        "/buy",
        "/upgrade",
        "/paid",
        "/renew",
        "/trial",
        "/wallet",
        "/topup",
        "/walletpay",
        "/replace",
        "/cancelorder",
    }
)


def dispatch_customer_commerce_command(
    host: Any, context: TelegramCommandContext
) -> bool:
    """Handle customer order, wallet, subscription, and access commands."""

    command = context.command
    if command not in CUSTOMER_COMMERCE_COMMANDS:
        return False
    chat_id = context.chat_id
    telegram_id = context.telegram_id
    first_name = context.first_name
    username = context.username
    args = context.args

    if command == "/myorders":
        host._send_my_orders(chat_id, telegram_id)
    elif command == "/order":
        if host.commerce is None or len(args) != 1:
            host.send(chat_id, "Usage: /order <order-id>")
        else:
            host._send_order_detail(
                chat_id, telegram_id, args[0], admin_view=host._is_admin(telegram_id)
            )
    elif command == "/plans":
        host._send_plans(chat_id, telegram_id)
    elif command in ("/buy", "/upgrade"):
        if host.commerce is None:
            host.send(chat_id, "Paid plans are not configured in this staging process.")
        elif len(args) != 1:
            host.send(chat_id, "Usage: /buy <plan-code>\n\nUse /plans first.")
        else:
            try:
                order = host.commerce.create_order(
                    telegram_id, first_name, args[0], username=username
                )
            except CommerceError as exc:
                host.send(chat_id, str(exc))
            else:
                if order.plan_conflict:
                    detail = host.commerce.order_detail(order.order_id, telegram_id)
                    untouched = bool(
                        detail
                        and detail.get("status") == "awaiting_payment"
                        and not detail.get("payment_status")
                        and not detail.get("receipt_status")
                    )
                    if not untouched:
                        host._send_order_detail(
                            chat_id,
                            telegram_id,
                            order.order_id,
                            heading="Existing open order",
                        )
                    else:
                        host.send(
                            chat_id,
                            f"You already have an open order for {order.plan.name}. "
                            f"Choose whether to replace that untouched order with {args[0]}.",
                            host._inline_keyboard(
                                [
                                    [
                                        (
                                            "Replace Open Order",
                                            f"p:x:{order.order_id}:{args[0]}",
                                        ),
                                        ("Keep Existing", f"o:v:{order.order_id}"),
                                    ]
                                ]
                            ),
                        )
                elif not order.created:
                    host._send_order_detail(
                        chat_id,
                        telegram_id,
                        order.order_id,
                        heading="Existing open order",
                    )
                else:
                    host._send_payment_method_chooser(
                        chat_id, telegram_id, order.order_id, heading="✅ Order created"
                    )
    elif command == "/paid":
        if host.commerce is None:
            host.send(chat_id, "Paid plans are not configured in this staging process.")
        elif len(args) < 1:
            host.send(chat_id, "Usage: /paid <order-id> then send the receipt screenshot")
        elif len(args) == 1:
            host.send(
                chat_id,
                f"Now send the receipt screenshot for order {args[0]}. "
                f"You may caption it with /paid {args[0]}.",
            )
        elif not host.allow_text_payment:
            host.send(
                chat_id,
                "Text payment references are disabled. Send the receipt screenshot instead.",
            )
        else:
            try:
                result = host.commerce.submit_payment(
                    telegram_id,
                    args[0],
                    "manual",
                    " ".join(args[1:]) if len(args) > 1 else "pending-receipt",
                )
            except CommerceError as exc:
                host.send(chat_id, str(exc))
            else:
                host.send(chat_id, f"Payment recorded ({result}). An admin will review it.")
                for admin_id in host.admin_ids:
                    try:
                        host.send(
                            admin_id,
                            f"Payment submitted for order {args[0]} by Telegram user {telegram_id}.",
                        )
                    except Exception as exc:
                        print(
                            f"admin notification error: {type(exc).__name__}",
                            file=sys.stderr,
                        )
    elif command == "/renew":
        if host.commerce is None:
            host.send(chat_id, "Paid plans are not configured in this staging process.")
        else:
            requested_plan = args[0] if args else None
            subscriptions = (
                host.commerce.user_vpns(telegram_id)
                if hasattr(host.commerce, "user_vpns")
                else []
            )
            subscription = (
                next(
                    (item for item in subscriptions if item.get("plan_code") == requested_plan),
                    None,
                )
                if requested_plan
                else host.commerce.user_vpn(telegram_id)
            )
            if subscription is None and requested_plan:
                host.send(chat_id, "That plan is not one of your previous plans.")
            elif subscription is None:
                host.send(chat_id, "No previous plan found. Use /plans and /buy first.")
            else:
                try:
                    order = host.commerce.create_order(
                        telegram_id,
                        first_name,
                        requested_plan or subscription["plan_code"],
                        username=username,
                    )
                except CommerceError as exc:
                    host.send(chat_id, str(exc))
                else:
                    heading = "Renewal order created" if order.created else "Existing open order"
                    host._send_payment_method_chooser(
                        chat_id, telegram_id, order.order_id, heading=heading
                    )
    elif command == "/wallet":
        if host.commerce is None:
            host.send(chat_id, "Wallet is not configured.")
        else:
            balance = host.commerce.wallet_balance(telegram_id)
            history = host.commerce.wallet_history(telegram_id, limit=5)
            history_text = ""
            if history:
                history_text = "\n\nRecent wallet events:\n" + "\n".join(
                    f"{format_user_datetime(item['created_at'])} · {item['kind']} "
                    f"{int(item['amount_minor']):,} {item['currency']} · {item['reference_id']}"
                    for item in history
                )
            host.send(
                chat_id,
                f"💰 AuriX Wallet\n\nBalance: {balance:,} MMK\n"
                f"Top-ups are credited only after receipt verification.{history_text}",
                host._inline_keyboard([[('➕ Top up wallet', 't:a:menu')]]),
            )
    elif command == "/topup":
        if host.commerce is None:
            host.send(chat_id, "Wallet is not configured.")
        elif not args:
            host.send(
                chat_id,
                "Choose a wallet top-up amount, or tap Other amount and type your own.",
                host._topup_amount_keyboard(),
            )
        elif len(args) != 1:
            host.send(chat_id, "Choose one top-up amount.", host._topup_amount_keyboard())
        else:
            try:
                order = host.commerce.create_wallet_topup(
                    telegram_id,
                    first_name,
                    int(args[0].replace(",", "")),
                    username=username,
                )
            except (ValueError, CommerceError) as exc:
                host.send(chat_id, str(exc) or "Top-up amount is invalid.")
            else:
                if not order.created and order.plan_conflict:
                    host.send(
                        chat_id,
                        "Finish or cancel your current open order before starting this top-up.",
                        host._inline_keyboard(
                            [[("Open current order", f"o:v:{order.order_id}")]]
                        ),
                    )
                else:
                    heading = "Wallet top-up created" if order.created else "Existing wallet top-up"
                    host._send_payment_method_chooser(
                        chat_id, telegram_id, order.order_id, heading=heading
                    )
    elif command == "/walletpay":
        if host.commerce is None:
            host.send(chat_id, "Wallet is not configured.")
        elif len(args) != 1:
            host.send(chat_id, "Usage: /walletpay <order-id>")
        else:
            try:
                result = host.commerce.pay_order_with_wallet(telegram_id, args[0])
            except CommerceError as exc:
                host.send(chat_id, str(exc))
            else:
                host.send(
                    chat_id,
                    f"Wallet payment {result}; an admin will review and approve the order.",
                )
    elif command == "/replace":
        if host.commerce is None or len(args) not in (1, 2):
            host.send(chat_id, "Usage: /replace <plan-code> [expected-order-id]")
        else:
            try:
                order = host.commerce.replace_open_order(
                    telegram_id,
                    first_name,
                    args[0],
                    username=username,
                    expected_order_id=args[1] if len(args) == 2 else None,
                )
            except CommerceError as exc:
                host.send(chat_id, str(exc))
            else:
                host.send(
                    chat_id,
                    f"Order replaced: {order.order_id}\nPlan: {order.plan.name}\n"
                    f"Amount: {order.plan.price_minor:,} {order.plan.currency}\n\n"
                    "Pay through the approved channel, then send the receipt screenshot.",
                    host._inline_keyboard(
                        [
                            [
                                ("📷 Send Receipt", f"o:r:{order.order_id}"),
                                ("View Order", f"o:v:{order.order_id}"),
                            ]
                        ]
                    ),
                )
    elif command == "/cancelorder":
        if host.commerce is None or len(args) != 1:
            host.send(chat_id, "Usage: /cancelorder <order-id>")
        else:
            try:
                result = host.commerce.cancel_order(telegram_id, args[0])
            except CommerceError as exc:
                host.send(chat_id, str(exc))
            else:
                host.send(
                    chat_id,
                    f"Order {args[0]} {result}.",
                    host._customer_keyboard(telegram_id),
                )
    return True
