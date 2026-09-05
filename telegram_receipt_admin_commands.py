"""Receipt-workflow administration Telegram command router."""

from __future__ import annotations

from typing import Any

from telegram_command_context import TelegramCommandContext

RECEIPT_ADMIN_COMMANDS = frozenset({"/receiptsystem", "/receiptmode", "/receipttest"})


def dispatch_receipt_admin_command(host: Any, context: TelegramCommandContext) -> bool:
    """Handle receipt policy and diagnostic commands after staff authorization."""

    command = context.command
    if command not in RECEIPT_ADMIN_COMMANDS:
        return False
    chat_id = context.chat_id
    telegram_id = context.telegram_id
    args = context.args

    if command == "/receiptsystem":
        host._send_receipt_system(chat_id, telegram_id)
    elif command == "/receiptmode":
        if len(args) != 1:
            host.send(chat_id, "Usage: /receiptmode manual|assisted")
        else:
            try:
                policy = host._admin_call(
                    telegram_id,
                    "set_receipt_mode",
                    args[0],
                    telegram_id,
                )
            except Exception as exc:
                host.send(chat_id, str(exc) or "Receipt mode could not be changed.")
            else:
                host.send(
                    chat_id,
                    f"Receipt workflow changed to {policy['mode'].title()}. "
                    "Financial approval remains human-verified.",
                    host._receipt_system_keyboard(),
                )
    elif command == "/receipttest":
        host.send(
            chat_id,
            "🧪 Safe Receipt Test\n\nFirst choose the payment method shown on the receipt. "
            "This activates its provider-specific labels; the test cannot create an order, "
            "credit, subscription or VPN key.",
            host._inline_keyboard(
                [
                    [
                        ("1 · KBZPay", "a:t:method:kbzpay"),
                        ("2 · WavePay", "a:t:method:wavepay"),
                    ],
                    [
                        ("3 · AYA Pay", "a:t:method:ayapay"),
                        ("4 · UABPay", "a:t:method:uabpay"),
                    ],
                    [("5 · CB Pay", "a:t:method:cbpay")],
                    [("Cancel Test", "a:t:cancel"), ("Last Test", "a:t:last")],
                ]
            ),
        )
    return True
