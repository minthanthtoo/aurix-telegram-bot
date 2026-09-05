"""Persistence boundary for free-key quota and termination workflows."""

from __future__ import annotations

from typing import Any


class EntitlementQuotaRepository:
    @staticmethod
    def table_exists(connection: Any, table_name: str) -> bool:
        if connection.__class__.__name__ == "_PostgresConnection":
            row = connection.execute(
                "SELECT to_regclass(?) AS table_name", (f"public.{table_name}",)
            ).fetchone()
            return bool(row and row["table_name"])
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
        ).fetchone() is not None

    @staticmethod
    def begin_termination(connection: Any, row: Any, reason: str, used_bytes: int | None, now_text: str) -> None:
        connection.execute(
            """UPDATE keys SET status = 'active', last_usage_bytes = COALESCE(?, last_usage_bytes),
                      last_usage_observed_at = COALESCE(?, last_usage_observed_at),
                      quota_reason = CASE WHEN ? = 'quota' THEN 'quota' ELSE quota_reason END
               WHERE id = ? AND status != 'revoked'""",
            (used_bytes, now_text if used_bytes is not None else None, reason, row["id"]),
        )
        connection.execute(
            """INSERT INTO key_termination_events
               (key_id, telegram_id, outline_key_id, reason, used_bytes, quota_bytes,
                expires_at, detected_at, remote_state)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'retrying')
               ON CONFLICT(key_id, reason) DO UPDATE SET
                   used_bytes = COALESCE(excluded.used_bytes, key_termination_events.used_bytes)""",
            (
                row["id"], row["telegram_id"], str(row["outline_key_id"]), reason,
                used_bytes, int(row["data_limit_bytes"]), row["expires_at"], now_text,
            ),
        )

    @staticmethod
    def mark_termination_failed(connection: Any, key_id: Any, reason: str, error: str) -> None:
        connection.execute(
            """UPDATE key_termination_events
               SET remote_state = CASE WHEN delete_attempts + 1 >= 10 THEN 'escalated' ELSE 'retrying' END,
                   delete_attempts = delete_attempts + 1, last_error = ?
               WHERE key_id = ? AND reason = ?""",
            (error, key_id, reason),
        )

    @staticmethod
    def finish_termination(
        connection: Any, *, key_id: Any, reason: str, state: str,
        verified_at: str | None,
    ) -> None:
        connection.execute("UPDATE keys SET status = 'revoked' WHERE id = ?", (key_id,))
        connection.execute(
            """UPDATE key_termination_events
               SET remote_state = ?, delete_attempts = delete_attempts + 1,
                   last_error = NULL, deletion_verified_at = ?
               WHERE key_id = ? AND reason = ?""",
            (state, verified_at, key_id, reason),
        )

    @staticmethod
    def enforceable_keys(connection: Any) -> list[Any]:
        return connection.execute(
            """SELECT id, telegram_id, server_id, outline_key_id,
                      data_limit_bytes, expires_at FROM keys
               WHERE status = 'active' OR (status = 'revoke_failed' AND quota_reason = 'quota')"""
        ).fetchall()

    @staticmethod
    def update_key_usage(connection: Any, key_id: Any, used_bytes: int, observed_at: str) -> None:
        connection.execute(
            """UPDATE keys SET last_usage_bytes = ?, last_usage_observed_at = ?
                WHERE id = ? AND status = 'active'""",
            (used_bytes, observed_at, key_id),
        )

    @staticmethod
    def warning_rows(connection: Any) -> list[Any]:
        return connection.execute(
            """SELECT keys.id, keys.telegram_id, keys.server_id, keys.outline_key_id,
                      keys.data_limit_bytes, keys.expires_at,
                      keys.quota_warning_percent, g.campaign_code
               FROM keys LEFT JOIN giveaway_claims g ON g.key_id = keys.id
               WHERE keys.status = 'active'"""
        ).fetchall()

    @staticmethod
    def warning_exists(connection: Any, dedupe_key: str) -> bool:
        return connection.execute(
            "SELECT id FROM notifications WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone() is not None

    @staticmethod
    def insert_warning(
        connection: Any, *, notification_id: str, dedupe_key: str,
        telegram_id: int, text: str, now_text: str,
    ) -> None:
        connection.execute(
            """INSERT INTO notifications
               (id, dedupe_key, telegram_id, kind, text, status,
                next_attempt_at, created_at)
               VALUES (?, ?, ?, 'quota_warning', ?, 'pending', ?, ?)""",
            (notification_id, dedupe_key, telegram_id, text, now_text, now_text),
        )

    @staticmethod
    def update_warning_percent(connection: Any, key_id: Any, percent: int) -> None:
        connection.execute(
            "UPDATE keys SET quota_warning_percent = ? WHERE id = ?",
            (percent, key_id),
        )

    @staticmethod
    def user_usage_rows(connection: Any, telegram_id: int) -> list[Any]:
        return connection.execute(
            """SELECT keys.server_id, keys.outline_key_id, keys.key_type, keys.created_at,
                      keys.expires_at, keys.data_limit_bytes, keys.status,
                      keys.last_usage_bytes, keys.quota_reason, g.campaign_code,
                      (SELECT remote_state FROM key_termination_events e
                       WHERE e.key_id = keys.id ORDER BY e.detected_at DESC LIMIT 1) AS termination_state,
                      (SELECT r.status FROM managed_key_repair_jobs r
                       WHERE r.kind = 'free' AND r.server_id = keys.server_id
                         AND r.local_key_ref = CAST(keys.id AS TEXT)
                       ORDER BY r.created_at DESC LIMIT 1) AS repair_status,
                      (SELECT r.last_error FROM managed_key_repair_jobs r
                       WHERE r.kind = 'free' AND r.server_id = keys.server_id
                         AND r.local_key_ref = CAST(keys.id AS TEXT)
                       ORDER BY r.created_at DESC LIMIT 1) AS repair_reason
               FROM keys LEFT JOIN giveaway_claims g ON g.key_id = keys.id
               WHERE keys.telegram_id = ?
                 AND (keys.status IN ('active', 'revoke_failed') OR keys.quota_reason = 'quota')
               ORDER BY keys.created_at DESC LIMIT 10""",
            (telegram_id,),
        ).fetchall()

    @staticmethod
    def migrated_usage(connection: Any, telegram_id: int) -> Any:
        return connection.execute(
            """SELECT COALESCE(SUM(source_used_bytes), 0) AS used
                 FROM connectivity_migration_jobs
                WHERE telegram_id = ?
                  AND status IN ('source_delete_pending', 'completed')
                  AND source_used_bytes IS NOT NULL""",
            (int(telegram_id),),
        ).fetchone()

    @staticmethod
    def expired_keys(connection: Any, now_text: str) -> list[Any]:
        return connection.execute(
            """SELECT id, telegram_id, server_id, outline_key_id,
                      data_limit_bytes, expires_at FROM keys
               WHERE status IN ('active', 'revoke_failed') AND expires_at <= ?""",
            (now_text,),
        ).fetchall()

    @staticmethod
    def pending_terminations(connection: Any, limit: int) -> list[Any]:
        return connection.execute(
            """SELECT k.id, k.telegram_id, k.server_id, k.outline_key_id, k.data_limit_bytes,
                      k.expires_at, e.reason, e.used_bytes
               FROM keys k JOIN key_termination_events e ON e.key_id = k.id
               WHERE e.remote_state IN ('retrying', 'escalated') AND k.status != 'revoked'
               ORDER BY e.detected_at LIMIT ?""",
            (limit,),
        ).fetchall()

    @staticmethod
    def termination_notices(connection: Any, column: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            f"""SELECT * FROM key_termination_events
                WHERE COALESCE({column}, '') != remote_state
                ORDER BY detected_at LIMIT 50"""
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def mark_termination_notice(connection: Any, column: str, event_id: int, state: str) -> None:
        connection.execute(
            f"UPDATE key_termination_events SET {column} = ? WHERE id = ?",
            (state, event_id),
        )

    @staticmethod
    def termination_summary(connection: Any, limit: int) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM key_termination_events ORDER BY detected_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
