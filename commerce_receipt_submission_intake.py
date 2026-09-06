"""Transactional receipt-evidence intake for the receipt workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from commerce_models import CommerceError, _new_id, _normalize_reference
from commerce_receipt_submission_repository import ReceiptSubmissionRepository
from receipt_fingerprint import NEAR_DUPLICATE_DISTANCE, fingerprint_distance


@dataclass(frozen=True, slots=True)
class PreparedReceipt:
    """State needed by the out-of-transaction storage/finalization phase."""

    evidence_id: str
    image_sha256: str
    provider_name: str
    submitted_at: str
    status: str
    extraction: dict[str, Any] | None
    storage_bucket: str | None
    storage_path: str | None
    storage_configured: bool
    is_new: bool
    near_duplicate: bool
    existing_result: dict[str, Any] | None = None


def _flag_near_duplicate(
    extraction: dict[str, Any] | None,
    provider_name: str,
    phash: str | None,
    prior_hashes: Any,
) -> tuple[dict[str, Any] | None, bool]:
    if not phash:
        return extraction, False
    for prior in prior_hashes:
        if str(prior["provider"] or "").strip().lower() != provider_name:
            continue
        distance = fingerprint_distance(phash, str(prior["image_phash"] or ""))
        if distance is not None and distance <= NEAR_DUPLICATE_DISTANCE:
            flagged = dict(extraction or {})
            flagged["flags"] = sorted(
                set(flagged.get("flags") or []) | {"duplicate_image_candidate"}
            )
            return flagged, True
    return extraction, False


def _flag_duplicate_transaction(
    extraction: dict[str, Any] | None,
    provider_name: str,
    tx_candidate: str,
    prior_evidence: Any,
) -> dict[str, Any] | None:
    if not tx_candidate:
        return extraction
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
            flagged = dict(extraction or {})
            flagged["flags"] = sorted(
                set(flagged.get("flags") or []) | {"duplicate_transaction_candidate"}
            )
            return flagged
    return extraction


def prepare_receipt_submission(
    self: Any,
    repository: ReceiptSubmissionRepository,
    *,
    telegram_id: int,
    order_id: str,
    provider_name: str,
    file_id: str,
    file_unique_id: str | None,
    mime_type: str,
    image_sha256: str,
    image_phash: str | None,
    extraction: dict[str, Any] | None,
    submitted_at: str,
    storage_bucket: str | None,
    storage_status: str,
    storage_configured: bool,
    telegram_media_type: str,
    image_bytes: bytes,
) -> PreparedReceipt:
    """Lock the order and prepare one idempotent evidence record."""
    tx_id = extraction.get("transaction_id") if extraction else None
    tx_candidate = str(tx_id).strip()[:128] if tx_id else ""
    status = "parsed" if tx_id else "needs_review"
    near_duplicate = False
    evidence_id: str
    storage_path: str | None = None
    is_new = False

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
            connection, image_sha256, file_unique_id, exclude_order_id=order_id
        )
        if duplicate_rows:
            raise CommerceError("This receipt was already submitted for another order")
        extraction, near_duplicate = _flag_near_duplicate(
            extraction,
            provider_name,
            image_phash,
            self.payments.prior_phash_rows(connection, order_id),
        )
        if near_duplicate:
            status = "needs_review"

        existing = self.payments.existing_evidence(connection, order_id, image_sha256)
        if existing is not None:
            parsed = json.loads(existing["extraction_json"] or "{}")
            result = parsed if isinstance(parsed, dict) else {}
            result.update(
                {
                    "evidence_id": existing["id"],
                    "extraction_status": existing["extraction_status"],
                    "review_status": existing["review_status"],
                    "image_sha256": image_sha256,
                    "storage_status": existing["storage_status"] or "not_configured",
                    "storage_path": existing["storage_path"],
                }
            )
            storage_ready = (
                result["storage_status"] == "stored"
                if storage_configured
                else result["storage_status"] in ("stored", "not_configured")
            )
            if storage_ready:
                if order["status"] == "awaiting_payment":
                    repository.mark_order_submitted(connection, order_id)
                    self._audit(
                        connection,
                        "receipt_state_recovered",
                        "order",
                        order_id,
                        "customer",
                        str(telegram_id),
                        {"evidence_id": existing["id"]},
                    )
                return PreparedReceipt(
                    evidence_id=str(existing["id"]),
                    image_sha256=image_sha256,
                    provider_name=provider_name,
                    submitted_at=submitted_at,
                    status=str(existing["extraction_status"]),
                    extraction=parsed if isinstance(parsed, dict) else None,
                    storage_bucket=storage_bucket,
                    storage_path=str(existing["storage_path"] or "") or None,
                    storage_configured=storage_configured,
                    is_new=False,
                    near_duplicate=False,
                    existing_result=result,
                )
            evidence_id = str(existing["id"])
            storage_path = str(existing["storage_path"] or "") or self._receipt_storage_path(
                order_id, evidence_id, mime_type
            )
            repository.mark_upload_pending(
                connection,
                evidence_id=evidence_id,
                storage_bucket=storage_bucket,
                storage_path=storage_path,
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
            extraction = _flag_duplicate_transaction(
                extraction,
                provider_name,
                tx_candidate,
                self.payments.transaction_candidates(connection, order_id),
            )
            if extraction and "duplicate_transaction_candidate" in set(
                extraction.get("flags") or []
            ):
                status = "needs_review"
            evidence_id = _new_id()
            storage_path = (
                self._receipt_storage_path(order_id, evidence_id, mime_type)
                if storage_configured
                else None
            )
            repository.insert_evidence(
                connection,
                evidence_id=evidence_id,
                order_id=order_id,
                telegram_id=telegram_id,
                provider=provider_name,
                file_id=file_id[:256],
                file_unique_id=file_unique_id[:256] if file_unique_id else None,
                media_type=telegram_media_type,
                image_sha256=image_sha256,
                image_phash=image_phash,
                mime_type=mime_type[:64],
                byte_size=len(image_bytes),
                storage_bucket=storage_bucket,
                storage_path=storage_path,
                storage_status=storage_status,
                extraction_json=json.dumps(extraction or {}, sort_keys=True),
                extraction_status=status,
                submitted_at=submitted_at,
            )
            is_new = True

    return PreparedReceipt(
        evidence_id=evidence_id,
        image_sha256=image_sha256,
        provider_name=provider_name,
        submitted_at=submitted_at,
        status=status,
        extraction=extraction,
        storage_bucket=storage_bucket,
        storage_path=storage_path,
        storage_configured=storage_configured,
        is_new=is_new,
        near_duplicate=near_duplicate,
    )
