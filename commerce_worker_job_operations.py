"""Durable job leasing and retry operations."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from commerce_repositories import _PostgresConnection
from commerce_models import (
    JOB_RETRY_DELAY,
    CommerceError,
    _now_text,
)


def _claim_job(self, operation: str, now: datetime) -> dict[str, Any] | None:
    now_text = _now_text(now)
    stale_before = _now_text(now - timedelta(minutes=5))
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        connection.execute(
            """UPDATE provisioning_jobs SET status = 'pending', locked_at = NULL
               WHERE status = 'running' AND locked_at < ?""",
            (stale_before,),
        )
        lock_clause = (
            " FOR UPDATE SKIP LOCKED" if isinstance(connection, _PostgresConnection) else ""
        )
        row = connection.execute(
            """SELECT * FROM provisioning_jobs
               WHERE operation = ? AND status = 'pending' AND next_attempt_at <= ?
               ORDER BY created_at LIMIT 1"""
            + lock_clause,
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
        result["attempts"] = row["attempts"] + 1
        return result

def _job_done(self, job_id: str) -> None:
    with self.database.connect() as connection:
        connection.execute(
            """UPDATE provisioning_jobs SET status = 'done', locked_at = NULL, last_error = NULL
               WHERE id = ?""",
            (job_id,),
        )

def _job_failed(self, job_id: str, error: Exception, now: datetime) -> None:
    safe_error = f"{type(error).__name__}: {str(error)[:500]}"
    next_attempt = _now_text(now + JOB_RETRY_DELAY)
    with self.database.connect() as connection:
        connection.execute(
            """UPDATE provisioning_jobs
               SET status = CASE WHEN attempts >= 8 THEN 'failed' ELSE 'pending' END,
                   next_attempt_at = ?, locked_at = NULL, last_error = ?
               WHERE id = ?""",
            (next_attempt, safe_error, job_id),
        )

def failed_jobs(
    self, limit: int = 20, include_nonterminal: bool = False
) -> list[dict[str, Any]]:
    """Return worker operations needing attention.

    The default remains terminal-only for API compatibility; operators can
    request pending/running retries so a silent revoke failure is visible
    before the eighth attempt.
    """
    with self.database.connect() as connection:
        rows = connection.execute(
            """SELECT j.id AS job_id, j.operation, j.attempts, j.last_error,
                      j.next_attempt_at, j.status AS job_status, s.order_id, s.telegram_id,
                      s.plan_code, s.status AS subscription_status
               FROM provisioning_jobs j
               JOIN subscriptions s ON s.id = j.subscription_id
               WHERE j.status = 'failed' OR (? = 1 AND j.status IN ('pending', 'running'))
               ORDER BY j.created_at LIMIT ?""",
            (1 if include_nonterminal else 0, max(1, min(limit, 100))),
        ).fetchall()
    return [dict(row) for row in rows]

def retry_job(self, job_id: str, admin_id: int, now: datetime | None = None) -> str:
    """Requeue one exact failed job (avoids ambiguous order-level retries)."""
    current = _now_text(now)
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        row = connection.execute(
            "SELECT id, operation, subscription_id FROM provisioning_jobs WHERE id = ? AND status = 'failed'",
            (job_id,),
        ).fetchone()
        if row is None:
            raise CommerceError("No terminal worker failure exists for that job")
        connection.execute(
            """UPDATE provisioning_jobs SET status = 'pending', attempts = 0,
                      next_attempt_at = ?, locked_at = NULL, last_error = NULL
               WHERE id = ? AND status = 'failed'""",
            (current, job_id),
        )
        self._audit(
            connection,
            "job_retried",
            "provisioning_job",
            job_id,
            "admin",
            str(admin_id),
            {"operation": row["operation"]},
        )
    return str(row["operation"])

def retry_failed_job(
    self,
    order_id: str,
    admin_id: int,
    now: datetime | None = None,
    operation: str | None = None,
) -> str:
    """Requeue one terminal job after an operator has reviewed its error."""
    current = _now_text(now)
    if operation not in (None, "provision", "revoke"):
        raise CommerceError("Unknown worker operation")
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        row = connection.execute(
            """SELECT j.id, j.operation, s.id AS subscription_id
               FROM provisioning_jobs j JOIN subscriptions s
                 ON s.id = j.subscription_id
               WHERE s.order_id = ? AND j.status = 'failed'
                 AND (? IS NULL OR j.operation = ?)
               ORDER BY j.created_at DESC LIMIT 1""",
            (order_id, operation, operation),
        ).fetchone()
        if row is None:
            raise CommerceError("No terminal worker failure exists for this order")
        connection.execute(
            """UPDATE provisioning_jobs
               SET status = 'pending', attempts = 0, next_attempt_at = ?,
                   locked_at = NULL, last_error = NULL
               WHERE id = ? AND status = 'failed'""",
            (current, row["id"]),
        )
        self._audit(
            connection,
            "job_retried",
            "subscription",
            str(row["subscription_id"]),
            "admin",
            str(admin_id),
            {"operation": row["operation"], "order_id": order_id},
        )
    return str(row["operation"])
