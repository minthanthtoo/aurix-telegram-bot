"""Telegram presentation transport and administrator command boundary."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
from urllib.parse import urlsplit
from datetime import datetime, timedelta, timezone
from typing import Any

import urllib3

from commerce import CommerceError, CommerceService
from entitlements import ClaimService
from observability import latency_log as _latency_log
from ports import ReceiptExtractorGateway
from telegram_admin import AdminOperations
from telegram_admin_panels import TelegramAdminMixin
from telegram_callbacks import TelegramCallbackMixin
from telegram_commands import TelegramCommandMixin
from telegram_maintenance import TelegramMaintenanceMixin
from vpn_dashboard import collect_customer_vpn_state
from receipt_llm import (
    OpenAICompatibleReceiptExtractor,
    ReceiptExtractionError,
    ReceiptLLMUnavailable,
)

UTC = timezone.utc
DEFAULT_MAINTENANCE_INTERVAL_SECONDS = 60.0
ADMIN_CONFIRMATION_TTL = timedelta(minutes=5)


class TelegramAPIError(RuntimeError):
    """A bounded, payload-free Telegram Bot API failure."""


class TelegramBot(
    TelegramAdminMixin,
    TelegramCallbackMixin,
    TelegramMaintenanceMixin,
    TelegramCommandMixin,
):
    CUSTOMER_BUTTON_COMMANDS = {
        "🎁 Daily 300MB": "/claim",
        "🚀 Monthly 3GB": "/trial",
        "💎 Upgrade 50GB": "/buy basic_50gb",
        "💠 Upgrade 100GB": "/buy standard_100gb",
        "💎 Plans & Upgrade": "/plans",
        "🔐 My VPN": "/myvpn",
        # Compatibility mappings for reply keyboards already present in old
        # Telegram messages. New menus use the unified My VPN dashboard.
        "📊 Status": "/status",
        "📶 Usage": "/usage",
        "💰 Wallet": "/wallet",
        "🧾 My Orders": "/myorders",
        "❓ Help": "/help",
        "🏠 Customer Menu": "/start",
    }
    ADMIN_BUTTON_COMMANDS = {
        "🛠 Admin Panel": "/admin",
        "📥 Pending Orders": "/orders",
        "🧾 Receipt Review": "/receipts",
        "📈 Capacity": "/capacity",
        "🔎 Consistency": "/reconcile",
        "🔁 Failed Jobs": "/failed",
        "🚨 Enforcement": "/enforcement",
        "🎁 Promo Settings": "/promo",
        # Retain this mapping for old keyboards, but do not render a global
        # ledger button: ledger access should be scoped to a specific order.
        "💰 Wallet Ledger": "/ledger",
    }
    ADMIN_ONLY_COMMANDS = frozenset(
        {
            "/admin",
            "/orders",
            "/receipts",
            "/capacity",
            "/reconcile",
            "/enforcement",
            "/promo",
            "/setpromo",
            "/stoppromo",
            "/resumepromo",
            "/failed",
            "/retry",
            "/retryjob",
            "/refund",
            "/ledger",
            "/receipt",
            "/rejectreceipt",
            "/verify",
            "/approve",
            "/reject",
        }
    )
    ADMIN_CONFIRMATION_COMMANDS = frozenset(
        {
            "/retry",
            "/refund",
            "/verify",
            "/rejectreceipt",
            "/approve",
            "/reject",
            "/setpromo",
            "/stoppromo",
            "/resumepromo",
        }
    )
    UNKNOWN_ACTION_TEXT = "Use the menu to choose an AuriX action."

    def __init__(
        self,
        token: str,
        service: ClaimService,
        commerce: CommerceService | None = None,
        admin_ids: set[int] | None = None,
        trial_ids: set[int] | None = None,
        receipt_extractor: ReceiptExtractorGateway | None = None,
        allow_text_payment: bool = True,
        maintenance_interval_seconds: float = DEFAULT_MAINTENANCE_INTERVAL_SECONDS,
        command_scope_cleanup_ids: set[int] | None = None,
    ):
        self.api = f"https://api.telegram.org/bot{token}"
        # urllib.request establishes a fresh TLS connection for every Bot API
        # call. On the Singapore host, a small fraction of those handshakes
        # stall for roughly 30 seconds. A bounded thread-safe pool keeps one
        # healthy connection hot while still allowing polling, maintenance,
        # and command-menu work to overlap.
        self._http = urllib3.PoolManager(
            num_pools=2,
            maxsize=4,
            block=True,
            retries=False,
        )
        self.service = service
        self.commerce = commerce
        self.admin_ids = admin_ids or set()
        self.admin_operations = AdminOperations(self.commerce, self.admin_ids, self.service)
        self.trial_ids = trial_ids or set()
        self.receipt_extractor = receipt_extractor or OpenAICompatibleReceiptExtractor()
        self.allow_text_payment = bool(allow_text_payment)
        configured_web_app_url = os.environ.get("AURIX_WEB_APP_URL", "").strip()
        parsed_web_app_url = urlsplit(configured_web_app_url)
        self.web_app_url = (
            configured_web_app_url
            if parsed_web_app_url.scheme == "https"
            and bool(parsed_web_app_url.netloc)
            and not parsed_web_app_url.username
            and not parsed_web_app_url.password
            else ""
        )
        self.maintenance_interval_seconds = max(1.0, float(maintenance_interval_seconds))
        self.command_scope_cleanup_ids = command_scope_cleanup_ids or set()
        self.offset = 0
        self.running = True
        self._maintenance_stop = threading.Event()
        self._maintenance_thread: threading.Thread | None = None
        self._admin_confirmations: dict[str, dict[str, Any]] = {}
        self._admin_confirmation_lock = threading.Lock()
        self._command_menu_ready = False
        self._command_menu_retry_enabled = hasattr(self.service, "database")
        self._command_menu_configure_attempted = False
        self._maintenance_lock = threading.Lock()
        self._panel_lock = threading.Lock()
        self._panels: dict[str, dict[str, Any]] = {}
        self._customer_inputs: dict[int, dict[str, Any]] = {}
        self._maintenance_last_status: dict[str, Any] = {
            "status": "never_run",
            "last_started_at": None,
            "last_completed_at": None,
            "last_success_at": None,
            "last_stage": None,
            "last_error": None,
        }

    def request(self, method: str, payload: dict[str, Any]) -> Any:
        started_at = time.perf_counter()
        try:
            response = self._http.request(
                "POST",
                f"{self.api}/{method}",
                body=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                timeout=urllib3.Timeout(
                    connect=5.0,
                    read=25.0 if method == "getUpdates" else 30.0,
                ),
                retries=False,
            )
            try:
                result = json.loads(response.data.decode("utf-8"))
            except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
                raise TelegramAPIError(
                    f"{method} returned an invalid JSON response"
                ) from exc
            if response.status >= 400:
                description = "request rejected"
                candidate = result.get("description") if isinstance(result, dict) else None
                if isinstance(candidate, str) and candidate.strip():
                    description = " ".join(candidate.split())[:240]
                raise TelegramAPIError(
                    f"{method} failed status={response.status}: {description}"
                )
        except urllib3.exceptions.HTTPError as exc:
            description = "request rejected"
            raise TelegramAPIError(
                f"{method} transport failed: {description}"
            ) from exc
        finally:
            _latency_log("telegram_request", started_at, method=method)
        if not result.get("ok"):
            raise RuntimeError("Telegram API request failed")
        return result["result"]

    def send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.request("sendMessage", payload)

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": text[:4096],
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.request("editMessageText", payload)

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

    def _promo_code_buttons(self, promo_code: str) -> list[dict[str, Any]]:
        """Build reusable one-tap redeem and clipboard controls for a promo."""
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
        if copy_button is not None:
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
        rows: list[list[dict[str, Any]]] = [
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
        if self.web_app_url:
            rows.insert(
                4,
                [{"text": "🌐 Open AuriX VPN Portal", "web_app": {"url": self.web_app_url}}],
            )
        return {"inline_keyboard": rows}

    def _handle_panel_callback(
        self, query: dict[str, Any], token: str, action: str, arg: str | None
    ) -> bool:
        user = query.get("from") or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        telegram_id, chat_id = user.get("id"), chat.get("id")
        with self._panel_lock:
            state = self._panels.get(token)
            if (
                state is None
                or state.get("telegram_id") != telegram_id
                or state.get("chat_id") != chat_id
            ):
                return False
            if time.monotonic() - float(state.get("updated_at", 0)) > 1800:
                self._panels.pop(token, None)
                return False
            if action == "next":
                state["page"] = int(state.get("page", 0)) + 1
            elif action == "prev":
                state["page"] = max(0, int(state.get("page", 0)) - 1)
            elif action == "refresh":
                pass
            elif action == "item":
                items = state.get("items", [])
                try:
                    item = items[int(arg or "-1")]
                except (ValueError, IndexError):
                    item = None
                if item is not None:
                    view = state["view"]
                    target = item.get("id") or item.get("job_id")
                    if view == "orders":
                        self._send_order_detail(chat_id, telegram_id, str(target), admin_view=True)
                    elif view == "receipts":
                        self.handle(
                            {
                                "chat": {"id": chat_id, "type": "private"},
                                "from": {"id": telegram_id},
                                "text": f"/receipt {target}",
                            }
                        )
                    elif view == "failed":
                        self.handle(
                            {
                                "chat": {"id": chat_id, "type": "private"},
                                "from": {"id": telegram_id},
                                "text": f"/order {item.get('order_id')}",
                            }
                        )
                    return True
            state["all_items"] = self._panel_data(telegram_id, state["view"])
            message_id = message.get("message_id") or state.get("message_id")
        text, markup = self._render_panel(token)
        if isinstance(message_id, int):
            try:
                self.edit_message(chat_id, message_id, text, markup)
                return True
            except Exception:
                pass
        self.send(chat_id, text, markup)
        return True

    @staticmethod
    def _payment_provider_rows(order_id: str) -> list[list[tuple[str, str]]]:
        return [
            [("🔵 KBZPay", f"t:p:{order_id}:kpay"), ("🟡 WavePay", f"t:p:{order_id}:wavepay")],
            [("🔴 AYA Pay", f"t:p:{order_id}:ayapay"), ("🟣 uabpay", f"t:p:{order_id}:uabpay")],
            [("🔷 CB Pay", f"t:p:{order_id}:cbpay")],
            [("🧾 Back to order", f"o:v:{order_id}")],
        ]

    def _send_payment_methods(self, chat_id: int, order: dict[str, Any]) -> None:
        self.send(
            chat_id,
            "Choose how you will pay\n\n"
            f"{order.get('plan_name') or order.get('plan_code')} · "
            f"{int(order['amount_minor']):,} {order['currency']}\n\n"
            "Tap one provider to see only its payment QR. Then send the completed "
            "receipt screenshot within 1 hour.",
            self._inline_keyboard(self._payment_provider_rows(str(order["id"]))),
        )

    def _send_payment_qr(
        self, chat_id: int, order: dict[str, Any], provider_code: str
    ) -> None:
        code = provider_code.strip().lower()
        env_code = {
            "kpay": "KPAY",
            "wavepay": "WAVEPAY",
            "ayapay": "AYAPAY",
            "uabpay": "UABPAY",
            "cbpay": "CBPAY",
        }.get(code)
        provider = str(order.get("selected_payment_provider") or code)
        if env_code is None:
            self.send(chat_id, "Unsupported payment method.")
            return
        qr = os.environ.get(f"PAYMENT_QR_{env_code}", "").strip()
        recipient = os.environ.get(f"PAYMENT_RECIPIENT_{env_code}", "").strip()
        caption = (
            f"{provider} · {int(order['amount_minor']):,} {order['currency']}\n"
            + (f"Recipient: {recipient}\n" if recipient else "")
            + "\nPay the exact amount, then send the completed receipt screenshot here "
            "within 1 hour. A QR is payment destination—not proof of payment."
        )
        markup = self._inline_keyboard(
            [
                [("🔁 Other payment method", f"t:m:{order['id']}")],
                [("🧾 View order", f"o:v:{order['id']}")],
            ]
        )
        if qr:
            try:
                self.send_photo(chat_id, qr, caption, markup)
                return
            except Exception as exc:
                print(f"payment QR delivery error: {type(exc).__name__}", file=sys.stderr)
        self.send(
            chat_id,
            caption
            + "\n\n⚠️ The QR image is temporarily unavailable. Do not transfer until it "
            "is shown; choose another method or contact support.",
            markup,
        )

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

    def _consume_customer_input(self, telegram_id: int, text: str) -> str | None:
        state = self._customer_inputs.get(int(telegram_id))
        if not state:
            return None
        if float(state.get("expires_at", 0)) <= time.monotonic():
            self._customer_inputs.pop(int(telegram_id), None)
            return None
        if state.get("action") == "topup_amount" and text.strip().replace(",", "").isdigit():
            self._customer_inputs.pop(int(telegram_id), None)
            return f"/topup {text.strip().replace(',', '')}"
        return None

    @staticmethod
    def _order_filter_match(order: dict[str, Any], selected: str) -> bool:
        if selected == "all":
            return True
        status = str(order.get("status") or "")
        stage = str(order.get("stage") or "")
        if selected == "open":
            return status in ("awaiting_payment", "payment_submitted")
        if selected == "completed":
            return stage == "fulfilled" or (
                str(order.get("order_type") or "vpn") == "wallet_topup"
                and status == "approved"
            )
        return status == selected

    def _render_customer_orders_panel(self, token: str) -> tuple[str, dict[str, Any]]:
        with self._panel_lock:
            state = self._panels[token]
            selected = str(state.get("filter") or "open")
            all_items = list(state.get("all_items") or [])
            page = max(0, int(state.get("page", 0)))
        filtered = [item for item in all_items if self._order_filter_match(item, selected)]
        page_size = 4
        pages = max(1, (len(filtered) + page_size - 1) // page_size)
        page = min(page, pages - 1)
        current = filtered[page * page_size : (page + 1) * page_size]
        counts = {
            name: sum(self._order_filter_match(item, name) for item in all_items)
            for name in ("open", "completed", "cancelled", "rejected", "all")
        }
        blocks = [
            "🧾 My Orders",
            f"{selected.title()} · {len(filtered)} order(s) · Page {page + 1}/{pages}",
        ]
        for offset, order in enumerate(current, start=page * page_size + 1):
            icon = {
                "fulfilled": "✅",
                "awaiting_payment": "🕒",
                "review_pending": "🔎",
                "rejected": "❌",
                "cancelled": "🚫",
            }.get(str(order.get("stage")), "•")
            blocks.append(
                f"{icon} #{offset} · {order.get('plan_name') or order.get('plan_code')}\n"
                f"{int(order['amount_minor']):,} {order['currency']} · "
                f"{str(order.get('stage') or order.get('status')).replace('_', ' ')}\n"
                f"{str(order['id'])[:10]} · {str(order.get('created_at') or '')[:16]}"
            )
        if not current:
            blocks.append("Nothing in this category.")
        rows: list[list[tuple[str, str]]] = [
            [
                (f"🕒 Open {counts['open']}", f"c2:{token}:filter:open"),
                (f"✅ Done {counts['completed']}", f"c2:{token}:filter:completed"),
            ],
            [
                (f"🚫 Cancelled/expired {counts['cancelled']}", f"c2:{token}:filter:cancelled"),
                (f"❌ Admin rejected {counts['rejected']}", f"c2:{token}:filter:rejected"),
            ],
            [(f"📚 All {counts['all']}", f"c2:{token}:filter:all")],
        ]
        for index, order in enumerate(current):
            rows.append(
                [(f"Open #{page * page_size + index + 1}", f"c2:{token}:item:{index}")]
            )
        navigation: list[tuple[str, str]] = []
        if page > 0:
            navigation.append(("◀", f"c2:{token}:prev"))
        navigation.append((f"{page + 1}/{pages}", f"c2:{token}:refresh"))
        if page + 1 < pages:
            navigation.append(("▶", f"c2:{token}:next"))
        rows.append(navigation)
        rows.append([("🔄 Refresh", f"c2:{token}:refresh")])
        with self._panel_lock:
            state = self._panels[token]
            state["page"] = page
            state["items"] = current
            state["updated_at"] = time.monotonic()
        return "\n\n".join(blocks)[:4096], self._inline_keyboard(rows)

    def _open_customer_orders_panel(
        self, chat_id: int, telegram_id: int, message_id: int | None = None
    ) -> None:
        if self.commerce is None:
            self.send(chat_id, "Order tracking is not configured.")
            return
        token = self._new_panel(chat_id, telegram_id, "customer_orders")
        orders = self.commerce.list_user_orders(telegram_id, limit=50)
        with self._panel_lock:
            self._panels[token].update(
                {
                    "filter": "open",
                    "all_items": orders,
                }
            )
        text, markup = self._render_customer_orders_panel(token)
        if message_id is not None:
            try:
                self.edit_message(chat_id, message_id, text, markup)
                return
            except Exception:
                pass
        result = self.send(chat_id, text, markup)
        if isinstance(result, dict) and result.get("message_id"):
            with self._panel_lock:
                self._panels[token]["message_id"] = int(result["message_id"])

    def _handle_customer_panel_callback(
        self, query: dict[str, Any], token: str, action: str, arg: str | None
    ) -> bool:
        user = query.get("from") or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        telegram_id, chat_id = user.get("id"), chat.get("id")
        selected_item: dict[str, Any] | None = None
        with self._panel_lock:
            state = self._panels.get(token)
            if (
                state is None
                or state.get("telegram_id") != telegram_id
                or state.get("chat_id") != chat_id
                or not str(state.get("view") or "").startswith("customer_")
                or time.monotonic() - float(state.get("updated_at", 0)) > 1800
            ):
                return False
            if action == "filter":
                state["filter"] = str(arg or "open")
                state["page"] = 0
            elif action == "next":
                state["page"] = int(state.get("page", 0)) + 1
            elif action == "prev":
                state["page"] = max(0, int(state.get("page", 0)) - 1)
            elif action == "first":
                state["page"] = 0
            elif action == "last":
                state["page"] = max(0, int(state.get("pages", 1)) - 1)
            elif action == "item":
                try:
                    selected_item = list(state.get("items") or [])[int(arg or "-1")]
                except (IndexError, ValueError):
                    return False
            view = str(state["view"])
            message_id = message.get("message_id") or state.get("message_id")
        if selected_item is not None:
            self._send_order_detail(chat_id, telegram_id, str(selected_item["id"]))
            return True
        if action == "refresh" and view == "customer_orders" and self.commerce is not None:
            fresh = self.commerce.list_user_orders(int(telegram_id), limit=50)
            with self._panel_lock:
                if token in self._panels:
                    self._panels[token]["all_items"] = fresh
        if view == "customer_orders":
            text, markup = self._render_customer_orders_panel(token)
        elif view == "customer_vpn":
            if action == "refresh":
                self._refresh_customer_vpn_panel(token, int(telegram_id))
            text, markup = self._render_customer_vpn_panel(token)
        else:
            return False
        if isinstance(message_id, int):
            try:
                self.edit_message(int(chat_id), message_id, text, markup)
                return True
            except Exception as exc:
                if "message is not modified" in str(exc).lower():
                    return True
        self.send(int(chat_id), text, markup)
        return True

    def _customer_keyboard(self, telegram_id: int) -> dict[str, Any]:
        try:
            promo_locked = bool(
                self.service.giveaway_status(telegram_id)["access_lock_active"]
            )
        except Exception:
            promo_locked = False
        rows = [["🔐 My VPN"]]
        paid_active = self._free_claim_blocked_by_paid(telegram_id)
        if not promo_locked and not paid_active:
            rows.append(["🎁 Daily 300MB", "🚀 Monthly 3GB"])
        if not promo_locked:
            rows.append(["💎 Plans & Upgrade", "🧾 My Orders"])
        else:
            rows.append(["🧾 My Orders"])
        rows.extend([["💰 Wallet", "❓ Help"]])
        markup = self._reply_keyboard(rows)
        if self.web_app_url:
            markup["keyboard"].append(
                [{"text": "🌐 Open AuriX VPN Portal", "web_app": {"url": self.web_app_url}}]
            )
        return markup

    def configure_commands(self) -> None:
        self._command_menu_configure_attempted = True
        customer_commands = [
            {"command": "start", "description": "Open the AuriX menu"},
            {"command": "myvpn", "description": "Keys, status and data usage"},
            {"command": "claim", "description": "Claim free 300 MB for 24 hours"},
            {"command": "trial", "description": "Claim free 3 GB for 30 days"},
            {"command": "plans", "description": "View current plans and prices"},
            {"command": "wallet", "description": "Show wallet balance"},
            {"command": "topup", "description": "Add money to your wallet"},
            {"command": "myorders", "description": "Track your recent orders"},
            {"command": "whoami", "description": "Show your Telegram ID"},
            {"command": "help", "description": "Show customer help"},
        ]
        errors: list[str] = []

        def set_and_verify(
            scope: dict[str, Any], commands: list[dict[str, str]], label: str
        ) -> bool:
            try:
                self.request("setMyCommands", {"commands": commands, "scope": scope})
                current = self.request("getMyCommands", {"scope": scope})
                current_names = (
                    {str(item.get("command")) for item in current}
                    if isinstance(current, list)
                    else set()
                )
                expected = {item["command"] for item in commands}
                if current_names != expected:
                    raise RuntimeError("Telegram returned an unexpected command list")
                return True
            except Exception as exc:
                errors.append(f"{label}: {type(exc).__name__}")
                return False

        set_and_verify({"type": "default"}, customer_commands, "default command scope")

        scope_store = getattr(self.service, "database", None)
        try:
            list_scopes = getattr(scope_store, "list_command_scope_ids", None)
            known_scopes = set(list_scopes()) if callable(list_scopes) else set()
        except Exception as exc:
            known_scopes = set()
            errors.append(f"load command scope state: {type(exc).__name__}")

        stale_scopes = (known_scopes | self.command_scope_cleanup_ids) - self.admin_ids
        for admin_id in sorted(stale_scopes):
            try:
                scope = {"type": "chat", "chat_id": admin_id}
                self.request(
                    "deleteMyCommands",
                    {"scope": scope},
                )
                remaining = self.request("getMyCommands", {"scope": scope})
                if not isinstance(remaining, list) or remaining:
                    raise RuntimeError("Telegram retained commands for removed admin scope")
                if scope_store and hasattr(scope_store, "remove_command_scope"):
                    scope_store.remove_command_scope(admin_id)
            except Exception as exc:
                errors.append(f"remove admin command scope {admin_id}: {type(exc).__name__}")

        admin_commands = customer_commands + [
            {"command": "admin", "description": "Open the admin panel"},
            {"command": "promo", "description": "View and configure promo campaign"},
        ]
        for admin_id in self.admin_ids:
            scope = {"type": "chat", "chat_id": admin_id}
            if set_and_verify(scope, admin_commands, f"admin command scope {admin_id}"):
                if scope_store and hasattr(scope_store, "record_command_scope"):
                    try:
                        scope_store.record_command_scope(admin_id)
                    except Exception as exc:
                        errors.append(
                            f"record admin command scope {admin_id}: {type(exc).__name__}"
                        )
        if self.web_app_url:
            try:
                self.request(
                    "setChatMenuButton",
                    {
                        "menu_button": {
                            "type": "web_app",
                            "text": "AuriX VPN",
                            "web_app": {"url": self.web_app_url},
                        }
                    },
                )
            except Exception as exc:
                # The inline/reply Web App button remains available if this
                # optional global menu-button call is unavailable.
                print(
                    f"WARNING: Telegram Web App menu button configuration failed: {type(exc).__name__}",
                    file=sys.stderr,
                )
        if errors:
            self._command_menu_ready = False
            raise RuntimeError("Telegram command menu degraded: " + "; ".join(errors))
        self._command_menu_ready = True

    def send_photo(
        self,
        chat_id: int,
        file_id: str,
        caption: str = "",
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": file_id,
            "caption": caption[:1024],
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.request("sendPhoto", payload)

    def send_document(
        self,
        chat_id: int,
        file_id: str,
        caption: str = "",
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "document": file_id,
            "caption": caption[:1024],
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.request("sendDocument", payload)

    @staticmethod
    def _receipt_review_caption(receipt: dict[str, Any]) -> str:
        extracted = receipt.get("extraction") or {}
        evidence_id = str(receipt["id"])
        flags = list(extracted.get("flags") or [])
        return (
            "🧾 Receipt awaiting review\n"
            f"Evidence: {evidence_id}\n"
            f"Order: {receipt['order_id']}\n"
            f"Customer: {receipt['telegram_id']}\n"
            f"Expected: {int(receipt['amount_minor']):,} {receipt['currency']}\n"
            f"Method: {receipt.get('provider') or '-'}\n\n"
            f"Extracted ID: {extracted.get('transaction_id') or '-'}\n"
            f"Extracted amount: {extracted.get('amount_minor') or '-'} "
            f"{extracted.get('currency') or ''}\n"
            f"Receipt time: {extracted.get('timestamp') or '-'}\n"
            f"Recipient: {extracted.get('recipient') or '-'}\n"
            f"Confidence: {float(extracted.get('confidence') or 0):.0%}\n"
            f"Checks: {'⚠️ ' + ', '.join(flags) if flags else '✅ parser checks passed'}\n\n"
            "Final approval still requires matching the receiving account transaction."
        )

    def _send_receipt_review(self, chat_id: int, receipt: dict[str, Any]) -> None:
        """Send stored evidence, preferring a private Storage signed URL."""
        evidence_id = str(receipt["id"])
        extracted = receipt.get("extraction") or {}
        rows = [[("View Order", f"a:o:{receipt['order_id']}")]]
        if (
            extracted.get("transaction_id")
            and extracted.get("amount_minor")
            and not list(extracted.get("flags") or [])
        ):
            rows.append([("✅ Verify extracted facts…", f"a:v:{evidence_id}")])
        rows.append([("🛑 Reject Receipt", f"a:q:{evidence_id}")])
        markup = self._inline_keyboard(rows)
        file_id = str(receipt["telegram_file_id"])
        storage = getattr(self.commerce, "receipt_storage", None)
        storage_path = receipt.get("storage_path")
        if storage is not None and storage_path and receipt.get("storage_status") == "stored":
            try:
                signed = storage.signed_url(str(storage_path), expires_in=300)
                if signed:
                    file_id = str(signed)
            except Exception as exc:
                # Telegram's original file ID remains a compatibility fallback
                # for legacy rows or a temporary Storage outage.
                print(
                    f"receipt storage signed URL error: {type(exc).__name__}",
                    file=sys.stderr,
                )
        caption = self._receipt_review_caption(receipt)
        media_type = receipt.get("telegram_media_type")
        primary = self.send_document if media_type == "document" else self.send_photo
        fallback = self.send_photo if media_type == "document" else self.send_document
        try:
            primary(chat_id, file_id, caption, markup)
        except (RuntimeError, urllib.error.HTTPError):
            # Older rows predate telegram_media_type, and Telegram file IDs can
            # only be reused by the API method matching their original type.
            fallback(chat_id, file_id, caption, markup)

    def _download_telegram_file(self, file_id: str) -> tuple[bytes, str]:
        info = self.request("getFile", {"file_id": file_id})
        file_path = info.get("file_path") if isinstance(info, dict) else None
        if not isinstance(file_path, str) or not file_path:
            raise RuntimeError("Telegram file path was unavailable")
        token = self.api.rsplit("/bot", 1)[-1]
        started_at = time.perf_counter()
        try:
            response = self._http.request(
                "GET",
                f"https://api.telegram.org/file/bot{token}/{file_path}",
                timeout=urllib3.Timeout(connect=5.0, read=30.0),
                retries=False,
            )
            if response.status >= 400:
                raise TelegramAPIError(
                    f"getFile download failed status={response.status}"
                )
            data = response.data
        except urllib3.exceptions.HTTPError as exc:
            raise TelegramAPIError("getFile download transport failed") from exc
        finally:
            _latency_log("telegram_file_download", started_at)
        if len(data) > 20 * 1024 * 1024:
            raise RuntimeError("Receipt image exceeds Telegram download limit")
        mime = "image/jpeg" if file_path.lower().endswith((".jpg", ".jpeg")) else "image/png"
        return data, mime

    def _latency_action(self, update: dict[str, Any]) -> str:
        """Return a bounded operation label without logging user text or IDs."""
        message = update.get("message")
        if isinstance(message, dict):
            if message.get("photo") or message.get("document"):
                return "receipt"
            text = message.get("text")
            if not isinstance(text, str):
                return "message"
            normalized = self.CUSTOMER_BUTTON_COMMANDS.get(text.strip(), text.strip())
            if normalized == text.strip():
                normalized = self.ADMIN_BUTTON_COMMANDS.get(text.strip(), text.strip())
            command = normalized.split(maxsplit=1)[0].split("@", 1)[0].lower()
            return command[:48] if command.startswith("/") else "text"
        query = update.get("callback_query")
        data = query.get("data") if isinstance(query, dict) else None
        if not isinstance(data, str):
            return "callback"
        parts = data.split(":", 2)[:2]
        if all(part.replace("_", "").isalnum() for part in parts):
            return ("callback:" + ":".join(parts))[:48]
        return "callback"

    def _pending_order_id(self, telegram_id: int, caption: str = "") -> str | None:
        candidate = caption.split()
        if candidate and candidate[0].startswith("/") and len(candidate) > 1:
            return candidate[1]
        if self.commerce is None:
            return None
        pending = self.commerce.pending_order_for_user(telegram_id)
        return pending["id"] if pending else None

    def _handle_receipt(self, message: dict[str, Any], chat_id: int, telegram_id: int) -> None:
        if self.commerce is None:
            self.send(chat_id, "Paid plans are not configured in this staging process.")
            return
        photos = message.get("photo")
        document = message.get("document")
        file_id = None
        unique_id = None
        mime = "image/jpeg"
        media_type = "photo"
        if isinstance(photos, list) and photos:
            item = photos[-1]
            if isinstance(item, dict):
                file_id = item.get("file_id")
                unique_id = item.get("file_unique_id")
                mime = "image/jpeg"
        elif isinstance(document, dict) and str(document.get("mime_type", "")).startswith("image/"):
            file_id = document.get("file_id")
            unique_id = document.get("file_unique_id")
            mime = str(document.get("mime_type"))[:64]
            media_type = "document"
        if not isinstance(file_id, str):
            return
        order_id = self._pending_order_id(telegram_id, str(message.get("caption") or ""))
        if not order_id:
            self.send(
                chat_id, "Create an order with /buy basic_50gb, then send its receipt screenshot."
            )
            return
        order = self.commerce.order_detail(order_id, telegram_id)
        selected_provider = (
            str(order.get("selected_payment_provider") or "manual")
            if isinstance(order, dict)
            else "manual"
        )
        try:
            image, mime = self._download_telegram_file(file_id)
            extraction = None
            duplicate_status = self.commerce.receipt_duplicate_status(
                telegram_id,
                order_id,
                image,
                str(unique_id) if unique_id else None,
            )
            if duplicate_status == "different_order":
                raise CommerceError(
                    "This receipt image was already submitted for another order"
                )
            if duplicate_status == "new":
                try:
                    extraction = self.receipt_extractor.extract(image, mime)
                except ReceiptLLMUnavailable:
                    pass  # retain evidence for a human reviewer
                except ReceiptExtractionError as exc:
                    print(f"receipt extraction error: {type(exc).__name__}", file=sys.stderr)
                except Exception as exc:
                    # Model/provider output is untrusted; a parser failure must not
                    # prevent the evidence record from reaching manual review.
                    print(f"receipt extraction error: {type(exc).__name__}", file=sys.stderr)
            extraction_data = (
                extraction.as_dict() if hasattr(extraction, "as_dict") else extraction
            )
            if isinstance(extraction_data, dict):
                provider_env = {
                    "kbzpay": "KPAY",
                    "wavepay": "WAVEPAY",
                    "ayapay": "AYAPAY",
                    "uabpay": "UABPAY",
                    "cbpay": "CBPAY",
                }.get("".join(selected_provider.lower().split()))
                expected_recipient = (
                    os.environ.get(f"PAYMENT_RECIPIENT_{provider_env}", "").strip()
                    if provider_env
                    else ""
                )
                if expected_recipient:
                    extraction_data["expected_recipient"] = expected_recipient
            result = self.commerce.submit_receipt(
                telegram_id,
                order_id,
                provider=selected_provider,
                file_id=file_id,
                file_unique_id=str(unique_id) if unique_id else None,
                image_bytes=image,
                mime_type=mime,
                extraction=extraction_data,
                telegram_media_type=media_type,
            )
        except (CommerceError, RuntimeError, urllib.error.URLError) as exc:
            self.send(chat_id, str(exc) or "Receipt could not be recorded. Try again later.")
            return
        if result.get("transaction_id"):
            self.send(
                chat_id,
                "Receipt received. Transaction ID extracted and queued for staff verification.",
            )
        else:
            self.send(
                chat_id,
                "Receipt received for manual review. No payment is activated from the image alone.",
            )
        evidence_id = str(result["evidence_id"])
        for admin_id in self.admin_ids:
            try:
                # Keep receipt images and customer metadata out of persistent
                # Telegram history. Admins open evidence on demand through the
                # authorized review route.
                self.send(
                    admin_id,
                    f"New receipt submitted for order {order_id}. Evidence: {evidence_id}.",
                    self._inline_keyboard(
                        [
                            [
                                ("Open Receipt", f"a:r:{evidence_id}"),
                                ("Open Order", f"a:o:{order_id}"),
                            ]
                        ]
                    ),
                )
            except Exception as exc:
                print(f"admin receipt notification error: {type(exc).__name__}", file=sys.stderr)

    def _is_admin(self, telegram_id: int) -> bool:
        return telegram_id in self.admin_ids

    def _trial_allowed(self, telegram_id: int) -> bool:
        """Keep the optional trial allow-list consistent across every entrypoint."""
        return not self.trial_ids or int(telegram_id) in self.trial_ids

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
        lines.append("free_3gb — free every 30 days — 3 GiB / 30 days (use /trial)")
        plans = self.commerce.plans()
        for plan in plans:
            quota = f"{plan.quota_bytes / 1024**3:g} GB" if plan.quota_bytes else "fair-use"
            lines.append(
                f"{plan.code} — {plan.price_minor:,} {plan.currency} — {quota} / {plan.duration_days} days"
            )
        lines.append("\nBuy with: /buy <plan-code>")
        markup = self._inline_keyboard(
            [
                [
                    (
                        f"💎 {plan.name} · {plan.price_minor:,} {plan.currency}",
                        f"p:b:{plan.code}",
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
                f"Expires: {giveaway['expires_at']}\n"
                "Regular plans: "
                + ("paused until gift or season ends" if giveaway["access_lock_active"] else "available")
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
            subscriptions = self.commerce.user_vpns(telegram_id)
        else:
            latest = self.commerce.user_vpn(telegram_id)
            subscriptions = [latest] if latest else []
        if not subscriptions:
            self.send(
                chat_id,
                giveaway_text + (
                    "\n\nThe access URL is shown only when the giveaway is first claimed. "
                    "Use /usage to track quota." if giveaway["winner"] else
                    "\n\nNo subscription found. Use /plans to see available plans."
                ),
                self._customer_keyboard(telegram_id),
            )
            return
        subscription = subscriptions[0]
        text = giveaway_text + "\n\n" + (
            f"Status: {subscription['status']}\n"
            f"Plan: {subscription['plan_code']}\n"
            f"Expires: {subscription['expires_at']}\n"
            f"Paid keys: {sum(1 for item in subscriptions if item.get('key_status') == 'active')}"
        )
        if include_key:
            key_blocks = []
            for item in subscriptions:
                if item.get("access_url") and item.get("key_status") == "active":
                    key_blocks.append(
                        f"{item['plan_code']} · expires {item['expires_at']}\n{item['access_url']}"
                    )
                elif item.get("status") == "pending":
                    key_blocks.append(
                        f"{item['plan_code']} · provisioning pending (expires {item['expires_at']})"
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
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        unit = units[0]
        for unit in units:
            if amount < 1024 or unit == units[-1]:
                break
            amount /= 1024
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

    def _collect_customer_vpn_state(self, telegram_id: int) -> dict[str, Any]:
        """Collect one fresh customer snapshot without performing Telegram I/O."""
        return collect_customer_vpn_state(self.service, self.commerce, telegram_id)

    @staticmethod
    def _vpn_filter_match(entry: dict[str, Any], selected: str) -> bool:
        status = str(entry.get("status") or "unknown").lower()
        if selected == "all":
            return True
        if selected == "active":
            return status == "active"
        if selected == "pending":
            return "pending" in status
        return status not in ("active",) and "pending" not in status

    def _render_customer_vpn_panel(self, token: str) -> tuple[str, dict[str, Any]]:
        with self._panel_lock:
            state = self._panels[token]
            entries = list(state.get("all_items") or [])
            selected = str(state.get("filter") or "active")
            page = max(0, int(state.get("page", 0)))
            usage_available = bool(state.get("usage_available", True))
            access_available = bool(state.get("access_available", True))
        filtered = [item for item in entries if self._vpn_filter_match(item, selected)]
        page_size = 2
        pages = max(1, (len(filtered) + page_size - 1) // page_size)
        page = min(page, pages - 1)
        displayed = filtered[page * page_size : (page + 1) * page_size]
        counts = {
            name: sum(self._vpn_filter_match(item, name) for item in entries)
            for name in ("active", "pending", "ended", "all")
        }
        blocks = [
            "🔐 My VPN",
            f"{selected.title()} · {len(filtered)} key(s) · Page {page + 1}/{pages}",
        ]
        copy_rows: list[list[dict[str, Any]]] = []
        for index, entry in enumerate(displayed, start=page * page_size + 1):
            status = str(entry.get("status") or "unknown")
            icon = "🟢" if status == "active" else "🟡" if "pending" in status else "🔴"
            quota = int(entry.get("quota_bytes") or 0)
            used = int(entry.get("used_bytes") or 0)
            remaining = max(0, int(entry.get("remaining_bytes") or 0))
            lines = [
                f"{index}. {entry['tier']}",
                f"{icon} {status} · Expires: {entry.get('expires_at') or 'pending'}",
            ]
            if quota > 0:
                formatter = (
                    self._format_decimal_bytes
                    if entry.get("decimal_quota")
                    else self._format_bytes
                )
                percent = min(100.0, used * 100 / quota)
                filled = min(10, max(0, int(percent / 10)))
                bar = "█" * filled + "░" * (10 - filled)
                observed_note = "" if entry.get("usage_observed") else " · awaiting traffic data"
                lines.extend(
                    [
                        f"{bar} {percent:.1f}%{observed_note}",
                        f"Used {formatter(used)} · Remaining "
                        f"{formatter(remaining)} / {formatter(quota)}",
                    ]
                )
            access_url = entry.get("access_url")
            if isinstance(access_url, str) and access_url:
                lines.append(f"Key:\n{access_url}")
                copy_button = self._copy_text_button(
                    f"📋 Copy #{index} · {str(entry['tier'])[:28]}", access_url
                )
                if copy_button is not None:
                    copy_rows.append([copy_button])
                else:
                    lines.append("Press and hold the key above to copy it.")
            elif status == "active" and entry.get("key_type") != "paid" and not access_available:
                lines.append("Key retrieval is temporarily unavailable; refresh shortly.")
            elif status == "activation pending":
                lines.append("Your key will appear here after activation.")
            blocks.append("\n".join(lines))
        if not displayed:
            blocks.append("Nothing in this category.")
        if not usage_available:
            blocks.append("⚠️ Usage is temporarily unavailable; key status is still shown.")
        else:
            blocks.append("Usage is Outline's rolling 30-day transfer total, not live speed.")
        rows: list[list[dict[str, Any]]] = [
            [
                {"text": f"🟢 Active {counts['active']}", "callback_data": f"c2:{token}:filter:active"},
                {"text": f"🟡 Pending {counts['pending']}", "callback_data": f"c2:{token}:filter:pending"},
            ],
            [
                {"text": f"⚫ Ended {counts['ended']}", "callback_data": f"c2:{token}:filter:ended"},
                {"text": f"📚 All {counts['all']}", "callback_data": f"c2:{token}:filter:all"},
            ],
        ]
        rows.extend(copy_rows)
        navigation: list[dict[str, Any]] = []
        if page > 0:
            navigation.extend(
                [
                    {"text": "⏮", "callback_data": f"c2:{token}:first"},
                    {"text": "◀", "callback_data": f"c2:{token}:prev"},
                ]
            )
        navigation.append({"text": f"{page + 1}/{pages}", "callback_data": f"c2:{token}:refresh"})
        if page + 1 < pages:
            navigation.extend(
                [
                    {"text": "▶", "callback_data": f"c2:{token}:next"},
                    {"text": "⏭", "callback_data": f"c2:{token}:last"},
                ]
            )
        rows.append(navigation)
        rows.append([{"text": "🔄 Refresh", "callback_data": f"c2:{token}:refresh"}])
        with self._panel_lock:
            state = self._panels[token]
            state["page"] = page
            state["pages"] = pages
            state["updated_at"] = time.monotonic()
        return "\n\n".join(blocks)[:4096], {"inline_keyboard": rows}

    def _refresh_customer_vpn_panel(self, token: str, telegram_id: int) -> None:
        fresh = self._collect_customer_vpn_state(telegram_id)
        with self._panel_lock:
            if token in self._panels:
                self._panels[token].update(fresh)

    def _send_my_vpn(self, chat_id: int, telegram_id: int) -> None:
        token = self._new_panel(chat_id, telegram_id, "customer_vpn")
        fresh = self._collect_customer_vpn_state(telegram_id)
        with self._panel_lock:
            self._panels[token].update(fresh)
            self._panels[token]["filter"] = "active" if fresh["all_items"] else "all"
        text, markup = self._render_customer_vpn_panel(token)
        result = self.send(chat_id, text, markup)
        if isinstance(result, dict) and result.get("message_id"):
            with self._panel_lock:
                self._panels[token]["message_id"] = int(result["message_id"])

    def _send_usage(self, chat_id: int, telegram_id: int) -> None:
        try:
            connectivity = getattr(self.service, "connectivity", None)
            by_key = (
                connectivity.collect_metrics()
                if connectivity is not None
                else self.service.outline.transfer_metrics()
            )
            if not isinstance(by_key, dict):
                raise ValueError("invalid Outline metrics response")
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
            used = int(entry["used_bytes"])
            quota = int(entry["quota_bytes"])
            remaining = int(entry["remaining_bytes"])
            percent = (used * 100 / quota) if quota else 0.0
            filled = min(10, max(0, int(percent / 10)))
            bar = "█" * filled + "░" * (10 - filled)
            observed_note = "" if entry.get("usage_observed") else " (no traffic recorded yet)"
            blocks.append(
                f"{entry['tier']}\n"
                f"{bar} {percent:.1f}%\n"
                f"Used: {self._format_bytes(used)}{observed_note}\n"
                f"Remaining: {self._format_bytes(remaining)} of {self._format_bytes(quota)}\n"
                f"Expires: {entry['expires_at']}\n"
                f"State: {entry['status']}"
            )
        blocks.append(
            "Traffic is bytes reported by Outline for each key. It is not live speed, "
            "and the window is not a calendar-month reset."
        )
        self.send(
            chat_id,
            "\n\n".join(blocks),
            self._inline_keyboard([[("🔄 Refresh Usage", "n:usage"), ("🔐 My VPN", "n:myvpn")]]),
        )

    def run(self) -> None:
        self._maintenance_stop.clear()
        maintenance_thread = threading.Thread(
            target=self._maintenance_loop,
            name="aurix-maintenance",
            daemon=True,
        )
        self._maintenance_thread = maintenance_thread
        maintenance_thread.start()
        try:
            while self.running:
                try:
                    updates = self.request(
                        "getUpdates",
                        {
                            "offset": self.offset,
                            "timeout": 20,
                            "allowed_updates": ["message", "callback_query"],
                        },
                    )
                except KeyboardInterrupt:
                    break
                except Exception as exc:
                    print(f"bot poll error: {type(exc).__name__}: {exc}", file=sys.stderr)
                    self._maintenance_stop.wait(5)
                    continue
                for update in updates:
                    self.offset = update["update_id"] + 1
                    if not self.service.database.mark_update_seen(update["update_id"]):
                        continue
                    started_at = time.perf_counter()
                    action = self._latency_action(update)
                    try:
                        if "message" in update:
                            self.handle(update["message"])
                        elif "callback_query" in update:
                            self.handle_callback(update["callback_query"])
                    except Exception as exc:
                        print(
                            f"update handler error: {type(exc).__name__}: {exc}",
                            file=sys.stderr,
                        )
                        _latency_log(
                            "update_handler",
                            started_at,
                            update_id=update["update_id"],
                            kind="message" if "message" in update else "callback",
                            action=action,
                            status="error",
                        )
                        continue
                    _latency_log(
                        "update_handler",
                        started_at,
                        update_id=update["update_id"],
                        kind="message" if "message" in update else "callback",
                        action=action,
                        status="ok",
                    )
        finally:
            self.stop()
            maintenance_thread.join(timeout=5)
            self._maintenance_thread = None
