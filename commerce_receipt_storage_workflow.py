"""Out-of-band receipt storage completion workflow."""

from __future__ import annotations

from typing import Any

from commerce_models import CommerceError


def _receipt_notification_message(
    order_id: str,
    evidence_id: str,
    telegram_id: int,
    provider_name: str,
    status: str,
) -> str:
    return (
        "🧾 RECEIPT AWAITING REVIEW\n\n"
        f"Order: #{order_id[:8]}\n"
        f"Evidence: {evidence_id[:10]}\n"
        f"Customer: tg:{telegram_id}\n"
        f"Method: {provider_name.upper()}\n"
        f"AI extraction: {status.replace('_', ' ')}\n\n"
        "Action: open the receipt and confirm it against the receiving account."
    )


def complete_stored_receipt(
    self: Any,
    repository: Any,
    *,
    evidence_id: str,
    order_id: str,
    telegram_id: int,
    provider_name: str,
    status: str,
    submitted_at: str,
    storage_bucket: str | None,
    storage_path: str,
    image_bytes: bytes,
    mime_type: str,
    is_new: bool,
    extraction: dict[str, Any] | None,
    queue_extraction: bool,
    near_duplicate: bool,
) -> str:
    try:
        uploaded_path = self.receipt_storage.upload(storage_path, image_bytes, mime_type)
        uploaded_path = str(uploaded_path or "").strip()
        if not uploaded_path:
            raise RuntimeError("Receipt storage returned an empty object path")
    except Exception as exc:
        try:
            with self.database.connect() as connection:
                repository.mark_upload_failed(
                    connection, evidence_id, type(exc).__name__[:128]
                )
        except Exception:
            pass
        raise CommerceError("Receipt image could not be saved. Please try again.") from exc
    try:
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            repository.mark_stored(
                connection,
                evidence_id=evidence_id,
                storage_bucket=storage_bucket,
                storage_path=uploaded_path,
                stored_at=submitted_at,
            )
            repository.mark_order_submitted(connection, order_id)
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
                _receipt_notification_message(
                    order_id, evidence_id, telegram_id, provider_name, status
                ),
                submitted_at,
            )
            if queue_extraction and extraction is None and not near_duplicate:
                self._queue_receipt_extraction(connection, evidence_id, submitted_at)
    except Exception:
        try:
            self.receipt_storage.delete(uploaded_path)
        except Exception:
            pass
        raise
    return uploaded_path


def complete_unstored_receipt(
    self: Any,
    repository: Any,
    *,
    evidence_id: str,
    order_id: str,
    telegram_id: int,
    provider_name: str,
    status: str,
    submitted_at: str,
    is_new: bool,
    extraction: dict[str, Any] | None,
    queue_extraction: bool,
) -> None:
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        repository.mark_order_submitted(connection, order_id)
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
            _receipt_notification_message(
                order_id, evidence_id, telegram_id, provider_name, status
            ),
            submitted_at,
        )
        if queue_extraction and extraction is None:
            self._queue_receipt_extraction(connection, evidence_id, submitted_at)
