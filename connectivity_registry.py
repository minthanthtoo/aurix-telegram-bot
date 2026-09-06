"""Provider/region/transport registry kept behind the existing Outline MVP.

The commercial services still use their proven ``outline_servers`` tables for
allocation.  This registry adds stable, provider-neutral identities beside
that model so a future Xray or alternate-provider adapter can be introduced
without rewriting orders, wallets, or subscriptions.  It deliberately stores
references and state only; management URLs, certificates, and plaintext access
URLs never enter these tables.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

from connectivity_registry_sync import (
    upsert_outline_dimensions,
    upsert_outline_endpoint,
    upsert_primary_route,
)
from lifecycle_policy import accepts_new_keys, endpoint_status

def _slug(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-")
    return (normalized or fallback)[:64]


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(str(part) for part in parts).encode()).hexdigest()[:32]
    return f"{prefix}-{digest}"


class ConnectivityRegistry:
    """Small compatibility layer for the generic connectivity data model."""

    PROVIDER_NAMES = {
        "digitalocean": "DigitalOcean",
        "nube": "Nube Cloud",
        "manual": "Manual / existing host",
    }

    @staticmethod
    def available(connection: Any) -> bool:
        """Return whether migration 14 is present on this connection."""
        if connection.__class__.__name__ == "_PostgresConnection":
            row = connection.execute(
                "SELECT to_regclass('public.connectivity_endpoints') AS table_name"
            ).fetchone()
            return bool(row and row["table_name"])
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'connectivity_endpoints'"
        ).fetchone()
        return row is not None

    @classmethod
    def sync_outline_endpoint(
        cls,
        connection: Any,
        *,
        server_id: str,
        label: str,
        provider: str = "manual",
        region: str = "unknown",
        lifecycle_state: str = "active",
        health_status: str = "unknown",
        now_text: str,
    ) -> dict[str, str] | None:
        """Upsert one Outline endpoint and its provider-neutral dimensions."""
        if not cls.available(connection):
            return None
        provider_id = _slug(provider, "manual")
        # Region labels are not globally unique (two providers can both use
        # ``sgp1``/``sin1``).  Keep the provider dimension in the stable key so
        # a later provider sync cannot silently relink an existing endpoint.
        region_id = _slug(f"{provider_id}-{region}", "unknown")
        transport_id = "outline"
        endpoint_id = _stable_id("endpoint", "outline", server_id)
        display_provider = cls.PROVIDER_NAMES.get(provider_id, provider_id.replace("-", " ").title())
        display_region = str(region or "Unknown")[:128]
        normalized_endpoint_status = endpoint_status(lifecycle_state, health_status)
        accepts = 1 if accepts_new_keys(lifecycle_state, health_status) else 0
        upsert_outline_dimensions(
            connection,
            provider_id=provider_id,
            display_provider=display_provider,
            region_id=region_id,
            display_region=display_region,
            transport_id=transport_id,
            now_text=now_text,
        )
        upsert_outline_endpoint(
            connection,
            endpoint_id=endpoint_id,
            server_id=server_id,
            provider_id=provider_id,
            region_id=region_id,
            transport_id=transport_id,
            status=normalized_endpoint_status,
            accepts=accepts,
            now_text=now_text,
        )
        # Migration 26 separates a physical endpoint from its service route.
        # Keep the compatibility route synchronized when a new server is
        # registered after the migration has already run.
        upsert_primary_route(
            connection,
            endpoint_id=endpoint_id,
            status=normalized_endpoint_status,
            now_text=now_text,
            table_exists=cls._table_exists,
        )
        return {
            "endpoint_id": endpoint_id,
            "provider_id": provider_id,
            "region_id": region_id,
            "transport_id": transport_id,
        }

    @classmethod
    def sync_outline_health(
        cls,
        connection: Any,
        *,
        server_id: str,
        lifecycle_state: str,
        health_status: str,
        now_text: str,
    ) -> None:
        """Mirror endpoint health/lifecycle into the generic endpoint row."""
        if not cls.available(connection):
            return
        endpoint = connection.execute(
            """SELECT e.endpoint_id, r.route_id
                 FROM connectivity_endpoints e
                 LEFT JOIN connectivity_routes r
                   ON r.endpoint_id = e.endpoint_id AND r.route_name = 'primary'
                WHERE e.outline_server_id = ?""",
            (server_id,),
        ).fetchone()
        if endpoint is None:
            return
        status = endpoint_status(lifecycle_state, health_status)
        connection.execute(
            """UPDATE connectivity_endpoints
                  SET status = ?, accepts_new_keys = ?, updated_at = ?
                WHERE outline_server_id = ?""",
            (status, 1 if accepts_new_keys(lifecycle_state, health_status) else 0, now_text, server_id),
        )
        if endpoint["route_id"] and cls._table_exists(connection, "connectivity_routes"):
            connection.execute(
                """UPDATE connectivity_routes
                      SET status = ?, updated_at = ?
                    WHERE route_id = ?""",
                (status, now_text, str(endpoint["route_id"])),
            )

    @classmethod
    def endpoint_snapshot(cls, connection: Any) -> list[dict[str, Any]]:
        """Return non-secret registry dimensions for operator dashboards."""
        if not cls.available(connection):
            return []
        rows = connection.execute(
            """SELECT e.endpoint_id, e.outline_server_id, e.status,
                      e.accepts_new_keys, e.updated_at,
                      p.provider_id, p.display_name AS provider_name,
                      r.region_id, r.display_name AS region_name,
                      t.transport_id, t.protocol, t.display_name AS transport_name
                 FROM connectivity_endpoints e
                 JOIN connectivity_providers p ON p.provider_id = e.provider_id
                 JOIN connectivity_regions r ON r.region_id = e.region_id
                 JOIN connectivity_transports t ON t.transport_id = e.transport_id
                ORDER BY e.outline_server_id"""
        ).fetchall()
        return [dict(row) for row in rows]

    @classmethod
    def route_snapshot(cls, connection: Any) -> list[dict[str, Any]]:
        """Return service-route identities and honest adapter capabilities."""
        if not cls._table_exists(connection, "connectivity_routes"):
            return []
        rows = connection.execute(
            """SELECT r.route_id, r.endpoint_id, e.outline_server_id,
                      r.route_name, r.protocol, r.status, r.priority,
                      r.supports_managed_config, r.supports_manual_export,
                      r.supports_quota_cap, r.supports_usage, r.supports_rotation,
                      r.supports_terminate_sessions, r.supports_management_probe,
                      r.supports_data_plane_probe, r.supports_reconcile,
                      r.capabilities_json, r.updated_at
                 FROM connectivity_routes r
                 JOIN connectivity_endpoints e ON e.endpoint_id = r.endpoint_id
                ORDER BY e.outline_server_id, r.priority, r.route_id"""
        ).fetchall()
        return [dict(row) for row in rows]

    @classmethod
    def bind_credential(
        cls,
        connection: Any,
        *,
        telegram_id: int,
        server_id: str,
        external_id: str,
        secret_ciphertext: str | None,
        now_text: str,
        profile_kind: str,
        subscription_id: str | None = None,
        route_id: str | None = None,
    ) -> None:
        """Create/update a generic profile, assignment, and credential binding."""
        if not cls.available(connection):
            return
        if secret_ciphertext and str(secret_ciphertext).startswith("ss://"):
            raise ValueError("connectivity credential secret must be encrypted")
        route_join_type = "JOIN" if route_id else "LEFT JOIN"
        route_join = "AND r.route_id = ?" if route_id else "AND r.route_name = 'primary'"
        endpoint = connection.execute(
            f"""SELECT e.endpoint_id, r.route_id
                  FROM connectivity_endpoints e
                  {route_join_type} connectivity_routes r
                    ON r.endpoint_id = e.endpoint_id {route_join}
                 WHERE e.outline_server_id = ?""",
            # The route predicate belongs to the JOIN, so its value precedes
            # the endpoint identity in the explicit-route variant.
            ((str(route_id), server_id) if route_id else (server_id,)),
        ).fetchone()
        if endpoint is None:
            return
        endpoint_id = str(endpoint["endpoint_id"])
        route_id = str(endpoint["route_id"]) if endpoint["route_id"] else None
        normalized_kind = str(profile_kind or "free").lower()
        if normalized_kind not in {"free", "paid", "trial", "promo"}:
            normalized_kind = "free"
        profile_id = _stable_id(
            "profile", normalized_kind, subscription_id or f"{server_id}:{external_id}"
        )
        connection.execute(
            """INSERT INTO connectivity_profiles
               (profile_id, telegram_id, subscription_id, profile_kind, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'active', ?, ?)
               ON CONFLICT(profile_id) DO UPDATE SET
                 telegram_id = excluded.telegram_id,
                 subscription_id = COALESCE(excluded.subscription_id, connectivity_profiles.subscription_id),
                 profile_kind = excluded.profile_kind,
                 status = 'active', updated_at = excluded.updated_at""",
            (profile_id, int(telegram_id), subscription_id, normalized_kind, now_text, now_text),
        )
        assignment_id = _stable_id("assignment", profile_id, endpoint_id)
        connection.execute(
            """INSERT INTO endpoint_assignments
               (assignment_id, profile_id, endpoint_id, route_id, status, assigned_at, reason)
               VALUES (?, ?, ?, ?, 'active', ?, 'initial compatibility binding')
               ON CONFLICT(assignment_id) DO UPDATE SET
                 route_id = excluded.route_id,
                 status = 'active', ended_at = NULL, reason = excluded.reason""",
            (assignment_id, profile_id, endpoint_id, route_id, now_text),
        )
        credential_id = _stable_id("credential", endpoint_id, external_id)
        connection.execute(
            """INSERT INTO connectivity_credentials
               (credential_id, profile_id, endpoint_id, route_id, transport_id, external_id,
                secret_ciphertext, status, created_at, revoked_at)
               VALUES (?, ?, ?, ?, 'outline', ?, ?, 'active', ?, NULL)
               ON CONFLICT(credential_id) DO UPDATE SET
                 profile_id = excluded.profile_id,
                 endpoint_id = excluded.endpoint_id,
                 route_id = excluded.route_id,
                 external_id = excluded.external_id,
                 secret_ciphertext = COALESCE(excluded.secret_ciphertext, connectivity_credentials.secret_ciphertext),
                 status = 'active', revoked_at = NULL""",
            (credential_id, profile_id, endpoint_id, route_id, str(external_id), secret_ciphertext, now_text),
        )

    @classmethod
    def revoke_credential(
        cls,
        connection: Any,
        *,
        server_id: str,
        external_id: str,
        now_text: str,
    ) -> None:
        if not cls.available(connection):
            return
        endpoint = connection.execute(
            "SELECT endpoint_id FROM connectivity_endpoints WHERE outline_server_id = ?",
            (server_id,),
        ).fetchone()
        if endpoint is None:
            return
        connection.execute(
            """UPDATE connectivity_credentials
                  SET status = 'revoked', revoked_at = ?
                WHERE endpoint_id = ? AND external_id = ?""",
            (now_text, str(endpoint["endpoint_id"]), str(external_id)),
        )
        connection.execute(
            """UPDATE endpoint_assignments
                  SET status = 'ended', ended_at = ?, reason = 'credential revoked'
                WHERE endpoint_id = ?
                  AND profile_id IN (
                      SELECT profile_id FROM connectivity_credentials
                       WHERE endpoint_id = ? AND external_id = ?
                  )
                  AND status = 'active'""",
            (now_text, str(endpoint["endpoint_id"]), str(endpoint["endpoint_id"]), str(external_id)),
        )
        connection.execute(
            """UPDATE connectivity_profiles
                  SET status = 'ended', updated_at = ?
                WHERE profile_id IN (
                      SELECT profile_id FROM connectivity_credentials
                       WHERE endpoint_id = ? AND external_id = ?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM connectivity_credentials active
                       WHERE active.profile_id = connectivity_profiles.profile_id
                         AND active.status = 'active'
                  )""",
            (now_text, str(endpoint["endpoint_id"]), str(external_id)),
        )

    @classmethod
    def rebuild_from_legacy(
        cls,
        connection: Any,
        *,
        now_text: str,
        encrypt_access_url: Callable[[str], str] | None = None,
    ) -> int:
        """Backfill generic bindings for existing paid/free rows after migration.

        Older ``paid_vpn_keys`` rows may contain a plaintext ``ss://`` URL,
        while current rows contain Fernet ciphertext. The generic registry
        must never receive the former. A caller that owns the access-url cipher
        can normalize legacy values; unverifiable values are omitted.
        """
        if not cls.available(connection):
            return 0
        count = 0
        for row in connection.execute(
            "SELECT telegram_id, subscription_id, server_id, outline_key_id, access_url, status FROM paid_vpn_keys"
        ).fetchall():
            if not row["server_id"] or not row["outline_key_id"]:
                continue
            raw_secret = str(row["access_url"] or "")
            secret_ciphertext: str | None = None
            if raw_secret and encrypt_access_url is not None:
                secret_ciphertext = encrypt_access_url(raw_secret)
            cls.bind_credential(
                connection,
                telegram_id=int(row["telegram_id"]),
                server_id=str(row["server_id"]),
                external_id=str(row["outline_key_id"]),
                secret_ciphertext=secret_ciphertext,
                now_text=now_text,
                profile_kind="paid",
                subscription_id=str(row["subscription_id"]),
            )
            if str(row.get("status") if hasattr(row, "get") else row["status"]) == "revoked":
                cls.revoke_credential(
                    connection,
                    server_id=str(row["server_id"]),
                    external_id=str(row["outline_key_id"]),
                    now_text=now_text,
                )
            count += 1
        if cls._table_exists(connection, "keys"):
            for row in connection.execute(
                "SELECT telegram_id, server_id, outline_key_id, key_type, status FROM keys"
            ).fetchall():
                if not row["server_id"] or not row["outline_key_id"]:
                    continue
                kind = {
                    "daily_free": "free",
                    "monthly_trial": "trial",
                }.get(str(row["key_type"]), "promo")
                cls.bind_credential(
                    connection,
                    telegram_id=int(row["telegram_id"]),
                    server_id=str(row["server_id"]),
                    external_id=str(row["outline_key_id"]),
                    secret_ciphertext=None,
                    now_text=now_text,
                    profile_kind=kind,
                )
                if str(row["status"]) == "revoked":
                    cls.revoke_credential(
                        connection,
                        server_id=str(row["server_id"]),
                        external_id=str(row["outline_key_id"]),
                        now_text=now_text,
                    )
                count += 1
        return count

    @staticmethod
    def _table_exists(connection: Any, name: str) -> bool:
        if connection.__class__.__name__ == "_PostgresConnection":
            row = connection.execute(
                "SELECT to_regclass(?) AS table_name", (f"public.{name}",)
            ).fetchone()
            return bool(row and row["table_name"])
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone() is not None
