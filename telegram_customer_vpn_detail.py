"""Read and render one paid-key detail panel."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Callable

from telegram_formatting import format_user_datetime


@dataclass(frozen=True, slots=True)
class PaidKeyDetailState:
    item: dict[str, Any]
    used: int
    observed: bool
    repair_status: str


def observe_paid_key_detail(transport: Any, item: dict[str, Any]) -> PaidKeyDetailState:
    """Refresh one key's usage/access view from its owning endpoint."""
    key_id = str(item.get("outline_key_id") or "")
    used = int(item.get("last_usage_bytes") or 0)
    repair_status = str(item.get("repair_status") or "").lower()
    observed = False
    try:
        metrics, access_state = transport._collect_outline_state(
            include_access=True,
            server_ids=(str(item.get("server_id") or ""),),
        )
        server_id = str(
            item.get("server_id")
            or getattr(transport.service.outline, "default_server_id", "primary")
        )
        usage = metrics.get("byServer", {}).get(server_id, {})
        if isinstance(usage, dict) and key_id in usage:
            used = max(0, int(usage[key_id] or 0))
            observed = True
        nested_access = access_state.get("byServer", {}) if isinstance(access_state, dict) else {}
        server_access = nested_access.get(server_id, {}) if isinstance(nested_access, dict) else {}
        if isinstance(server_access, dict) and server_access.get(key_id):
            item["access_url"] = server_access[key_id]
    except Exception as exc:
        print(f"paid key detail usage error: {type(exc).__name__}", file=sys.stderr)
    return PaidKeyDetailState(item, used, observed, repair_status)


def render_paid_key_detail(
    state: PaidKeyDetailState,
    subscription_id: str,
    *,
    format_bytes: Callable[[int], str],
    copy_text_button: Callable[[str], dict[str, Any] | None],
) -> tuple[str, dict[str, Any]]:
    """Build the detail text and actions without sending a Telegram request."""
    item = state.item
    used = state.used
    observed = state.observed
    repair_status = state.repair_status
    quota = int(item.get("quota_bytes") or 0)
    remaining = max(0, quota - used)
    status = str(item.get("key_status") or item.get("status") or "pending")
    display_status = status
    if repair_status in {"pending", "running", "failed"}:
        display_status = "key recovery in progress"
    elif repair_status == "manual":
        display_status = "key recovery needs review"
    name = str(item.get("plan_name") or item.get("plan_code") or "Paid key")
    lines = [
        f"🔑 {name}",
        "",
        f"Status: {display_status}",
        f"Key reference: {subscription_id[-6:]}",
        f"Expires: {format_user_datetime(item.get('expires_at'), 'pending')}",
    ]
    server_label = str(item.get("server_label") or item.get("server_id") or "assigned endpoint")
    server_health = str(item.get("server_health_status") or "unknown")
    lines.append(
        f"Endpoint: {server_label}"
        + (f" · {server_health}" if server_health != "healthy" else "")
    )
    if quota:
        if observed:
            percent = min(100.0, used * 100 / quota)
            filled = min(10, max(0, int(percent / 10)))
            lines.extend(
                [
                    f"Usage: {'█' * filled}{'░' * (10 - filled)} {percent:.1f}%",
                    f"Used {format_bytes(used)} · Remaining "
                    f"{format_bytes(remaining)} / {format_bytes(quota)}",
                ]
            )
        else:
            lines.extend(
                [
                    "Usage: temporarily unavailable (endpoint telemetry not confirmed)",
                    "Remaining: withheld until a fresh Outline counter is received",
                ]
            )
    if not observed:
        lines.append("Usage snapshot may be delayed; refresh for the latest Outline total.")
    access_url = item.get("access_url")
    rows: list[list[dict[str, Any]]] = []
    if isinstance(access_url, str) and access_url:
        copy = copy_text_button(access_url)
        if copy is not None:
            rows.append([copy])
        else:
            lines.append("The key is too long for Telegram's copy button. Use Show Keys as Text.")
    elif repair_status in {"pending", "running", "failed"}:
        lines.append("🛠 Your key is being restored. Your quota is protected; refresh this panel shortly.")
    elif repair_status == "manual":
        lines.append(
            "🛠 This key needs owner review because trusted traffic data was unavailable. "
            "No quota reset or replacement has been issued."
        )
    elif status == "active":
        lines.append("Key retrieval is temporarily unavailable; refresh shortly.")
    elif "pending" in status:
        lines.append("The Outline key will appear here after activation.")
    plan_code = str(item.get("plan_code") or "")
    if plan_code:
        rows.append([{"text": f"➕ Buy Another {name[:20]}", "callback_data": f"p:b:{plan_code}"[:64]}])
    rows.append(
        [
            {"text": "◀ All Paid Keys", "callback_data": "k:l:0"},
            {"text": "🔄 Refresh", "callback_data": f"k:v:{subscription_id}"[:64]},
        ]
    )
    return "\n".join(lines), {"inline_keyboard": rows}
