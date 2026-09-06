"""Receipt submission orchestration across transactional and storage phases."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from commerce_models import CommerceError, _normalize_reference, _now_text
from commerce_receipt_submission_repository import ReceiptSubmissionRepository
from commerce_receipt_storage_workflow import (
    complete_stored_receipt,
    complete_unstored_receipt,
)
from commerce_receipt_submission_intake import prepare_receipt_submission
from receipt_fingerprint import receipt_perceptual_hash


_RECEIPTS = ReceiptSubmissionRepository()


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
    """Persist receipt metadata, upload the image, and finalize review state."""
    if not isinstance(file_id, str) or not file_id.strip():
        raise CommerceError("Receipt file id is missing")
    if not image_bytes or len(image_bytes) > 20 * 1024 * 1024:
        raise CommerceError("Receipt image is empty or too large")
    if telegram_media_type not in ("photo", "document"):
        raise CommerceError("Receipt media type is invalid")

    digest = hashlib.sha256(image_bytes).hexdigest()
    phash = receipt_perceptual_hash(image_bytes)
    extraction = extraction if isinstance(extraction, dict) else None
    provider_name = _normalize_reference(provider)[:64]
    submitted_at = _now_text(now)
    storage_configured = self._storage_is_configured()
    if self.receipt_storage_required and not storage_configured:
        raise CommerceError("Receipt storage is not configured")
    storage_status = "pending" if storage_configured else "not_configured"
    storage_bucket = self._storage_bucket() if storage_configured else None

    prepared = prepare_receipt_submission(
        self,
        _RECEIPTS,
        telegram_id=telegram_id,
        order_id=order_id,
        provider_name=provider_name,
        file_id=file_id,
        file_unique_id=file_unique_id,
        mime_type=mime_type,
        image_sha256=digest,
        image_phash=phash,
        extraction=extraction,
        submitted_at=submitted_at,
        storage_bucket=storage_bucket,
        storage_status=storage_status,
        storage_configured=storage_configured,
        telegram_media_type=telegram_media_type,
        image_bytes=image_bytes,
    )
    if prepared.existing_result is not None:
        return prepared.existing_result

    storage_path = prepared.storage_path
    if storage_configured:
        assert storage_path is not None
        storage_path = complete_stored_receipt(
            self,
            _RECEIPTS,
            evidence_id=prepared.evidence_id,
            order_id=order_id,
            telegram_id=telegram_id,
            provider_name=prepared.provider_name,
            status=prepared.status,
            submitted_at=prepared.submitted_at,
            storage_bucket=prepared.storage_bucket,
            storage_path=storage_path,
            image_bytes=image_bytes,
            mime_type=mime_type,
            is_new=prepared.is_new,
            extraction=prepared.extraction,
            queue_extraction=queue_extraction,
            near_duplicate=prepared.near_duplicate,
        )
    else:
        complete_unstored_receipt(
            self,
            _RECEIPTS,
            evidence_id=prepared.evidence_id,
            order_id=order_id,
            telegram_id=telegram_id,
            provider_name=prepared.provider_name,
            status=prepared.status,
            submitted_at=prepared.submitted_at,
            is_new=prepared.is_new,
            extraction=prepared.extraction,
            queue_extraction=queue_extraction,
        )

    return {
        **(prepared.extraction or {}),
        "evidence_id": prepared.evidence_id,
        "image_sha256": prepared.image_sha256,
        "extraction_status": prepared.status,
        "storage_status": "stored" if storage_configured else "not_configured",
        "storage_path": storage_path,
    }
