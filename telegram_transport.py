"""Telegram presentation transport and administrator command boundary."""

from __future__ import annotations

import json
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
from receipt_llm import build_receipt_extractor
from telegram_admin import AdminOperations
from telegram_components import TelegramComponents
from telegram_formatting import format_user_datetime
from telegram_transport_support import (
    ADMIN_CONFIRMATION_TTL,
    DEFAULT_MAINTENANCE_INTERVAL_SECONDS,
    INTERACTION_STATE_TTL,
    TelegramAPIError,
    UTC,
)


class TelegramBot:
    CUSTOMER_BUTTON_COMMANDS = {
        "🎁 Daily 300MB": "/claim",
        "🚀 Monthly 3GB": "/trial",
        "💎 Upgrade 50GB": "/buy basic_50gb",
        "💠 Upgrade 100GB": "/buy standard_100gb",
        "💎 Plans & Upgrade": "/plans",
        "🔐 My VPN": "/myvpn",
        "📱 Open AuriX App": "/pair",
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
        "🛰 Fleet Probes": "/probes",
        "🔎 Consistency": "/reconcile",
        "🔁 Failed Jobs": "/failed",
        "🧩 Key Repairs": "/repairs",
        "🛡 Route Failover": "/failover",
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
            "/probes",
            "/reconcile",
            "/enforcement",
            "/promo",
            "/setpromo",
            "/stoppromo",
            "/resumepromo",
            "/failed",
            "/repairs",
            "/migrations",
            "/failover",
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
        probe_service: Any | None = None,
        device_api_url: str | None = None,
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
        self.probe_service = probe_service
        self.device_api_url = str(device_api_url or "").rstrip("/")
        self.admin_ids = admin_ids or set()
        self.staff_access = staff_access
        self.control_group_id = int(control_group_id) if control_group_id else None
        self.admin_operations = AdminOperations(
            self.commerce,
            self.admin_ids,
            self.service,
            staff_access=self.staff_access,
            probe_service=self.probe_service,
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
        self._components = TelegramComponents(self)

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
    def __getattr__(self, name: str) -> Any:
        components = self.__dict__.get("_components")
        if components is None:
            raise AttributeError(name)
        try:
            return components.resolve(name)
        except AttributeError:
            raise AttributeError(name) from None
