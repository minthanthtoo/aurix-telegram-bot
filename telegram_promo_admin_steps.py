"""Execution steps for promotional campaign administration commands."""

from __future__ import annotations

from typing import Any

from telegram_admin_confirmation import parse_promo_datetime, promo_gb_to_bytes
from telegram_formatting import format_user_datetime


def show_promo(host: Any, chat_id: int, telegram_id: int) -> None:
    """Render the current campaign and its available admin actions."""
    promo = host._admin_service_call(telegram_id, "giveaway_status", telegram_id)
    if not promo["exists"]:
        host.send(chat_id, "No promo campaign is configured.")
        return
    quota = host._promo_quota_label(promo["quota_bytes"])
    example = "/setpromo NEWCODE 100 30 5 campaign 2026-09-01T00:00Z 2026-09-30T23:59Z"
    buttons = host._promo_code_buttons(str(promo["code"]), include_copy=True)
    rows: list[list[dict[str, Any]]] = []
    if buttons:
        rows.append(buttons)
    copy_setup = host._copy_text_button("📋 Copy Setup Example", example)
    if copy_setup:
        rows.append([copy_setup])
    action = "stop" if promo["campaign_state"] != "paused" else "resume"
    rows.append(
        [
            {
                "text": "⏸ Stop Promo" if action == "stop" else "▶ Resume Promo",
                "callback_data": f"a:g:{action}:{promo['code']}"[:64],
            },
            {"text": "🏠 Admin Home", "callback_data": "a:n:admin"},
        ]
    )
    host.send(
        chat_id,
        "Promo campaign\n\n"
        f"Code: {promo['code']}\n"
        f"State: {promo['campaign_state']}\n"
        f"Gift: {quota} / {promo['duration_days']} days\n"
        f"Capacity: {promo['winner_limit']} "
        f"{host._promo_frequency_label(promo['frequency'])}\n"
        f"Current window: {promo['window_claimed_count']} claimed · "
        f"{promo['remaining_slots']} remaining\n"
        f"Lifetime claims: {promo['claimed_count']}\n"
        f"From: {format_user_datetime(promo['starts_at'], 'open')}\n"
        f"To: {format_user_datetime(promo['ends_at'], 'open')}\n\n"
        "Setup syntax:\n"
        "/setpromo CODE QUOTA_GB DAYS COUNT campaign|daily|hourly FROM_UTC TO_UTC\n\n"
        "Each account can claim once per campaign. Daily/hourly resets the slot count, "
        "not the same account's eligibility.",
        {"inline_keyboard": rows},
    )


def configure_promo(host: Any, chat_id: int, telegram_id: int, args: list[str]) -> None:
    """Validate and persist a newly configured campaign."""
    if len(args) != 7:
        host.send(
            chat_id,
            "Usage: /setpromo CODE QUOTA_GB DAYS COUNT campaign|daily|hourly "
            "FROM_UTC TO_UTC",
        )
        return
    try:
        promo = host._admin_service_call(
            telegram_id,
            "configure_giveaway",
            code=args[0],
            quota_bytes=promo_gb_to_bytes(args[1]),
            duration_days=int(args[2]),
            winner_limit=int(args[3]),
            frequency=args[4],
            starts_at=parse_promo_datetime(args[5]),
            ends_at=parse_promo_datetime(args[6]),
        )
    except (ValueError, OverflowError) as exc:
        host.send(chat_id, str(exc))
    else:
        host.send(
            chat_id,
            f"Promo {promo['code']} saved. Current state: {promo['campaign_state']}.",
            host._admin_keyboard(telegram_id),
        )


def set_promo_state(host: Any, chat_id: int, telegram_id: int, command: str, args: list[str]) -> None:
    """Activate or pause a named campaign."""
    if len(args) != 1:
        host.send(chat_id, f"Usage: {command} <promo-code>")
        return
    try:
        promo = host._admin_service_call(
            telegram_id,
            "set_giveaway_active",
            args[0],
            command == "/resumepromo",
        )
    except ValueError as exc:
        host.send(chat_id, str(exc))
    else:
        host.send(
            chat_id,
            f"Promo {promo['code']} is now {promo['campaign_state']}. "
            "Customer plan choices update immediately.",
            host._admin_keyboard(telegram_id),
        )
