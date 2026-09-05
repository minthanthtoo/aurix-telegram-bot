"""Administrative receipt review Telegram command router."""

from __future__ import annotations

import sys
from typing import Any

from commerce import CommerceError
from telegram_command_context import TelegramCommandContext

RECEIPT_REVIEW_COMMANDS = frozenset({"/receipt", "/verify", "/rejectreceipt"})


def dispatch_receipt_review_command(host: Any, context: TelegramCommandContext) -> bool:
    """Handle receipt review mutations and their safe error responses."""

    command = context.command
    if command not in RECEIPT_REVIEW_COMMANDS:
        return False
    chat_id = context.chat_id
    telegram_id = context.telegram_id
    args = context.args

    if command == "/receipt":
        if host.commerce is None or len(args) != 1:
            host.send(chat_id, "Usage: /receipt <evidence-id> (admin)")
        else:
            receipt = host._admin_call(telegram_id, "get_receipt", args[0])
            if receipt is None:
                host.send(chat_id, "Receipt evidence not found.")
            else:
                try:
                    host._send_receipt_review(chat_id, receipt)
                except Exception as exc:
                    print(f"receipt review media error: {type(exc).__name__}", file=sys.stderr)
                    host.send(
                        chat_id,
                        "⚠️ Receipt evidence is recorded, but its image could not be loaded "
                        "from private storage or Telegram right now. Retry first; request a new "
                        "screenshot only if it remains unavailable.",
                        host._inline_keyboard(
                            [
                                [("🔄 Retry Receipt", f"a:r:{receipt['id']}")],
                                [("View Order", f"a:o:{receipt['order_id']}")],
                            ]
                        ),
                    )
    elif command == "/verify":
        if host.commerce is None:
            host.send(chat_id, "Commerce is not configured.")
        elif len(args) != 3:
            host.send(chat_id, "Usage: /verify <evidence-id> <transaction-id> <amount>")
        else:
            try:
                amount = int(args[2].replace(",", ""))
                order_id = host._admin_call(
                    telegram_id,
                    "verify_receipt",
                    args[0],
                    telegram_id,
                    args[1],
                    amount,
                )
            except (CommerceError, ValueError) as exc:
                host.send(chat_id, str(exc) or "Verified amount must be an integer.")
            else:
                host._send_order_detail(
                    chat_id,
                    telegram_id,
                    order_id,
                    admin_view=True,
                    heading="✅ Receipt verified · ready for approval",
                )
    elif command == "/rejectreceipt":
        if host.commerce is None or not args:
            host.send(chat_id, "Usage: /rejectreceipt <evidence-id> [reason]")
        else:
            try:
                order_id = host._admin_call(
                    telegram_id,
                    "reject_receipt",
                    args[0],
                    telegram_id,
                    " ".join(args[1:])
                    or "Receipt rejected; please submit a clearer screenshot.",
                )
            except CommerceError as exc:
                host.send(chat_id, str(exc))
            else:
                host.send(
                    chat_id,
                    f"Receipt rejected for order {order_id}; the customer can submit a replacement.",
                    host._inline_keyboard([[('📥 Orders', 'a:n:orders')]]),
                )
    return True
