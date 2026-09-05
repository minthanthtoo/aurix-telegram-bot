"""Persistence boundary for inventory review and managed-repair operations."""

from __future__ import annotations

from typing import Any


class InventoryOperationsRepository:
    """SQL operations used by the inventory/review application workflows."""

    @staticmethod
    def table_exists(connection: Any, table_name: str) -> bool:
        if connection.__class__.__name__ != "_PostgresConnection":
            return connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone() is not None
        row = connection.execute(
            "SELECT to_regclass(?) AS table_name", (f"public.{table_name}",)
        ).fetchone()
        return row is not None and row["table_name"] is not None

    @staticmethod
    def endpoint_migration_jobs(
        connection: Any, *, limit: int, include_completed: bool
    ) -> list[dict[str, Any]]:
        statuses = (
            "('pending', 'creating', 'source_delete_pending', 'failed', 'cancelled')"
            if not include_completed
            else "('pending', 'creating', 'source_delete_pending', 'failed', 'cancelled', 'completed')"
        )
        rows = connection.execute(
            f"""SELECT id AS job_id, profile_kind, telegram_id,
                              source_server_id, target_server_id,
                              source_external_id, target_external_id,
                              status AS job_status, attempts,
                              source_used_bytes, quota_bytes,
                              next_attempt_at, last_error,
                              requested_by, created_at, updated_at,
                              completed_at
                         FROM connectivity_migration_jobs
                        WHERE status IN {statuses}
                        ORDER BY CASE status
                                   WHEN 'failed' THEN 0
                                   WHEN 'source_delete_pending' THEN 1
                                   WHEN 'creating' THEN 2
                                   WHEN 'pending' THEN 3
                                   ELSE 4
                                 END,
                                 created_at DESC
                        LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def migratable_credentials(connection: Any, source_server_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT c.credential_id, c.external_id, p.profile_kind,
                      p.telegram_id, c.created_at, c.status
                 FROM connectivity_credentials c
                 JOIN connectivity_profiles p ON p.profile_id = c.profile_id
                 JOIN connectivity_endpoints e ON e.endpoint_id = c.endpoint_id
                WHERE e.outline_server_id = ? AND c.status = 'active'
                ORDER BY c.created_at, c.external_id""",
            (source_server_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def open_managed_repairs(connection: Any) -> list[Any]:
        return connection.execute(
            """SELECT id, kind, server_id, telegram_id, source_external_id,
                      quota_bytes, used_bytes, status
                 FROM managed_key_repair_jobs
                WHERE status IN ('pending', 'running', 'manual', 'failed')
                ORDER BY created_at, id"""
        ).fetchall()

    @staticmethod
    def repair_notification_exists(connection: Any, repair_id: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM notifications WHERE dedupe_key LIKE ? LIMIT 1",
            (f"staff:key_repairs:{repair_id}:%",),
        ).fetchone()
        return row is not None

    @staticmethod
    def remote_key_inventory(
        connection: Any,
        server_id: str,
        *,
        status: str,
        managed: bool | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        if connection.execute(
            "SELECT 1 FROM outline_servers WHERE server_id = ?", (server_id,)
        ).fetchone() is None:
            raise ValueError("Outline server is unavailable")
        clauses = ["outline_remote_keys.server_id = ?"]
        params: list[Any] = [server_id]
        if status != "all":
            clauses.append("outline_remote_keys.status = ?")
            params.append(status)
        if managed is not None:
            clauses.append("outline_remote_keys.managed = ?")
            params.append(1 if managed else 0)
        params.append(limit)
        rows = connection.execute(
            """SELECT outline_remote_keys.server_id, outline_remote_keys.outline_key_id,
                      outline_remote_keys.remote_name, outline_remote_keys.managed,
                      outline_remote_keys.status, outline_remote_keys.first_seen_at,
                      outline_remote_keys.last_seen_at, outline_remote_keys.last_usage_bytes,
                      COALESCE(r.review_state,
                               CASE WHEN outline_remote_keys.managed = 1
                                    THEN 'managed' ELSE 'unreviewed' END) AS review_state,
                      r.reviewed_by, r.reviewed_at, r.review_note
                 FROM outline_remote_keys
                 LEFT JOIN outline_remote_key_reviews r
                   ON r.server_id = outline_remote_keys.server_id
                  AND r.outline_key_id = outline_remote_keys.outline_key_id
                WHERE """
            + " AND ".join(clauses)
            + """ ORDER BY CASE WHEN outline_remote_keys.status = 'present' THEN 0 ELSE 1 END,
                              outline_remote_keys.last_seen_at DESC,
                              outline_remote_keys.outline_key_id
                LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def remote_key_for_review(connection: Any, server_id: str, key_id: str) -> Any:
        return connection.execute(
            """SELECT managed, status FROM outline_remote_keys
               WHERE server_id = ? AND outline_key_id = ?""",
            (server_id, key_id),
        ).fetchone()

    @staticmethod
    def save_remote_key_review(
        connection: Any,
        *,
        server_id: str,
        key_id: str,
        review_state: str,
        reviewer_id: int,
        reviewed_at: str,
        note: str | None,
    ) -> None:
        connection.execute(
            """INSERT INTO outline_remote_key_reviews
               (server_id, outline_key_id, review_state, reviewed_by, reviewed_at, review_note)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(server_id, outline_key_id) DO UPDATE SET
                 review_state = excluded.review_state,
                 reviewed_by = excluded.reviewed_by,
                 reviewed_at = excluded.reviewed_at,
                 review_note = excluded.review_note""",
            (server_id, key_id, review_state, int(reviewer_id), reviewed_at, note),
        )

    @staticmethod
    def recompute_orphan_count(connection: Any, server_id: str, updated_at: str) -> None:
        connection.execute(
            """UPDATE outline_servers
                  SET remote_orphan_key_count = (
                      SELECT COUNT(*)
                        FROM outline_remote_keys remote
                       WHERE remote.server_id = outline_servers.server_id
                         AND remote.status = 'present'
                         AND remote.managed = 0
                         AND COALESCE(
                               (SELECT review_state
                                  FROM outline_remote_key_reviews review
                                 WHERE review.server_id = remote.server_id
                                   AND review.outline_key_id = remote.outline_key_id),
                               'unreviewed'
                             ) = 'unreviewed'
                  ),
                      updated_at = ?
                WHERE server_id = ?""",
            (updated_at, server_id),
        )

    @staticmethod
    def managed_key_repair_jobs(
        connection: Any, *, status: str, limit: int
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status == "open":
            clauses.append("status IN ('pending', 'running', 'failed', 'manual')")
        elif status != "all":
            clauses.append("status = ?")
            params.append(status)
        params.append(limit)
        rows = connection.execute(
            """SELECT id, kind, server_id, telegram_id, local_key_ref,
                      source_external_id, target_external_id, key_name,
                      quota_bytes, used_bytes, expires_at, status, attempts,
                      next_attempt_at, locked_at, last_error, observed_at,
                      created_at, completed_at
                 FROM managed_key_repair_jobs"""
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
            + " ORDER BY CASE status WHEN 'manual' THEN 0 WHEN 'failed' THEN 1 "
              "WHEN 'pending' THEN 2 WHEN 'running' THEN 3 ELSE 4 END, created_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def managed_key_repair(connection: Any, repair_id: str) -> Any:
        return connection.execute(
            "SELECT * FROM managed_key_repair_jobs WHERE id = ?", (repair_id,)
        ).fetchone()

    @staticmethod
    def approve_managed_key_repair(
        connection: Any,
        *,
        repair_id: str,
        now_text: str,
        used_bytes: int | None,
        last_error: str | None,
    ) -> bool:
        updated = connection.execute(
            """UPDATE managed_key_repair_jobs
                  SET status = 'pending', attempts = 0, next_attempt_at = ?,
                      locked_at = NULL, used_bytes = ?,
                      last_error = ?, completed_at = NULL
                WHERE id = ? AND status IN ('manual', 'failed')""",
            (now_text, used_bytes, last_error, repair_id),
        )
        return int(getattr(updated, "rowcount", 0) or 0) == 1
