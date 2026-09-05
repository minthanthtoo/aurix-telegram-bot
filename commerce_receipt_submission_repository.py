"""Transaction-aware persistence for receipt submission and storage."""

from __future__ import annotations

from typing import Any


class ReceiptSubmissionRepository:
    """SQL operations for the receipt upload state machine."""

    @staticmethod
    def mark_order_submitted(connection: Any, order_id: str) -> None:
        connection.execute(
            "UPDATE orders SET status = 'payment_submitted' WHERE id = ?", (order_id,)
        )

    @staticmethod
    def mark_upload_pending(
        connection: Any,
        *,
        evidence_id: str,
        storage_bucket: str | None,
        storage_path: str,
    ) -> None:
        connection.execute(
            """UPDATE payment_evidence
               SET storage_bucket = ?, storage_path = ?, storage_status = 'pending',
                   storage_error = NULL WHERE id = ?""",
            (storage_bucket, storage_path, evidence_id),
        )

    @staticmethod
    def insert_evidence(connection: Any, **values: Any) -> None:
        connection.execute(
            """INSERT INTO payment_evidence
               (id, order_id, telegram_id, provider, telegram_file_id,
                telegram_file_unique_id, telegram_media_type, image_sha256,
                image_phash, mime_type, byte_size, storage_bucket, storage_path,
                storage_status, extraction_json, extraction_status, submitted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                values["evidence_id"],
                values["order_id"],
                values["telegram_id"],
                values["provider"],
                values["file_id"],
                values["file_unique_id"],
                values["media_type"],
                values["image_sha256"],
                values["image_phash"],
                values["mime_type"],
                values["byte_size"],
                values["storage_bucket"],
                values["storage_path"],
                values["storage_status"],
                values["extraction_json"],
                values["extraction_status"],
                values["submitted_at"],
            ),
        )

    @staticmethod
    def mark_upload_failed(connection: Any, evidence_id: str, error_type: str) -> None:
        connection.execute(
            """UPDATE payment_evidence
               SET storage_status = 'failed', storage_error = ? WHERE id = ?""",
            (error_type, evidence_id),
        )

    @staticmethod
    def mark_stored(
        connection: Any,
        *,
        evidence_id: str,
        storage_bucket: str | None,
        storage_path: str,
        stored_at: str,
    ) -> None:
        connection.execute(
            """UPDATE payment_evidence
               SET storage_bucket = ?, storage_path = ?, storage_status = 'stored',
                   storage_error = NULL, stored_at = ? WHERE id = ?""",
            (storage_bucket, storage_path, stored_at, evidence_id),
        )
