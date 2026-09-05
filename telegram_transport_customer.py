"""Telegram customer navigation, keyboards, and usage views."""

from __future__ import annotations

import json
import hashlib
import sys
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import urllib3
from urllib3.filepost import encode_multipart_formdata

from commerce import CommerceError, CommerceService
from commerce_models import summarize_entitlement_usage
from entitlements import ClaimService
from observability import latency_log as _latency_log
from ports import ReceiptExtractorGateway
from quota_alerts import MODE_STEPS, alert_level_labels
from telegram_admin import AdminOperations
from telegram_formatting import format_user_datetime
from telegram_transport_support import ADMIN_CONFIRMATION_TTL, INTERACTION_STATE_TTL, TelegramAPIError, UTC


class TelegramCustomerTransportMixin:

    @staticmethod
    def _mask_technical_value(value: Any, prefix: int = 4, suffix: int = 4) -> str:
        """Keep diagnostics recognizable without disclosing full infrastructure IDs."""
        text = str(value or "").strip()
        if not text:
            return "-"
        if len(text) <= prefix + suffix:
            return "****"
        return f"{text[:prefix]}****{text[-suffix:]}"

    @staticmethod
    def _reply_keyboard(rows: list[list[str]]) -> dict[str, Any]:
        return {
            "keyboard": [[{"text": label} for label in row] for row in rows],
            "resize_keyboard": True,
            "is_persistent": True,
            "input_field_placeholder": "Choose an AuriX action",
        }

    @staticmethod
    def _inline_keyboard(
        rows: list[list[tuple[str, str]]],
    ) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": label, "callback_data": callback_data[:64]}
                    for label, callback_data in row
                ]
                for row in rows
            ]
        }

    @staticmethod
    def _copy_text_button(label: str, value: str) -> dict[str, Any] | None:
        """Build Telegram's native one-tap clipboard button when supported by size."""
        if not isinstance(value, str) or not 1 <= len(value) <= 256:
            return None
        return {"text": label, "copy_text": {"text": value}}

    def _key_delivery_keyboard(self, access_url: str) -> dict[str, Any]:
        rows: list[list[dict[str, Any]]] = []
        copy_button = self._copy_text_button("📋 Copy Outline Key", access_url)
        if copy_button is not None:
            rows.append([copy_button])
        rows.append([{"text": "🔐 Open My VPN", "callback_data": "n:myvpn"}])
        return {"inline_keyboard": rows}

    def _promo_code_buttons(
        self, promo_code: str, *, include_copy: bool = False
    ) -> list[dict[str, Any]]:
        """Build a redeem-first promo action; copying is secondary/share-only."""
        normalized = str(promo_code).strip().upper()
        if not normalized or len(normalized.encode("utf-8")) > 60:
            return []
        buttons = [
            {
                "text": f"🎁 Redeem {normalized}",
                "callback_data": f"g:c:{normalized}"[:64],
            }
        ]
        copy_button = self._copy_text_button("📋 Copy Promo Code", normalized)
        if include_copy and copy_button is not None:
            buttons.append(copy_button)
        return buttons

    @staticmethod
    def _promo_quota_label(quota_bytes: int) -> str:
        amount = int(quota_bytes)
        if amount % 1_000_000_000 == 0:
            return f"{amount // 1_000_000_000} GB"
        if amount % 1_000_000 == 0:
            return f"{amount // 1_000_000} MB"
        return f"{amount:,} bytes"

    @staticmethod
    def _promo_frequency_label(frequency: str) -> str:
        return {
            "hourly": "each UTC hour",
            "daily": "each UTC day",
            "campaign": "for the whole campaign",
        }.get(str(frequency), str(frequency))

    def _launch_promo_keyboard(self, promo_code: str) -> dict[str, Any]:
        rows: list[list[dict[str, Any]]] = []
        promo_buttons = self._promo_code_buttons(promo_code)
        if promo_buttons:
            rows.append(promo_buttons)
        rows.extend(
            [
                [
                    {"text": "🔐 My VPN", "callback_data": "n:myvpn"},
                    {"text": "💎 Plans & Upgrade", "callback_data": "n:plans"},
                ],
                [{"text": "❓ Help", "callback_data": "n:menu"}],
            ]
        )
        return {"inline_keyboard": rows}

    def _outline_help_keyboard(self) -> dict[str, Any]:
        """Official client downloads plus the shortest path from key to connection."""
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "📱 iPhone / iPad",
                        "url": "https://apps.apple.com/app/outline-app/id1356177741",
                    },
                    {
                        "text": "🤖 Android",
                        "url": "https://play.google.com/store/apps/details?id=org.outline.android.client",
                    },
                ],
                [
                    {
                        "text": "🍎 macOS",
                        "url": "https://apps.apple.com/app/outline-secure-internet-access/id1356178125",
                    },
                    {
                        "text": "🪟 Windows",
                        "url": "https://s3.amazonaws.com/outline-releases/client/windows/stable/Outline-Client.exe",
                    },
                ],
                [
                    {
                        "text": "🐧 Linux Guide",
                        "url": "https://support.getoutline.org/client/getting-started/install-linux/",
                    },
                    {
                        "text": "📦 Android APK",
                        "url": "https://s3.amazonaws.com/outline-releases/client/android/stable/Outline-Client.apk",
                    },
                ],
                [
                    {"text": "🔐 Get / Copy My Key", "callback_data": "n:myvpn"},
                    {
                        "text": "🌐 Check My IP",
                        "url": "https://www.google.com/search?q=what+is+my+ip",
                    },
                ],
                [
                    {
                        "text": "🆘 Ask AuriX Support",
                        "url": "https://t.me/+oA18TDWAD9NiNWU1",
                    },
                    {"text": "🏠 Main Menu", "callback_data": "n:start"},
                ],
                [
                    {"text": "ℹ️ About Outline", "url": "https://getoutline.org/"},
                ],
            ]
        }

    def _customer_keyboard(self, telegram_id: int) -> dict[str, Any]:
        try:
            promo_locked = bool(self.service.giveaway_status(telegram_id)["access_lock_active"])
        except Exception:
            promo_locked = False
        rows = [["🔐 My VPN"]]
        if self.device_api_url:
            rows.append(["📱 Open AuriX App"])
        paid_active = self._free_claim_blocked_by_paid(telegram_id)
        if not promo_locked and not paid_active:
            rows.append(["🎁 Daily 300MB", "🚀 Monthly 3GB"])
        if not promo_locked:
            rows.append(["💎 Plans & Upgrade", "🧾 My Orders"])
        else:
            rows.append(["🧾 My Orders"])
        rows.extend([["💰 Wallet", "🔔 Usage Alerts"], ["❓ Help"]])
        return self._reply_keyboard(rows)

    def _topup_amount_keyboard(self) -> dict[str, Any]:
        return self._inline_keyboard(
            [
                [("3,000 MMK", "t:a:3000"), ("6,000 MMK", "t:a:6000")],
                [("10,000 MMK", "t:a:10000"), ("20,000 MMK", "t:a:20000")],
                [("✍️ Other amount", "t:a:custom")],
            ]
        )

    def _expect_customer_input(self, telegram_id: int, action: str) -> None:
        self._customer_inputs[int(telegram_id)] = {
            "action": action,
            "expires_at": time.monotonic() + 600,
        }
        self._save_interaction_state(telegram_id, "customer_input", {"action": str(action)})

    def _expect_receipt_order(self, telegram_id: int, order_id: str) -> None:
        """Bind the next uncaptioned receipt to the order the user selected."""
        normalized = str(order_id or "").strip()
        if not normalized:
            return
        self._receipt_order_context[int(telegram_id)] = {
            "order_id": normalized,
            "expires_at": time.monotonic() + INTERACTION_STATE_TTL.total_seconds(),
        }
        self._save_interaction_state(telegram_id, "receipt_order", {"order_id": normalized})

    def _free_claim_blocked_by_paid(self, telegram_id: int) -> bool:
        """Block daily claims only for a confirmed, currently usable paid key.

        A stale ``pending`` subscription without a paid key must not consume a
        customer's daily entitlement indefinitely.
        """
        if self.commerce is None:
            return False
        try:
            subscriptions = (
                self.commerce.user_vpns(telegram_id)
                if hasattr(self.commerce, "user_vpns")
                else [self.commerce.user_vpn(telegram_id)]
            )
        except Exception as exc:
            print(f"paid claim guard error: {type(exc).__name__}", file=sys.stderr)
            return False
        now = datetime.now(UTC)
        for subscription in subscriptions or []:
            if not subscription or subscription.get("status") != "active":
                continue
            if subscription.get("key_status") != "active":
                continue
            try:
                if (
                    datetime.fromisoformat(str(subscription.get("expires_at"))).astimezone(UTC)
                    <= now
                ):
                    continue
            except (TypeError, ValueError):
                continue
            return True
        return False

    def _send_quota_alert_settings(
        self, chat_id: int, telegram_id: int, *, message_id: int | None = None
    ) -> None:
        preferences = self.service.quota_alert_preferences(telegram_id)
        mode = str(preferences["mode"])
        count = int(preferences["alert_count"])
        step = int(preferences["step_value"])
        suffix = "%" if mode == "percent" else f" {mode.upper()}"
        levels = alert_level_labels(preferences)
        text = (
            "📶 Your VPN Usage Alerts\n\n"
            f"Status: {'On ✅' if preferences['enabled'] else 'Off 🔕'}\n"
            f"Basis: {'Percent remaining' if mode == 'percent' else mode.upper() + ' remaining'}\n"
            f"Alerts per key: {count}\n"
            f"Levels: {', '.join(levels)} remaining\n\n"
            "These alerts are sent only to you for your own free, promo, and paid keys. "
            "Owner/admin operational alerts are configured separately under Admin → My Alerts. "
            "Levels larger than a key's total quota are skipped. A key is still stopped "
            "at its hard quota even when alerts are off."
        )
        mode_rows = [
            [
                (f"{'✓ ' if mode == item else ''}{label}", f"q:m:{item}")
                for item, label in (("percent", "%"), ("mb", "MB"), ("gb", "GB"))
            ]
        ]
        count_rows = [
            [
                (
                    f"{'✓ ' if count == value else ''}{value} alert{'s' if value > 1 else ''}",
                    f"q:c:{value}",
                )
                for value in (1, 2, 3)
            ]
        ]
        step_rows = [
            [
                (f"{'✓ ' if step == value else ''}{value}{suffix}", f"q:v:{value}")
                for value in MODE_STEPS[mode]
            ]
        ]
        rows = [
            [("🔕 Turn off" if preferences["enabled"] else "🔔 Turn on", "q:e:toggle")],
            *mode_rows,
            *count_rows,
            *step_rows,
            [("🔄 Refresh", "n:alerts"), ("⬅ My VPN", "n:myvpn")],
        ]
        markup = self._inline_keyboard(rows)
        if message_id is not None:
            self.edit_message(chat_id, message_id, text, markup)
        else:
            self.send(chat_id, text, markup)

    def _send_plans(self, chat_id: int, telegram_id: int | None = None) -> None:
        giveaway = self.service.giveaway_status(telegram_id or chat_id)
        if giveaway["access_lock_active"]:
            self.send(
                chat_id,
                f"Promo gift #{giveaway['winner_number']} · {giveaway['code']}\n\n"
                "Normal plans are paused while both your gift and its promo season are active. "
                "They return automatically when either one ends.",
                self._customer_keyboard(telegram_id or chat_id),
            )
            return
        if self.commerce is None:
            self.send(chat_id, "Paid plans are not configured in this staging process.")
            return
        lines = ["AuriX plans:"]
        if giveaway["exists"]:
            quota = self._promo_quota_label(giveaway["quota_bytes"])
            state = giveaway["campaign_state"]
            lines.append(
                f"{giveaway['code']} promo — {quota} / {giveaway['duration_days']} days — "
                f"{state}; {giveaway['remaining_slots']} of {giveaway['winner_limit']} "
                f"slot(s) remain {self._promo_frequency_label(giveaway['frequency'])}"
            )
        lines.append("free_3gb — free every 30 days — 3 GB / 30 days (use /trial)")
        plans = self.commerce.plans()
        availability = self.commerce.plan_availability()
        for plan in plans:
            quota = f"{plan.quota_bytes / 1_000_000_000:g} GB" if plan.quota_bytes else "fair-use"
            capacity = availability.get(plan.code, {})
            slots = capacity.get("remaining_slots")
            availability_text = (
                "temporarily full"
                if not capacity.get("available", True)
                else (f"{slots} slot(s) left" if slots is not None else "available")
            )
            lines.append(
                f"{plan.code} — {plan.price_minor:,} {plan.currency} — {quota} / {plan.duration_days} days — {availability_text}"
            )
        lines.append(
            "\nEach paid purchase creates its own Outline key. Buy again after the current "
            "order is completed if you need keys for more people or devices."
        )
        markup = self._inline_keyboard(
            [
                [
                    (
                        f"{'💎' if availability.get(plan.code, {}).get('available', True) else '⏳'} {plan.name} · {plan.price_minor:,} {plan.currency}",
                        f"p:b:{plan.code}"
                        if availability.get(plan.code, {}).get("available", True)
                        else "n:plans",
                    )
                ]
                for plan in plans
            ]
            + [[("🚀 Free Monthly 3GB", "p:t:trial")]]
        )
        promo_buttons = self._promo_code_buttons(str(giveaway["code"]))
        if (
            giveaway["active"]
            and giveaway["remaining_slots"] > 0
            and not giveaway["winner"]
            and promo_buttons
        ):
            markup["inline_keyboard"].insert(0, promo_buttons)
        self.send(chat_id, "\n".join(lines), markup)

    def _send_status(self, chat_id: int, telegram_id: int, include_key: bool = False) -> None:
        giveaway = self.service.giveaway_status(telegram_id)
        giveaway_text = ""
        if giveaway["winner"]:
            giveaway_key_status = (
                "quota exhausted" if giveaway.get("quota_reason") == "quota" else giveaway["status"]
            )
            giveaway_text = (
                f"🎉 Giveaway: winner #{giveaway['winner_number']} of {giveaway['winner_limit']}\n"
                f"Plan: {self._promo_quota_label(giveaway['quota_bytes'])} / "
                f"{giveaway['duration_days']} days\n"
                f"Key status: {giveaway_key_status}\n"
                f"Expires: {format_user_datetime(giveaway['expires_at'])}\n"
                "Regular plans: "
                + (
                    "paused until gift or season ends"
                    if giveaway["access_lock_active"]
                    else "available"
                )
            )
        elif giveaway["exists"]:
            giveaway_text = (
                f"{giveaway['code']} promo ({giveaway['campaign_state']}): "
                f"{giveaway['remaining_slots']} of "
                f"{giveaway['winner_limit']} slot(s) remain"
            )
        else:
            giveaway_text = "No promo campaign is configured."
        if self.commerce is None:
            self.send(chat_id, giveaway_text, self._customer_keyboard(telegram_id))
            return
        if hasattr(self.commerce, "user_vpns"):
            subscriptions = self.commerce.user_vpns(telegram_id, limit=100)
        else:
            latest = self.commerce.user_vpn(telegram_id)
            subscriptions = [latest] if latest else []
        if not subscriptions:
            self.send(
                chat_id,
                giveaway_text
                + (
                    "\n\nThe access URL is shown only when the giveaway is first claimed. "
                    "Use /usage to track quota."
                    if giveaway["winner"]
                    else "\n\nNo subscription found. Use /plans to see available plans."
                ),
                self._customer_keyboard(telegram_id),
            )
            return
        subscription = subscriptions[0]
        text = (
            giveaway_text
            + "\n\n"
            + (
                f"Status: {subscription['status']}\n"
                f"Plan: {subscription['plan_code']}\n"
                f"Expires: {format_user_datetime(subscription['expires_at'])}\n"
                f"Paid keys: {sum(1 for item in subscriptions if item.get('key_status') == 'active')}"
            )
        )
        if include_key:
            key_blocks = []
            for item in subscriptions:
                if item.get("access_url") and item.get("key_status") == "active":
                    key_blocks.append(
                        f"{item['plan_code']} · expires {format_user_datetime(item['expires_at'])}\n{item['access_url']}"
                    )
                elif item.get("status") == "pending":
                    key_blocks.append(
                        f"{item['plan_code']} · provisioning pending (expires {format_user_datetime(item['expires_at'])})"
                    )
            text += (
                "\n\nYour paid Outline keys:\n\n" + "\n\n".join(key_blocks)
                if key_blocks
                else "\n\nNo active paid key is available."
            )
        actions = [[("📶 Usage", "n:usage"), ("🧾 My Orders", "n:myorders")]]
        if not giveaway["access_lock_active"] and subscription.get("status") in (
            "active",
            "expired",
            "revoked",
        ):
            actions[0].append(("🔄 Renew", f"p:r:{subscription['plan_code']}"))
        self.send(chat_id, text, self._inline_keyboard(actions))

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(max(0, int(value)))
        units = ("B", "kB", "MB", "GB", "TB")
        unit = units[0]
        for unit in units:
            if amount < 1000 or unit == units[-1]:
                break
            amount /= 1000
        if unit == "B":
            return f"{int(amount)} {unit}"
        return f"{amount:.2f} {unit}"

    @staticmethod
    def _format_decimal_bytes(value: int) -> str:
        amount = float(max(0, int(value)))
        units = ("B", "KB", "MB", "GB", "TB")
        unit = units[0]
        for unit in units:
            if amount < 1000 or unit == units[-1]:
                break
            amount /= 1000
        return f"{int(amount)} B" if unit == "B" else f"{amount:.2f} {unit}"

    def _usage_rollup_block(self, entries: list[dict[str, Any]], telegram_id: int) -> str | None:
        """Render a conservative total across all active endpoint keys.

        Every Outline counter is endpoint-local.  The customer should still
        have one clear account view, but an unavailable endpoint must never be
        rendered as zero usage or as an exact remaining balance.
        """
        historical = 0
        reader = None
        if self.commerce is not None:
            reader = getattr(self.commerce, "user_migrated_usage", None)
        if not callable(reader):
            reader = getattr(self.service, "user_migrated_usage", None)
        if callable(reader):
            try:
                historical = int(reader(telegram_id) or 0)
            except (TypeError, ValueError, RuntimeError):
                historical = 0
        summary = summarize_entitlement_usage(
            entries,
            historical_used_bytes=historical,
        )
        if not summary["active_key_count"]:
            return None
        key_count = int(summary["active_key_count"])
        server_count = int(summary["server_count"])
        known = self._format_bytes(int(summary["known_used_bytes"]))
        lines = [
            "🌐 Account total · all servers",
            f"Active keys: {key_count} · Endpoints: {server_count}",
        ]
        if summary["has_fair_use"]:
            lines.append(f"Known transfer: ≥{known} · Quota: fair-use")
        else:
            quota = self._format_bytes(int(summary["total_quota_bytes"]))
            lines.append(f"Known transfer: ≥{known} · Quota: {quota}")
            remaining = summary.get("remaining_bytes")
            if remaining is None:
                unknown = int(summary["unknown_key_count"])
                noun = "key" if unknown == 1 else "keys"
                lines.append(f"Remaining: waiting for telemetry from {unknown} {noun}")
            else:
                lines.append(f"Remaining: {self._format_bytes(int(remaining))} (exact)")
        if int(summary.get("historical_used_bytes") or 0):
            lines.append("Includes verified usage from completed key turnover.")
        return "\n".join(lines)
