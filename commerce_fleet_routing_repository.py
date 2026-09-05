"""Persistence boundary for provider-neutral route resolution."""

from __future__ import annotations

from typing import Any


class FleetRoutingRepository:
    @staticmethod
    def route(
        connection: Any,
        *,
        server_id: str | None = None,
        service_route_id: str | None = None,
    ) -> dict[str, Any] | None:
        if service_route_id:
            row = connection.execute(
                """SELECT r.route_id, r.protocol, r.endpoint_id,
                          e.outline_server_id
                     FROM connectivity_routes r
                     JOIN connectivity_endpoints e ON e.endpoint_id = r.endpoint_id
                    WHERE r.route_id = ?""",
                (str(service_route_id),),
            ).fetchone()
        elif server_id:
            row = connection.execute(
                """SELECT r.route_id, r.protocol, r.endpoint_id,
                          e.outline_server_id
                     FROM connectivity_routes r
                     JOIN connectivity_endpoints e ON e.endpoint_id = r.endpoint_id
                    WHERE e.outline_server_id = ? AND r.route_name = 'primary'""",
                (server_id,),
            ).fetchone()
        else:
            row = connection.execute(
                """SELECT r.route_id, r.protocol, r.endpoint_id,
                          e.outline_server_id
                     FROM connectivity_routes r
                     JOIN connectivity_endpoints e ON e.endpoint_id = r.endpoint_id
                    WHERE r.route_name = 'primary'
                    ORDER BY r.priority, r.route_id LIMIT 1"""
            ).fetchone()
        return dict(row) if row is not None else None
