"""Customer VPN dashboard presentation workflow."""

from __future__ import annotations

import sys
from typing import Any

from telegram_vpn_view import render_vpn_dashboard


class TelegramCustomerVpnDashboardMixin:
    def _send_my_vpn(
        self,
        chat_id: int,
        telegram_id: int,
        *,
        show_key_text: bool = False,
        message_id: int | None = None,
    ) -> None:
        """Render keys, lifecycle state, usage, and next actions in one dashboard."""
        giveaway = self.service.giveaway_status(telegram_id)
        usage_available = True
        try:
            usage_by_key, access_by_key = self._collect_outline_state(
                include_access=True,
                server_ids=self._customer_server_ids(telegram_id),
            )
        except Exception as exc:
            usage_available = False
            usage_by_key = {}
            print(f"myvpn usage error: {type(exc).__name__}", file=sys.stderr)

        access_available = usage_available
        if not usage_available:
            access_by_key = {}
            access_available = False

        entries = self.service.user_usage(telegram_id, usage_by_key, access_by_key)
        subscriptions: list[dict[str, Any]] = []
        open_order: dict[str, Any] | None = None
        if self.commerce is not None:
            # Outline key IDs are local to an endpoint.  Always include the
            # server ID so key ``1`` on Singapore A cannot be confused with
            # key ``1`` on another node.
            paid_usage = {
                (str(item.get("server_id") or ""), str(item.get("outline_key_id"))): item
                for item in self.commerce.user_usage(telegram_id, usage_by_key)
                if item.get("outline_key_id") and item.get("server_id")
            }
            subscriptions = self.commerce.user_vpns(telegram_id, limit=100)
            relevant = [
                item
                for item in subscriptions
                if item.get("status") in ("active", "pending")
                or item.get("key_status") in ("active", "revoke_failed")
            ]
            if not relevant and subscriptions:
                relevant = subscriptions[:1]
            for item in relevant:
                key_id = str(item.get("outline_key_id") or "")
                server_id = str(item.get("server_id") or "")
                usage = paid_usage.get((server_id, key_id), {})
                status = str(usage.get("status") or item.get("status") or "unknown")
                if item.get("status") == "pending" and not item.get("key_status"):
                    status = "activation pending"
                quota = int(usage.get("quota_bytes") or item.get("quota_bytes") or 0)
                current_access = None
                nested_access = access_by_key.get("byServer") if isinstance(access_by_key, dict) else None
                if isinstance(nested_access, dict):
                    server_access = nested_access.get(server_id, {})
                    if isinstance(server_access, dict):
                        current_access = server_access.get(key_id)
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
            orders = self.commerce.list_user_orders(telegram_id, limit=5)
            open_order = next(
                (
                    order
                    for order in orders
                    if order.get("stage") not in ("fulfilled", "rejected", "cancelled", "refunded")
                ),
                None,
            )

        view = render_vpn_dashboard(
            entries, giveaway, subscriptions, open_order,
            usage_available=usage_available, access_available=access_available,
            show_key_text=show_key_text, telegram_id=telegram_id,
            format_bytes=self._format_bytes, format_decimal_bytes=self._format_decimal_bytes,
            usage_rollup=self._usage_rollup_block, copy_text_button=self._copy_text_button,
            promo_frequency_label=self._promo_frequency_label, device_api_url=self.device_api_url,
        )
        if message_id is not None:
            self.edit_message(chat_id, message_id, view.text, view.markup)
        else:
            self.send(chat_id, view.text, view.markup)
