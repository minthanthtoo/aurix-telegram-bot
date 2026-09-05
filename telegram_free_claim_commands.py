"""Customer free-entitlement claim Telegram router."""

from __future__ import annotations

from typing import Any

from entitlements import OutlineError
from telegram_command_context import TelegramCommandContext
from telegram_formatting import format_user_datetime


def dispatch_free_claim_command(host: Any, context: TelegramCommandContext) -> bool:
    """Handle the daily free claim while preserving fail-closed behavior."""

    if context.command != "/claim":
        return False
    telegram_id = context.telegram_id
    chat_id = context.chat_id
    if host.trial_ids and telegram_id not in host.trial_ids:
        host.send(
            chat_id,
            "Free staging claims are limited to the configured test accounts.",
        )
        return True
    if host._free_claim_blocked_by_paid(telegram_id):
        host.send(
            chat_id,
            "Your paid account is active; free claims are paused until it ends.",
        )
        return True
    try:
        result = host.service.claim(
            telegram_id,
            context.first_name,
            username=context.username,
        )
    except OutlineError:
        host.send(
            chat_id,
            "Service temporarily unavailable. Your claim was not consumed. Try again later.",
        )
        return True
    if result.denied_reason == "active_promo":
        host.send(
            chat_id,
            "Your promo gift is active. Daily 300 MB returns automatically when the "
            "gift or promo season ends.",
        )
    elif result.pending:
        host.send(
            chat_id,
            "⏳ Your daily 300 MB key is being prepared safely. "
            "Open My VPN in a moment to retrieve it; your claim is reserved.",
            host._customer_keyboard(telegram_id),
        )
    elif result.access_url:
        expiry = format_user_datetime(result.expires_at)
        amount = host.service.limit_bytes / 1_000_000
        host.send(
            chat_id,
            f"Your {amount:g} MB Outline key:\n\n{result.access_url}\n\nExpires: {expiry}",
            host._key_delivery_keyboard(str(result.access_url)),
        )
    elif result.next_claim_at:
        retry = format_user_datetime(result.next_claim_at)
        host.send(chat_id, f"Already claimed. Come back after {retry}.")
    else:
        host.send(chat_id, "Claims are unavailable for this account.")
    return True
