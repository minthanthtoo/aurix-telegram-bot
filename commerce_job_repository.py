"""Persistence boundary for provisioning-job leasing and retry state."""

from __future__ import annotations

from typing import Any


class CommerceJobRepository:
    @staticmethod
    def claim(connection: Any, operation: str, now_text: str, stale_before: str) -> Any:
        connection.execute(
            """UPDATE provisioning_jobs SET status = 'pending', locked_at = NULL
               WHERE status = 'running' AND locked_at < ?""",
            (stale_before,),
        )
        lock_clause = (
            " FOR UPDATE SKIP LOCKED" if connection.__class__.__name__ == "_PostgresConnection" else ""
        )
        row = connection.execute(
            """SELECT * FROM provisioning_jobs
               WHERE operation = ? AND status = 'pending' AND next_attempt_at <= ?
               ORDER BY created_at LIMIT 1""" + lock_clause,
            (operation, now_text),
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            """UPDATE provisioning_jobs
               SET status = 'running', attempts = attempts + 1, locked_at = ?
               WHERE id = ? AND status = 'pending'""",
            (now_text, row["id"]),
        )
        result = dict(row)
        result["attempts"] = int(row["attempts"] or 0) + 1
        return result

    @staticmethod
    def mark_done(connection: Any, job_id: str) -> None:
        connection.execute(
            """UPDATE provisioning_jobs SET status = 'done', locked_at = NULL, last_error = NULL
               WHERE id = ?""",
            (job_id,),
        )

    @staticmethod
    def mark_failed(
        connection: Any, *, job_id: str, next_attempt_at: str, error: str
    ) -> None:
        connection.execute(
            """UPDATE provisioning_jobs
               SET status = CASE WHEN attempts >= 8 THEN 'failed' ELSE 'pending' END,
                   next_attempt_at = ?, locked_at = NULL, last_error = ?
               WHERE id = ?""",
            (next_attempt_at, error, job_id),
        )

    @staticmethod
    def failed_jobs(connection: Any, limit: int, include_nonterminal: bool) -> list[Any]:
        return connection.execute(
            """SELECT j.id AS job_id, j.operation, j.attempts, j.last_error,
                      j.next_attempt_at, j.status AS job_status, s.order_id, s.telegram_id,
                      s.plan_code, s.status AS subscription_status
               FROM provisioning_jobs j JOIN subscriptions s ON s.id = j.subscription_id
               WHERE j.status = 'failed' OR (? = 1 AND j.status IN ('pending', 'running'))
               ORDER BY j.created_at LIMIT ?""",
            (1 if include_nonterminal else 0, limit),
        ).fetchall()

    @staticmethod
    def failed_job(connection: Any, job_id: str) -> Any:
        return connection.execute(
            "SELECT id, operation, subscription_id FROM provisioning_jobs WHERE id = ? AND status = 'failed'",
            (job_id,),
        ).fetchone()

    @staticmethod
    def reset(connection: Any, job_id: str, now_text: str) -> None:
        connection.execute(
            """UPDATE provisioning_jobs SET status = 'pending', attempts = 0,
                      next_attempt_at = ?, locked_at = NULL, last_error = NULL
               WHERE id = ? AND status = 'failed'""",
            (now_text, job_id),
        )

    @staticmethod
    def failed_job_for_order(
        connection: Any, order_id: str, operation: str | None
    ) -> Any:
        return connection.execute(
            """SELECT j.id, j.operation, s.id AS subscription_id
               FROM provisioning_jobs j JOIN subscriptions s ON s.id = j.subscription_id
               WHERE s.order_id = ? AND j.status = 'failed'
                 AND (? IS NULL OR j.operation = ?)
               ORDER BY j.created_at DESC LIMIT 1""",
            (order_id, operation, operation),
        ).fetchone()
