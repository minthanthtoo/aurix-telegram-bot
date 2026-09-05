"""Capacity read model for entitlement server allocation."""

from __future__ import annotations

from typing import Any


class EntitlementAllocationRepository:
    """SQL boundary for allocation evidence and route-decision persistence."""

    @staticmethod
    def fleet_size(connection: Any) -> int:
        return int(
            connection.execute("SELECT COUNT(*) AS n FROM outline_servers").fetchone()["n"]
        )

    @staticmethod
    def eligible_servers(connection: Any, fresh_after: str) -> list[Any]:
        return connection.execute(
            """SELECT * FROM outline_servers
               WHERE enabled = 1 AND lifecycle_state = 'active'
                 AND health_status = 'healthy'
                 AND last_synced_at IS NOT NULL AND last_synced_at >= ?
               ORDER BY server_id""",
            (fresh_after,),
        ).fetchall()

    @staticmethod
    def has_tier_allocations(connection: Any, tier_code: str) -> bool:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM server_tier_allocations WHERE tier_code = ?",
            (tier_code,),
        ).fetchone()
        return int(row["n"]) > 0

    @staticmethod
    def route_health(connection: Any, server_id: str) -> Any:
        return connection.execute(
            """SELECT status, score, last_observed_at
               FROM route_health_snapshots WHERE server_id = ?""",
            (server_id,),
        ).fetchone()

    @staticmethod
    def tier_allocation(connection: Any, server_id: str, tier_code: str) -> Any:
        return connection.execute(
            """SELECT slot_limit FROM server_tier_allocations
               WHERE server_id = ? AND tier_code = ?""",
            (server_id, tier_code),
        ).fetchone()

    @staticmethod
    def active_tier_count(connection: Any, server_id: str, tier_code: str) -> int:
        row = connection.execute(
            """SELECT COUNT(*) AS n FROM keys k
               LEFT JOIN giveaway_claims g ON g.key_id = k.id
               WHERE k.server_id = ? AND k.status IN ('active', 'revoke_failed')
                 AND CASE
                   WHEN ? = 'FREE300MB' THEN k.key_type = 'daily_free'
                   WHEN ? = 'FREE3GB' THEN k.key_type = 'monthly_trial' AND g.key_id IS NULL
                   ELSE g.key_id IS NOT NULL END""",
            (server_id, tier_code, tier_code),
        ).fetchone()
        return int(row["n"])

    @staticmethod
    def pending_tier_count(connection: Any, server_id: str, kind: str) -> int:
        row = connection.execute(
            """SELECT COUNT(*) AS n FROM free_provisioning_intents
               WHERE server_id = ? AND kind = ? AND status IN ('pending', 'running')""",
            (server_id, kind),
        ).fetchone()
        return int(row["n"])

    @staticmethod
    def traffic_commitment(connection: Any, server_id: str) -> tuple[int, int]:
        free = connection.execute(
            """SELECT COALESCE(SUM(data_limit_bytes), 0) AS n FROM keys
               WHERE server_id = ? AND status IN ('active', 'revoke_failed')""",
            (server_id,),
        ).fetchone()["n"]
        paid = connection.execute(
            """SELECT COALESCE(SUM(COALESCE(quota_bytes, 0)), 0) AS n
               FROM subscriptions WHERE server_id = ? AND status IN ('pending', 'active')""",
            (server_id,),
        ).fetchone()["n"]
        return int(free or 0), int(paid or 0)

    @staticmethod
    def record_decision(
        connection: Any,
        *,
        decision_id: str,
        telegram_id: int | None,
        server_id: str,
        score: float | None,
        evidence_json: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO route_decisions
               (decision_id, telegram_id, entitlement_ref, requested_region,
                selected_server_id, decision_mode, score, evidence_json, created_at)
               VALUES (?, ?, NULL, NULL, ?, 'automatic', ?, ?, ?)""",
            (decision_id, telegram_id, server_id, score, evidence_json, created_at),
        )
