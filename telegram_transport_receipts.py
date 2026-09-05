"""Telegram receipt media, diagnostics, and upload handling."""

from __future__ import annotations

import hashlib
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
from observability import latency_log as _latency_log
from ports import ReceiptExtractorGateway
from quota_alerts import MODE_STEPS, alert_level_labels
from telegram_admin import AdminOperations
from telegram_formatting import format_user_datetime
from telegram_transport_support import ADMIN_CONFIRMATION_TTL, INTERACTION_STATE_TTL, TelegramAPIError, UTC


class TelegramReceiptTransportMixin:

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
