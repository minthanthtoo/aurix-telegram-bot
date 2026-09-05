"""Persistence queries for payment and receipt evidence workflows."""

from __future__ import annotations

from typing import Any

from commerce_repositories import _PostgresConnection


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

    @staticmethod
    def verification_context(connection: Any, evidence_id: str) -> Any:
        return connection.execute(
            """SELECT e.*, o.amount_minor, o.currency, o.plan_code, o.status AS order_status
                 FROM payment_evidence e JOIN orders o ON o.id = e.order_id
                WHERE e.id = ?""",
            (evidence_id,),
        ).fetchone()

    @staticmethod
    def payment_reference_conflicts(
        connection: Any, normalized_reference: str, order_id: str
    ) -> list[Any]:
        return connection.execute(
            """SELECT order_id, provider, normalized_reference FROM payments
               WHERE status IN ('submitted', 'verified')
                 AND normalized_reference = ? AND order_id != ?""",
            (normalized_reference, order_id),
        ).fetchall()

    @staticmethod
    def insert_verified_payment(connection: Any, **values: Any) -> None:
        connection.execute(
            """INSERT INTO payments
               (id, order_id, provider, provider_reference, normalized_reference,
                status, submitted_at, verified_at)
               VALUES (?, ?, ?, ?, ?, 'verified', ?, ?)""",
            (
                values["payment_id"], values["order_id"], values["provider"],
                values["provider_reference"], values["normalized_reference"],
                values["reviewed_at"], values["reviewed_at"],
            ),
        )

    @staticmethod
    def update_verified_payment(connection: Any, **values: Any) -> None:
        connection.execute(
            """UPDATE payments SET provider = ?, provider_reference = ?,
                   normalized_reference = ?, status = 'verified', verified_at = ?
                WHERE id = ?""",
            (
                values["provider"], values["provider_reference"],
                values["normalized_reference"], values["reviewed_at"], values["payment_id"],
            ),
        )

    @staticmethod
    def mark_evidence_verified(connection: Any, **values: Any) -> None:
        connection.execute(
            """UPDATE payment_evidence
               SET reviewer_id = ?, review_notes = 'verified against receiving account',
                   review_status = 'verified', verified_provider_reference = ?,
                   verified_amount_minor = ?, verified_currency = ?, reviewed_at = ?
               WHERE id = ?""",
            (
                values["admin_id"], values["provider_reference"], values["amount"],
                values["currency"], values["reviewed_at"], values["evidence_id"],
            ),
        )

    @staticmethod
    def claim_extraction_job(connection: Any, current_text: str, stale_before: str) -> dict[str, Any] | None:
        connection.execute(
            """UPDATE receipt_extraction_jobs
               SET status = 'pending', locked_at = NULL
               WHERE status = 'running' AND locked_at < ?""",
            (stale_before,),
        )
        lock_clause = " FOR UPDATE SKIP LOCKED" if isinstance(connection, _PostgresConnection) else ""
        row = connection.execute(
            """SELECT j.id AS job_id, j.attempts, e.id AS evidence_id,
                      e.provider, e.telegram_file_id, e.mime_type,
                      e.storage_path, e.storage_status
               FROM receipt_extraction_jobs j
               JOIN payment_evidence e ON e.id = j.evidence_id
               WHERE j.status = 'pending' AND j.next_attempt_at <= ?
                 AND e.review_status = 'pending'
               ORDER BY j.created_at LIMIT 1""" + lock_clause,
            (current_text,),
        ).fetchone()
        if row is None:
            return None
        updated = connection.execute(
            """UPDATE receipt_extraction_jobs
               SET status = 'running', attempts = attempts + 1, locked_at = ?
               WHERE id = ? AND status = 'pending'""",
            (current_text, row["job_id"]),
        )
        if getattr(updated, "rowcount", 1) == 0:
            return None
        result = dict(row)
        result["attempts"] = int(row["attempts"]) + 1
        return result

    @staticmethod
    def extraction_context(connection: Any, evidence_id: str) -> Any:
        return connection.execute(
            """SELECT e.provider, e.order_id, e.submitted_at,
                      o.amount_minor, o.currency, o.payment_method
               FROM payment_evidence e JOIN orders o ON o.id = e.order_id
               WHERE e.id = ? AND e.review_status = 'pending'""",
            (evidence_id,),
        ).fetchone()

    @staticmethod
    def complete_extraction(
        connection: Any, *, evidence_id: str, job_id: str, extraction_json: str,
        status: str, completed_at: str
    ) -> None:
        connection.execute(
            "UPDATE payment_evidence SET extraction_json = ?, extraction_status = ? WHERE id = ?",
            (extraction_json, status, evidence_id),
        )
        connection.execute(
            """UPDATE receipt_extraction_jobs
               SET status = 'done', locked_at = NULL, last_error = NULL, completed_at = ?
               WHERE id = ?""",
            (completed_at, job_id),
        )

    @staticmethod
    def complete_missing_extraction(connection: Any, job_id: str, completed_at: str) -> None:
        connection.execute(
            """UPDATE receipt_extraction_jobs
               SET status = 'done', locked_at = NULL, completed_at = ? WHERE id = ?""",
            (completed_at, job_id),
        )

    @staticmethod
    def fail_extraction(connection: Any, job_id: str, next_attempt: str, error: str) -> None:
        connection.execute(
            """UPDATE receipt_extraction_jobs
               SET status = CASE WHEN attempts >= 3 THEN 'failed' ELSE 'pending' END,
                   next_attempt_at = ?, locked_at = NULL, last_error = ?
               WHERE id = ?""",
            (next_attempt, error, job_id),
        )

    @staticmethod
    def rejection_context(connection: Any, evidence_id: str) -> Any:
        return connection.execute(
            """SELECT e.id, e.order_id, e.review_status, e.telegram_id, e.provider,
                      o.plan_name, o.plan_code
               FROM payment_evidence e JOIN orders o ON o.id = e.order_id
               WHERE e.id = ?""",
            (evidence_id,),
        ).fetchone()

    @staticmethod
    def reject_evidence(connection: Any, **values: Any) -> None:
        connection.execute(
            """UPDATE payment_evidence SET reviewer_id = ?, review_notes = ?,
                       review_status = 'rejected', reviewed_at = ? WHERE id = ?""",
            (values["admin_id"], values["notes"], values["reviewed_at"], values["evidence_id"]),
        )
        connection.execute(
            "UPDATE payments SET status = 'rejected' WHERE order_id = ? AND status = 'submitted'",
            (values["order_id"],),
        )

    @staticmethod
    def insert_notification(connection: Any, **values: Any) -> None:
        connection.execute(
            """INSERT INTO notifications
               (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
               ON CONFLICT(dedupe_key) DO NOTHING""",
            (
                values["notification_id"], values["dedupe_key"], values["telegram_id"],
                values["kind"], values["text"], values["now_text"], values["now_text"],
            ),
        )

    @staticmethod
    def start_diagnostic(connection: Any, run_id: str, admin_id: int, now_text: str) -> None:
        connection.execute(
            """INSERT INTO receipt_diagnostic_runs
               (id, admin_id, status, result_json, started_at)
               VALUES (?, ?, 'running', '{}', ?)""",
            (run_id, int(admin_id), now_text),
        )

    @staticmethod
    def finish_diagnostic(
        connection: Any, run_id: str, admin_id: int, status: str, result_json: str, completed_at: str
    ) -> Any:
        return connection.execute(
            """UPDATE receipt_diagnostic_runs
               SET status = ?, result_json = ?, completed_at = ?
               WHERE id = ? AND admin_id = ? AND status = 'running'""",
            (status, result_json, completed_at, str(run_id), int(admin_id)),
        )

    @staticmethod
    def latest_diagnostic(connection: Any) -> Any:
        return connection.execute(
            """SELECT * FROM receipt_diagnostic_runs
               WHERE status IN ('passed', 'failed')
               ORDER BY started_at DESC LIMIT 1"""
        ).fetchone()

    @staticmethod
    def receipt_counts(connection: Any) -> tuple[int, int]:
        pending = connection.execute(
            "SELECT COUNT(*) AS count FROM payment_evidence WHERE review_status = 'pending'"
        ).fetchone()
        failed = connection.execute(
            "SELECT COUNT(*) AS count FROM payment_evidence WHERE storage_status = 'failed'"
        ).fetchone()
        return int(pending["count"]), int(failed["count"])
