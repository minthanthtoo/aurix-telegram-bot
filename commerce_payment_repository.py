"""Persistence queries for payment and receipt evidence workflows."""

from __future__ import annotations

from typing import Any


class PaymentRepository:
    """Transaction-neutral payment/evidence query boundary."""

    @staticmethod
    def activity_counts(connection: Any, order_id: str) -> tuple[int, int]:
        payments = connection.execute(
            "SELECT COUNT(*) AS n FROM payments WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        evidence = connection.execute(
            "SELECT COUNT(*) AS n FROM payment_evidence WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        return int(payments["n"]), int(evidence["n"])

    @staticmethod
    def has_evidence(connection: Any, order_id: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM payment_evidence WHERE order_id = ? LIMIT 1",
                (order_id,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def payments_for_order(connection: Any, order_id: str) -> list[Any]:
        return connection.execute(
            "SELECT provider, provider_reference, status FROM payments WHERE order_id = ?",
            (order_id,),
        ).fetchall()

    @staticmethod
    def latest_payment(connection: Any, order_id: str) -> Any:
        return connection.execute(
            """SELECT id, provider, provider_reference, status FROM payments
               WHERE order_id = ? ORDER BY submitted_at DESC LIMIT 1""",
            (order_id,),
        ).fetchone()

    @staticmethod
    def latest_eligible_payment(connection: Any, order_id: str) -> Any:
        return connection.execute(
            """SELECT id, provider, status FROM payments
               WHERE order_id = ? AND status IN ('submitted', 'verified')
               ORDER BY submitted_at DESC LIMIT 1""",
            (order_id,),
        ).fetchone()

    @staticmethod
    def verified_payment(connection: Any, order_id: str) -> Any:
        return connection.execute(
            "SELECT id FROM payments WHERE order_id = ? AND status = 'verified' LIMIT 1",
            (order_id,),
        ).fetchone()

    @staticmethod
    def verified_evidence(connection: Any, order_id: str) -> Any:
        return connection.execute(
            "SELECT id FROM payment_evidence WHERE order_id = ? AND review_status = 'verified' LIMIT 1",
            (order_id,),
        ).fetchone()

    @staticmethod
    def latest_evidence_review(connection: Any, order_id: str) -> Any:
        return connection.execute(
            """SELECT review_status FROM payment_evidence
               WHERE order_id = ? ORDER BY submitted_at DESC LIMIT 1""",
            (order_id,),
        ).fetchone()

    @staticmethod
    def latest_evidence_for_approval(connection: Any, order_id: str) -> Any:
        return connection.execute(
            """SELECT review_status, verified_amount_minor, verified_currency,
                      storage_status
               FROM payment_evidence WHERE order_id = ?
               ORDER BY submitted_at DESC LIMIT 1""",
            (order_id,),
        ).fetchone()

    @staticmethod
    def verified_evidence_amount(connection: Any, order_id: str) -> Any:
        return connection.execute(
            """SELECT verified_amount_minor, verified_currency FROM payment_evidence
               WHERE order_id = ? AND review_status = 'verified'
               ORDER BY reviewed_at DESC LIMIT 1""",
            (order_id,),
        ).fetchone()

    @staticmethod
    def find_exact_duplicate(
        connection: Any,
        image_sha256: str,
        file_unique_id: str | None,
        exclude_order_id: str | None = None,
    ) -> list[Any]:
        exclusion = "" if exclude_order_id is None else " AND order_id != ?"
        params: tuple[Any, ...] = (image_sha256, file_unique_id, file_unique_id)
        if exclude_order_id is not None:
            params += (exclude_order_id,)
        return connection.execute(
            """SELECT order_id FROM payment_evidence
               WHERE (image_sha256 = ? OR
                      (? IS NOT NULL AND telegram_file_unique_id = ?))"""
            + exclusion,
            params,
        ).fetchall()

    @staticmethod
    def prior_phash_rows(connection: Any, exclude_order_id: str | None = None) -> list[Any]:
        if exclude_order_id is None:
            return connection.execute(
                """SELECT order_id, provider, image_phash FROM payment_evidence
                   WHERE image_phash IS NOT NULL"""
            ).fetchall()
        return connection.execute(
            """SELECT order_id, provider, image_phash FROM payment_evidence
               WHERE image_phash IS NOT NULL AND order_id != ?""",
            (exclude_order_id,),
        ).fetchall()

    @staticmethod
    def existing_evidence(connection: Any, order_id: str, image_sha256: str) -> Any:
        return connection.execute(
            """SELECT id, extraction_json, extraction_status, review_status,
                      storage_bucket, storage_path, storage_status
               FROM payment_evidence
               WHERE order_id = ? AND image_sha256 = ?""",
            (order_id, image_sha256),
        ).fetchone()

    @staticmethod
    def transaction_candidates(
        connection: Any,
        exclude_order_id: str,
        exclude_evidence_id: str | None = None,
    ) -> list[Any]:
        exclusion = "" if exclude_evidence_id is None else " AND id != ?"
        params: tuple[Any, ...] = (exclude_order_id,)
        if exclude_evidence_id is not None:
            params += (exclude_evidence_id,)
        return connection.execute(
            "SELECT provider, extraction_json FROM payment_evidence WHERE order_id != ?" + exclusion,
            params,
        ).fetchall()

    @staticmethod
    def list_pending_receipts(connection: Any, limit: int) -> list[Any]:
        return connection.execute(
            """SELECT e.id, e.order_id, e.telegram_id, e.provider, e.image_sha256,
                      e.byte_size, e.storage_bucket, e.storage_path, e.storage_status,
                      e.extraction_json, e.extraction_status, e.submitted_at,
                      o.plan_code, o.amount_minor, o.currency
               FROM payment_evidence e JOIN orders o ON o.id = e.order_id
               WHERE e.review_status = 'pending'
                 AND e.storage_status IN ('stored', 'not_configured')
               ORDER BY e.submitted_at LIMIT ?""",
            (limit,),
        ).fetchall()

    @staticmethod
    def get_receipt(connection: Any, evidence_id: str) -> Any:
        return connection.execute(
            """SELECT e.*, o.plan_code, o.amount_minor, o.currency, o.status AS order_status
               FROM payment_evidence e JOIN orders o ON o.id = e.order_id
               WHERE e.id = ?""",
            (evidence_id,),
        ).fetchone()
