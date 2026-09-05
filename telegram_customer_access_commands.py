"""Customer access, pairing, and trial Telegram command router."""

from __future__ import annotations

import sys
from html import escape as html_escape
from typing import Any

from entitlements import OutlineError
from telegram_command_context import TelegramCommandContext
from telegram_formatting import format_user_datetime

CUSTOMER_ACCESS_COMMANDS = frozenset(
    {"/myvpn", "/status", "/usage", "/pair", "/alerts", "/keysastext", "/trial"}
)


def dispatch_customer_access_command(
    host: Any, context: TelegramCommandContext
) -> bool:
    """Handle customer VPN dashboard, device pairing, and trial commands."""

    command = context.command
    if command not in CUSTOMER_ACCESS_COMMANDS:
        return False
    chat_id = context.chat_id
    telegram_id = context.telegram_id
    first_name = context.first_name
    username = context.username

    if command in ("/myvpn", "/status", "/usage"):
        host._send_my_vpn(chat_id, telegram_id)
    elif command == "/pair":
        device_url = str(getattr(host, "device_api_url", "") or "").rstrip("/")
        identity = getattr(host.service, "identity", None)
        if not device_url or identity is None or not callable(
            getattr(identity, "create_pairing_token", None)
        ):
            host.send(chat_id, "Managed-device pairing is not enabled on this AuriX deployment.")
        else:
            try:
                token = identity.create_pairing_token(telegram_id)
            except Exception as exc:
                print(f"device pairing token error: {type(exc).__name__}", file=sys.stderr)
                host.send(chat_id, "A device pairing token could not be created. Try again later.")
            else:
                host.send(
                    chat_id,
                    "📱 AuriX managed-device pairing\n\n"
                    f"API: {html_escape(device_url)}\n"
                    f"Open in the AuriX app (if installed): "
                    f"<code>aurix://pair/{html_escape(token)}</code>\n"
                    f"One-time token (expires in 5 minutes):\n<code>{html_escape(token)}</code>\n\n"
                    "Enter this token in the official AuriX client. "
                    "Do not post it in a group or share it with anyone.",
                    parse_mode="HTML",
                )
    elif command == "/alerts":
        host._send_quota_alert_settings(chat_id, telegram_id)
    elif command == "/keysastext":
        host._send_my_vpn(chat_id, telegram_id, show_key_text=True)
    elif command == "/trial":
        if not host._trial_allowed(telegram_id):
            host.send(
                chat_id,
                "The monthly trial is currently invite-only. Use /claim or /plans instead.",
            )
        elif host._free_claim_blocked_by_paid(telegram_id):
            host.send(
                chat_id,
                "Your paid account is already active; the free trial is not needed.",
            )
        else:
            try:
                result = host.service.claim_trial(telegram_id, first_name, username=username)
            except OutlineError:
                host.send(chat_id, "Trial service temporarily unavailable. Try again later.")
                return True
            if result.denied_reason == "active_promo":
                host.send(
                    chat_id,
                    "Your promo gift is active. Monthly 3 GB returns automatically when the "
                    "gift or promo season ends.",
                )
            elif result.pending:
                host.send(
                    chat_id,
                    "⏳ Your monthly 3 GB key is being prepared safely. "
                    "Open My VPN in a moment to retrieve it; your trial slot is reserved.",
                    host._customer_keyboard(telegram_id),
                )
            elif result.access_url:
                host.send(
                    chat_id,
                    f"Your monthly 3 GB key:\n\n{result.access_url}\n\n"
                    f"Expires: {format_user_datetime(result.expires_at)}",
                    host._key_delivery_keyboard(str(result.access_url)),
                )
            else:
                retry = (
                    format_user_datetime(result.next_claim_at)
                    if result.next_claim_at
                    else "later"
                )
                host.send(chat_id, f"Monthly 3 GB already claimed. Come back after {retry}.")
    return True
