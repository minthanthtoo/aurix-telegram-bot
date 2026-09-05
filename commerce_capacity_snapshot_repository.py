"""Read-model persistence for capacity and admission snapshots."""

from __future__ import annotations

from typing import Any


class CapacitySnapshotRepository:
    """Collect capacity inputs without leaking SQL into worker orchestration."""

    @staticmethod
    def snapshot_inputs(
        connection: Any,
        *,
        expiring_at: str,
        current_time: str,
        include_free_keys: bool,
    ) -> dict[str, Any]:
        counts = connection.execute(
            """SELECT
               (SELECT COUNT(*) FROM subscriptions WHERE status = 'active') AS active_subscriptions,
               (SELECT COUNT(*) FROM paid_vpn_keys WHERE status = 'active') AS active_keys,
               (SELECT COUNT(*) FROM provisioning_jobs
                 WHERE status IN ('pending', 'running')) AS pending_jobs,
               (SELECT COUNT(*) FROM provisioning_jobs WHERE status = 'failed') AS failed_jobs,
               (SELECT COUNT(*) FROM subscriptions
                 WHERE status = 'active' AND expires_at <= ?) AS expiring_24h""",
            (expiring_at,),
        ).fetchone()
        key_rows = connection.execute(
            """SELECT outline_key_id, telegram_id, quota_bytes, server_id
               FROM paid_vpn_keys WHERE status = 'active'"""
        ).fetchall()
        server_rows = connection.execute(
            "SELECT * FROM outline_servers ORDER BY enabled DESC, label, server_id"
        ).fetchall()
        allocation_rows = connection.execute(
            """SELECT a.server_id, a.plan_code, a.slot_limit, p.name,
                      (SELECT COUNT(*) FROM subscriptions s
                        WHERE s.server_id = a.server_id AND s.plan_code = a.plan_code
                          AND s.status IN ('pending', 'active')) AS active_count,
                      (SELECT COUNT(*) FROM orders o
                        WHERE o.server_id = a.server_id AND o.plan_code = a.plan_code
                          AND (o.status = 'payment_submitted' OR
                               (o.status = 'awaiting_payment'
                                AND o.capacity_reserved_until > ?))) AS reserved_count
               FROM server_plan_allocations a JOIN plans p ON p.code = a.plan_code
               ORDER BY a.server_id, p.price_minor""",
            (current_time,),
        ).fetchall()
        tier_allocation_rows = connection.execute(
            """SELECT server_id, tier_code, slot_limit
               FROM server_tier_allocations ORDER BY server_id, tier_code"""
        ).fetchall()
        free_key_rows = []
        if include_free_keys:
            free_key_rows = connection.execute(
                """SELECT k.server_id, k.key_type,
                          CASE WHEN g.key_id IS NULL THEN 0 ELSE 1 END AS is_promo
                   FROM keys k LEFT JOIN giveaway_claims g ON g.key_id = k.id
                   WHERE k.status IN ('active', 'revoke_failed')"""
            ).fetchall()
        return {
            "counts": counts,
            "key_rows": key_rows,
            "server_rows": server_rows,
            "allocation_rows": allocation_rows,
            "tier_allocation_rows": tier_allocation_rows,
            "free_key_rows": free_key_rows,
        }

    @staticmethod
    def server_commitments(
        connection: Any,
        *,
        server_id: str,
        current_time: str,
        include_free_keys: bool,
        include_free_intents: bool,
    ) -> dict[str, int]:
        reserved_orders = connection.execute(
            """SELECT COUNT(*) AS n FROM orders WHERE server_id = ?
               AND (status = 'payment_submitted' OR
                    (status = 'awaiting_payment' AND capacity_reserved_until > ?))""",
            (server_id, current_time),
        ).fetchone()["n"]
        pending_keys = connection.execute(
            """SELECT COUNT(*) AS n FROM subscriptions
               WHERE server_id = ? AND status = 'pending'""",
            (server_id,),
        ).fetchone()["n"]
        committed_traffic = connection.execute(
            """SELECT
               COALESCE((SELECT SUM(COALESCE(quota_bytes, 0)) FROM subscriptions
                 WHERE server_id = ? AND status IN ('pending', 'active')), 0) +
               COALESCE((SELECT SUM(COALESCE(quota_bytes_snapshot, 0)) FROM orders
                 WHERE server_id = ? AND (status = 'payment_submitted' OR
                   (status = 'awaiting_payment' AND capacity_reserved_until > ?))), 0) AS n""",
            (server_id, server_id, current_time),
        ).fetchone()["n"]
        active_free = 0
        if include_free_keys:
            active_free = connection.execute(
                """SELECT COUNT(*) AS n FROM keys
                   WHERE server_id = ? AND status IN ('active', 'revoke_failed')""",
                (server_id,),
            ).fetchone()["n"]
        active_paid = connection.execute(
            """SELECT COUNT(*) AS n FROM paid_vpn_keys
               WHERE server_id = ? AND status IN ('active', 'revoke_failed')""",
            (server_id,),
        ).fetchone()["n"]
        open_orders = connection.execute(
            """SELECT COUNT(*) AS n FROM orders WHERE server_id = ?
               AND status IN ('awaiting_payment', 'payment_submitted')""",
            (server_id,),
        ).fetchone()["n"]
        pending_free = 0
        if include_free_intents:
            pending_free = connection.execute(
                """SELECT COUNT(*) AS n FROM free_provisioning_intents
                   WHERE server_id = ? AND status IN ('pending', 'running')""",
                (server_id,),
            ).fetchone()["n"]
        return {
            "reserved_order_count": int(reserved_orders),
            "pending_key_count": int(pending_keys),
            "committed_traffic_bytes": int(committed_traffic),
            "active_free_key_count": int(active_free),
            "active_paid_key_count": int(active_paid),
            "open_order_count": int(open_orders),
            "pending_provisioning_count": int(pending_free),
        }
