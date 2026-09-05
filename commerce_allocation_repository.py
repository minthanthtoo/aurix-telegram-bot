"""Persistence boundary for server and plan allocation policy."""

from __future__ import annotations

from typing import Any


class CommerceAllocationRepository:
    """SQL reads/writes for capacity policy, selection, and availability."""

    @staticmethod
    def update_server_capacity(connection: Any, **values: Any) -> Any:
        return connection.execute(
            """UPDATE outline_servers SET max_keys = ?, reserved_keys = ?,
                      monthly_traffic_bytes = ?, updated_at = ? WHERE server_id = ?""",
            (
                values["max_keys"], values["reserved_keys"],
                values["monthly_traffic_bytes"], values["now_text"], values["server_id"],
            ),
        )

    @staticmethod
    def enabled_server(connection: Any, server_id: str) -> Any:
        return connection.execute(
            "SELECT 1 FROM outline_servers WHERE server_id = ? AND enabled = 1",
            (server_id,),
        ).fetchone()

    @staticmethod
    def plan(connection: Any, plan_code: str, *, active_only: bool = False) -> Any:
        suffix = " AND active = 1" if active_only else ""
        return connection.execute(
            "SELECT quota_bytes FROM plans WHERE code = ?" + suffix, (plan_code,)
        ).fetchone()

    @staticmethod
    def upsert_plan_allocation(connection: Any, **values: Any) -> None:
        connection.execute(
            """INSERT INTO server_plan_allocations
               (server_id, plan_code, slot_limit, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(server_id, plan_code) DO UPDATE SET
                 slot_limit = excluded.slot_limit, updated_at = excluded.updated_at""",
            (values["server_id"], values["plan_code"], values["slot_limit"], values["now_text"]),
        )

    @staticmethod
    def upsert_tier_allocation(connection: Any, **values: Any) -> None:
        connection.execute(
            """INSERT INTO server_tier_allocations
               (server_id, tier_code, slot_limit, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(server_id, tier_code) DO UPDATE SET
                 slot_limit = excluded.slot_limit, updated_at = excluded.updated_at""",
            (values["server_id"], values["tier_code"], values["slot_limit"], values["now_text"]),
        )

    @staticmethod
    def allocation_capacity(connection: Any, server_id: str) -> tuple[Any, int, int]:
        server = connection.execute(
            "SELECT max_keys, reserved_keys FROM outline_servers WHERE server_id = ?",
            (server_id,),
        ).fetchone()
        plan_total = int(
            connection.execute(
                "SELECT COALESCE(SUM(slot_limit), 0) AS n FROM server_plan_allocations WHERE server_id = ?",
                (server_id,),
            ).fetchone()["n"] or 0
        )
        tier_total = int(
            connection.execute(
                "SELECT COALESCE(SUM(slot_limit), 0) AS n FROM server_tier_allocations WHERE server_id = ?",
                (server_id,),
            ).fetchone()["n"] or 0
        )
        return server, plan_total, tier_total

    @staticmethod
    def policy_state(connection: Any, server_id: str) -> tuple[Any, dict[str, int], dict[str, int]]:
        server = connection.execute(
            """SELECT max_keys, reserved_keys, monthly_traffic_bytes
               FROM outline_servers WHERE server_id = ? AND enabled = 1""",
            (server_id,),
        ).fetchone()
        plans = {
            str(row["plan_code"]): int(row["slot_limit"] or 0)
            for row in connection.execute(
                "SELECT plan_code, slot_limit FROM server_plan_allocations WHERE server_id = ?",
                (server_id,),
            ).fetchall()
            if int(row["slot_limit"] or 0) > 0
        }
        tiers = {
            str(row["tier_code"]): int(row["slot_limit"] or 0)
            for row in connection.execute(
                "SELECT tier_code, slot_limit FROM server_tier_allocations WHERE server_id = ?",
                (server_id,),
            ).fetchall()
            if int(row["slot_limit"] or 0) > 0
        }
        return server, plans, tiers

    @staticmethod
    def clear_policy(connection: Any, server_id: str, now_text: str) -> None:
        connection.execute(
            "UPDATE server_plan_allocations SET slot_limit = 0, updated_at = ? WHERE server_id = ?",
            (now_text, server_id),
        )
        connection.execute(
            "UPDATE server_tier_allocations SET slot_limit = 0, updated_at = ? WHERE server_id = ?",
            (now_text, server_id),
        )

    @staticmethod
    def write_policy_values(
        connection: Any,
        *,
        server_id: str,
        max_keys: int | None,
        reserved_keys: int,
        monthly_traffic_bytes: int | None,
        now_text: str,
        plan_slots: dict[str, int],
        tier_slots: dict[str, int],
    ) -> None:
        connection.execute(
            """UPDATE outline_servers SET max_keys = ?, reserved_keys = ?,
                      monthly_traffic_bytes = ?, updated_at = ? WHERE server_id = ?""",
            (max_keys, reserved_keys, monthly_traffic_bytes, now_text, server_id),
        )
        for code, limit in plan_slots.items():
            CommerceAllocationRepository.upsert_plan_allocation(
                connection,
                server_id=server_id, plan_code=code, slot_limit=limit, now_text=now_text,
            )
        for code, limit in tier_slots.items():
            CommerceAllocationRepository.upsert_tier_allocation(
                connection,
                server_id=server_id, tier_code=code, slot_limit=limit, now_text=now_text,
            )

    @staticmethod
    def selection_inputs(
        connection: Any,
        *,
        plan_code: str,
        fresh_after: str,
        now_text: str,
        include_route_health: bool = True,
    ) -> dict[str, Any]:
        plan = CommerceAllocationRepository.plan(connection, plan_code, active_only=True)
        has_allocations = int(
            connection.execute(
                "SELECT COUNT(*) AS n FROM server_plan_allocations WHERE plan_code = ?",
                (plan_code,),
            ).fetchone()["n"]
        ) > 0
        servers = connection.execute(
            """SELECT * FROM outline_servers
               WHERE enabled = 1 AND lifecycle_state = 'active'
                 AND health_status = 'healthy'
                 AND last_synced_at IS NOT NULL AND last_synced_at >= ?
               ORDER BY server_id""",
            (fresh_after,),
        ).fetchall()
        values = []
        for server in servers:
            server_id = str(server["server_id"])
            probe = (
                connection.execute(
                    """SELECT status, score, last_observed_at
                       FROM route_health_snapshots WHERE server_id = ?""",
                    (server_id,),
                ).fetchone()
                if include_route_health
                else None
            )
            allocation = connection.execute(
                """SELECT slot_limit FROM server_plan_allocations
                   WHERE server_id = ? AND plan_code = ?""",
                (server_id, plan_code),
            ).fetchone()
            allocated = connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM subscriptions WHERE server_id = ? AND plan_code = ?
                       AND status IN ('pending', 'active')) +
                     (SELECT COUNT(*) FROM orders WHERE server_id = ? AND plan_code = ?
                       AND (status = 'payment_submitted' OR
                            (status = 'awaiting_payment' AND capacity_reserved_until > ?))) AS n""",
                (server_id, plan_code, server_id, plan_code, now_text),
            ).fetchone()["n"]
            reservations = connection.execute(
                """SELECT COUNT(*) AS n FROM orders WHERE server_id = ?
                   AND (status = 'payment_submitted' OR
                        (status = 'awaiting_payment' AND capacity_reserved_until > ?))""",
                (server_id, now_text),
            ).fetchone()["n"]
            pending = connection.execute(
                "SELECT COUNT(*) AS n FROM subscriptions WHERE server_id = ? AND status = 'pending'",
                (server_id,),
            ).fetchone()["n"]
            committed = connection.execute(
                """SELECT COALESCE((SELECT SUM(COALESCE(quota_bytes, 0)) FROM subscriptions
                         WHERE server_id = ? AND status IN ('pending', 'active')), 0) +
                       COALESCE((SELECT SUM(COALESCE(quota_bytes_snapshot, 0)) FROM orders
                         WHERE server_id = ? AND (status = 'payment_submitted' OR
                           (status = 'awaiting_payment' AND capacity_reserved_until > ?))), 0) AS n""",
                (server_id, server_id, now_text),
            ).fetchone()["n"]
            values.append({
                "server": server,
                "probe": probe,
                "allocation": allocation,
                "allocated_count": int(allocated or 0),
                "reservations": int(reservations or 0),
                "pending_keys": int(pending or 0),
                "committed": int(committed or 0),
            })
        return {"plan": plan, "has_allocations": has_allocations, "servers": values}

    @staticmethod
    def record_decision(connection: Any, **values: Any) -> None:
        connection.execute(
            """INSERT INTO route_decisions
               (decision_id, telegram_id, entitlement_ref, requested_region,
                selected_server_id, decision_mode, score, evidence_json, created_at)
               VALUES (?, ?, NULL, NULL, ?, 'automatic', ?, ?, ?)""",
            (values["decision_id"], values["telegram_id"], values["server_id"],
             values["score"], values["evidence_json"], values["now_text"]),
        )

    @staticmethod
    def active_plans(connection: Any) -> list[Any]:
        return connection.execute(
            """SELECT code, name, price_minor, currency, quota_bytes, duration_days
               FROM plans WHERE active = 1 ORDER BY price_minor"""
        ).fetchall()

    @staticmethod
    def active_plan(connection: Any, code: str) -> Any:
        return connection.execute(
            """SELECT code, name, price_minor, currency, quota_bytes, duration_days
               FROM plans WHERE code = ? AND active = 1""",
            (code,),
        ).fetchone()

    @staticmethod
    def availability_inputs(connection: Any, fresh_after: str, now_text: str) -> dict[str, Any]:
        plans = connection.execute("SELECT code FROM plans WHERE active = 1").fetchall()
        server_count = int(
            connection.execute(
                "SELECT COUNT(*) AS n FROM outline_servers WHERE enabled = 1"
            ).fetchone()["n"]
        )
        allocations: dict[str, list[Any]] = {}
        for plan in plans:
            code = str(plan["code"])
            allocations[code] = connection.execute(
                """SELECT a.server_id, a.slot_limit FROM server_plan_allocations a
                   JOIN outline_servers s ON s.server_id = a.server_id
                   WHERE a.plan_code = ? AND s.enabled = 1
                     AND s.health_status = 'healthy'
                     AND s.last_synced_at IS NOT NULL AND s.last_synced_at >= ?""",
                (code, fresh_after),
            ).fetchall()
        return {"plans": plans, "server_count": server_count, "allocations": allocations, "now_text": now_text}

    @staticmethod
    def used_for_plan(connection: Any, server_id: str, plan_code: str, now_text: str) -> int:
        return int(
            connection.execute(
                """SELECT
                   (SELECT COUNT(*) FROM subscriptions WHERE server_id = ? AND plan_code = ?
                      AND status IN ('pending', 'active')) +
                   (SELECT COUNT(*) FROM orders WHERE server_id = ? AND plan_code = ?
                      AND (status = 'payment_submitted' OR
                           (status = 'awaiting_payment' AND capacity_reserved_until > ?))) AS n""",
                (server_id, plan_code, server_id, plan_code, now_text),
            ).fetchone()["n"]
            or 0
        )
