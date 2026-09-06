"""Transaction-neutral persistence for endpoint requests, leases, and cutover."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from commerce_postgres_database import _PostgresConnection


@dataclass(frozen=True)
class MigrationWriteResult:
    rowcount: int


class EndpointMigrationRepository:
    @staticmethod
    def insert_request(
        connection: Any,
        *,
        job_id: str,
        profile_id: str,
        credential_id: str,
        source_endpoint_id: str,
        target_endpoint_id: str,
        source: str,
        target: str,
        source_external_id: str,
        target_external_id: str,
        target_name: str,
        profile_kind: str,
        telegram_id: int,
        quota_bytes: int,
        expires_at: str,
        next_attempt_at: str,
        requested_by: int,
        created_at: str,
        updated_at: str,
    ) -> MigrationWriteResult:
        result = connection.execute(
            """INSERT INTO connectivity_migration_jobs
               (id, profile_id, credential_id, source_endpoint_id, target_endpoint_id,
                source_server_id, target_server_id, source_external_id, target_external_id,
                target_name, profile_kind, telegram_id, quota_bytes, expires_at,
                status, attempts, next_attempt_at, requested_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)""",
            (
                job_id,
                profile_id,
                credential_id,
                source_endpoint_id,
                target_endpoint_id,
                source,
                target,
                source_external_id,
                target_external_id,
                target_name,
                profile_kind,
                telegram_id,
                quota_bytes,
                expires_at,
                next_attempt_at,
                requested_by,
                created_at,
                updated_at,
            ),
        )
        return MigrationWriteResult(int(result.rowcount))

    @staticmethod
    def request_context(
        connection: Any,
        *,
        target: str,
        source: str,
        external_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """SELECT c.credential_id, c.profile_id, c.status AS credential_status,
                      c.external_id, p.profile_kind, p.telegram_id, p.subscription_id,
                      se.outline_server_id AS source_server, se.endpoint_id AS source_endpoint,
                      te.outline_server_id AS target_server, te.endpoint_id AS target_endpoint,
                      te.status AS target_status, te.accepts_new_keys
                 FROM connectivity_credentials c
                 JOIN connectivity_profiles p ON p.profile_id = c.profile_id
                 JOIN connectivity_endpoints se ON se.endpoint_id = c.endpoint_id
                 JOIN connectivity_endpoints te ON te.outline_server_id = ?
                WHERE se.outline_server_id = ? AND c.external_id = ?
                LIMIT 1""",
            (target, source, external_id),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def existing_request(
        connection: Any,
        *,
        credential_id: str,
        target_endpoint_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """SELECT id, status, target_server_id FROM connectivity_migration_jobs
                WHERE credential_id = ? AND target_endpoint_id = ?
                ORDER BY created_at DESC LIMIT 1""",
            (credential_id, target_endpoint_id),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def paid_entitlement(
        connection: Any,
        *,
        subscription_id: str | None,
        source: str,
        external_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """SELECT k.outline_key_id, k.status, k.quota_bytes,
                          s.expires_at, s.status AS subscription_status
                     FROM paid_vpn_keys k JOIN subscriptions s ON s.id = k.subscription_id
                    WHERE k.subscription_id = ? AND k.server_id = ? AND k.outline_key_id = ?""",
            (subscription_id, source, external_id),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def free_entitlement(
        connection: Any,
        *,
        source: str,
        external_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """SELECT outline_key_id, status, data_limit_bytes AS quota_bytes, expires_at
                     FROM keys
                    WHERE server_id = ? AND outline_key_id = ?""",
            (source, external_id),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def recover_stale(
        connection: Any,
        *,
        now_text: str,
        stale_before: str,
    ) -> MigrationWriteResult:
        result = connection.execute(
            """UPDATE connectivity_migration_jobs
                  SET status = CASE WHEN target_access_url_ciphertext IS NOT NULL
                                    THEN 'source_delete_pending' ELSE 'pending' END,
                      locked_at = NULL,
                      updated_at = ?
                WHERE status IN ('creating', 'source_delete_pending') AND locked_at < ?""",
            (now_text, stale_before),
        )
        return MigrationWriteResult(int(result.rowcount))

    @staticmethod
    def mark_claimed(
        connection: Any,
        *,
        locked_at: str,
        updated_at: str,
        job_id: str,
    ) -> MigrationWriteResult:
        result = connection.execute(
            """UPDATE connectivity_migration_jobs
                  SET status = 'creating', attempts = attempts + 1,
                      locked_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('pending', 'failed', 'source_delete_pending')""",
            (locked_at, updated_at, job_id),
        )
        return MigrationWriteResult(int(result.rowcount))

    @staticmethod
    def mark_failed(
        connection: Any,
        *,
        status: str,
        next_attempt_at: str,
        error: str,
        now_text: str,
        job_id: str,
        attempt: int,
    ) -> MigrationWriteResult:
        result = connection.execute(
            """UPDATE connectivity_migration_jobs
                  SET status = ?, next_attempt_at = ?, locked_at = NULL,
                      last_error = ?, updated_at = ?
                WHERE id = ? AND attempts = ? AND status = 'creating'""",
            (status, next_attempt_at, error, now_text, job_id, attempt),
        )
        return MigrationWriteResult(int(result.rowcount))

    @staticmethod
    def complete(
        connection: Any,
        *,
        completed_at: str,
        updated_at: str,
        job_id: str,
        attempt: int,
    ) -> MigrationWriteResult:
        result = connection.execute(
            """UPDATE connectivity_migration_jobs
                  SET status = 'completed', locked_at = NULL,
                      last_error = NULL, completed_at = ?, updated_at = ?
                WHERE id = ? AND attempts = ?
                  AND status IN ('creating', 'source_delete_pending') AND locked_at IS NOT NULL""",
            (completed_at, updated_at, job_id, attempt),
        )
        return MigrationWriteResult(int(result.rowcount))

    @staticmethod
    def defer_source_delete(
        connection: Any,
        *,
        next_attempt_at: str,
        error: str,
        now_text: str,
        job_id: str,
        attempt: int,
    ) -> MigrationWriteResult:
        result = connection.execute(
            """UPDATE connectivity_migration_jobs
                  SET status = 'source_delete_pending', locked_at = NULL,
                      next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE id = ? AND attempts = ?
                  AND status IN ('creating', 'source_delete_pending') AND locked_at IS NOT NULL""",
            (next_attempt_at, error, now_text, job_id, attempt),
        )
        return MigrationWriteResult(int(result.rowcount))

    @staticmethod
    def notify_cutover(
        connection: Any,
        *,
        notification_id: str,
        dedupe_key: str,
        telegram_id: int,
        text: str,
        encrypted: str,
        next_attempt_at: str,
        created_at: str,
    ) -> MigrationWriteResult:
        result = connection.execute(
            """INSERT INTO notifications
               (id, dedupe_key, telegram_id, kind, text, access_url_ciphertext,
                status, next_attempt_at, created_at)
               VALUES (?, ?, ?, 'vpn_migrated', ?, ?, 'pending', ?, ?)
               ON CONFLICT(dedupe_key) DO NOTHING""",
            (
                notification_id,
                dedupe_key,
                telegram_id,
                text,
                encrypted,
                next_attempt_at,
                created_at,
            ),
        )
        return MigrationWriteResult(int(result.rowcount))

    @staticmethod
    def mark_cutover(
        connection: Any,
        *,
        used: int,
        target_id: str,
        encrypted: str,
        next_attempt_at: str,
        updated_at: str,
        job_id: str,
    ) -> MigrationWriteResult:
        result = connection.execute(
            """UPDATE connectivity_migration_jobs
                  SET source_used_bytes = ?, target_external_id = ?,
                      target_access_url_ciphertext = ?, status = 'source_delete_pending',
                      last_error = NULL, next_attempt_at = ?, updated_at = ?
                WHERE id = ?""",
            (used, target_id, encrypted, next_attempt_at, updated_at, job_id),
        )
        return MigrationWriteResult(int(result.rowcount))

    @staticmethod
    def move_paid_key(
        connection: Any,
        *,
        target_server_id: str,
        target_id: str,
        encrypted: str,
        remaining: int,
        subscription_id: str | None,
        source_server_id: str,
        source_external_id: str,
    ) -> MigrationWriteResult:
        result = connection.execute(
            """UPDATE paid_vpn_keys SET server_id = ?, outline_key_id = ?,
                          access_url = ?, quota_bytes = ?
                     WHERE subscription_id = ? AND server_id = ?
                       AND outline_key_id = ? AND status = 'active'""",
            (
                target_server_id,
                target_id,
                encrypted,
                remaining,
                subscription_id,
                source_server_id,
                source_external_id,
            ),
        )
        return MigrationWriteResult(int(result.rowcount))

    @staticmethod
    def next_request(
        connection: Any,
        *,
        now_text: str,
        limit: int,
    ) -> dict[str, Any] | None:
        lock_clause = (
            " FOR UPDATE SKIP LOCKED" if isinstance(connection, _PostgresConnection) else ""
        )
        row = connection.execute(
            """SELECT * FROM connectivity_migration_jobs
                WHERE status IN ('pending', 'failed', 'source_delete_pending')
                  AND locked_at IS NULL AND next_attempt_at <= ?
                ORDER BY created_at LIMIT ?"""
            + lock_clause,
            (now_text, limit),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def attempts(
        connection: Any,
        *,
        job_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT attempts FROM connectivity_migration_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def current_state(
        connection: Any,
        *,
        job_id: str,
    ) -> dict[str, Any] | None:
        lock_clause = " FOR UPDATE" if isinstance(connection, _PostgresConnection) else ""
        row = connection.execute(
            "SELECT status, attempts, locked_at FROM connectivity_migration_jobs WHERE id = ?"
            + lock_clause,
            (job_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def profile_subscription(
        connection: Any,
        *,
        profile_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT subscription_id FROM connectivity_profiles WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def move_free_key(
        connection: Any,
        *,
        target_server_id: str,
        target_id: str,
        remaining: int,
        source_server_id: str,
        source_external_id: str,
    ) -> MigrationWriteResult:
        result = connection.execute(
            """UPDATE keys SET server_id = ?, outline_key_id = ?, data_limit_bytes = ?
                     WHERE server_id = ? AND outline_key_id = ?
                       AND status IN ('active', 'revoke_failed')""",
            (target_server_id, target_id, remaining, source_server_id, source_external_id),
        )
        return MigrationWriteResult(int(result.rowcount))
