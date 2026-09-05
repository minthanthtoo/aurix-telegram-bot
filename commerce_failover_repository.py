"""Persistence boundary for route-failover execution state."""

from __future__ import annotations

from typing import Any

from connectivity_registry import ConnectivityRegistry


class FailoverRepository:
    """Keep failover source/policy/claim reads behind one repository."""

    @staticmethod
    def target_has_fresh_probe(connection: Any, server_id: str, cutoff: str) -> bool:
        if not FailoverRepository.table_exists(connection, "route_health_snapshots"):
            return False
        return (
            connection.execute(
                """SELECT status, last_observed_at FROM route_health_snapshots
                    WHERE server_id = ? AND status = 'healthy'
                      AND last_observed_at >= ?""",
                (str(server_id), cutoff),
            ).fetchone()
            is not None
        )

    @staticmethod
    def source_record(connection: Any, decision_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            """SELECT d.*, g.credential_id, g.status AS generation_status,
                      c.external_id, c.secret_ciphertext, c.status AS credential_status,
                      cp.telegram_id, cp.subscription_id,
                      en.kind, en.quota_bytes, en.expires_at, en.status AS entitlement_status,
                      te.outline_server_id AS target_server_id,
                      tr.protocol AS target_protocol
                 FROM failover_decisions d
                 JOIN credential_generations g ON g.generation_id = d.source_generation_id
                 JOIN connectivity_credentials c ON c.credential_id = g.credential_id
                 JOIN connectivity_profiles cp ON cp.profile_id = c.profile_id
                 JOIN entitlements en ON en.entitlement_id = d.entitlement_id
                 JOIN connectivity_routes tr ON tr.route_id = d.target_route_id
                 JOIN connectivity_endpoints te ON te.endpoint_id = tr.endpoint_id
                WHERE d.decision_id = ?""",
            (str(decision_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def standby_lease_bytes(connection: Any, entitlement_id: str) -> Any:
        return connection.execute(
            "SELECT standby_lease_bytes FROM route_failover_policies WHERE entitlement_id = ?",
            (str(entitlement_id),),
        ).fetchone()

    @staticmethod
    def decision_state(connection: Any, decision_id: str) -> Any:
        return connection.execute(
            "SELECT state FROM failover_decisions WHERE decision_id = ?",
            (str(decision_id),),
        ).fetchone()

    @staticmethod
    def target_credential_id(connection: Any, endpoint_id: str, external_id: str) -> str | None:
        row = connection.execute(
            """SELECT credential_id FROM connectivity_credentials
                WHERE endpoint_id = ? AND external_id = ? AND status = 'active'""",
            (str(endpoint_id), str(external_id)),
        ).fetchone()
        return str(row["credential_id"]) if row is not None else None

    @staticmethod
    def bind_credential(connection: Any, **values: Any) -> None:
        ConnectivityRegistry.bind_credential(connection, **values)

    @staticmethod
    def table_exists(connection: Any, name: str) -> bool:
        if connection.__class__.__name__ == "_PostgresConnection":
            row = connection.execute(
                "SELECT to_regclass(?) AS table_name", (f"public.{name}",)
            ).fetchone()
            return bool(row and row["table_name"])
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
            ).fetchone()
            is not None
        )
