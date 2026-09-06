"""Data assembly for the customer VPN dashboard."""

from __future__ import annotations

import sys
from typing import Any


def collect_dashboard_data(
    host: Any, telegram_id: int
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any] | None,
    bool,
    bool,
]:
    """Collect free and paid entitlement data for one customer dashboard."""
    giveaway = host.service.giveaway_status(telegram_id)
    usage_available = True
    try:
        usage_by_key, access_by_key = host._collect_outline_state(
            include_access=True,
            server_ids=host._customer_server_ids(telegram_id),
        )
    except Exception as exc:
        usage_available = False
        usage_by_key = {}
        print(f"myvpn usage error: {type(exc).__name__}", file=sys.stderr)

    access_available = usage_available
    if not usage_available:
        access_by_key = {}
        access_available = False

    entries = host.service.user_usage(telegram_id, usage_by_key, access_by_key)
    subscriptions: list[dict[str, Any]] = []
    open_order: dict[str, Any] | None = None
    if host.commerce is not None:
        paid_usage = {
            (str(item.get("server_id") or ""), str(item.get("outline_key_id"))): item
            for item in host.commerce.user_usage(telegram_id, usage_by_key)
            if item.get("outline_key_id") and item.get("server_id")
        }
        subscriptions = host.commerce.user_vpns(telegram_id, limit=100)
        relevant = [
            item
            for item in subscriptions
            if item.get("status") in ("active", "pending")
            or item.get("key_status") in ("active", "revoke_failed")
        ]
        if not relevant and subscriptions:
            relevant = subscriptions[:1]
        _append_paid_entries(entries, relevant, paid_usage, access_by_key)
        orders = host.commerce.list_user_orders(telegram_id, limit=5)
        open_order = next(
            (
                order
                for order in orders
                if order.get("stage")
                not in ("fulfilled", "rejected", "cancelled", "refunded")
            ),
            None,
        )
    return entries, giveaway, subscriptions, open_order, usage_available, access_available


def _append_paid_entries(
    entries: list[dict[str, Any]],
    subscriptions: list[dict[str, Any]],
    paid_usage: dict[tuple[str, str], dict[str, Any]],
    access_by_key: dict[str, Any],
) -> None:
    """Merge active paid subscriptions with endpoint observations."""
    for item in subscriptions:
        key_id = str(item.get("outline_key_id") or "")
        server_id = str(item.get("server_id") or "")
        usage = paid_usage.get((server_id, key_id), {})
        status = str(usage.get("status") or item.get("status") or "unknown")
        if item.get("status") == "pending" and not item.get("key_status"):
            status = "activation pending"
        quota = int(usage.get("quota_bytes") or item.get("quota_bytes") or 0)
        current_access = _current_access(access_by_key, server_id, key_id)
        entries.append(
            {
                "outline_key_id": key_id,
                "key_type": "paid",
                "tier": item.get("plan_name") or item.get("plan_code") or "Paid VPN",
                "plan_code": item.get("plan_code"),
                "used_bytes": int(usage.get("used_bytes") or 0),
                "quota_bytes": quota,
                "remaining_bytes": int(usage.get("remaining_bytes") or quota),
                "usage_observed": bool(usage.get("usage_observed")),
                "expires_at": item.get("expires_at"),
                "status": status,
                "repair_status": usage.get("repair_status") or item.get("repair_status"),
                "repair_reason": usage.get("repair_reason") or item.get("repair_reason"),
                "access_url": current_access or item.get("access_url"),
                "subscription_id": item.get("subscription_id"),
                "created_at": item.get("created_at") or item.get("starts_at"),
                "server_label": item.get("server_label"),
                "server_health_status": item.get("server_health_status"),
            }
        )


def _current_access(access_by_key: dict[str, Any], server_id: str, key_id: str) -> Any:
    """Return the observed access URL for a server-local key, if present."""
    nested_access = access_by_key.get("byServer")
    if not isinstance(nested_access, dict):
        return None
    server_access = nested_access.get(server_id, {})
    if not isinstance(server_access, dict):
        return None
    return server_access.get(key_id)
