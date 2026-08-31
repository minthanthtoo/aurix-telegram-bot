"""Telegram presentation transport and administrator command boundary."""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
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
        "🏠 Customer Menu": "/help",
    }
    ADMIN_BUTTON_COMMANDS = {
        "🛠 Admin Panel": "/admin",
        "📥 Pending Orders": "/orders",
        "🧾 Receipt Review": "/receipts",
        "📈 Capacity": "/capacity",
        "🔎 Consistency": "/reconcile",
        "🔁 Failed Jobs": "/failed",
        "🚨 Enforcement": "/enforcement",
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
        {"/retry", "/refund", "/verify", "/rejectreceipt", "/approve", "/reject"}
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

    def _customer_keyboard(self, telegram_id: int) -> dict[str, Any]:
        try:
            giveaway_winner = bool(self.service.giveaway_status(telegram_id)["winner"])
        except Exception:
            giveaway_winner = False
        rows = [["🔐 My VPN"]]
        paid_active = self._free_claim_blocked_by_paid(telegram_id)
        if not giveaway_winner and not paid_active:
            rows.append(["🎁 Daily 300MB", "🚀 Monthly 3GB"])
        if not giveaway_winner:
            rows.append(["💎 Plans & Upgrade", "🧾 My Orders"])
        else:
            rows.append(["🧾 My Orders"])
        rows.extend([["💰 Wallet", "❓ Help"]])
        return self._reply_keyboard(rows)

    def configure_commands(self) -> None:
        self._command_menu_configure_attempted = True
        customer_commands = [
            {"command": "start", "description": "Open the AuriX menu"},
            {"command": "myvpn", "description": "Keys, status and data usage"},
            {"command": "claim", "description": "Claim free 300 MB for 24 hours"},
            {"command": "trial", "description": "Claim free 3 GB for 30 days"},
            {"command": "plans", "description": "View current plans and prices"},
            {"command": "wallet", "description": "Show wallet balance"},
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
        return (
            "Receipt awaiting review\n"
            f"Evidence: {evidence_id}\n"
            f"Order: {receipt['order_id']}\n"
            f"Customer: {receipt['telegram_id']}\n"
            f"Expected: {int(receipt['amount_minor']):,} {receipt['currency']}\n"
            f"Extracted transaction: {extracted.get('transaction_id') or '-'}\n\n"
            "Check the receiving account, then use:\n"
            f"/verify {evidence_id} <transaction-id> <amount>"
        )

    def _send_receipt_review(self, chat_id: int, receipt: dict[str, Any]) -> None:
        """Send stored evidence, preferring a private Storage signed URL."""
        evidence_id = str(receipt["id"])
        markup = self._inline_keyboard(
            [
                [("View Order", f"a:o:{receipt['order_id']}")],
                [("🛑 Reject Receipt", f"a:q:{evidence_id}")],
            ]
        )
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
        try:
            image, mime = self._download_telegram_file(file_id)
            extraction = None
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
            result = self.commerce.submit_receipt(
                telegram_id,
                order_id,
                provider="manual",
                file_id=file_id,
                file_unique_id=str(unique_id) if unique_id else None,
                image_bytes=image,
                mime_type=mime,
                extraction=extraction.as_dict() if hasattr(extraction, "as_dict") else extraction,
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
        if giveaway["winner"]:
            self.send(
                chat_id,
                f"Giveaway winner #{giveaway['winner_number']}\n\n"
                "Your 100 GiB giveaway is your final AuriX entitlement. Additional free, "
                "paid, renewal, and replacement plans are disabled for this account.",
                self._customer_keyboard(telegram_id or chat_id),
            )
            return
        if self.commerce is None:
            self.send(chat_id, "Paid plans are not configured in this staging process.")
            return
        lines = ["AuriX plans:"]
        lines.append(
            f"100GBFREE giveaway — 100 GiB / 30 days — "
            f"{giveaway['remaining_slots']} of {giveaway['winner_limit']} slot(s) remain"
        )
        lines.append("free_3gb — free every 30 days — 3 GiB / 30 days (use /trial)")
        plans = self.commerce.plans()
        for plan in plans:
            quota = f"{plan.quota_bytes / 1024**3:g} GB" if plan.quota_bytes else "fair-use"
            lines.append(
                f"{plan.code} — {plan.price_minor:,} {plan.currency} — {quota} / {plan.duration_days} days"
            )
        lines.append("\nBuy with: /buy <plan-code>")
        self.send(
            chat_id,
            "\n".join(lines),
            self._inline_keyboard(
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
            ),
        )

    def _send_status(self, chat_id: int, telegram_id: int, include_key: bool = False) -> None:
        giveaway = self.service.giveaway_status(telegram_id)
        giveaway_text = ""
        if giveaway["winner"]:
            giveaway_key_status = (
                "quota exhausted" if giveaway.get("quota_reason") == "quota" else giveaway["status"]
            )
            giveaway_text = (
                f"🎉 Giveaway: winner #{giveaway['winner_number']} of {giveaway['winner_limit']}\n"
                f"Plan: 100 GiB / 30 days\n"
                f"Key status: {giveaway_key_status}\n"
                f"Expires: {giveaway['expires_at']}\n"
                "Eligibility: final entitlement — all further free and paid plans disabled"
            )
        else:
            giveaway_text = (
                f"100GBFREE giveaway: {giveaway['remaining_slots']} of "
                f"{giveaway['winner_limit']} slot(s) remain"
            )
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
        if not giveaway["winner"] and subscription.get("status") in ("active", "expired", "revoked"):
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

    def _send_my_vpn(self, chat_id: int, telegram_id: int) -> None:
        """Render keys, lifecycle state, usage, and next actions in one dashboard."""
        giveaway = self.service.giveaway_status(telegram_id)
        usage_available = True
        try:
            metrics = self.service.outline.transfer_metrics()
            usage_by_key = (
                metrics.get("bytesTransferredByUserId", {}) if isinstance(metrics, dict) else {}
            )
            if not isinstance(usage_by_key, dict):
                raise ValueError("invalid Outline metrics response")
        except Exception as exc:
            usage_available = False
            usage_by_key = {}
            print(f"myvpn usage error: {type(exc).__name__}", file=sys.stderr)

        access_available = True
        access_by_key: dict[str, str] = {}
        try:
            remote = self.service.outline.list_keys()
            remote_keys = remote.get("accessKeys", []) if isinstance(remote, dict) else []
            if not isinstance(remote_keys, list):
                raise ValueError("invalid Outline key response")
            for item in remote_keys:
                if not isinstance(item, dict) or not item.get("id") or not item.get("accessUrl"):
                    continue
                value = str(item["accessUrl"]).replace("\r", "").replace("\n", "").strip()
                if value:
                    access_by_key[str(item["id"])] = value
        except Exception as exc:
            access_available = False
            print(f"myvpn key retrieval error: {type(exc).__name__}", file=sys.stderr)

        entries = self.service.user_usage(telegram_id, usage_by_key, access_by_key)
        subscriptions: list[dict[str, Any]] = []
        open_order: dict[str, Any] | None = None
        if self.commerce is not None:
            paid_usage = {
                str(item.get("outline_key_id")): item
                for item in self.commerce.user_usage(telegram_id, usage_by_key)
                if item.get("outline_key_id")
            }
            subscriptions = self.commerce.user_vpns(telegram_id)
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
                usage = paid_usage.get(key_id, {})
                status = str(usage.get("status") or item.get("status") or "unknown")
                if item.get("status") == "pending" and not item.get("key_status"):
                    status = "activation pending"
                quota = int(usage.get("quota_bytes") or item.get("quota_bytes") or 0)
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
                        "access_url": item.get("access_url"),
                        "created_at": item.get("created_at") or item.get("starts_at"),
                    }
                )
            orders = self.commerce.list_user_orders(telegram_id, limit=5)
            open_order = next(
                (
                    order
                    for order in orders
                    if order.get("stage")
                    not in ("fulfilled", "rejected", "cancelled", "refunded")
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
        entries.sort(
            key=lambda item: (
                priority.get(str(item.get("status")), 6),
                str(item.get("created_at") or ""),
            )
        )
        displayed = entries[:6]
        blocks = ["🔐 My VPN\nKeys • status • usage • next action"]
        copy_rows: list[list[dict[str, Any]]] = []
        for index, entry in enumerate(displayed, start=1):
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
                percent = min(100.0, used * 100 / quota)
                filled = min(10, max(0, int(percent / 10)))
                bar = "█" * filled + "░" * (10 - filled)
                observed_note = "" if entry.get("usage_observed") else " · awaiting traffic data"
                lines.extend(
                    [
                        f"{bar} {percent:.1f}%{observed_note}",
                        f"Used {self._format_bytes(used)} · Remaining "
                        f"{self._format_bytes(remaining)} / {self._format_bytes(quota)}",
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
            blocks.append("No VPN key yet. Choose a free entitlement or view current plans below.")
        elif len(entries) > len(displayed):
            blocks.append(f"{len(entries) - len(displayed)} older entitlement(s) hidden.")
        if open_order is not None:
            blocks.append(
                f"🧾 Open order {str(open_order['id'])[:8]} · "
                f"{open_order.get('plan_name') or open_order.get('plan_code')} · "
                f"{str(open_order.get('stage') or '').replace('_', ' ')}"
            )
        if giveaway["winner"]:
            blocks.append(
                f"🎉 Giveaway winner #{giveaway['winner_number']} · this is your final entitlement."
            )
        elif not displayed:
            blocks.append(
                f"🎁 100GBFREE launch promo: {giveaway['remaining_slots']} / "
                f"{giveaway['winner_limit']} slots remain."
            )
        if not usage_available:
            blocks.append("Usage is temporarily unavailable; keys and lifecycle status are still shown.")
        else:
            blocks.append("Usage is Outline's rolling 30-day transfer total, not live speed.")

        action_rows: list[list[dict[str, Any]]] = []
        action_rows.extend(copy_rows)
        action_rows.append(
            [
                {"text": "🔄 Refresh", "callback_data": "n:myvpn"},
                {"text": "🧾 My Orders", "callback_data": "n:myorders"},
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
        if not giveaway["winner"]:
            active_paid = any(
                item.get("status") == "active" and item.get("key_status") == "active"
                for item in subscriptions
            )
            if active_paid:
                plan_codes = []
                for item in subscriptions:
                    code = item.get("plan_code")
                    if item.get("status") == "active" and code and code not in plan_codes:
                        plan_codes.append(str(code))
                for code in plan_codes[:2]:
                    action_rows.append(
                        [{"text": f"🔄 Renew {code}", "callback_data": f"p:r:{code}"[:64]}]
                    )
            else:
                free_row = [{"text": "🎁 Daily 300MB", "callback_data": "n:claim"}]
                if self._trial_allowed(telegram_id):
                    free_row.append({"text": "🚀 Monthly 3GB", "callback_data": "n:trial"})
                action_rows.append(free_row)
            action_rows.append([{"text": "💎 Plans & Upgrade", "callback_data": "n:plans"}])
        text = "\n\n".join(blocks)
        self.send(chat_id, text[:4096], {"inline_keyboard": action_rows})

    def _send_usage(self, chat_id: int, telegram_id: int) -> None:
        try:
            metrics = self.service.outline.transfer_metrics()
            by_key = (
                metrics.get("bytesTransferredByUserId", {}) if isinstance(metrics, dict) else {}
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
