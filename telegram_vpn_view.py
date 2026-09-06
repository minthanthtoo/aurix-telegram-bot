"""Pure customer VPN dashboard view model and renderer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from telegram_formatting import format_user_datetime


@dataclass(frozen=True, slots=True)
class VpnDashboardView:
    text: str
    markup: dict[str, Any]


def _displayed_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priority = {
        "active": 0,
        "activation pending": 1,
        "revocation pending": 2,
        "quota exhausted": 3,
        "expired": 4,
        "revoked": 5,
    }
    ordered = list(entries)
    ordered.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    ordered.sort(key=lambda item: priority.get(str(item.get("status")), 6))
    return ordered[:4]


def _entry_block(
    entry: dict[str, Any],
    index: int,
    *,
    show_key_text: bool,
    access_available: bool,
    format_bytes: Callable[[int], str],
    format_decimal_bytes: Callable[[int], str],
    copy_text_button: Callable[[str, str], dict[str, Any] | None],
) -> tuple[str, list[list[dict[str, Any]]]]:
    status = str(entry.get("status") or "unknown")
    repair_status = str(entry.get("repair_status") or "").lower()
    if repair_status in {"pending", "running", "failed"}:
        display_status, icon = "key recovery in progress", "🟡"
    elif repair_status == "manual":
        display_status, icon = "key recovery needs review", "🟠"
    else:
        display_status = status
        icon = "🟢" if status == "active" else "🟡" if "pending" in status else "🔴"
    quota = int(entry.get("quota_bytes") or 0)
    lines = [
        f"#{index} · {entry['tier']}",
        f"{icon} {display_status} · Expires: "
        f"{format_user_datetime(entry.get('expires_at'), 'pending')}",
    ]
    if quota > 0:
        formatter = format_decimal_bytes if entry.get("decimal_quota") else format_bytes
        if entry.get("usage_observed"):
            used = int(entry.get("used_bytes") or 0)
            remaining = max(0, int(entry.get("remaining_bytes") or 0))
            percent = min(100.0, used * 100 / quota)
            filled = min(10, max(0, int(percent / 10)))
            bar = "█" * filled + "░" * (10 - filled)
            lines.extend(
                [
                    f"{bar} {percent:.1f}%",
                    f"Used {formatter(used)} · Remaining "
                    f"{formatter(remaining)} / {formatter(quota)}",
                ]
            )
        else:
            lines.extend(
                [
                    "📡 Usage: temporarily unavailable (endpoint telemetry not confirmed)",
                    "Remaining: withheld until a fresh Outline counter is received",
                ]
            )
    if (
        str(entry.get("server_health_status") or "unknown") != "healthy"
        and entry.get("server_label")
    ):
        lines.append(
            f"Endpoint: {entry['server_label']} is currently unavailable; "
            "new issuance is paused until it recovers."
        )
    copy_rows: list[list[dict[str, Any]]] = []
    access_url = entry.get("access_url")
    if isinstance(access_url, str) and access_url:
        if show_key_text:
            lines.append(f"Outline key (press and hold to copy):\n{access_url}")
        if entry.get("key_type") != "paid":
            copy_button = copy_text_button(
                f"📋 #{index} · Copy {str(entry['tier'])[:20]}", access_url
            )
            if copy_button is not None:
                copy_rows.append([copy_button])
            else:
                lines.append(
                    "Open Show Keys as Text, then press and hold the key to copy it."
                )
        elif entry.get("subscription_id"):
            copy_rows.append(
                [
                    {
                        "text": f"🔑 #{index} · Open {str(entry['tier'])[:20]}",
                        "callback_data": f"k:v:{entry['subscription_id']}"[:64],
                    }
                ]
            )
    elif repair_status in {"pending", "running", "failed"}:
        lines.append(
            "🛠 Your Outline key is being restored. Your quota is protected; "
            "refresh this panel shortly."
        )
    elif repair_status == "manual":
        lines.append(
            "🛠 This key needs owner review because trusted traffic data was "
            "unavailable. No quota reset or replacement has been issued."
        )
    elif status == "active" and entry.get("key_type") != "paid" and not access_available:
        lines.append("Key retrieval is temporarily unavailable; refresh shortly.")
    elif status == "activation pending":
        lines.append("Your key will appear here after activation.")
    return "\n".join(lines), copy_rows


def _context_blocks(
    entries: list[dict[str, Any]],
    displayed: list[dict[str, Any]],
    giveaway: dict[str, Any],
    open_order: dict[str, Any] | None,
    *,
    usage_available: bool,
    promo_frequency_label: Callable[[str], str],
) -> list[str]:
    blocks: list[str] = []
    if not displayed:
        blocks.append("No VPN key yet. Choose a free entitlement or view current plans below.")
    elif len(entries) > len(displayed):
        blocks.append(
            f"{len(entries) - len(displayed)} more entitlement(s) are kept out of this summary. "
            "Use the paid-key browser for the complete paid list."
        )
    if open_order is not None:
        blocks.append(
            f"🧾 Open order {str(open_order['id'])[:8]} · "
            f"{open_order.get('plan_name') or open_order.get('plan_code')} · "
            f"{str(open_order.get('stage') or '').replace('_', ' ')}"
        )
    if giveaway["winner"]:
        blocks.append(
            f"🎉 Promo gift #{giveaway['winner_number']} · {giveaway['code']} · "
            + (
                "regular plans paused until gift or season ends."
                if giveaway["access_lock_active"]
                else "regular plans are available again."
            )
        )
    elif giveaway["exists"] and giveaway["active"] and not displayed:
        blocks.append(
            f"🎁 {giveaway['code']} promo: {giveaway['remaining_slots']} / "
            f"{giveaway['winner_limit']} slots remain "
            f"{promo_frequency_label(giveaway['frequency'])}."
        )
    if not usage_available:
        blocks.append(
            "Usage is temporarily unavailable; keys and lifecycle status are still shown."
        )
    else:
        blocks.append("Usage is Outline's rolling 30-day transfer total, not live speed.")
    return blocks


def _action_rows(
    displayed: list[dict[str, Any]],
    subscriptions: list[dict[str, Any]],
    copy_rows: list[list[dict[str, Any]]],
    *,
    show_key_text: bool,
    device_api_url: str,
    open_order: dict[str, Any] | None,
) -> list[list[dict[str, Any]]]:
    rows = list(copy_rows)
    has_copyable_free = any(
        entry.get("key_type") != "paid" and isinstance(entry.get("access_url"), str)
        for entry in displayed
    )
    if has_copyable_free and not show_key_text:
        rows.append([{"text": "👁 Show Keys as Text", "callback_data": "n:keytext"}])
    if subscriptions:
        active_paid_count = sum(
            1
            for item in subscriptions
            if item.get("status") == "active" and item.get("key_status") == "active"
        )
        rows.append(
            [
                {
                    "text": f"🔑 Paid Keys · {active_paid_count} active / {len(subscriptions)} total",
                    "callback_data": "k:l:0",
                }
            ]
        )
    rows.append(
        [
            {"text": "🔄 Refresh", "callback_data": "n:myvpn"},
            {"text": "🔔 Usage Alerts", "callback_data": "n:alerts"},
        ]
    )
    if device_api_url:
        rows.append([{"text": "📱 Open AuriX App", "callback_data": "n:pair"}])
    if open_order is not None:
        rows.append(
            [
                {
                    "text": f"Open Order {str(open_order['id'])[:8]}",
                    "callback_data": f"o:v:{open_order['id']}"[:64],
                }
            ]
        )
    return rows


def render_vpn_dashboard(
    entries: list[dict[str, Any]],
    giveaway: dict[str, Any],
    subscriptions: list[dict[str, Any]],
    open_order: dict[str, Any] | None,
    *,
    usage_available: bool,
    access_available: bool,
    show_key_text: bool,
    telegram_id: int,
    format_bytes: Callable[[int], str],
    format_decimal_bytes: Callable[[int], str],
    usage_rollup: Callable[[list[dict[str, Any]], int], str | None],
    copy_text_button: Callable[[str, str], dict[str, Any] | None],
    promo_frequency_label: Callable[[str], str],
    device_api_url: str,
) -> VpnDashboardView:
    displayed = _displayed_entries(entries)
    blocks = ["🔐 My VPN\nKeys • status • usage • next action"]
    rollup = usage_rollup(entries, telegram_id)
    if rollup:
        blocks.append(rollup)
    copy_rows: list[list[dict[str, Any]]] = []
    for index, entry in enumerate(displayed, start=1):
        block, rows = _entry_block(
            entry,
            index,
            show_key_text=show_key_text,
            access_available=access_available,
            format_bytes=format_bytes,
            format_decimal_bytes=format_decimal_bytes,
            copy_text_button=copy_text_button,
        )
        blocks.append(block)
        copy_rows.extend(rows)
    blocks.extend(
        _context_blocks(
            entries,
            displayed,
            giveaway,
            open_order,
            usage_available=usage_available,
            promo_frequency_label=promo_frequency_label,
        )
    )
    markup = {
        "inline_keyboard": _action_rows(
            displayed,
            subscriptions,
            copy_rows,
            show_key_text=show_key_text,
            device_api_url=device_api_url,
            open_order=open_order,
        )
    }
    return VpnDashboardView(text="\n\n".join(blocks)[:4096], markup=markup)
