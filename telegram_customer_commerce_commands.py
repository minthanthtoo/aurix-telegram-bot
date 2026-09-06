"""Customer commerce and access Telegram command router."""

from __future__ import annotations

from typing import Any, Callable

from telegram_command_context import TelegramCommandContext
from telegram_customer_commerce_handlers import (
    handle_buy,
    handle_cancelorder,
    handle_myorders,
    handle_order,
    handle_paid,
    handle_plans,
    handle_replace,
    handle_renew,
    handle_topup,
    handle_trial,
    handle_wallet,
    handle_walletpay,
)

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

_COMMAND_HANDLERS: dict[str, Callable[[Any, TelegramCommandContext], None]] = {
    "/myorders": handle_myorders,
    "/order": handle_order,
    "/plans": handle_plans,
    "/buy": handle_buy,
    "/upgrade": handle_buy,
    "/paid": handle_paid,
    "/renew": handle_renew,
    "/trial": handle_trial,
    "/wallet": handle_wallet,
    "/topup": handle_topup,
    "/walletpay": handle_walletpay,
    "/replace": handle_replace,
    "/cancelorder": handle_cancelorder,
}


def dispatch_customer_commerce_command(
    host: Any, context: TelegramCommandContext
) -> bool:
    """Handle customer order, wallet, subscription, and access commands."""
    handler = _COMMAND_HANDLERS.get(context.command)
    if handler is None:
        return False
    handler(host, context)
    return True
