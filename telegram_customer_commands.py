"""Customer-facing Telegram command routers."""

from __future__ import annotations

from typing import Any

from entitlements import OutlineError
from telegram_command_context import TelegramCommandContext
from telegram_formatting import format_user_datetime


def _start_message(host: Any, telegram_id: int) -> tuple[str, dict[str, Any]]:
    giveaway = host.service.giveaway_status(telegram_id)
    remaining = int(giveaway["remaining_slots"])
    if giveaway["exists"] and giveaway["campaign_state"] in {"active", "scheduled"}:
        quota = host._promo_quota_label(giveaway["quota_bytes"])
        availability = (
            f"🔥 {remaining}/{giveaway['winner_limit']} gifts available "
            f"{host._promo_frequency_label(giveaway['frequency'])}."
            if giveaway["campaign_state"] == "active" and remaining > 0
            else "⏳ This promo is scheduled; Redeem appears when it starts."
        )
        text = (
            "🎉 AuriX VPN မှ ကြိုဆိုပါတယ်!\n\n"
            f"🎁 Promo: {giveaway['code']}\n"
            f"{quota} Outline VPN • {giveaway['duration_days']} days • Free\n"
            f"{availability}\n\n"
            f"👇 Tap Redeem or send {giveaway['code']} exactly. "
            "No payment or receipt.\n\n"
            "While both the promo season and your gift are active, other plans pause. "
            "Daily 300 MB, monthly 3 GB, and paid plans return automatically when "
            "the season or your gift ends. One gift per account per campaign.\n\n"
            "ℹ️ This is Outline VPN allowance, not SIM/mobile data. Network speed "
            "depends on your ISP and server conditions.\n\n"
            "အကူအညီ — https://t.me/+oA18TDWAD9NiNWU1\n"
            "သတင်း — https://t.me/AurixDigitalStore\n\n"
            "AuriX is not an official Outline Foundation partner."
        )
    else:
        text = (
            "🎉 Welcome to AuriX VPN!\n\n"
            "The seasonal promo is currently closed. Your regular choices are ready: "
            "daily 300 MB, monthly 3 GB, and paid 50/100 GB plans."
        )
    giveaway["remaining_slots"] = remaining
    return text, giveaway


def _help_message() -> str:
    return (
        "🧭 Connect with Outline · quick setup\n\n"
        "1️⃣ Install the official Outline app\n"
        "Choose your device below. Android users can use Google Play or the direct "
        "APK when Play Store is unavailable.\n\n"
        "2️⃣ Copy your AuriX key\n"
        "Tap Get / Copy My Key. An Outline key starts with ss://. Use the Copy button "
        "beside your active key.\n\n"
        "3️⃣ Connect\n"
        "Open Outline. If it detects the copied key, accept it and tap Connect. "
        "Otherwise tap +, paste the complete key, add the server, then Connect.\n\n"
        "4️⃣ Confirm it works\n"
        "Tap Check My IP after connecting. Your public IP should change from your "
        "normal mobile/Wi-Fi address.\n\n"
        "🛡 Keep the ss:// key private—anyone who has it can use its quota. "
        "If connection fails, copy the key again without spaces, switch between "
        "Wi-Fi/mobile data, then ask AuriX Support."
    )


def _handle_welcome(host: Any, context: TelegramCommandContext) -> None:
    giveaway: dict[str, Any] | None = None
    if context.command == "/start":
        welcome_text, giveaway = _start_message(host, context.telegram_id)
    else:
        welcome_text = _help_message()
    if context.command == "/help":
        reply_markup = host._outline_help_keyboard()
    elif giveaway and giveaway["active"] and giveaway["remaining_slots"] > 0 and not giveaway["winner"]:
        reply_markup = host._launch_promo_keyboard(str(giveaway["code"]))
    else:
        reply_markup = host._customer_keyboard(context.telegram_id)
    host.send(context.chat_id, welcome_text, reply_markup)


def _handle_whoami(host: Any, context: TelegramCommandContext) -> None:
    access = "\nAdmin access: enabled" if host._is_admin(context.telegram_id) else ""
    host.send(
        context.chat_id,
        f"Your Telegram ID: {context.telegram_id}{access}",
        host._customer_keyboard(context.telegram_id),
    )


def _handle_giveaway(host: Any, context: TelegramCommandContext) -> None:
    promo_code = context.args[0] if context.command == "/claimpromo" and context.args else None
    try:
        result = host.service.claim_giveaway(
            context.telegram_id,
            context.first_name,
            username=context.username,
            code=promo_code,
        )
    except OutlineError:
        host.send(
            context.chat_id,
            "The giveaway key could not be provisioned. No winner slot was consumed; try again.",
        )
        return
    if result.outcome == "won":
        quota = host._promo_quota_label(int(result.quota_bytes or 0))
        host.send(
            context.chat_id,
            f"🎉 Promo gift #{result.winner_number}: {result.code}\n\n"
            f"Your {quota} / {result.duration_days}-day Outline key:\n\n"
            f"{result.access_url}\n\n"
            f"Expires: {format_user_datetime(result.expires_at)}\n\n"
            "Enjoy your gift—no payment or receipt was needed. Other AuriX plans rest "
            "while this gift and its promo season are active, then return automatically:\n"
            "• Daily Free — 300 MB for 24 hours\n"
            "• Monthly Free — 3 GB for 30 days\n"
            "• Paid 50 GB — 3,000 MMK for 30 days\n"
            "• Paid 100 GB — 6,000 MMK for 30 days",
            host._key_delivery_keyboard(str(result.access_url)),
        )
    elif result.pending:
        host.send(
            context.chat_id,
            "⏳ Your promo slot is reserved and the Outline key is being prepared. "
            "Open My VPN in a moment to retrieve it.",
            host._customer_keyboard(context.telegram_id),
        )
    elif result.outcome == "already_won":
        host.send(
            context.chat_id,
            f"You already won slot #{result.winner_number}. No second key or slot was created.\n"
            f"Expires: {format_user_datetime(result.expires_at)}\n"
            "Open My VPN to retrieve the key and track usage. Regular plans are available "
            "again after the gift or season ends.",
            host._customer_keyboard(context.telegram_id),
        )
    elif result.outcome == "ineligible":
        host.send(context.chat_id, f"This account is not eligible: {result.reason}")
    elif result.outcome == "scheduled":
        host.send(context.chat_id, "This promo has not started yet. Open Plans later to refresh.")
    elif result.outcome in {"ended", "paused", "unavailable"}:
        host.send(
            context.chat_id,
            result.reason or "This promo is not active. Your regular plans remain available.",
            host._customer_keyboard(context.telegram_id),
        )
    else:
        host.send(context.chat_id, "This promo's current giveaway window is fully claimed.")


def dispatch_onboarding(host: Any, context: TelegramCommandContext) -> bool:
    """Handle welcome, identity, and promotional-claim commands."""

    command = context.command
    if command not in {"/start", "/help", "/whoami", "/claimpromo", "/giveaway100gb"}:
        return False

    if command in ("/start", "/help"):
        _handle_welcome(host, context)
        return True

    if command == "/whoami":
        _handle_whoami(host, context)
        return True

    _handle_giveaway(host, context)
    return True
