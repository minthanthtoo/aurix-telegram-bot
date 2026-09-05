"""Validation and challenge creation for mutating Telegram admin commands."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from telegram_command_context import TelegramCommandContext

UTC = timezone.utc


def parse_promo_datetime(value: str) -> datetime:
    normalized = str(value).strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def promo_gb_to_bytes(value: str) -> int:
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Promo quota must be a number of decimal GB") from exc
    if not amount.is_finite():
        raise ValueError("Promo quota must be a finite number")
    return int(amount * Decimal(1_000_000_000))


def intercept_admin_confirmation(host: Any, context: TelegramCommandContext) -> bool:
    """Queue a safe challenge when valid syntax requires explicit confirmation."""

    command = context.command
    args = context.args
    if command == "/setpromo" and not context.confirmed and len(args) == 7:
        try:
            promo_gb_to_bytes(args[1])
            int(args[2])
            int(args[3])
            if args[4].lower() not in {"campaign", "daily", "hourly"}:
                raise ValueError("invalid frequency")
            parse_promo_datetime(args[5])
            parse_promo_datetime(args[6])
        except (ValueError, OverflowError):
            host.send(
                context.chat_id,
                "Invalid promo settings. Use: /setpromo CODE QUOTA_GB DAYS COUNT "
                "campaign|daily|hourly FROM_UTC TO_UTC",
            )
            return True

    if command not in host.ADMIN_CONFIRMATION_COMMANDS or context.confirmed:
        return False
    invalid_syntax = (
        (command in {"/approve", "/reject"} and len(args) != 1)
        or (command == "/retry" and len(args) not in (1, 2))
        or (command == "/refund" and not args)
        or (command == "/verify" and len(args) != 3)
        or (command == "/rejectreceipt" and not args)
        or (command == "/setpromo" and len(args) != 7)
        or (command in {"/stoppromo", "/resumepromo"} and len(args) != 1)
        or (command == "/receiptmode" and len(args) != 1)
        or (command in {"/addadmin", "/removeadmin"} and len(args) != 1)
        or (command == "/serverstate" and len(args) != 2)
        or (command == "/migratekey" and len(args) != 3)
        or (command == "/approverepair" and len(args) not in (1, 2))
    )
    if invalid_syntax:
        return False

    prompt = {
        "/approve": lambda: f"Approve order {args[0]} and queue VPN provisioning?",
        "/reject": lambda: f"Reject order {args[0]} and notify the customer?",
        "/retry": lambda: f"Retry the failed worker job for order {args[0]}?",
        "/refund": lambda: f"Refund order {args[0]} to the customer wallet and revoke paid access?",
        "/verify": lambda: (
            f"Verify receipt {args[0]} for transaction {args[1]} and amount {args[2]}?"
        ),
        "/rejectreceipt": lambda: f"Reject receipt {args[0]} and request a replacement screenshot?",
        "/setpromo": lambda: f"Activate promo campaign {args[0]} with these settings?",
        "/stoppromo": lambda: f"Stop promo campaign {args[0]}?",
        "/resumepromo": lambda: f"Resume promo campaign {args[0]}?",
        "/receiptmode": lambda: f"Change receipt analysis mode to {args[0]}?",
        "/addadmin": lambda: f"Grant AuriX administrator access to Telegram user {args[0]}?",
        "/removeadmin": lambda: f"Revoke AuriX administrator access from Telegram user {args[0]}?",
        "/serverstate": lambda: f"Change endpoint {args[0]} lifecycle to {args[1]}?",
        "/migratekey": lambda: (
            f"Move credential {args[1]} from {args[0]} to {args[2]} while preserving "
            "remaining quota and expiry?"
        ),
        "/approverepair": lambda: (
            f"Approve managed-key repair {args[0]} with explicit full-quota restoration?"
            if len(args) == 2 and args[1].lower() == "full"
            else f"Approve managed-key repair {args[0]} while preserving observed usage?"
        ),
    }[command]()
    confirm_label = {
        "/approve": "Confirm Approve",
        "/reject": "Confirm Reject",
        "/retry": "🔁 Confirm Retry",
        "/refund": "💸 Confirm Refund",
        "/verify": "✅ Confirm Verify",
        "/rejectreceipt": "🛑 Confirm Receipt Rejection",
        "/setpromo": "🎁 Confirm Promo",
        "/stoppromo": "⏸ Confirm Stop",
        "/resumepromo": "▶ Confirm Resume",
        "/receiptmode": "✅ Confirm Mode Change",
        "/addadmin": "✅ Confirm Add Admin",
        "/removeadmin": "🛑 Confirm Remove Admin",
        "/serverstate": "✅ Confirm Endpoint State",
        "/migratekey": "🔁 Confirm Key Migration",
        "/approverepair": "✅ Confirm Repair",
    }[command]
    host._queue_admin_confirmation(
        context.chat_id,
        context.telegram_id,
        command,
        list(args),
        prompt,
        confirm_label=confirm_label,
    )
    return True
