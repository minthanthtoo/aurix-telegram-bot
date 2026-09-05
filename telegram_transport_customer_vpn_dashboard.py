"""Customer VPN dashboard presentation workflow."""

from __future__ import annotations

import sys
from typing import Any

from telegram_formatting import format_user_datetime


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

        priority = {
            "active": 0,
            "activation pending": 1,
            "revocation pending": 2,
            "quota exhausted": 3,
            "expired": 4,
            "revoked": 5,
        }
        entries.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        entries.sort(key=lambda item: priority.get(str(item.get("status")), 6))
        displayed = entries[:4]
        blocks = ["🔐 My VPN\nKeys • status • usage • next action"]
        rollup = self._usage_rollup_block(entries, telegram_id)
        if rollup:
            blocks.append(rollup)
        copy_rows: list[list[dict[str, Any]]] = []
        for index, entry in enumerate(displayed, start=1):
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
                f"{icon} {display_status} · Expires: {format_user_datetime(entry.get('expires_at'), 'pending')}",
            ]
            if quota > 0:
                formatter = (
                    self._format_decimal_bytes if entry.get("decimal_quota") else self._format_bytes
                )
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
            access_url = entry.get("access_url")
            if isinstance(access_url, str) and access_url:
                if show_key_text:
                    lines.append(f"Outline key (press and hold to copy):\n{access_url}")
                if entry.get("key_type") != "paid":
                    copy_button = self._copy_text_button(
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
            blocks.append("\n".join(lines))

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
                f"{self._promo_frequency_label(giveaway['frequency'])}."
            )
        if not usage_available:
            blocks.append(
                "Usage is temporarily unavailable; keys and lifecycle status are still shown."
            )
        else:
            blocks.append("Usage is Outline's rolling 30-day transfer total, not live speed.")

        action_rows: list[list[dict[str, Any]]] = []
        action_rows.extend(copy_rows)
        has_copyable_free = any(
            entry.get("key_type") != "paid" and isinstance(entry.get("access_url"), str)
            for entry in displayed
        )
        if has_copyable_free and not show_key_text:
            action_rows.append([{"text": "👁 Show Keys as Text", "callback_data": "n:keytext"}])
        if subscriptions:
            active_paid_count = sum(
                1
                for item in subscriptions
                if item.get("status") == "active" and item.get("key_status") == "active"
            )
            action_rows.append(
                [
                    {
                        "text": f"🔑 Paid Keys · {active_paid_count} active / {len(subscriptions)} total",
                        "callback_data": "k:l:0",
                    }
                ]
            )
        action_rows.append(
            [
                {"text": "🔄 Refresh", "callback_data": "n:myvpn"},
                {"text": "🔔 Usage Alerts", "callback_data": "n:alerts"},
            ]
        )
        if self.device_api_url:
            action_rows.append([{"text": "📱 Open AuriX App", "callback_data": "n:pair"}])
        if open_order is not None:
            action_rows.append(
                [
                    {
                        "text": f"Open Order {str(open_order['id'])[:8]}",
                        "callback_data": f"o:v:{open_order['id']}"[:64],
                    }
                ]
            )
        text = "\n\n".join(blocks)
        markup = {"inline_keyboard": action_rows}
        if message_id is not None:
            self.edit_message(chat_id, message_id, text[:4096], markup)
        else:
            self.send(chat_id, text[:4096], markup)
