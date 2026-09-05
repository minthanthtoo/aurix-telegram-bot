"""Durable job leasing and retry operations."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from commerce_models import (
    JOB_RETRY_DELAY,
    CommerceError,
    _now_text,
)
from commerce_job_repository import CommerceJobRepository


_REPOSITORY = CommerceJobRepository()


def _claim_job(self, operation: str, now: datetime) -> dict[str, Any] | None:
    now_text = _now_text(now)
    stale_before = _now_text(now - timedelta(minutes=5))
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        result = _REPOSITORY.claim(connection, operation, now_text, stale_before)
        if result is None:
            return None
        return result

def _job_done(self, job_id: str) -> None:
    with self.database.connect() as connection:
        _REPOSITORY.mark_done(connection, job_id)

def _job_failed(self, job_id: str, error: Exception, now: datetime) -> None:
    safe_error = f"{type(error).__name__}: {str(error)[:500]}"
    next_attempt = _now_text(now + JOB_RETRY_DELAY)
    with self.database.connect() as connection:
        _REPOSITORY.mark_failed(
            connection, job_id=job_id, next_attempt_at=next_attempt, error=safe_error
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
        rows = _REPOSITORY.failed_jobs(
            connection, max(1, min(limit, 100)), include_nonterminal
        )
    return [dict(row) for row in rows]

def retry_job(self, job_id: str, admin_id: int, now: datetime | None = None) -> str:
    """Requeue one exact failed job (avoids ambiguous order-level retries)."""
    current = _now_text(now)
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        row = _REPOSITORY.failed_job(connection, job_id)
        if row is None:
            raise CommerceError("No terminal worker failure exists for that job")
        _REPOSITORY.reset(connection, job_id, current)
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
        row = _REPOSITORY.failed_job_for_order(connection, order_id, operation)
        if row is None:
            raise CommerceError("No terminal worker failure exists for this order")
        _REPOSITORY.reset(connection, str(row["id"]), current)
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
