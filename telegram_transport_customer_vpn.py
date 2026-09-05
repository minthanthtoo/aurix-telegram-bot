"""Telegram VPN, order, and account detail views."""

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


from telegram_transport_customer_vpn_dashboard import TelegramCustomerVpnDashboardMixin


class TelegramCustomerVpnTransportMixin(TelegramCustomerVpnDashboardMixin):

    def _send_paid_key_list(
        self,
        chat_id: int,
        telegram_id: int,
        page: int = 0,
        *,
        selected: str = "active",
        message_id: int | None = None,
    ) -> None:
        if self.commerce is None:
            text = "Paid keys are not configured."
            if message_id is not None:
                self.edit_message(chat_id, message_id, text)
            else:
                self.send(chat_id, text)
            return
        keys = self.commerce.user_vpns(telegram_id, limit=100)
        selected = selected if selected in {"active", "ended", "all"} else "active"

        def matches(item: dict[str, Any], category: str) -> bool:
            status = str(item.get("key_status") or item.get("status") or "pending")
            if category == "all":
                return True
            if category == "active":
                return status == "active"
            return status not in {"active", "pending", "activation pending"}

        filtered = [item for item in keys if matches(item, selected)]
        page_size = 5
        page_count = max(1, (len(filtered) + page_size - 1) // page_size)
        page = min(max(0, int(page)), page_count - 1)
        visible = filtered[page * page_size : (page + 1) * page_size]
        active = sum(
            1
            for item in keys
            if item.get("status") == "active" and item.get("key_status") == "active"
        )
        text = (
            "🔑 Your Paid Keys\n\n"
            f"{active} active · {len(keys)} total · Page {page + 1}/{page_count}\n"
            f"Filter: {selected.title()} · {len(filtered)} key(s)\n\n"
            "Open one key to see its quota, expiry and one-tap copy control. "
            "Each completed purchase creates a separate Outline key."
        )
        rows: list[list[dict[str, Any]]] = [
            [
                {
                    "text": f"🟢 Active {sum(matches(item, 'active') for item in keys)}",
                    "callback_data": "k:l:active:0",
                },
                {
                    "text": f"⚫ Ended {sum(matches(item, 'ended') for item in keys)}",
                    "callback_data": "k:l:ended:0",
                },
                {"text": f"📚 All {len(keys)}", "callback_data": "k:l:all:0"},
            ]
        ]
        for offset, item in enumerate(visible, start=page * page_size + 1):
            status = str(item.get("key_status") or item.get("status") or "pending")
            icon = "🟢" if status == "active" else "🟡" if "pending" in status else "⚫"
            name = str(item.get("plan_name") or item.get("plan_code") or "Paid key")
            short_id = str(item.get("subscription_id") or "")[-6:]
            rows.append(
                [
                    {
                        "text": f"{icon} #{offset} · {name[:22]} · {short_id}",
                        "callback_data": f"k:v:{item['subscription_id']}"[:64],
                    }
                ]
            )
        nav: list[dict[str, Any]] = []
        if page > 1:
            nav.append({"text": "⏮ First", "callback_data": f"k:l:{selected}:0"})
        if page > 0:
            nav.append({"text": "◀ Previous", "callback_data": f"k:l:{selected}:{page - 1}"})
        if page + 1 < page_count:
            nav.append({"text": "Next ▶", "callback_data": f"k:l:{selected}:{page + 1}"})
        if page + 2 < page_count:
            nav.append({"text": "Last ⏭", "callback_data": f"k:l:{selected}:{page_count - 1}"})
        if nav:
            rows.append(nav)
        rows.extend(
            [
                [{"text": "🔐 My VPN", "callback_data": "n:myvpn"}],
            ]
        )
        markup = {"inline_keyboard": rows}
        if isinstance(message_id, int):
            self.edit_message(chat_id, message_id, text, markup)
        else:
            self.send(chat_id, text, markup)

    @staticmethod
    def _order_filter_match(order: dict[str, Any], selected: str) -> bool:
        status = str(order.get("status") or "")
        stage = str(order.get("stage") or "")
        if selected == "all":
            return True
        if selected == "open":
            return status in ("awaiting_payment", "payment_submitted") or stage in {
                "activation_pending",
                "activation_failed",
                "revocation_pending",
                "revocation_failed",
            }
        if selected == "completed":
            return stage in {"fulfilled", "approved", "refunded"}
        return status == selected

    def _send_my_orders(
        self,
        chat_id: int,
        telegram_id: int,
        *,
        selected: str = "open",
        page: int = 0,
        message_id: int | None = None,
    ) -> None:
        if self.commerce is None:
            text = "Order tracking is not configured."
            markup = self._customer_keyboard(telegram_id)
        else:
            orders = self.commerce.list_user_orders(telegram_id, limit=50)
            allowed = {"open", "completed", "cancelled", "rejected", "all"}
            selected = selected if selected in allowed else "open"
            filtered = [item for item in orders if self._order_filter_match(item, selected)]
            page_size = 4
            pages = max(1, (len(filtered) + page_size - 1) // page_size)
            page = min(max(0, int(page)), pages - 1)
            visible = filtered[page * page_size : (page + 1) * page_size]
            counts = {
                name: sum(self._order_filter_match(item, name) for item in orders)
                for name in allowed
            }
            title = {
                "open": "Open",
                "completed": "Completed",
                "cancelled": "Cancelled or expired",
                "rejected": "Rejected by staff",
                "all": "All",
            }[selected]
            blocks = [
                "🧾 Your recent orders",
                f"{title} · {len(filtered)} order(s) · Page {page + 1}/{pages}",
            ]
            blocks.extend(self._order_summary(order) for order in visible)
            if not visible:
                blocks.append("Nothing in this category.")
            text = "\n\n".join(blocks)
            rows: list[list[tuple[str, str]]] = [
                [
                    (f"🟡 Open {counts['open']}", "c:o:open:0"),
                    (f"✅ Done {counts['completed']}", "c:o:completed:0"),
                ],
                [
                    (f"🚫 Cancelled {counts['cancelled']}", "c:o:cancelled:0"),
                    (f"❌ Staff {counts['rejected']}", "c:o:rejected:0"),
                    (f"📚 All {counts['all']}", "c:o:all:0"),
                ],
            ]
            rows.extend(
                [
                    (
                        f"{title} #{page * page_size + offset + 1} · {str(order['id'])[:8]}",
                        f"o:v:{order['id']}",
                    )
                ]
                for offset, order in enumerate(visible)
            )
            nav: list[tuple[str, str]] = []
            if page > 0:
                nav.extend([("⏮", f"c:o:{selected}:0"), ("◀", f"c:o:{selected}:{page - 1}")])
            nav.append((f"{page + 1}/{pages}", f"c:o:{selected}:{page}"))
            if page + 1 < pages:
                nav.extend(
                    [
                        ("▶", f"c:o:{selected}:{page + 1}"),
                        ("⏭", f"c:o:{selected}:{pages - 1}"),
                    ]
                )
            rows.append(nav)
            rows.append([("🔄 Refresh", f"c:o:{selected}:{page}")])
            markup = self._inline_keyboard(rows)
        if message_id is not None:
            self.edit_message(chat_id, message_id, text, markup)
        else:
            self.send(chat_id, text, markup)

    def _customer_server_ids(self, telegram_id: int) -> tuple[str, ...]:
        """Return only endpoint ids that can contain this customer's keys.

        A fleet may deliberately contain a draining or temporarily unreachable
        node.  Customer views must not probe unrelated endpoints and turn that
        node's outage into a slow/broken response for keys that live elsewhere.
        The query is best-effort: if the legacy schema cannot be inspected we
        fall back to the default endpoint, preserving the old behaviour.
        """
        server_ids: set[str] = set()
        databases = [getattr(self.service, "database", None)]
        if self.commerce is not None:
            databases.append(getattr(self.commerce, "database", None))
        for database in databases:
            if database is None:
                continue
            try:
                with database.connect() as connection:
                    for table in ("keys", "paid_vpn_keys"):
                        try:
                            rows = connection.execute(
                                f"SELECT DISTINCT server_id FROM {table} "
                                "WHERE telegram_id = ? AND server_id IS NOT NULL",
                                (telegram_id,),
                            ).fetchall()
                        except Exception:
                            continue
                        server_ids.update(
                            str(row["server_id"] if isinstance(row, dict) else row[0]).strip()
                            for row in rows
                            if str(row["server_id"] if isinstance(row, dict) else row[0]).strip()
                        )
            except Exception:
                continue
        if server_ids:
            return tuple(sorted(server_ids))
        default_id = str(getattr(self.service.outline, "default_server_id", "primary"))
        return (default_id,)

    def _collect_outline_state(
        self,
        *,
        include_access: bool = False,
        server_ids: tuple[str, ...] | list[str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Collect collision-safe state from selected Outline endpoints.

        Passing ``server_ids`` is important for customer views: it bounds work
        to the nodes that own the customer's credentials.  Administrative and
        fleet-wide callers continue to omit it and inspect every configured
        endpoint.
        """
        outline = self.service.outline
        configured_ids = (
            outline.server_ids()
            if callable(getattr(outline, "server_ids", None))
            else (str(getattr(outline, "default_server_id", "primary")),)
        )
        allowed = {str(item) for item in configured_ids}
        if server_ids is None:
            selected_ids = tuple(str(item) for item in configured_ids)
        else:
            requested = tuple(
                str(item).strip() for item in server_ids if str(item).strip()
            )
            # An explicitly requested retired/unknown server must not broaden
            # into a fleet-wide probe.  The caller can still render the
            # encrypted/stored key and lifecycle state while reporting usage
            # unavailable; admin health remains responsible for the endpoint.
            selected_ids = tuple(item for item in requested if item in allowed)
        server_ids = selected_ids
        def collect_server_state(
            server_id: str,
        ) -> tuple[str, dict[str, Any], dict[str, str], str | None]:
            client_getter = getattr(outline, "client", None)
            client = client_getter(server_id) if callable(client_getter) else outline
            try:
                payload = client.transfer_metrics()
                usage = payload.get("bytesTransferredByUserId", {}) if isinstance(payload, dict) else {}
                if not isinstance(usage, dict):
                    raise ValueError("invalid Outline metrics response")
                metrics_by_server[str(server_id)] = dict(usage)
                if include_access:
                    remote = client.list_keys()
                    items = remote.get("accessKeys", []) if isinstance(remote, dict) else []
                    if not isinstance(items, list):
                        raise ValueError("invalid Outline key response")
                    access: dict[str, str] = {}
                    for item in items:
                        if not isinstance(item, dict) or not item.get("id") or not item.get("accessUrl"):
                            continue
                        value = str(item["accessUrl"]).replace("\r", "").replace("\n", "").strip()
                        if value:
                            access[str(item["id"])] = value
                return str(server_id), dict(usage), access, None
            except Exception as exc:
                return str(server_id), {}, {}, type(exc).__name__

        metrics_by_server: dict[str, dict[str, Any]] = {}
        access_by_server: dict[str, dict[str, str]] = {}
        # A customer may legitimately own keys on multiple nodes.  Keep the
        # reads bounded and parallel so one unreachable node cannot serialize
        # every other customer's view behind its network timeout.
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(server_ids)))) as executor:
            collected = list(executor.map(collect_server_state, server_ids))
        for server_id, usage, access, error_type in collected:
            if error_type is not None:
                print(
                    f"Outline state unavailable server={server_id}: {error_type}",
                    file=sys.stderr,
                )
                continue
            metrics_by_server[server_id] = usage
            if include_access:
                access_by_server[server_id] = access
        if not metrics_by_server:
            raise ValueError("all Outline endpoints are unavailable")
        return {"byServer": metrics_by_server}, {"byServer": access_by_server}

    def _send_paid_key_detail(
        self,
        chat_id: int,
        telegram_id: int,
        subscription_id: str,
        *,
        message_id: int | None = None,
    ) -> None:
        if self.commerce is None:
            self.send(chat_id, "Paid keys are not configured.")
            return
        item = self.commerce.user_vpn_detail(telegram_id, subscription_id)
        if item is None:
            self.send(chat_id, "That paid key was not found.")
            return
        key_id = str(item.get("outline_key_id") or "")
        used = int(item.get("last_usage_bytes") or 0)
        repair_status = str(item.get("repair_status") or "").lower()
        observed = False
        try:
            metrics, access_state = self._collect_outline_state(
                include_access=True,
                server_ids=(str(item.get("server_id") or ""),),
            )
            server_id = str(item.get("server_id") or getattr(self.service.outline, "default_server_id", "primary"))
            usage = metrics.get("byServer", {}).get(server_id, {})
            if isinstance(usage, dict) and key_id in usage:
                used = max(0, int(usage[key_id] or 0))
                observed = True
            nested_access = access_state.get("byServer", {}) if isinstance(access_state, dict) else {}
            server_access = nested_access.get(server_id, {}) if isinstance(nested_access, dict) else {}
            if isinstance(server_access, dict) and server_access.get(key_id):
                # A stable DNS hostname may have been applied after this
                # subscription was provisioned; prefer the current remote URL.
                item["access_url"] = server_access[key_id]
        except Exception as exc:
            print(f"paid key detail usage error: {type(exc).__name__}", file=sys.stderr)
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
        server_label = str(
            item.get("server_label") or item.get("server_id") or "assigned endpoint"
        )
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
                        f"Used {self._format_bytes(used)} · Remaining "
                        f"{self._format_bytes(remaining)} / {self._format_bytes(quota)}",
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
            copy = self._copy_text_button("📋 Copy Outline Key", access_url)
            if copy is not None:
                rows.append([copy])
            else:
                lines.append(
                    "The key is too long for Telegram's copy button. Use Show Keys as Text."
                )
        elif repair_status in {"pending", "running", "failed"}:
            lines.append(
                "🛠 Your key is being restored. Your quota is protected; refresh this panel shortly."
            )
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
            rows.append(
                [{"text": f"➕ Buy Another {name[:20]}", "callback_data": f"p:b:{plan_code}"[:64]}]
            )
        rows.append(
            [
                {"text": "◀ All Paid Keys", "callback_data": "k:l:0"},
                {"text": "🔄 Refresh", "callback_data": f"k:v:{subscription_id}"[:64]},
            ]
        )
        markup = {"inline_keyboard": rows}
        text = "\n".join(lines)
        if isinstance(message_id, int):
            self.edit_message(chat_id, message_id, text, markup)
        else:
            self.send(chat_id, text, markup)

    def _send_usage(self, chat_id: int, telegram_id: int) -> None:
        try:
            by_key, _ = self._collect_outline_state(
                server_ids=self._customer_server_ids(telegram_id),
            )
        except Exception as exc:
            self.send(chat_id, "VPN usage is temporarily unavailable. Please try again shortly.")
            print(f"usage metrics error: {type(exc).__name__}", file=sys.stderr)
            return
        entries = self.service.user_usage(telegram_id, by_key)
        if self.commerce is not None:
            entries.extend(self.commerce.user_usage(telegram_id, by_key))
        entries.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        if not entries:
            self.send(
                chat_id,
                "No VPN key usage is available yet. Claim a free tier or activate a paid plan first.",
                self._inline_keyboard([[("🎁 View Plans", "n:plans")]]),
            )
            return
        blocks = ["📶 Your VPN usage\nOutline transfer accounting (rolling 30-day window)"]
        for entry in entries:
            quota = int(entry["quota_bytes"])
            lines = [f"{entry['tier']}"]
            if entry.get("usage_observed"):
                used = int(entry["used_bytes"])
                remaining = int(entry["remaining_bytes"])
                percent = (used * 100 / quota) if quota else 0.0
                filled = min(10, max(0, int(percent / 10)))
                bar = "█" * filled + "░" * (10 - filled)
                lines.extend(
                    [
                        f"{bar} {percent:.1f}%",
                        f"Used: {self._format_bytes(used)}",
                        f"Remaining: {self._format_bytes(remaining)} of {self._format_bytes(quota)}",
                    ]
                )
            else:
                lines.extend(
                    [
                        "Usage: temporarily unavailable (endpoint telemetry not confirmed)",
                        "Remaining: withheld until a fresh Outline counter is received",
                    ]
                )
            lines.extend(
                [f"Expires: {format_user_datetime(entry['expires_at'])}", f"State: {entry['status']}"],
            )
            if (
                entry.get("server_label")
                and str(entry.get("server_health_status") or "unknown") != "healthy"
            ):
                lines.append(f"Endpoint: {entry['server_label']} · {entry['server_health_status']}")
            blocks.append("\n".join(lines))
        blocks.append(
            "Traffic is bytes reported by Outline for each key. It is not live speed, "
            "and the window is not a calendar-month reset."
        )
        self.send(
            chat_id,
            "\n\n".join(blocks),
            self._inline_keyboard([[("🔄 Refresh Usage", "n:usage"), ("🔐 My VPN", "n:myvpn")]]),
        )
