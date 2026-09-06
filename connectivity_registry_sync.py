"""Persistence steps for synchronizing an Outline endpoint registry row."""

from __future__ import annotations

from typing import Any, Callable


def upsert_outline_dimensions(
    connection: Any,
    *,
    provider_id: str,
    display_provider: str,
    region_id: str,
    display_region: str,
    transport_id: str,
    now_text: str,
) -> None:
    """Upsert provider, region, and transport dimensions."""
    connection.execute(
        """INSERT INTO connectivity_providers
           (provider_id, display_name, status, created_at, updated_at)
           VALUES (?, ?, 'active', ?, ?)
           ON CONFLICT(provider_id) DO UPDATE SET
             display_name = excluded.display_name, updated_at = excluded.updated_at""",
        (provider_id, display_provider, now_text, now_text),
    )
    connection.execute(
        """INSERT INTO connectivity_regions
           (region_id, provider_id, display_name, status, created_at, updated_at)
           VALUES (?, ?, ?, 'active', ?, ?)
           ON CONFLICT(region_id) DO UPDATE SET
             provider_id = excluded.provider_id,
             display_name = excluded.display_name,
             updated_at = excluded.updated_at""",
        (region_id, provider_id, display_region, now_text, now_text),
    )
    connection.execute(
        """INSERT INTO connectivity_transports
           (transport_id, protocol, display_name, status, created_at, updated_at)
           VALUES (?, 'outline', 'Outline', 'active', ?, ?)
           ON CONFLICT(transport_id) DO UPDATE SET updated_at = excluded.updated_at""",
        (transport_id, now_text, now_text),
    )


def upsert_outline_endpoint(
    connection: Any,
    *,
    endpoint_id: str,
    server_id: str,
    provider_id: str,
    region_id: str,
    transport_id: str,
    status: str,
    accepts: int,
    now_text: str,
) -> None:
    """Upsert the physical endpoint identity and lifecycle state."""
    connection.execute(
        """INSERT INTO connectivity_endpoints
           (endpoint_id, outline_server_id, provider_id, region_id, transport_id,
            status, accepts_new_keys, management_secret_ref, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(endpoint_id) DO UPDATE SET
             outline_server_id = excluded.outline_server_id,
             provider_id = excluded.provider_id,
             region_id = excluded.region_id,
             transport_id = excluded.transport_id,
             status = excluded.status,
             accepts_new_keys = excluded.accepts_new_keys,
             management_secret_ref = excluded.management_secret_ref,
             updated_at = excluded.updated_at""",
        (
            endpoint_id,
            server_id,
            provider_id,
            region_id,
            transport_id,
            status,
            accepts,
            f"env:OUTLINE_SERVERS_JSON:{server_id}",
            now_text,
            now_text,
        ),
    )


def upsert_primary_route(
    connection: Any,
    *,
    endpoint_id: str,
    status: str,
    now_text: str,
    table_exists: Callable[[Any, str], bool],
) -> None:
    """Keep the compatibility primary route synchronized after migration 26."""
    if not table_exists(connection, "connectivity_routes"):
        return
    route_id = f"route-{endpoint_id}"
    outline_capabilities = (
        '{"managed_config":true,"manual_export":true,"quota_cap":true,'
        '"usage":true,"rotation":true,"terminate_sessions":false,'
        '"management_probe":true,"data_plane_probe":true,"reconcile":true}'
    )
    connection.execute(
        """INSERT INTO connectivity_routes
           (route_id, endpoint_id, route_name, protocol, status, priority,
            supports_managed_config, supports_manual_export, supports_quota_cap,
            supports_usage, supports_rotation, supports_terminate_sessions,
            supports_management_probe, supports_data_plane_probe, supports_reconcile,
            capabilities_json, created_at, updated_at)
           VALUES (?, ?, 'primary', 'outline', ?, 100, ?, ?, ?, ?, ?, FALSE, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(endpoint_id, route_name) DO UPDATE SET
             protocol = excluded.protocol, status = excluded.status,
             supports_managed_config = excluded.supports_managed_config,
             supports_manual_export = excluded.supports_manual_export,
             supports_quota_cap = excluded.supports_quota_cap,
             supports_usage = excluded.supports_usage,
             supports_rotation = excluded.supports_rotation,
             supports_management_probe = excluded.supports_management_probe,
             supports_data_plane_probe = excluded.supports_data_plane_probe,
             supports_reconcile = excluded.supports_reconcile,
             capabilities_json = excluded.capabilities_json,
             updated_at = excluded.updated_at""",
        (
            route_id,
            endpoint_id,
            status,
            True,
            True,
            True,
            True,
            True,
            True,
            True,
            True,
            outline_capabilities,
            now_text,
            now_text,
        ),
    )
