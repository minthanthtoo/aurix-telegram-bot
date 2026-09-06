"""Receipt submission orchestration across transactional and storage phases."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from commerce_models import CommerceError, _normalize_reference, _now_text
from commerce_receipt_submission_repository import ReceiptSubmissionRepository
from commerce_receipt_submission_intake import prepare_receipt_submission
from commerce_service_receipt_submission_flow import finalize_receipt_submission
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

    return finalize_receipt_submission(
        self,
        _RECEIPTS,
        prepared=prepared,
        order_id=order_id,
        telegram_id=telegram_id,
        image_bytes=image_bytes,
        mime_type=mime_type,
        queue_extraction=queue_extraction,
        storage_configured=storage_configured,
    )
