"""Persistence boundary for durable fleet probe jobs and health projections."""

from __future__ import annotations

from typing import Any


class FleetProbeRepository:
    @staticmethod
    def upsert_target(connection: Any, values: tuple[Any, ...]) -> None:
        connection.execute(
            """INSERT INTO probe_targets
               (target_id, label, target_kind, host, port, scheme, enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(target_id) DO UPDATE SET
                 label = excluded.label, target_kind = excluded.target_kind,
                 host = excluded.host, port = excluded.port, scheme = excluded.scheme,
                 enabled = excluded.enabled, updated_at = excluded.updated_at""",
            values,
        )

    @staticmethod
    def schedule_dependencies(connection: Any, source_server_id: str, target_id: str) -> bool:
        source = connection.execute(
            "SELECT 1 FROM outline_servers WHERE server_id = ?", (source_server_id,)
        ).fetchone()
        target = connection.execute(
            "SELECT 1 FROM probe_targets WHERE target_id = ?", (target_id,)
        ).fetchone()
        return source is not None and target is not None

    @staticmethod
    def upsert_schedule(connection: Any, values: tuple[Any, ...]) -> None:
        connection.execute(
            """INSERT INTO probe_schedules
               (schedule_id, source_server_id, target_id, probe_type, interval_seconds,
                timeout_ms, payload_bytes, enabled, next_run_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(schedule_id) DO UPDATE SET
                 source_server_id = excluded.source_server_id, target_id = excluded.target_id,
                 probe_type = excluded.probe_type, interval_seconds = excluded.interval_seconds,
                 timeout_ms = excluded.timeout_ms, payload_bytes = excluded.payload_bytes,
                 enabled = excluded.enabled, next_run_at = excluded.next_run_at,
                 updated_at = excluded.updated_at""",
            values,
        )

    @staticmethod
    def due_schedules(connection: Any, timestamp: str, limit: int) -> list[Any]:
        return connection.execute(
            """SELECT s.*, t.host, t.port, t.scheme, t.target_kind, t.label AS target_label
                 FROM probe_schedules s
                 JOIN probe_targets t ON t.target_id = s.target_id
                WHERE s.enabled = 1 AND t.enabled = 1 AND s.next_run_at <= ?
                ORDER BY s.next_run_at, s.schedule_id
                LIMIT ?""",
            (timestamp, limit),
        ).fetchall()

    @staticmethod
    def insert_job(connection: Any, values: tuple[Any, ...]) -> None:
        connection.execute(
            """INSERT INTO probe_jobs
               (job_id, schedule_id, source_server_id, target_id, probe_type,
                instruction_json, nonce, status, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            values,
        )

    @staticmethod
    def advance_schedule(connection: Any, values: tuple[Any, ...]) -> None:
        connection.execute(
            """UPDATE probe_schedules
                  SET next_run_at = ?, last_enqueued_at = ?, updated_at = ?
                WHERE schedule_id = ?""",
            values,
        )

    @staticmethod
    def claimable_jobs(
        connection: Any, *, timestamp: str, source_server_id: str | None, limit: int
    ) -> list[Any]:
        params: list[Any] = [timestamp, timestamp]
        source_clause = ""
        if source_server_id:
            source_clause = " AND source_server_id = ?"
            params.append(str(source_server_id))
        params.append(limit)
        return connection.execute(
            """SELECT * FROM probe_jobs
                WHERE (status = 'pending' OR (status = 'claimed' AND claimed_at < ?))
                  AND expires_at > ?"""
            + source_clause
            + " ORDER BY created_at, job_id LIMIT ?",
            tuple(params),
        ).fetchall()

    @staticmethod
    def claim_job(connection: Any, *, agent_id: str, lease_at: str, job_id: str, timestamp: str) -> bool:
        updated = connection.execute(
            """UPDATE probe_jobs
                  SET status = 'claimed', claimed_by = ?, claimed_at = ?,
                      attempts = attempts + 1
                WHERE job_id = ?
                  AND (status = 'pending' OR (status = 'claimed' AND claimed_at < ?))""",
            (agent_id, lease_at, job_id, timestamp),
        )
        return int(getattr(updated, "rowcount", 0) or 0) == 1

    @staticmethod
    def job(connection: Any, job_id: str) -> Any:
        return connection.execute("SELECT * FROM probe_jobs WHERE job_id = ?", (job_id,)).fetchone()

    @staticmethod
    def expire_job(connection: Any, job_id: str) -> None:
        connection.execute(
            "UPDATE probe_jobs SET status = 'expired', last_error = ? WHERE job_id = ?",
            ("result_after_expiry", job_id),
        )

    @staticmethod
    def observation_for_job(connection: Any, job_id: str) -> Any:
        return connection.execute(
            "SELECT observation_id, status FROM probe_observations WHERE job_id = ?", (job_id,)
        ).fetchone()

    @staticmethod
    def insert_observation(connection: Any, values: tuple[Any, ...]) -> None:
        connection.execute(
            """INSERT INTO probe_observations
               (observation_id, job_id, source_server_id, target_id, probe_type, agent_id,
                status, latency_ms, packet_loss_percent, bytes_transferred, duration_ms,
                error_class, result_json, signature, observed_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            values,
        )

    @staticmethod
    def complete_job(connection: Any, job_id: str, timestamp: str) -> None:
        connection.execute(
            "UPDATE probe_jobs SET status = 'completed', completed_at = ?, last_error = NULL WHERE job_id = ?",
            (timestamp, job_id),
        )

    @staticmethod
    def expire_jobs(connection: Any, timestamp: str) -> int:
        result = connection.execute(
            """UPDATE probe_jobs SET status = 'expired', last_error = COALESCE(last_error, 'job_expired')
                WHERE status IN ('pending', 'claimed') AND expires_at <= ?""",
            (timestamp,),
        )
        return int(getattr(result, "rowcount", 0) or 0)

    @staticmethod
    def recent_observations(connection: Any, server_id: str, cutoff: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT status, latency_ms, packet_loss_percent, bytes_transferred,
                      duration_ms, observed_at
                 FROM probe_observations
                WHERE source_server_id = ? AND observed_at >= ?
                ORDER BY observed_at DESC""",
            (server_id, cutoff),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def save_health_snapshot(connection: Any, values: tuple[Any, ...]) -> None:
        connection.execute(
            """INSERT INTO route_health_snapshots
               (server_id, status, score, availability_score, latency_score, loss_score,
                throughput_score, sample_count, freshness_seconds, last_observed_at, reason, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(server_id) DO UPDATE SET
                 status = excluded.status, score = excluded.score,
                 availability_score = excluded.availability_score, latency_score = excluded.latency_score,
                 loss_score = excluded.loss_score, throughput_score = excluded.throughput_score,
                 sample_count = excluded.sample_count, freshness_seconds = excluded.freshness_seconds,
                 last_observed_at = excluded.last_observed_at, reason = excluded.reason,
                 updated_at = excluded.updated_at""",
            values,
        )

    @staticmethod
    def recommendation_rows(
        connection: Any, *, region: str | None, cutoff: str, limit: int
    ) -> list[Any]:
        where = ["s.enabled = 1", "COALESCE(s.lifecycle_state, 'active') = 'active'"]
        params: list[Any] = []
        if region:
            where.append("LOWER(COALESCE(r.display_name, '')) = LOWER(?)")
            params.append(str(region))
        params.extend([cutoff, limit])
        query = (
            """SELECT s.server_id, s.label, COALESCE(r.display_name, 'Unknown') AS region,
                              COALESCE(h.status, 'unknown') AS probe_status, h.score,
                              COALESCE(h.sample_count, 0) AS sample_count,
                              h.last_observed_at, h.reason
                         FROM outline_servers s
                         LEFT JOIN connectivity_endpoints e ON e.outline_server_id = s.server_id
                         LEFT JOIN connectivity_regions r ON r.region_id = e.region_id
                         LEFT JOIN route_health_snapshots h ON h.server_id = s.server_id
                        WHERE """
            + " AND ".join(where)
            + """ AND (h.last_observed_at IS NULL OR h.last_observed_at >= ?)
                        ORDER BY CASE COALESCE(h.status, 'unknown')
                                   WHEN 'healthy' THEN 0 WHEN 'degraded' THEN 1 ELSE 2 END,
                                 h.score DESC NULLS LAST, s.server_id
                        LIMIT ?"""
        )
        return connection.execute(query, tuple(params)).fetchall()

    @staticmethod
    def record_decision(connection: Any, values: tuple[Any, ...]) -> None:
        connection.execute(
            """INSERT INTO route_decisions
               (decision_id, telegram_id, entitlement_ref, requested_region,
                selected_server_id, decision_mode, score, evidence_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            values,
        )
