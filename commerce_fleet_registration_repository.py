"""Persistence boundary for Outline fleet registration and legacy assignment."""

from __future__ import annotations

from typing import Any


class FleetRegistrationRepository:
    @staticmethod
    def existing_server_for_resource(connection: Any, resource_id: str) -> Any:
        return connection.execute(
            "SELECT server_id FROM outline_servers WHERE provider_resource_id = ?",
            (resource_id,),
        ).fetchone()

    @staticmethod
    def upsert_server(
        connection: Any,
        *,
        server_id: str,
        label: str,
        provider_resource_id: str | None,
        now_text: str,
    ) -> None:
        connection.execute(
            """INSERT INTO outline_servers
               (server_id, label, provider_resource_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(server_id) DO UPDATE SET
                 label = excluded.label,
                 provider_resource_id = COALESCE(excluded.provider_resource_id,
                                                 outline_servers.provider_resource_id),
                 updated_at = excluded.updated_at""",
            (server_id, label, provider_resource_id, now_text, now_text),
        )

    @staticmethod
    def state(connection: Any, server_id: str) -> Any:
        return connection.execute(
            "SELECT lifecycle_state, health_status FROM outline_servers WHERE server_id = ?",
            (server_id,),
        ).fetchone()

    @staticmethod
    def retire_missing(connection: Any, server_ids: tuple[str, ...], now_text: str) -> None:
        placeholders = ",".join("?" for _ in server_ids)
        connection.execute(
            f"""UPDATE outline_servers
                   SET enabled = 0,
                       lifecycle_state = 'retired',
                       lifecycle_reason = COALESCE(lifecycle_reason, 'removed from OUTLINE_SERVERS_JSON'),
                       lifecycle_changed_at = CASE
                           WHEN lifecycle_state != 'retired' OR enabled = 1 THEN ?
                           ELSE lifecycle_changed_at
                       END,
                       updated_at = ?
                 WHERE server_id NOT IN ({placeholders})""",
            (now_text, now_text, *server_ids),
        )

    @staticmethod
    def assign_legacy_defaults(
        connection: Any,
        *,
        default_server_id: str,
        server_ids: tuple[str, ...],
        orders_reserved_until: str,
        include_keys: bool,
    ) -> None:
        connection.execute(
            "UPDATE subscriptions SET server_id = ? WHERE server_id IS NULL",
            (default_server_id,),
        )
        connection.execute(
            "UPDATE paid_vpn_keys SET server_id = ? WHERE server_id IS NULL",
            (default_server_id,),
        )
        if include_keys:
            connection.execute(
                "UPDATE keys SET server_id = ? WHERE server_id IS NULL",
                (default_server_id,),
            )
            if "primary" not in server_ids:
                connection.execute(
                    "UPDATE keys SET server_id = ? WHERE server_id = 'primary'",
                    (default_server_id,),
                )
        connection.execute(
            """UPDATE orders SET server_id = ?, capacity_reserved_until = COALESCE(capacity_reserved_until, ?)
               WHERE server_id IS NULL AND status IN ('awaiting_payment', 'payment_submitted')""",
            (default_server_id, orders_reserved_until),
        )
