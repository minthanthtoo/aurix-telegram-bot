"""Persistence boundary for remote inventory reconciliation."""

from __future__ import annotations

from typing import Any


class InventoryReconciliationRepository:
    """SQL operations that participate in a caller-owned reconciliation transaction."""

    @staticmethod
    def enabled_server_ids(connection: Any) -> list[str]:
        rows = connection.execute(
            "SELECT server_id FROM outline_servers WHERE enabled = 1 ORDER BY server_id"
        ).fetchall()
        return [str(row["server_id"]) for row in rows]

    @staticmethod
    def remote_key_ledger(connection: Any, server_id: str) -> dict[str, dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM outline_remote_keys WHERE server_id = ?", (server_id,)
        ).fetchall()
        return {str(row["outline_key_id"]): dict(row) for row in rows}

    @staticmethod
    def managed_key_ids(
        connection: Any, server_id: str, *, include_free_keys: bool
    ) -> set[str]:
        rows = connection.execute(
            """SELECT outline_key_id FROM paid_vpn_keys
               WHERE server_id = ? AND outline_key_id IS NOT NULL""",
            (server_id,),
        ).fetchall()
        managed = {str(row["outline_key_id"]) for row in rows}
        if include_free_keys:
            rows = connection.execute(
                """SELECT outline_key_id FROM keys
                   WHERE server_id = ? AND outline_key_id IS NOT NULL""",
                (server_id,),
            ).fetchall()
            managed.update(str(row["outline_key_id"]) for row in rows)
        return managed

    @staticmethod
    def mark_present_keys_missing(connection: Any, server_id: str) -> None:
        connection.execute(
            """UPDATE outline_remote_keys SET status = 'missing'
               WHERE server_id = ? AND status = 'present'""",
            (server_id,),
        )

    @staticmethod
    def upsert_present_key(
        connection: Any,
        *,
        server_id: str,
        outline_key_id: str,
        remote_name: str | None,
        managed: bool,
        observed_at: str,
        usage_bytes: int | None,
    ) -> None:
        connection.execute(
            """INSERT INTO outline_remote_keys
               (server_id, outline_key_id, remote_name, managed, status,
                first_seen_at, last_seen_at, last_usage_bytes,
                missing_observation_count, missing_since_at, last_missing_at)
               VALUES (?, ?, ?, ?, 'present', ?, ?, ?, 0, NULL, NULL)
               ON CONFLICT(server_id, outline_key_id) DO UPDATE SET
                 remote_name = excluded.remote_name, managed = excluded.managed,
                 status = 'present', last_seen_at = excluded.last_seen_at,
                 last_usage_bytes = COALESCE(
                     excluded.last_usage_bytes, outline_remote_keys.last_usage_bytes),
                 missing_observation_count = 0, missing_since_at = NULL,
                 last_missing_at = NULL""",
            (
                server_id,
                outline_key_id,
                remote_name,
                int(managed),
                observed_at,
                observed_at,
                usage_bytes,
            ),
        )

    @staticmethod
    def update_managed_usage(
        connection: Any,
        *,
        kind: str,
        local_id: str,
        server_id: str,
        usage_bytes: int,
        observed_at: str,
    ) -> None:
        parameters = (usage_bytes, observed_at, local_id, server_id)
        if kind == "paid":
            connection.execute(
                """UPDATE paid_vpn_keys
                      SET last_usage_bytes = ?, last_usage_observed_at = ?
                    WHERE id = ? AND server_id = ?""",
                parameters,
            )
        else:
            connection.execute(
                """UPDATE keys
                      SET last_usage_bytes = ?, last_usage_observed_at = ?
                    WHERE id = ? AND server_id = ?""",
                parameters,
            )

    @staticmethod
    def upsert_missing_key(
        connection: Any,
        *,
        server_id: str,
        outline_key_id: str,
        observed_at: str,
        usage_bytes: int | None,
        observation_count: int,
        missing_since: str,
    ) -> None:
        connection.execute(
            """INSERT INTO outline_remote_keys
               (server_id, outline_key_id, remote_name, managed, status,
                first_seen_at, last_seen_at, last_usage_bytes,
                missing_observation_count, missing_since_at, last_missing_at)
               VALUES (?, ?, NULL, 1, 'missing', ?, ?, ?, ?, ?, ?)
               ON CONFLICT(server_id, outline_key_id) DO UPDATE SET
                 managed = 1, status = 'missing',
                 last_usage_bytes = COALESCE(
                     excluded.last_usage_bytes, outline_remote_keys.last_usage_bytes),
                 missing_observation_count = excluded.missing_observation_count,
                 missing_since_at = excluded.missing_since_at,
                 last_missing_at = excluded.last_missing_at""",
            (
                server_id,
                outline_key_id,
                observed_at,
                observed_at,
                usage_bytes,
                observation_count,
                missing_since,
                observed_at,
            ),
        )

    @staticmethod
    def unreviewed_orphan_count(connection: Any, server_id: str) -> int:
        row = connection.execute(
            """SELECT COUNT(*) AS n FROM outline_remote_keys
               WHERE server_id = ? AND status = 'present' AND managed = 0
                 AND COALESCE(
                       (SELECT review_state FROM outline_remote_key_reviews r
                         WHERE r.server_id = outline_remote_keys.server_id
                           AND r.outline_key_id = outline_remote_keys.outline_key_id),
                       'unreviewed') = 'unreviewed'""",
            (server_id,),
        ).fetchone()
        return int(row["n"])

    @staticmethod
    def update_server_metrics(
        connection: Any,
        *,
        server_id: str,
        remote_key_count: int,
        remote_transfer_bytes: int,
        current_bandwidth_bytes: int | None,
        peak_bandwidth_bytes: int | None,
        telemetry_experimental: bool,
        remote_orphan_key_count: int,
    ) -> None:
        connection.execute(
            """UPDATE outline_servers SET remote_key_count = ?, remote_transfer_bytes = ?,
                      current_bandwidth_bytes = ?, peak_bandwidth_bytes = ?,
                      telemetry_experimental = ?, remote_orphan_key_count = ?
                   WHERE server_id = ?""",
            (
                remote_key_count,
                remote_transfer_bytes,
                current_bandwidth_bytes,
                peak_bandwidth_bytes,
                int(telemetry_experimental),
                remote_orphan_key_count,
                server_id,
            ),
        )

    @staticmethod
    def lifecycle_state(connection: Any, server_id: str) -> str:
        row = connection.execute(
            "SELECT lifecycle_state FROM outline_servers WHERE server_id = ?",
            (server_id,),
        ).fetchone()
        return str(row["lifecycle_state"] if row else "active")
