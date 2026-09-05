"""Persistence boundary for provider inventory observations and orphan checks."""

from __future__ import annotations

from typing import Any


class InfrastructureInventoryRepository:
    @staticmethod
    def configured_provider_ids(connection: Any) -> list[Any]:
        return connection.execute(
            "SELECT provider_resource_id FROM outline_servers WHERE provider_resource_id IS NOT NULL"
        ).fetchall()

    @staticmethod
    def known_counts(connection: Any) -> tuple[int, int]:
        servers = connection.execute(
            "SELECT COUNT(*) AS n FROM outline_servers WHERE enabled = 1"
        ).fetchone()
        jobs = connection.execute(
            """SELECT COUNT(*) AS n FROM infrastructure_jobs
               WHERE operation = 'provision' AND status IN
               ('running', 'awaiting_verification', 'completed')"""
        ).fetchone()
        return int(servers["n"]), int(jobs["n"])

    @staticmethod
    def update_server(connection: Any, **values: Any) -> int:
        result = connection.execute(
            """UPDATE outline_servers
               SET provider_status = ?, provider_last_seen_at = ?, updated_at = ?
               WHERE provider_resource_id = ?""",
            (values["status"], values["now_text"], values["now_text"], values["provider_id"]),
        )
        return int(getattr(result, "rowcount", 0) or 0)

    @staticmethod
    def recent_event(connection: Any, metadata: str, since: str) -> bool:
        return connection.execute(
            """SELECT 1 FROM infrastructure_events
               WHERE event_type = 'provider_inventory_observed'
                 AND metadata_json = ? AND created_at >= ? LIMIT 1""",
            (metadata, since),
        ).fetchone() is not None

    @staticmethod
    def insert_event(connection: Any, event_id: str, event_type: str, metadata: str, now_text: str) -> None:
        connection.execute(
            """INSERT INTO infrastructure_events (id, event_type, metadata_json, created_at)
               VALUES (?, ?, ?, ?)""",
            (event_id, event_type, metadata, now_text),
        )

    @staticmethod
    def orphan_inputs(connection: Any) -> tuple[list[Any], list[Any], list[Any]]:
        registered = connection.execute(
            "SELECT provider_resource_id FROM outline_servers WHERE provider_resource_id IS NOT NULL"
        ).fetchall()
        jobs = connection.execute(
            """SELECT provider_resource_id FROM infrastructure_jobs
               WHERE provider_resource_id IS NOT NULL
                 AND status NOT IN ('failed', 'completed')"""
        ).fetchall()
        events = connection.execute(
            """SELECT metadata_json, created_at FROM infrastructure_events
               WHERE event_type = 'provider_inventory_observed'
               ORDER BY created_at DESC LIMIT 2000"""
        ).fetchall()
        return registered, jobs, events

    @staticmethod
    def protected(connection: Any, provider_id: str) -> bool:
        return connection.execute(
            """SELECT 1 FROM outline_servers WHERE provider_resource_id = ?
               UNION ALL
               SELECT 1 FROM infrastructure_jobs WHERE provider_resource_id = ?
                 AND status NOT IN ('failed', 'completed') LIMIT 1""",
            (provider_id, provider_id),
        ).fetchone() is not None
