"""Storage finalization and response assembly for receipt submissions."""

from __future__ import annotations

from typing import Any

from commerce_receipt_storage_workflow import (
    complete_stored_receipt,
    complete_unstored_receipt,
)


def finalize_receipt_submission(
    service: Any,
    repository: Any,
    *,
    prepared: Any,
    order_id: str,
    telegram_id: int,
    image_bytes: bytes,
    mime_type: str,
    queue_extraction: bool,
    storage_configured: bool,
) -> dict[str, Any]:
    """Upload or finalize receipt state after intake has committed its identity."""
    storage_path = prepared.storage_path
    if storage_configured:
        assert storage_path is not None
        storage_path = complete_stored_receipt(
            service,
            repository,
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
            service,
            repository,
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
