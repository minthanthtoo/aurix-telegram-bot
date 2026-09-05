"""Persistence boundary for customer-facing commerce reads and receipt policy."""

from __future__ import annotations

from typing import Any


class CustomerRepository:
    """Keep customer projections and receipt-policy writes out of use cases."""

    @staticmethod
    def user_usage(connection: Any, telegram_id: int) -> list[Any]:
        return connection.execute(
            """SELECT s.plan_code, s.plan_name, s.status AS subscription_status, s.expires_at, s.starts_at,
                      k.server_id, os.label AS server_label, os.health_status AS server_health_status,
                      k.outline_key_id, k.quota_bytes, k.status,
                      k.last_usage_bytes, k.quota_reason, k.created_at,
                      (SELECT r.status FROM managed_key_repair_jobs r
                       WHERE r.kind = 'paid' AND r.server_id = k.server_id
                         AND r.local_key_ref = CAST(k.id AS TEXT)
                       ORDER BY r.created_at DESC LIMIT 1) AS repair_status,
                      (SELECT r.last_error FROM managed_key_repair_jobs r
                       WHERE r.kind = 'paid' AND r.server_id = k.server_id
                         AND r.local_key_ref = CAST(k.id AS TEXT)
                       ORDER BY r.created_at DESC LIMIT 1) AS repair_reason,
                      (SELECT j.status FROM provisioning_jobs j WHERE j.subscription_id = s.id
                       AND j.operation = 'revoke' LIMIT 1) AS revocation_status
               FROM subscriptions s
               JOIN paid_vpn_keys k ON k.subscription_id = s.id
               LEFT JOIN outline_servers os ON os.server_id = k.server_id
               WHERE s.telegram_id = ?
                 AND (k.status IN ('active', 'revoke_failed') OR k.quota_reason = 'quota')
               ORDER BY k.created_at DESC LIMIT 10""",
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
    def table_exists(connection: Any, name: str) -> bool:
        if connection.__class__.__name__ == "_PostgresConnection":
            row = connection.execute(
                "SELECT to_regclass(?) AS table_name", (f"public.{name}",)
            ).fetchone()
            return bool(row and row["table_name"])
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
            ).fetchone()
            is not None
        )

    @staticmethod
    def usage_snapshot_table_available(connection: Any) -> bool:
        return CustomerRepository.table_exists(connection, "usage_snapshots")

    @staticmethod
    def delete_usage_snapshots(connection: Any, cutoff: str) -> int:
        deleted = connection.execute(
            "DELETE FROM usage_snapshots WHERE observed_at < ?", (cutoff,)
        )
        return max(0, int(getattr(deleted, "rowcount", 0) or 0))

    @staticmethod
    def user_vpns(connection: Any, telegram_id: int, limit: int) -> list[Any]:
        return connection.execute(
            """SELECT s.id AS subscription_id, s.plan_code, s.plan_name, s.status,
                      s.expires_at, s.starts_at,
                      COALESCE(k.server_id, s.server_id) AS server_id,
                      os.label AS server_label, os.health_status AS server_health_status,
                      os.last_synced_at AS server_last_synced_at,
                      k.outline_key_id, k.access_url,
                      COALESCE(k.quota_bytes, s.quota_bytes) AS quota_bytes,
                      k.status AS key_status, k.created_at, k.quota_reason,
                      (SELECT r.status FROM managed_key_repair_jobs r
                       WHERE r.kind = 'paid'
                         AND r.server_id = COALESCE(k.server_id, s.server_id)
                         AND r.local_key_ref = CAST(k.id AS TEXT)
                       ORDER BY r.created_at DESC LIMIT 1) AS repair_status,
                      (SELECT r.last_error FROM managed_key_repair_jobs r
                       WHERE r.kind = 'paid'
                         AND r.server_id = COALESCE(k.server_id, s.server_id)
                         AND r.local_key_ref = CAST(k.id AS TEXT)
                       ORDER BY r.created_at DESC LIMIT 1) AS repair_reason
               FROM subscriptions s
               LEFT JOIN paid_vpn_keys k ON k.subscription_id = s.id
               LEFT JOIN outline_servers os
                 ON os.server_id = COALESCE(k.server_id, s.server_id)
               WHERE s.telegram_id = ? AND s.status IN ('pending', 'active', 'expired', 'revoked')
               ORDER BY CASE s.status WHEN 'active' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                        s.starts_at DESC LIMIT ?""",
            (telegram_id, limit),
        ).fetchall()

    @staticmethod
    def user_vpn_detail(connection: Any, telegram_id: int, subscription_id: str) -> Any:
        return connection.execute(
            """SELECT s.id AS subscription_id, s.plan_code, s.plan_name, s.status,
                      s.expires_at, s.starts_at,
                      COALESCE(k.server_id, s.server_id) AS server_id,
                      os.label AS server_label, os.health_status AS server_health_status,
                      os.last_synced_at AS server_last_synced_at,
                      k.outline_key_id, k.access_url,
                      COALESCE(k.quota_bytes, s.quota_bytes) AS quota_bytes,
                      k.status AS key_status, k.created_at, k.quota_reason,
                      k.last_usage_bytes, k.last_usage_observed_at,
                      (SELECT r.status FROM managed_key_repair_jobs r
                       WHERE r.kind = 'paid'
                         AND r.server_id = COALESCE(k.server_id, s.server_id)
                         AND r.local_key_ref = CAST(k.id AS TEXT)
                       ORDER BY r.created_at DESC LIMIT 1) AS repair_status,
                      (SELECT r.last_error FROM managed_key_repair_jobs r
                       WHERE r.kind = 'paid'
                         AND r.server_id = COALESCE(k.server_id, s.server_id)
                         AND r.local_key_ref = CAST(k.id AS TEXT)
                       ORDER BY r.created_at DESC LIMIT 1) AS repair_reason
               FROM subscriptions s
               LEFT JOIN paid_vpn_keys k ON k.subscription_id = s.id
               LEFT JOIN outline_servers os
                 ON os.server_id = COALESCE(k.server_id, s.server_id)
               WHERE s.telegram_id = ? AND s.id = ?""",
            (telegram_id, subscription_id),
        ).fetchone()

    @staticmethod
    def receipt_policy(connection: Any) -> Any:
        return connection.execute(
            "SELECT * FROM receipt_verification_policy WHERE id = 1"
        ).fetchone()

    @staticmethod
    def receipt_policy_version(connection: Any) -> Any:
        return connection.execute(
            "SELECT mode, version FROM receipt_verification_policy WHERE id = 1"
        ).fetchone()

    @staticmethod
    def update_receipt_mode(
        connection: Any,
        mode: str,
        admin_id: int,
        now_text: str,
        reason: str,
    ) -> None:
        connection.execute(
            """UPDATE receipt_verification_policy
               SET mode = ?, version = version + 1, updated_by = ?,
                   updated_at = ?, change_reason = ? WHERE id = 1""",
            (mode, int(admin_id), now_text, str(reason)[:500]),
        )
