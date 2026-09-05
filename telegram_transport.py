"""Telegram presentation transport and administrator command boundary."""

from __future__ import annotations

import json
import hashlib
import sys
import threading
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import urllib3
from urllib3.filepost import encode_multipart_formdata

from commerce import CommerceError, CommerceService
from entitlements import ClaimService
from observability import latency_log as _latency_log
from ports import ReceiptExtractorGateway
from quota_alerts import MODE_STEPS, alert_level_labels
from telegram_admin import AdminOperations
from telegram_admin_panels import TelegramAdminMixin
from telegram_callbacks import TelegramCallbackMixin
from telegram_commands import TelegramCommandMixin
from telegram_maintenance import TelegramMaintenanceMixin
from receipt_llm import build_receipt_extractor
from telegram_formatting import format_user_datetime

UTC = timezone.utc
DEFAULT_MAINTENANCE_INTERVAL_SECONDS = 60.0
ADMIN_CONFIRMATION_TTL = timedelta(minutes=5)
INTERACTION_STATE_TTL = timedelta(minutes=10)


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
        "🔔 Usage Alerts": "/alerts",
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
        "🧩 Key Repairs": "/repairs",
        "🚨 Enforcement": "/enforcement",
        "🎁 Promo Settings": "/promo",
        "🧪 Receipt System": "/receiptsystem",
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
            "/repairs",
            "/migrations",
            "/retry",
            "/retryjob",
            "/refund",
            "/ledger",
            "/receipt",
            "/rejectreceipt",
            "/verify",
            "/approve",
            "/reject",
            "/receiptsystem",
            "/receiptmode",
            "/receipttest",
            "/staff",
            "/notifications",
            "/serverstate",
            "/migratekey",
            "/approverepair",
        }
    )
    OWNER_ONLY_COMMANDS = frozenset({"/owner", "/staff", "/addadmin", "/removeadmin", "/groupsync", "/serverstate", "/migratekey", "/approverepair"})
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
            "/receiptmode",
            "/addadmin",
            "/removeadmin",
            "/serverstate",
            "/migratekey",
            "/approverepair",
        }
    )
    UNKNOWN_ACTION_TEXT = "Use the menu to choose an AuriX action."
    PAYMENT_METHODS = {
        "kbzpay": {"label": "KBZPay", "button": "📷 1 · KBZPay", "asset": "kbzpay.png"},
        "wavepay": {"label": "WavePay", "button": "📷 2 · WavePay", "asset": "wavepay.png"},
        "ayapay": {"label": "AYA Pay", "button": "📷 3 · AYA Pay", "asset": "ayapay.png"},
        "uabpay": {"label": "UABPay", "button": "📷 4 · UABPay", "asset": "uabpay.png"},
        "cbpay": {"label": "CB Pay", "button": "📷 5 · CB Pay", "asset": "cbpay.png"},
    }
    PAYMENT_METHOD_ORDER = ("kbzpay", "wavepay", "ayapay", "uabpay", "cbpay")
    PAYMENT_QR_DIR = Path(__file__).resolve().parent / "assets" / "payment_qr"

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
        staff_access: Any | None = None,
        control_group_id: int | None = None,
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
        self.staff_access = staff_access
        self.control_group_id = int(control_group_id) if control_group_id else None
        self.admin_operations = AdminOperations(
            self.commerce, self.admin_ids, self.service, staff_access=self.staff_access
        )
        self.trial_ids = trial_ids or set()
        self.receipt_extractor = receipt_extractor or build_receipt_extractor()
        self.allow_text_payment = bool(allow_text_payment)
        self.maintenance_interval_seconds = max(1.0, float(maintenance_interval_seconds))
        self.command_scope_cleanup_ids = command_scope_cleanup_ids or set()
        self.offset = 0
        self.running = True
        self._maintenance_stop = threading.Event()
        self._maintenance_thread: threading.Thread | None = None
        self._admin_confirmations: dict[str, dict[str, Any]] = {}
        self._receipt_verify_inputs: dict[int, str] = {}
        self._admin_confirmation_lock = threading.Lock()
        self._command_menu_ready = False
        self._command_menu_lock = threading.Lock()
        self._command_menu_retry_enabled = hasattr(self.service, "database")
        self._command_menu_configure_attempted = False
        self._maintenance_lock = threading.Lock()
        self._panel_lock = threading.Lock()
        self._panels: dict[str, dict[str, Any]] = {}
        self._receipt_test_waiting: set[int] = set()
        self._receipt_test_providers: dict[int, str] = {}
        self._admin_add_waiting: set[int] = set()
        self._customer_inputs: dict[int, dict[str, Any]] = {}
        self._receipt_order_context: dict[int, dict[str, Any]] = {}
        self._interaction_state_checked: set[tuple[int, str]] = set()
        self._maintenance_last_status: dict[str, Any] = {
            "status": "never_run",
            "last_started_at": None,
            "last_completed_at": None,
            "last_success_at": None,
            "last_stage": None,
            "last_error": None,
        }

    def _save_interaction_state(
        self, telegram_id: int, state_key: str, payload: dict[str, Any]
    ) -> None:
        """Best-effort persistence for short-lived conversational prompts.

        Telegram retries and service restarts are normal. Persisting only the
        small workflow marker (never an image, access URL, or credential) lets
        a numeric top-up amount or staff reply continue safely after restart.
        In-memory state remains the fast path and compatibility fallback for
        lightweight test doubles that do not expose a database.
        """
        database = getattr(self.commerce, "database", None)
        saver = getattr(database, "save_interaction_state", None)
        if not callable(saver):
            return
        try:
            saver(
                int(telegram_id),
                str(state_key),
                dict(payload),
                (datetime.now(UTC) + INTERACTION_STATE_TTL).isoformat(),
            )
            self._interaction_state_checked.add((int(telegram_id), str(state_key)))
        except Exception as exc:
            # The prompt is still kept in memory; do not turn a recoverability
            # enhancement into a customer-facing failure or leak DB details.
            print(
                f"WARNING: interaction state persistence failed: {type(exc).__name__}",
                file=sys.stderr,
            )

    def _load_interaction_state(self, telegram_id: int, state_key: str) -> dict[str, Any] | None:
        marker = (int(telegram_id), str(state_key))
        if marker in self._interaction_state_checked:
            return None
        database = getattr(self.commerce, "database", None)
        loader = getattr(database, "load_interaction_state", None)
        if not callable(loader):
            return None
        try:
            value = loader(int(telegram_id), str(state_key))
        except Exception:
            return None
        self._interaction_state_checked.add(marker)
        return value if isinstance(value, dict) else None

    def _clear_interaction_state(self, telegram_id: int, state_key: str) -> None:
        database = getattr(self.commerce, "database", None)
        clearer = getattr(database, "clear_interaction_state", None)
        if not callable(clearer):
            return
        try:
            clearer(int(telegram_id), str(state_key))
            self._interaction_state_checked.add((int(telegram_id), str(state_key)))
        except Exception:
            # Best effort: the expiry column is authoritative and will prevent
            # an old prompt from being accepted even if cleanup is unavailable.
            return

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
                raise TelegramAPIError(f"{method} returned an invalid JSON response") from exc
            if response.status >= 400:
                description = "request rejected"
                candidate = result.get("description") if isinstance(result, dict) else None
                if isinstance(candidate, str) and candidate.strip():
                    description = " ".join(candidate.split())[:240]
                raise TelegramAPIError(f"{method} failed status={response.status}: {description}")
        except urllib3.exceptions.HTTPError as exc:
            description = "request rejected"
            raise TelegramAPIError(f"{method} transport failed: {description}") from exc
        finally:
            _latency_log(
                "telegram_request",
                started_at,
                method=method,
                request_kind="long_poll" if method == "getUpdates" else "api",
            )
        if not result.get("ok"):
            description = "request rejected"
            candidate = result.get("description") if isinstance(result, dict) else None
            if isinstance(candidate, str) and candidate.strip():
                description = " ".join(candidate.split())[:240]
            raise TelegramAPIError(f"{method} failed status={response.status}: {description}")
        return result["result"]

    def _multipart_request(self, method: str, fields: dict[str, Any]) -> Any:
        body, content_type = encode_multipart_formdata(fields)
        started_at = time.perf_counter()
        try:
            response = self._http.request(
                "POST",
                f"{self.api}/{method}",
                body=body,
                headers={"Content-Type": content_type},
                timeout=urllib3.Timeout(connect=5.0, read=30.0),
                retries=False,
            )
            try:
                result = json.loads(response.data.decode("utf-8"))
            except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
                raise TelegramAPIError(f"{method} returned an invalid JSON response") from exc
            if response.status >= 400 or not result.get("ok"):
                description = str(result.get("description") or "request rejected")
                raise TelegramAPIError(
                    f"{method} failed status={response.status}: {' '.join(description.split())[:240]}"
                )
            return result["result"]
        except urllib3.exceptions.HTTPError as exc:
            raise TelegramAPIError(f"{method} transport failed: request rejected") from exc
        finally:
            _latency_log("telegram_request", started_at, method=method, request_kind="multipart")

    def send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
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
        try:
            return self.request("editMessageText", payload)
        except TelegramAPIError as exc:
            # Repeated refreshes can produce an identical render. Telegram
            # reports that as a 400 even though the requested UI state is
            # already visible, so converge without emitting a replacement.
            if "message is not modified" in str(exc).lower():
                return None
            raise

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
            elif action == "first":
                state["page"] = 0
            elif action == "last":
                state["page"] = max(0, (len(state.get("all_items", [])) - 1) // 5)
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
                        self._send_order_detail(
                            chat_id,
                            telegram_id,
                            str(target),
                            admin_view=True,
                            message_id=message.get("message_id"),
                        )
                    elif view == "receipts":
                        self.handle(
                            {
                                "chat": {"id": chat_id, "type": "private"},
                                "from": {"id": telegram_id},
                                "text": f"/receipt {target}",
                            }
                        )
                    elif view == "failed":
                        order_id = item.get("order_id")
                        if order_id:
                            self._send_order_detail(
                                chat_id,
                                telegram_id,
                                str(order_id),
                                admin_view=True,
                                message_id=message.get("message_id"),
                            )
                        else:
                            self.send(chat_id, "This worker item has no customer order reference.")
                    elif view == "migrations":
                        self._show_migration_detail(
                            chat_id,
                            telegram_id,
                            item,
                            message_id=message.get("message_id"),
                        )
                    elif view == "repairs":
                        self._show_managed_repair_detail(
                            chat_id,
                            telegram_id,
                            item,
                            message_id=message.get("message_id"),
                        )
                    return True
            state["all_items"] = self._panel_data(telegram_id, state["view"])
            message_id = message.get("message_id") or state.get("message_id")
        text, markup = self._render_panel(token)
        if isinstance(message_id, int):
            self.edit_message(chat_id, message_id, text, markup)
            return True
        self.send(chat_id, text, markup)
        return True

    def _customer_keyboard(self, telegram_id: int) -> dict[str, Any]:
        try:
            promo_locked = bool(self.service.giveaway_status(telegram_id)["access_lock_active"])
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

    def configure_commands(self) -> None:
        # Startup configures scopes asynchronously while maintenance starts
        # immediately. Serialize the whole set-and-verify sequence so a retry
        # cannot rewrite a scope between its setMyCommands/getMyCommands calls.
        with self._command_menu_lock:
            self._configure_commands_locked()

    def _configure_commands_locked(self) -> None:
        self._command_menu_configure_attempted = True
        customer_commands = [
            {"command": "start", "description": "Open the AuriX menu"},
            {"command": "myvpn", "description": "Keys, status and data usage"},
            {"command": "alerts", "description": "Configure personal usage alerts"},
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
            {"command": "migrations", "description": "Monitor endpoint migrations"},
            {"command": "notifications", "description": "Choose operational alerts"},
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
        owner_id = self.staff_access.owner_id() if self.staff_access is not None else None
        if owner_id:
            owner_commands = admin_commands + [
                {"command": "owner", "description": "Open owner controls"},
            ]
            scope = {"type": "chat", "chat_id": int(owner_id)}
            if set_and_verify(scope, owner_commands, f"owner command scope {owner_id}"):
                if scope_store and hasattr(scope_store, "record_command_scope"):
                    scope_store.record_command_scope(int(owner_id))
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

    def send_local_photo(
        self,
        chat_id: int,
        path: Path,
        caption: str = "",
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        data = path.read_bytes()
        fields: dict[str, Any] = {
            "chat_id": str(chat_id),
            "caption": caption[:1024],
            "photo": (path.name, data, "image/png"),
        }
        if reply_markup is not None:
            fields["reply_markup"] = json.dumps(reply_markup, separators=(",", ":"))
        return self._multipart_request("sendPhoto", fields)

    def edit_local_photo(
        self,
        chat_id: int,
        message_id: int,
        path: Path,
        caption: str = "",
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        data = path.read_bytes()
        media = {"type": "photo", "media": "attach://photo", "caption": caption[:1024]}
        fields: dict[str, Any] = {
            "chat_id": str(chat_id),
            "message_id": str(int(message_id)),
            "media": json.dumps(media, separators=(",", ":")),
            "photo": (path.name, data, "image/png"),
        }
        if reply_markup is not None:
            fields["reply_markup"] = json.dumps(reply_markup, separators=(",", ":"))
        return self._multipart_request("editMessageMedia", fields)

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

    def send_receipt_bytes(
        self,
        chat_id: int,
        data: bytes,
        mime_type: str,
        caption: str,
        reply_markup: dict[str, Any],
        *,
        as_document: bool = False,
    ) -> Any:
        extension = "png" if mime_type == "image/png" else "jpg"
        field = "document" if as_document else "photo"
        fields: dict[str, Any] = {
            "chat_id": str(chat_id),
            "caption": caption[:1024],
            field: (f"receipt.{extension}", data, mime_type),
            "reply_markup": json.dumps(reply_markup, separators=(",", ":")),
        }
        return self._multipart_request("sendDocument" if as_document else "sendPhoto", fields)

    @staticmethod
    def _receipt_review_caption(receipt: dict[str, Any]) -> str:
        extracted = receipt.get("extraction") or {}
        evidence_id = str(receipt["id"])
        raw_flags = extracted.get("flags", [])
        flags = (
            ", ".join(str(item) for item in raw_flags if item)
            if isinstance(raw_flags, (list, tuple))
            else "invalid extraction flags"
        )
        amount = extracted.get("amount_minor")
        if amount is None:
            amount = extracted.get("amount")
        return (
            "🧾 Receipt awaiting review\n"
            f"Evidence: {evidence_id}\n"
            f"Order: {receipt['order_id']}\n"
            f"Customer: {receipt['telegram_id']}\n"
            f"Method: {str(receipt.get('provider') or 'manual').upper()}\n"
            f"Expected: {int(receipt['amount_minor']):,} {receipt['currency']}\n"
            f"Extracted transaction: {extracted.get('transaction_id') or '-'}\n"
            f"Reference label: {extracted.get('transaction_id_label') or '-'}\n"
            f"AI amount: {amount if amount is not None else '-'}\n"
            f"AI time: {extracted.get('timestamp') or '-'}\n"
            f"AI recipient: {extracted.get('recipient') or '-'}\n"
            f"AI triage: {str(extracted.get('automation_decision') or 'manual_review').replace('_', ' ')}\n"
            f"Confidence: {extracted.get('confidence', '-')}\n"
            f"⚠️ Risk flags: {flags[:180] if flags else 'none reported'}\n\n"
            "AI extraction is a hint—not payment proof. Check the receiving account, "
            "including its transaction time, then tap Verify Payment."
        )

    def _send_receipt_review(self, chat_id: int, receipt: dict[str, Any]) -> None:
        """Send evidence, preferring durable private storage over Telegram IDs."""
        evidence_id = str(receipt["id"])
        markup = self._inline_keyboard(
            [
                [("✅ Verify Payment", f"a:v:{evidence_id}")],
                [("View Order", f"a:o:{receipt['order_id']}")],
                [("🛑 Reject Receipt", f"a:q:{evidence_id}")],
            ]
        )
        file_id = str(receipt["telegram_file_id"])
        storage = getattr(self.commerce, "receipt_storage", None)
        storage_path = receipt.get("storage_path")
        if storage is not None and storage_path and receipt.get("storage_status") == "stored":
            try:
                image = storage.download(str(storage_path))
                if image:
                    self.send_receipt_bytes(
                        chat_id,
                        image,
                        str(receipt.get("mime_type") or "image/jpeg"),
                        self._receipt_review_caption(receipt),
                        markup,
                        as_document=receipt.get("telegram_media_type") == "document",
                    )
                    return
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
            try:
                fallback(chat_id, file_id, caption, markup)
            except (RuntimeError, urllib.error.HTTPError):
                # A reusable file ID may be rejected even while getFile still
                # permits downloading the underlying object. Re-upload the
                # bytes as a last recovery path for legacy receipts.
                image, mime_type = self._download_telegram_file(file_id)
                self.send_receipt_bytes(
                    chat_id,
                    image,
                    mime_type,
                    caption,
                    markup,
                    as_document=media_type == "document",
                )

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
                raise TelegramAPIError(f"getFile download failed status={response.status}")
            data = response.data
        except urllib3.exceptions.HTTPError as exc:
            raise TelegramAPIError("getFile download transport failed") from exc
        finally:
            _latency_log("telegram_file_download", started_at)
        if len(data) > 20 * 1024 * 1024:
            raise RuntimeError("Receipt image exceeds Telegram download limit")
        mime = "image/jpeg" if file_path.lower().endswith((".jpg", ".jpeg")) else "image/png"
        return data, mime

    def _control_group_staff(
        self,
        control_group_id: int | None = None,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        target_group_id = (
            int(control_group_id) if control_group_id is not None else self.control_group_id
        )
        if target_group_id is None:
            raise RuntimeError("AURIX_CONTROL_GROUP_ID is not configured")
        members = self.request("getChatAdministrators", {"chat_id": target_group_id})
        if not isinstance(members, list):
            raise RuntimeError("Telegram returned an invalid administrator list")
        owner = None
        administrators = []
        for member in members:
            user = member.get("user") if isinstance(member, dict) else None
            if (
                not isinstance(user, dict)
                or user.get("is_bot")
                or not isinstance(user.get("id"), int)
            ):
                continue
            profile = {
                "id": int(user["id"]),
                "username": user.get("username"),
                "display_name": " ".join(
                    value
                    for value in (
                        str(user.get("first_name") or "").strip(),
                        str(user.get("last_name") or "").strip(),
                    )
                    if value
                ),
                "is_bot": False,
            }
            if member.get("status") == "creator":
                owner = profile
            elif member.get("status") == "administrator":
                administrators.append(profile)
        return owner, administrators

    @staticmethod
    def _receipt_file_metadata(message: dict[str, Any]) -> tuple[str, str | None, str, str] | None:
        photos = message.get("photo")
        document = message.get("document")
        if isinstance(photos, list) and photos and isinstance(photos[-1], dict):
            item = photos[-1]
            file_id = item.get("file_id")
            if isinstance(file_id, str):
                return file_id, item.get("file_unique_id"), "image/jpeg", "photo"
        if isinstance(document, dict) and str(document.get("mime_type", "")).startswith("image/"):
            file_id = document.get("file_id")
            if isinstance(file_id, str):
                return (
                    file_id,
                    document.get("file_unique_id"),
                    str(document.get("mime_type"))[:64],
                    "document",
                )
        return None

    def _handle_receipt_diagnostic(
        self, message: dict[str, Any], chat_id: int, telegram_id: int
    ) -> None:
        if not self._is_admin(telegram_id) or self.commerce is None:
            self._send_customer_fallback(chat_id, telegram_id)
            return
        metadata = self._receipt_file_metadata(message)
        if metadata is None:
            self.send(chat_id, "Send a JPEG, PNG or WebP receipt image for the safe test.")
            return
        run_id = self._admin_call(telegram_id, "start_receipt_diagnostic", telegram_id)
        started = time.perf_counter()
        storage_path = None
        try:
            image, mime = self._download_telegram_file(metadata[0])
            digest = hashlib.sha256(image).hexdigest()
            storage = getattr(self.commerce, "receipt_storage", None)
            storage_configured = bool(getattr(storage, "configured", False))
            storage_ms = None
            if storage_configured:
                extension = self.commerce._receipt_storage_extension(mime)
                storage_path = f"diagnostics/{run_id}.{extension}"
                storage_started = time.perf_counter()
                storage.upload(storage_path, image, mime)
                storage_ms = round((time.perf_counter() - storage_started) * 1000, 1)
            expected_provider = self._receipt_test_providers.pop(telegram_id, "")
            extraction, technical = self.receipt_extractor.extract_with_diagnostics(
                image, mime, expected_provider=expected_provider or None
            )
            result = {
                "summary": "LLM extraction and schema validation passed",
                "image": {"mime_type": mime, "byte_size": len(image), "sha256_prefix": digest[:12]},
                "storage": {"configured": storage_configured, "upload_ms": storage_ms},
                "llm": technical,
                "extraction": extraction.as_dict(),
                "selected_payment_method": expected_provider or "not selected",
                "simulated_decision": "ready for assisted human review; automatic approval unavailable",
                "total_duration_ms": round((time.perf_counter() - started) * 1000, 1),
            }
            diagnostic = self._admin_call(
                telegram_id, "finish_receipt_diagnostic", run_id, telegram_id, "passed", result
            )
        except Exception as exc:
            details = dict(getattr(exc, "diagnostics", {}) or {})
            result = {
                "summary": str(exc)[:300] or type(exc).__name__,
                "error_type": type(exc).__name__,
                "llm": details,
                "total_duration_ms": round((time.perf_counter() - started) * 1000, 1),
            }
            try:
                diagnostic = self._admin_call(
                    telegram_id, "finish_receipt_diagnostic", run_id, telegram_id, "failed", result
                )
            except Exception:
                diagnostic = {"id": run_id, "status": "failed", "result": result}
        finally:
            self._receipt_test_providers.pop(telegram_id, None)
            if storage_path:
                try:
                    self.commerce.receipt_storage.delete(storage_path)
                except Exception:
                    result["cleanup_warning"] = "temporary object deletion failed"
        self._send_receipt_diagnostic_result(chat_id, telegram_id, diagnostic)

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
            self._receipt_order_context.pop(int(telegram_id), None)
            self._clear_interaction_state(telegram_id, "receipt_order")
            return candidate[1]
        if self.commerce is None:
            return None
        context = self._receipt_order_context.get(int(telegram_id))
        if context is not None:
            if float(context.get("expires_at", 0)) > time.monotonic():
                return str(context.get("order_id") or "") or None
            self._receipt_order_context.pop(int(telegram_id), None)
            self._clear_interaction_state(telegram_id, "receipt_order")
        persisted = self._load_interaction_state(telegram_id, "receipt_order")
        if isinstance(persisted, dict) and str(persisted.get("order_id") or "").strip():
            order_id = str(persisted["order_id"]).strip()
            self._receipt_order_context[int(telegram_id)] = {
                "order_id": order_id,
                "expires_at": time.monotonic() + INTERACTION_STATE_TTL.total_seconds(),
            }
            return order_id
        list_open = getattr(self.commerce, "open_order_ids_for_user", None)
        if callable(list_open):
            try:
                order_ids = [str(value).strip() for value in list_open(telegram_id, limit=20)]
            except Exception:
                order_ids = []
            if len(order_ids) != 1:
                return None
            return order_ids[0]
        # Compatibility fallback for lightweight test doubles and older
        # deployments that do not yet expose the ambiguity-safe query.
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
            list_open = getattr(self.commerce, "open_order_ids_for_user", None)
            try:
                open_count = len(list_open(telegram_id, limit=20)) if callable(list_open) else 0
            except Exception:
                open_count = 0
            if open_count > 1:
                self.send(
                    chat_id,
                    "I found more than one open order. Open My Orders and tap “Upload Receipt” "
                    "on the exact order, or send the screenshot with /paid <order-id> in its caption.",
                    self._customer_keyboard(telegram_id),
                )
            else:
                self.send(
                    chat_id,
                    "Create an order with Plans, then send its receipt screenshot. "
                    "Use the order’s Upload Receipt button or caption it with /paid <order-id>.",
                    self._customer_keyboard(telegram_id),
                )
            return
        try:
            order = self.commerce.order_detail(order_id, telegram_id)
            provider = str((order or {}).get("payment_method") or "manual")
            image, mime = self._download_telegram_file(file_id)
            duplicate_status = self.commerce.receipt_duplicate_status(
                telegram_id,
                order_id,
                image,
                str(unique_id) if unique_id else None,
                provider=provider,
            )
            if duplicate_status == "different_order":
                raise CommerceError(
                    "This receipt was already submitted for another order; please send the original "
                    "receipt for this order"
                )
            policy = self.commerce.receipt_policy()
            extraction_configured = bool(
                getattr(self.receipt_extractor, "base_url", "")
                and getattr(self.receipt_extractor, "model", "")
                and getattr(self.receipt_extractor, "api_key", "")
            )
            queue_extraction = (
                str(policy.get("mode") or "manual") == "assisted" and extraction_configured
            )
            result = self.commerce.submit_receipt(
                telegram_id,
                order_id,
                provider=provider,
                file_id=file_id,
                file_unique_id=str(unique_id) if unique_id else None,
                image_bytes=image,
                mime_type=mime,
                extraction=None,
                telegram_media_type=media_type,
                queue_extraction=queue_extraction,
            )
        except (CommerceError, RuntimeError, urllib.error.URLError) as exc:
            self.send(chat_id, str(exc) or "Receipt could not be recorded. Try again later.")
            return
        self._receipt_order_context.pop(int(telegram_id), None)
        self._clear_interaction_state(telegram_id, "receipt_order")
        duplicate_image_candidate = "duplicate_image_candidate" in set(
            result.get("flags") or []
        )
        if duplicate_image_candidate:
            self.send(
                chat_id,
                "⚠️ Receipt securely received for manual review. It resembles an earlier upload, "
                "so staff will compare the original transaction directly. No payment or VPN plan "
                "is activated from the image alone.",
            )
        elif queue_extraction:
            self.send(
                chat_id,
                "✅ Receipt securely received.\n\n"
                "AI field extraction is queued in the background; staff will still verify the "
                "recipient, amount and transaction ID against the receiving wallet. The image "
                "alone never activates a VPN plan.",
            )
        else:
            self.send(
                chat_id,
                "Receipt received for manual review. No payment is activated from the image alone.",
            )

    def _is_admin(self, telegram_id: int) -> bool:
        if self.staff_access is not None:
            return bool(self.staff_access.is_admin(telegram_id))
        return telegram_id in self.admin_ids

    def _is_owner(self, telegram_id: int) -> bool:
        if self.staff_access is not None:
            return bool(self.staff_access.is_owner(telegram_id))
        return False

    def _refresh_staff_scopes(self) -> None:
        if self.staff_access is not None:
            self.admin_ids = set(self.staff_access.admin_ids())
            self.admin_operations.admin_ids = self.admin_ids
        threading.Thread(
            target=self.configure_commands,
            name="aurix-staff-command-scopes",
            daemon=True,
        ).start()

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

    def _collect_outline_state(self, *, include_access: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        """Collect collision-safe state from every configured Outline endpoint."""
        outline = self.service.outline
        server_ids = (
            outline.server_ids()
            if callable(getattr(outline, "server_ids", None))
            else (str(getattr(outline, "default_server_id", "primary")),)
        )
        metrics_by_server: dict[str, dict[str, Any]] = {}
        access_by_server: dict[str, dict[str, str]] = {}
        for server_id in server_ids:
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
                    access_by_server[str(server_id)] = access
            except Exception as exc:
                print(
                    f"Outline state unavailable server={server_id}: {type(exc).__name__}",
                    file=sys.stderr,
                )
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
        observed = False
        try:
            metrics, access_state = self._collect_outline_state(include_access=True)
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
        name = str(item.get("plan_name") or item.get("plan_code") or "Paid key")
        lines = [
            f"🔑 {name}",
            "",
            f"Status: {status}",
            f"Key reference: {subscription_id[-6:]}",
            f"Expires: {format_user_datetime(item.get('expires_at'), 'pending')}",
        ]
        if quota:
            percent = min(100.0, used * 100 / quota)
            filled = min(10, max(0, int(percent / 10)))
            lines.extend(
                [
                    f"Usage: {'█' * filled}{'░' * (10 - filled)} {percent:.1f}%",
                    f"Used {self._format_bytes(used)} · Remaining "
                    f"{self._format_bytes(remaining)} / {self._format_bytes(quota)}",
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
            usage_by_key, access_by_key = self._collect_outline_state(include_access=True)
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
            paid_usage = {
                str(item.get("outline_key_id")): item
                for item in self.commerce.user_usage(telegram_id, usage_by_key)
                if item.get("outline_key_id")
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
                usage = paid_usage.get(key_id, {})
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
                        "access_url": current_access or item.get("access_url"),
                        "subscription_id": item.get("subscription_id"),
                        "created_at": item.get("created_at") or item.get("starts_at"),
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
        copy_rows: list[list[dict[str, Any]]] = []
        for index, entry in enumerate(displayed, start=1):
            status = str(entry.get("status") or "unknown")
            icon = "🟢" if status == "active" else "🟡" if "pending" in status else "🔴"
            quota = int(entry.get("quota_bytes") or 0)
            used = int(entry.get("used_bytes") or 0)
            remaining = max(0, int(entry.get("remaining_bytes") or 0))
            lines = [
                f"#{index} · {entry['tier']}",
                f"{icon} {status} · Expires: {format_user_datetime(entry.get('expires_at'), 'pending')}",
            ]
            if quota > 0:
                formatter = (
                    self._format_decimal_bytes if entry.get("decimal_quota") else self._format_bytes
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

    def _send_usage(self, chat_id: int, telegram_id: int) -> None:
        try:
            by_key, _ = self._collect_outline_state()
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
                f"Expires: {format_user_datetime(entry['expires_at'])}\n"
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
