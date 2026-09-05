"""Persistence boundary for one-time fleet enrollment state."""

from __future__ import annotations

import json
import uuid
from typing import Any


class FleetEnrollmentRepository:
    """Store enrollment tokens, encrypted callbacks, and audit events."""

    @staticmethod
    def existing(connection: Any, job_id: str) -> Any:
        return connection.execute(
            "SELECT job_id, token_hash, expires_at FROM infrastructure_enrollments WHERE job_id = ?",
            (job_id,),
        ).fetchone()

    @staticmethod
    def job(connection: Any, job_id: str) -> Any:
        return connection.execute(
            "SELECT id, operation FROM infrastructure_jobs WHERE id = ?", (job_id,)
        ).fetchone()

    @staticmethod
    def insert_enrollment(
        connection: Any, job_id: str, token_hash: str, expires_at: str, now_text: str
    ) -> None:
        connection.execute(
            """INSERT INTO infrastructure_enrollments
               (job_id, token_hash, expires_at, status, created_at)
               VALUES (?, ?, ?, 'pending', ?)""",
            (job_id, token_hash, expires_at, now_text),
        )

    @staticmethod
    def event(
        connection: Any, job_id: str, event_type: str, now_text: str, metadata: dict[str, Any]
    ) -> None:
        connection.execute(
            """INSERT INTO infrastructure_events
               (id, infrastructure_job_id, event_type, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (uuid.uuid4().hex, job_id, event_type, json.dumps(metadata, sort_keys=True), now_text),
        )

    @staticmethod
    def by_token(connection: Any, token_hash: str) -> Any:
        suffix = " FOR UPDATE" if connection.__class__.__name__ == "_PostgresConnection" else ""
        return connection.execute(
            "SELECT * FROM infrastructure_enrollments WHERE token_hash = ?" + suffix,
            (token_hash,),
        ).fetchone()

    @staticmethod
    def mark_expired(connection: Any, job_id: str, error: str) -> None:
        connection.execute(
            "UPDATE infrastructure_enrollments SET status = 'expired', last_error = ? WHERE job_id = ?",
            (error, job_id),
        )

    @staticmethod
    def update_payload(connection: Any, job_id: str, ciphertext: str, now_text: str) -> None:
        connection.execute(
            """UPDATE infrastructure_enrollments
               SET payload_ciphertext = ?, received_at = ?, last_error = NULL
               WHERE job_id = ? AND status = 'pending'""",
            (ciphertext, now_text, job_id),
        )

    @staticmethod
    def pending_payload(connection: Any, job_id: str) -> Any:
        return connection.execute(
            "SELECT * FROM infrastructure_enrollments WHERE job_id = ?", (job_id,)
        ).fetchone()

    @staticmethod
    def expire_one(connection: Any, job_id: str, error: str) -> None:
        connection.execute(
            """UPDATE infrastructure_enrollments
               SET status = 'expired', last_error = ?
               WHERE job_id = ? AND status = 'pending'""",
            (error, job_id),
        )

    @staticmethod
    def mark_consumed(connection: Any, job_id: str, now_text: str) -> int:
        return int(
            connection.execute(
                """UPDATE infrastructure_enrollments
                   SET status = 'consumed', consumed_at = ?, last_error = NULL
                   WHERE job_id = ? AND status = 'pending' AND payload_ciphertext IS NOT NULL""",
                (now_text, job_id),
            ).rowcount
            or 0
        )

    @staticmethod
    def status(connection: Any, job_id: str) -> Any:
        return connection.execute(
            "SELECT status FROM infrastructure_enrollments WHERE job_id = ?", (job_id,)
        ).fetchone()

    @staticmethod
    def expired_jobs(connection: Any, now_text: str) -> list[Any]:
        return connection.execute(
            "SELECT job_id FROM infrastructure_enrollments WHERE status = 'pending' AND expires_at <= ?",
            (now_text,),
        ).fetchall()

    @staticmethod
    def expire_pending(connection: Any, now_text: str, error: str) -> int:
        return int(
            connection.execute(
                """UPDATE infrastructure_enrollments
                   SET status = 'expired', last_error = COALESCE(last_error, ?)
                   WHERE status = 'pending' AND expires_at <= ?""",
                (error, now_text),
            ).rowcount
            or 0
        )
