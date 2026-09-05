"""Persistence boundary shared by managed-key repair workflows."""

from __future__ import annotations

from typing import Any

from commerce_models import _new_id


class ManagedRepairRepository:
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
    def recent_snapshots(connection: Any, server_id: str, cutoff: str) -> set[tuple[str, str]]:
        rows = connection.execute(
            """SELECT entitlement_kind, local_key_ref, MAX(observed_at) AS observed_at
                 FROM usage_snapshots
                WHERE server_id = ? AND observed_at >= ?
                GROUP BY entitlement_kind, local_key_ref""",
            (server_id, cutoff),
        ).fetchall()
        return {(str(row["entitlement_kind"]), str(row["local_key_ref"])) for row in rows}

    @staticmethod
    def insert_snapshot(connection: Any, values: tuple[Any, ...]) -> None:
        connection.execute(
            """INSERT INTO usage_snapshots
               (id, telegram_id, entitlement_kind, local_key_ref, server_id,
                outline_key_id, observed_at, used_bytes, quota_bytes, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'outline_metrics')
               ON CONFLICT(server_id, entitlement_kind, local_key_ref, observed_at)
               DO NOTHING""",
            values,
        )

    @staticmethod
    def endpoint(connection: Any, server_id: str) -> Any:
        return connection.execute(
            "SELECT endpoint_id FROM connectivity_endpoints WHERE outline_server_id = ?",
            (server_id,),
        ).fetchone()

    @staticmethod
    def aggregate_paid(connection: Any, subscription_id: str, server_id: str, external_id: str, observed_at: str) -> None:
        connection.execute(
            """UPDATE paid_vpn_keys SET quota_reason = 'aggregate_quota'
                WHERE subscription_id = ? AND server_id = ? AND outline_key_id = ?
                  AND status = 'active'""",
            (subscription_id, server_id, external_id),
        )
        connection.execute(
            "UPDATE subscriptions SET status = 'revoked' WHERE id = ? AND status = 'active'",
            (subscription_id,),
        )
        connection.execute(
            """INSERT INTO provisioning_jobs
               (id, subscription_id, operation, status, next_attempt_at, created_at)
               VALUES (?, ?, 'revoke', 'pending', ?, ?)
               ON CONFLICT(subscription_id, operation) DO NOTHING""",
            (_new_id(), subscription_id, observed_at, observed_at),
        )

    @staticmethod
    def aggregate_free(
        connection: Any, *, local_id: Any, server_id: str, external_id: str,
        used_bytes: int, quota_bytes: int, observed_at: str,
    ) -> None:
        connection.execute(
            """UPDATE keys SET quota_reason = 'quota'
                WHERE id = ? AND server_id = ? AND outline_key_id = ?
                  AND status = 'active'""",
            (local_id, server_id, external_id),
        )
        if ManagedRepairRepository.table_exists(connection, "key_termination_events"):
            connection.execute(
                """INSERT INTO key_termination_events
                   (key_id, telegram_id, outline_key_id, reason, used_bytes,
                    quota_bytes, expires_at, detected_at, remote_state)
                   SELECT id, telegram_id, outline_key_id, 'quota', ?, ?,
                          expires_at, ?, 'retrying'
                     FROM keys
                    WHERE id = ? AND server_id = ? AND outline_key_id = ?
                   ON CONFLICT(key_id, reason) DO UPDATE SET
                     used_bytes = COALESCE(excluded.used_bytes, key_termination_events.used_bytes),
                     quota_bytes = excluded.quota_bytes""",
                (max(0, int(used_bytes)), max(0, int(quota_bytes)), observed_at,
                 local_id, server_id, external_id),
            )

    @staticmethod
    def managed_paid_rows(connection: Any, server_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT 'paid' AS kind, CAST(k.id AS TEXT) AS local_key_ref,
                      k.id AS local_id, k.telegram_id, k.outline_key_id AS source_external_id,
                      k.quota_bytes, k.last_usage_bytes, k.last_usage_observed_at,
                      k.created_at, s.expires_at, s.plan_name, s.plan_code, s.duration_days, u.username
                 FROM paid_vpn_keys k JOIN subscriptions s ON s.id = k.subscription_id
                 JOIN users u ON u.telegram_id = k.telegram_id
                WHERE k.server_id = ? AND k.status = 'active' AND s.status = 'active'
                  AND k.quota_bytes IS NOT NULL AND COALESCE(k.quota_reason, '') = ''""",
            (server_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def managed_free_rows(connection: Any, server_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT 'free' AS kind, CAST(k.id AS TEXT) AS local_key_ref,
                      k.id AS local_id, k.telegram_id, k.outline_key_id AS source_external_id,
                      k.data_limit_bytes AS quota_bytes, k.last_usage_bytes,
                      k.last_usage_observed_at, k.created_at, k.expires_at, k.key_type, u.username,
                      g.campaign_code,
                      COALESCE(c.duration_days,
                               CASE WHEN k.key_type = 'monthly_trial' THEN 30 ELSE 1 END) AS duration_days
                 FROM keys k JOIN users u ON u.telegram_id = k.telegram_id
                 LEFT JOIN giveaway_claims g ON g.key_id = k.id
                 LEFT JOIN giveaway_campaigns c ON c.code = g.campaign_code
                WHERE k.server_id = ? AND k.status = 'active'
                  AND COALESCE(k.quota_reason, '') = '' AND k.data_limit_bytes IS NOT NULL""",
            (server_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def usage_snapshots(connection: Any, server_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT entitlement_kind, local_key_ref, used_bytes, observed_at
                 FROM usage_snapshots WHERE server_id = ? ORDER BY observed_at DESC""",
            (server_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def existing_repair(connection: Any, server_id: str, kind: str, local_ref: str) -> Any:
        return connection.execute(
            """SELECT * FROM managed_key_repair_jobs
               WHERE server_id = ? AND kind = ? AND local_key_ref = ?""",
            (server_id, kind, local_ref),
        ).fetchone()

    @staticmethod
    def insert_repair(connection: Any, values: tuple[Any, ...]) -> None:
        connection.execute(
            """INSERT INTO managed_key_repair_jobs
               (id, kind, server_id, telegram_id, local_key_ref,
                source_external_id, target_external_id, key_name, quota_bytes,
                used_bytes, expires_at, status, attempts, next_attempt_at,
                last_error, observed_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
            values,
        )

    @staticmethod
    def reopen_repair(connection: Any, values: tuple[Any, ...]) -> None:
        connection.execute(
            """UPDATE managed_key_repair_jobs
               SET source_external_id = ?, target_external_id = ?, key_name = ?,
                   quota_bytes = ?, used_bytes = ?, expires_at = ?, status = ?,
                   attempts = 0, next_attempt_at = ?, locked_at = NULL,
                   last_error = ?, observed_at = ?, completed_at = NULL
             WHERE id = ?""",
            values,
        )

    @staticmethod
    def repair_alert_exists(connection: Any, repair_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM notifications WHERE dedupe_key LIKE ? LIMIT 1",
            (f"staff:key_repairs:{repair_id}:%",),
        ).fetchone() is not None

    @staticmethod
    def free_key_ref(connection: Any, server_id: str, external_id: str) -> Any:
        return connection.execute(
            """SELECT id FROM keys WHERE server_id = ? AND outline_key_id = ?
                ORDER BY id DESC LIMIT 1""",
            (server_id, external_id),
        ).fetchone()

    @staticmethod
    def active_credential(connection: Any, server_id: str, external_id: str) -> Any:
        return connection.execute(
            """SELECT c.credential_id, c.endpoint_id
                 FROM connectivity_credentials c
                 JOIN connectivity_endpoints e ON e.endpoint_id = c.endpoint_id
                WHERE e.outline_server_id = ? AND c.external_id = ?
                  AND c.status = 'active'""",
            (server_id, external_id),
        ).fetchone()

    @staticmethod
    def reset_stale_repairs(connection: Any, stale_before: str) -> None:
        connection.execute(
            """UPDATE managed_key_repair_jobs
                  SET status = 'pending', locked_at = NULL
                WHERE status = 'running' AND locked_at < ?""",
            (stale_before,),
        )

    @staticmethod
    def claim_repair(connection: Any, *, max_attempts: int, now_text: str) -> tuple[Any, bool]:
        lock_clause = " FOR UPDATE SKIP LOCKED" if connection.__class__.__name__ == "_PostgresConnection" else ""
        row = connection.execute(
            """SELECT * FROM managed_key_repair_jobs
                WHERE status IN ('pending', 'failed')
                  AND attempts < ? AND next_attempt_at <= ?
                ORDER BY created_at LIMIT 1""" + lock_clause,
            (max_attempts, now_text),
        ).fetchone()
        if row is None:
            return None, False
        updated = connection.execute(
            """UPDATE managed_key_repair_jobs
                  SET status = 'running', attempts = attempts + 1, locked_at = ?
                WHERE id = ? AND status IN ('pending', 'failed')""",
            (now_text, row["id"]),
        )
        return row, int(getattr(updated, "rowcount", 0) or 0) == 1

    @staticmethod
    def repair_attempts(connection: Any, job_id: str) -> Any:
        return connection.execute(
            "SELECT attempts FROM managed_key_repair_jobs WHERE id = ?", (job_id,)
        ).fetchone()

    @staticmethod
    def update_failed(connection: Any, *, job_id: str, status: str, next_attempt_at: str, error: str) -> None:
        connection.execute(
            """UPDATE managed_key_repair_jobs
                  SET status = ?, next_attempt_at = ?, locked_at = NULL, last_error = ?
                WHERE id = ?""",
            (status, next_attempt_at, error, job_id),
        )

    @staticmethod
    def mark_manual(connection: Any, job_id: str, reason: str) -> bool:
        changed = connection.execute(
            """UPDATE managed_key_repair_jobs
                  SET status = 'manual', locked_at = NULL,
                      next_attempt_at = '9999-12-31T00:00:00+00:00', last_error = ?
                WHERE id = ? AND status = 'running'""",
            (reason, job_id),
        )
        return int(getattr(changed, "rowcount", 0) or 0) == 1

    @staticmethod
    def repair_entitlement(connection: Any, *, kind: str, local_ref: str, server_id: str) -> Any:
        if kind == "paid":
            return connection.execute(
                """SELECT k.*, s.status AS subscription_status, s.expires_at,
                          s.plan_name, s.plan_code, s.duration_days, u.username
                     FROM paid_vpn_keys k JOIN subscriptions s ON s.id = k.subscription_id
                     JOIN users u ON u.telegram_id = k.telegram_id
                    WHERE CAST(k.id AS TEXT) = ? AND k.server_id = ?""",
                (local_ref, server_id),
            ).fetchone()
        return connection.execute(
            """SELECT k.*, u.username, g.campaign_code,
                      COALESCE(c.duration_days,
                               CASE WHEN k.key_type = 'monthly_trial' THEN 30 ELSE 1 END) AS duration_days
                 FROM keys k JOIN users u ON u.telegram_id = k.telegram_id
                 LEFT JOIN giveaway_claims g ON g.key_id = k.id
                 LEFT JOIN giveaway_campaigns c ON c.code = g.campaign_code
                WHERE CAST(k.id AS TEXT) = ? AND k.server_id = ?""",
            (local_ref, server_id),
        ).fetchone()

    @staticmethod
    def current_repair(connection: Any, job_id: str) -> Any:
        return connection.execute(
            "SELECT status FROM managed_key_repair_jobs WHERE id = ?", (job_id,)
        ).fetchone()

    @staticmethod
    def update_paid(connection: Any, values: tuple[Any, ...]) -> bool:
        changed = connection.execute(
            """UPDATE paid_vpn_keys
                  SET outline_key_id = ?, access_url = ?, quota_bytes = ?,
                      last_usage_bytes = 0, last_usage_observed_at = ?, quota_reason = NULL
                WHERE CAST(id AS TEXT) = ? AND server_id = ?
                  AND outline_key_id = ? AND status = 'active'""",
            values,
        )
        return int(getattr(changed, "rowcount", 0) or 0) == 1

    @staticmethod
    def update_free(connection: Any, values: tuple[Any, ...]) -> bool:
        changed = connection.execute(
            """UPDATE keys
                  SET outline_key_id = ?, data_limit_bytes = ?,
                      last_usage_bytes = 0, quota_reason = NULL
                WHERE CAST(id AS TEXT) = ? AND server_id = ?
                  AND outline_key_id = ? AND status = 'active'""",
            values,
        )
        return int(getattr(changed, "rowcount", 0) or 0) == 1

    @staticmethod
    def update_free_intent(connection: Any, remote_id: str, key_id: int, server_id: str) -> None:
        connection.execute(
            "UPDATE free_provisioning_intents SET outline_key_id = ? WHERE key_id = ? AND server_id = ?",
            (remote_id, key_id, server_id),
        )

    @staticmethod
    def mark_remote_missing(connection: Any, server_id: str, external_id: str) -> None:
        connection.execute(
            """UPDATE outline_remote_keys
                  SET status = 'missing', managed = 1,
                      missing_observation_count = 0, missing_since_at = NULL, last_missing_at = NULL
                WHERE server_id = ? AND outline_key_id = ?""",
            (server_id, external_id),
        )

    @staticmethod
    def upsert_remote_present(connection: Any, server_id: str, external_id: str, name: str, now_text: str) -> None:
        connection.execute(
            """INSERT INTO outline_remote_keys
               (server_id, outline_key_id, remote_name, managed, status,
                first_seen_at, last_seen_at, last_usage_bytes,
                missing_observation_count, missing_since_at, last_missing_at)
               VALUES (?, ?, ?, 1, 'present', ?, ?, 0, 0, NULL, NULL)
               ON CONFLICT(server_id, outline_key_id) DO UPDATE SET
                 remote_name = excluded.remote_name, managed = 1, status = 'present',
                 last_seen_at = excluded.last_seen_at, last_usage_bytes = excluded.last_usage_bytes,
                 missing_observation_count = 0, missing_since_at = NULL, last_missing_at = NULL""",
            (server_id, external_id, name, now_text, now_text),
        )

    @staticmethod
    def complete_repair(connection: Any, job_id: str, remote_id: str, remaining: int, usage: int, now_text: str) -> None:
        connection.execute(
            """UPDATE managed_key_repair_jobs
                  SET source_external_id = ?, target_external_id = ?,
                      quota_bytes = ?, used_bytes = ?, status = 'done',
                      locked_at = NULL, last_error = NULL, completed_at = ?
                WHERE id = ? AND status = 'running'""",
            (remote_id, remote_id, remaining, usage, now_text, job_id),
        )

    @staticmethod
    def queue_repaired_notification(
        connection: Any, *, notification_id: str, dedupe_key: str, telegram_id: int,
        text: str, encrypted: str, now_text: str,
    ) -> None:
        connection.execute(
            """INSERT INTO notifications
               (id, dedupe_key, telegram_id, kind, text, access_url_ciphertext,
                status, next_attempt_at, created_at)
               VALUES (?, ?, ?, 'vpn_key_repaired', ?, ?, 'pending', ?, ?)
               ON CONFLICT(dedupe_key) DO NOTHING""",
            (notification_id, dedupe_key, telegram_id, text, encrypted, now_text, now_text),
        )

    @staticmethod
    def mark_converged(connection: Any, job_id: str, now_text: str) -> None:
        connection.execute(
            """UPDATE managed_key_repair_jobs
                  SET status = 'done', locked_at = NULL, last_error = NULL, completed_at = ?
                WHERE id = ? AND status = 'running'""",
            (now_text, job_id),
        )

    @staticmethod
    def mark_remote_present(
        connection: Any, *, name: str | None, now_text: str, server_id: str, external_id: str
    ) -> None:
        connection.execute(
            """UPDATE outline_remote_keys
                  SET status = 'present', managed = 1,
                      remote_name = COALESCE(?, remote_name),
                      missing_observation_count = 0, missing_since_at = NULL,
                      last_missing_at = NULL, last_seen_at = ?
                WHERE server_id = ? AND outline_key_id = ?""",
            (name, now_text, server_id, external_id),
        )
