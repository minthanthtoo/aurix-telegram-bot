"""Receipt evidence submission and storage workflow."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from commerce_models import CommerceError, _new_id, _normalize_reference, _now_text
from receipt_fingerprint import (
    NEAR_DUPLICATE_DISTANCE,
    fingerprint_distance,
    receipt_perceptual_hash,
)


def submit_receipt(
    self,
    telegram_id: int,
    order_id: str,
    provider: str,
    file_id: str,
    file_unique_id: str | None,
    image_bytes: bytes,
    mime_type: str,
    extraction: dict[str, Any] | None = None,
    now: datetime | None = None,
    telegram_media_type: str = "photo",
    queue_extraction: bool = False,
) -> dict[str, Any]:
    """Persist receipt metadata and upload the raw image out-of-band.

    The database transaction creates an upload-pending evidence row, then
    the object is uploaded without holding a database connection open. A
    second short transaction marks the object stored and moves the order to
    payment review. This keeps network latency out of the database lock and
    makes a lost response safely retryable using the same immutable path.
    """
    if not isinstance(file_id, str) or not file_id.strip():
        raise CommerceError("Receipt file id is missing")
    if not image_bytes or len(image_bytes) > 20 * 1024 * 1024:
        raise CommerceError("Receipt image is empty or too large")
    if telegram_media_type not in ("photo", "document"):
        raise CommerceError("Receipt media type is invalid")
    digest = hashlib.sha256(image_bytes).hexdigest()
    phash = receipt_perceptual_hash(image_bytes)
    extraction = extraction if isinstance(extraction, dict) else None
    tx_id = extraction.get("transaction_id") if extraction else None
    # The customer-selected method is authoritative workflow state. Model
    # output may describe a different provider, but must never overwrite it.
    provider_name = _normalize_reference(provider)[:64]
    tx_candidate = str(tx_id).strip()[:128] if tx_id else ""
    status = "parsed" if tx_id else "needs_review"
    submitted_at = _now_text(now)
    storage_configured = self._storage_is_configured()
    if self.receipt_storage_required and not storage_configured:
        raise CommerceError("Receipt storage is not configured")
    storage_status = "pending" if storage_configured else "not_configured"
    storage_bucket = self._storage_bucket() if storage_configured else None
    evidence_id: str
    storage_path: str | None = None
    is_new = False
    near_duplicate = False
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        self._lock_order(connection, order_id)
        order = self.orders.get_owned(connection, order_id, telegram_id)
        if order is None or order["telegram_id"] != telegram_id:
            raise CommerceError("Order not found")
        if str(order["plan_code"]) != "wallet_topup":
            self._assert_no_active_promo(connection, telegram_id)
        if order["status"] == "approved":
            raise CommerceError("Order is already approved")
        if order["status"] not in ("awaiting_payment", "payment_submitted"):
            raise CommerceError("Order is not open for a receipt")
        duplicate_rows = self.payments.find_exact_duplicate(
            connection, digest, file_unique_id, exclude_order_id=order_id
        )
        if duplicate_rows:
            raise CommerceError("This receipt was already submitted for another order")
        if phash:
            prior_hashes = self.payments.prior_phash_rows(connection, order_id)
            for prior in prior_hashes:
                if str(prior["provider"] or "").strip().lower() != provider_name:
                    continue
                distance = fingerprint_distance(phash, str(prior["image_phash"] or ""))
                if distance is not None and distance <= NEAR_DUPLICATE_DISTANCE:
                    # Do not hard-reject a perceptual match: two receipts
                    # from the same provider can share a template. Preserve
                    # the upload, stop automatic extraction, and surface a
                    # strong manual-review flag instead.
                    near_duplicate = True
                    status = "needs_review"
                    flagged = dict(extraction or {})
                    flagged["flags"] = sorted(
                        set(flagged.get("flags") or [])
                        | {"duplicate_image_candidate"}
                    )
                    extraction = flagged
                    break
        existing = self.payments.existing_evidence(connection, order_id, digest)
        if existing is not None:
            parsed = json.loads(existing["extraction_json"] or "{}")
            result = parsed if isinstance(parsed, dict) else {}
            result["evidence_id"] = existing["id"]
            result["extraction_status"] = existing["extraction_status"]
            result["review_status"] = existing["review_status"]
            result["image_sha256"] = digest
            result["storage_status"] = existing["storage_status"] or "not_configured"
            result["storage_path"] = existing["storage_path"]
            storage_ready = (
                result["storage_status"] == "stored"
                if storage_configured
                else result["storage_status"] in ("stored", "not_configured")
            )
            if storage_ready:
                # A prior process may have committed the evidence row but
                # lost the response before moving the order state. Repair
                # that narrow inconsistency on an idempotent retry.
                if order["status"] == "awaiting_payment":
                    connection.execute(
                        "UPDATE orders SET status = 'payment_submitted' WHERE id = ?",
                        (order_id,),
                    )
                    self._audit(
                        connection,
                        "receipt_state_recovered",
                        "order",
                        order_id,
                        "customer",
                        str(telegram_id),
                        {"evidence_id": existing["id"]},
                    )
                return result
            evidence_id = str(existing["id"])
            storage_path = str(existing["storage_path"] or "") or self._receipt_storage_path(
                order_id, evidence_id, mime_type
            )
            connection.execute(
                """UPDATE payment_evidence
                   SET storage_bucket = ?, storage_path = ?, storage_status = 'pending',
                       storage_error = NULL
                   WHERE id = ?""",
                (storage_bucket, storage_path, evidence_id),
            )
        else:
            latest = self.payments.latest_evidence_review(connection, order_id)
            if latest is not None and str(latest["review_status"] or "pending") != "rejected":
                raise CommerceError(
                    "A receipt is already awaiting review; wait for staff feedback"
                )
            payment_rows = self.payments.payments_for_order(connection, order_id)
            if any(str(item["provider"] or "").lower() == "wallet" for item in payment_rows):
                raise CommerceError(
                    "This order already uses wallet payment; receipt payment cannot be combined"
                )
            if any(
                str(item["status"] or "") in ("submitted", "verified", "refunded")
                for item in payment_rows
            ):
                raise CommerceError("A payment is already attached to this order")
            # Keep model output as evidence only. Detect a repeated
            # candidate across screenshots without creating an
            # authoritative payment row.
            if tx_candidate:
                prior_evidence = self.payments.transaction_candidates(connection, order_id)
                for prior in prior_evidence:
                    try:
                        prior_extraction = json.loads(prior["extraction_json"] or "{}")
                    except json.JSONDecodeError:
                        prior_extraction = {}
                    prior_tx = (
                        prior_extraction.get("transaction_id")
                        if isinstance(prior_extraction, dict)
                        else None
                    )
                    if _normalize_reference(str(prior["provider"])) == _normalize_reference(
                        provider_name or "manual"
                    ) and _normalize_reference(str(prior_tx or "")) == _normalize_reference(
                        tx_candidate
                    ):
                        status = "needs_review"
                        flagged = dict(extraction or {})
                        flagged["flags"] = sorted(
                            set(flagged.get("flags") or [])
                            | {"duplicate_transaction_candidate"}
                        )
                        extraction = flagged
                        break
            evidence_id = _new_id()
            storage_path = (
                self._receipt_storage_path(order_id, evidence_id, mime_type)
                if storage_configured
                else None
            )
            connection.execute(
                """INSERT INTO payment_evidence
                   (id, order_id, telegram_id, provider, telegram_file_id,
                    telegram_file_unique_id, telegram_media_type, image_sha256,
                    image_phash, mime_type, byte_size, storage_bucket, storage_path,
                    storage_status, extraction_json, extraction_status,
                    submitted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    evidence_id,
                    order_id,
                    telegram_id,
                    provider_name,
                    file_id[:256],
                    file_unique_id[:256] if file_unique_id else None,
                    telegram_media_type,
                    digest,
                    phash,
                    mime_type[:64],
                    len(image_bytes),
                    storage_bucket,
                    storage_path,
                    storage_status,
                    json.dumps(extraction or {}, sort_keys=True),
                    status,
                    submitted_at,
                ),
            )
            is_new = True

    if storage_configured:
        assert storage_path is not None
        try:
            uploaded_path = self.receipt_storage.upload(storage_path, image_bytes, mime_type)
            uploaded_path = str(uploaded_path or "").strip()
            if not uploaded_path:
                raise RuntimeError("Receipt storage returned an empty object path")
        except Exception as exc:
            # Preserve the row so a retry can reuse the same object path.
            try:
                with self.database.connect() as connection:
                    connection.execute(
                        """UPDATE payment_evidence
                           SET storage_status = 'failed', storage_error = ?
                           WHERE id = ?""",
                        (type(exc).__name__[:128], evidence_id),
                    )
            except Exception:
                pass
            raise CommerceError("Receipt image could not be saved. Please try again.") from exc
        try:
            storage_path = uploaded_path
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                connection.execute(
                    """UPDATE payment_evidence
                       SET storage_bucket = ?, storage_path = ?, storage_status = 'stored',
                           storage_error = NULL, stored_at = ?
                       WHERE id = ?""",
                    (storage_bucket, str(uploaded_path), submitted_at, evidence_id),
                )
                connection.execute(
                    "UPDATE orders SET status = 'payment_submitted' WHERE id = ?",
                    (order_id,),
                )
                self._audit(
                    connection,
                    "receipt_submitted" if is_new else "receipt_storage_recovered",
                    "order",
                    order_id,
                    "customer",
                    str(telegram_id),
                    {"evidence_id": evidence_id, "extraction_status": status},
                )
                self._queue_staff_notification(
                    connection,
                    "receipt_submitted",
                    evidence_id,
                    "🧾 RECEIPT AWAITING REVIEW\n\n"
                    f"Order: #{order_id[:8]}\n"
                    f"Evidence: {evidence_id[:10]}\n"
                    f"Customer: tg:{telegram_id}\n"
                    f"Method: {provider_name.upper()}\n"
                    f"AI extraction: {status.replace('_', ' ')}\n\n"
                    "Action: open the receipt and confirm it against the receiving account.",
                    submitted_at,
                )
                if queue_extraction and extraction is None and not near_duplicate:
                    self._queue_receipt_extraction(connection, evidence_id, submitted_at)
        except Exception:
            # Do not leave a billable orphan if the final metadata commit
            # fails. Deletion is best-effort and the row remains retryable.
            try:
                self.receipt_storage.delete(storage_path)
            except Exception:
                pass
            raise
    else:
        # A receipt is a payment submission even when OCR/LLM extraction
        # failed. Approval still requires a human verification decision.
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                "UPDATE orders SET status = 'payment_submitted' WHERE id = ?",
                (order_id,),
            )
            if is_new:
                self._audit(
                    connection,
                    "receipt_submitted",
                    "order",
                    order_id,
                    "customer",
                    str(telegram_id),
                    {"evidence_id": evidence_id, "extraction_status": status},
                )
            self._queue_staff_notification(
                connection,
                "receipt_submitted",
                evidence_id,
                "🧾 RECEIPT AWAITING REVIEW\n\n"
                f"Order: #{order_id[:8]}\n"
                f"Evidence: {evidence_id[:10]}\n"
                f"Customer: tg:{telegram_id}\n"
                f"Method: {provider_name.upper()}\n"
                f"AI extraction: {status.replace('_', ' ')}\n\n"
                "Action: open the receipt and confirm it against the receiving account.",
                submitted_at,
            )
            if queue_extraction and extraction is None:
                self._queue_receipt_extraction(connection, evidence_id, submitted_at)
    result = dict(extraction or {})
    result["evidence_id"] = evidence_id
    result["image_sha256"] = digest
    result["extraction_status"] = status
    result["storage_status"] = "stored" if storage_configured else "not_configured"
    result["storage_path"] = storage_path
    return result

