"""Persistence boundary for infrastructure provisioning jobs and events."""

from __future__ import annotations

import json
from typing import Any


class InfrastructureProvisioningRepository:
    """Keep provider-job state transitions out of infrastructure orchestration."""

    @staticmethod
    def job_by_fingerprint(connection: Any, fingerprint: str) -> Any:
        return connection.execute(
            "SELECT id FROM infrastructure_jobs WHERE request_fingerprint = ?",
            (fingerprint,),
        ).fetchone()

    @staticmethod
    def created_today(connection: Any, day_start: str) -> int:
        return int(
            connection.execute(
                """SELECT COUNT(*) AS n FROM infrastructure_jobs
                   WHERE operation = 'provision' AND created_at >= ?""",
                (day_start,),
            ).fetchone()["n"]
        )

    @staticmethod
    def latest_provision(connection: Any) -> Any:
        return connection.execute(
            """SELECT created_at FROM infrastructure_jobs
               WHERE operation = 'provision' ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()

    @staticmethod
    def active_provision(connection: Any) -> Any:
        return connection.execute(
            """SELECT 1 FROM infrastructure_jobs
               WHERE operation = 'provision' AND status IN ('pending', 'running') LIMIT 1"""
        ).fetchone()

    @staticmethod
    def insert_job(
        connection: Any, job_id: str, fingerprint: str, now_text: str
    ) -> None:
        connection.execute(
            """INSERT INTO infrastructure_jobs
               (id, operation, status, attempts, next_attempt_at,
                request_fingerprint, created_at)
               VALUES (?, 'provision', 'pending', 0, ?, ?, ?)""",
            (job_id, now_text, fingerprint, now_text),
        )

    @staticmethod
    def insert_event(
        connection: Any,
        *,
        event_id: str,
        job_id: str,
        event_type: str,
        now_text: str,
        metadata: dict[str, Any] | None = None,
        server_id: str | None = None,
    ) -> None:
        if server_id is None:
            connection.execute(
                """INSERT INTO infrastructure_events
                   (id, infrastructure_job_id, event_type, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (event_id, job_id, event_type, json.dumps(metadata or {}, sort_keys=True), now_text),
            )
        else:
            connection.execute(
                """INSERT INTO infrastructure_events
                   (id, infrastructure_job_id, server_id, event_type, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (event_id, job_id, server_id, event_type, json.dumps(metadata or {}, sort_keys=True), now_text),
            )

    @staticmethod
    def pending_job(connection: Any, job_id: str, *, lock: bool = False) -> Any:
        suffix = " FOR UPDATE SKIP LOCKED" if lock and connection.__class__.__name__ == "_PostgresConnection" else ""
        return connection.execute(
            "SELECT * FROM infrastructure_jobs WHERE id = ? AND status = 'pending'" + suffix,
            (job_id,),
        ).fetchone()

    @staticmethod
    def provision_event(connection: Any, job_id: str, event_type: str) -> Any:
        return connection.execute(
            """SELECT metadata_json FROM infrastructure_events
               WHERE infrastructure_job_id = ? AND event_type = ?
               ORDER BY created_at LIMIT 1""",
            (job_id, event_type),
        ).fetchone()

    @staticmethod
    def mark_running(connection: Any, job_id: str, now_text: str) -> None:
        connection.execute(
            """UPDATE infrastructure_jobs SET status = 'running', attempts = attempts + 1,
                      locked_at = ? WHERE id = ?""",
            (now_text, job_id),
        )

    @staticmethod
    def recover_created(
        connection: Any, job_id: str, resource_id: str, action_id: str | None, now_text: str
    ) -> None:
        connection.execute(
            """UPDATE infrastructure_jobs
                  SET status = 'running', provider_resource_id = ?,
                      provider_action_id = ?, locked_at = ?, last_error = ?
                WHERE id = ?""",
            (resource_id, action_id, now_text, "create response ambiguous; recovered by exact name", job_id),
        )

    @staticmethod
    def mark_failed(connection: Any, job_id: str, error: str) -> None:
        connection.execute(
            """UPDATE infrastructure_jobs SET status = 'failed', locked_at = NULL,
                      last_error = ? WHERE id = ?""",
            (error, job_id),
        )

    @staticmethod
    def set_provider_ids(
        connection: Any, job_id: str, resource_id: str, action_id: str | None
    ) -> None:
        connection.execute(
            """UPDATE infrastructure_jobs SET provider_resource_id = ?,
                      provider_action_id = ? WHERE id = ?""",
            (resource_id, action_id, job_id),
        )

    @staticmethod
    def job(connection: Any, job_id: str) -> Any:
        return connection.execute(
            "SELECT * FROM infrastructure_jobs WHERE id = ? AND operation = 'provision'",
            (job_id,),
        ).fetchone()

    @staticmethod
    def latest_event(connection: Any, job_id: str, event_type: str) -> Any:
        return connection.execute(
            """SELECT metadata_json FROM infrastructure_events
               WHERE infrastructure_job_id = ? AND event_type = ?
               ORDER BY created_at DESC LIMIT 1""",
            (job_id, event_type),
        ).fetchone()

    @staticmethod
    def mark_action_failed(connection: Any, job_id: str) -> None:
        connection.execute(
            "UPDATE infrastructure_jobs SET status = 'failed', last_error = ? WHERE id = ?",
            ("provider action failed", job_id),
        )

    @staticmethod
    def mark_awaiting_verification(connection: Any, job_id: str) -> None:
        connection.execute(
            """UPDATE infrastructure_jobs SET status = 'awaiting_verification',
                      locked_at = NULL WHERE id = ? AND status = 'running'""",
            (job_id,),
        )

    @staticmethod
    def activated_job(connection: Any, job_id: str, node_id: str) -> Any:
        return connection.execute(
            """SELECT j.status, j.provider_resource_id,
                      s.provider_resource_id AS node_provider_resource_id
               FROM infrastructure_jobs AS j
               LEFT JOIN outline_servers AS s ON s.server_id = ?
               WHERE j.id = ? AND j.operation = 'provision'""",
            (node_id, job_id),
        ).fetchone()

    @staticmethod
    def mark_completed(connection: Any, job_id: str, now_text: str) -> None:
        connection.execute(
            """UPDATE infrastructure_jobs
               SET status = 'completed', completed_at = ?, locked_at = NULL, last_error = NULL
               WHERE id = ? AND status = 'awaiting_verification'""",
            (now_text, job_id),
        )
