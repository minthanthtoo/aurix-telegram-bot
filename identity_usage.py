"""Usage accounting, quota snapshots, and account route handlers."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from identity_support import IdentityError, _now_text, _parse_time


from identity_usage_recording import IdentityUsageRecordingMixin
from identity_usage_repository import IdentityUsageRepository


class IdentityUsageMixin(IdentityUsageRecordingMixin):
    usage_recording = IdentityUsageRepository()

    def subscription_is_exhausted(self, subscription_id: str) -> bool:
        """Return the authoritative aggregate-quota stop for a paid entitlement."""
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM entitlements
                    WHERE subscription_id = ? AND status = 'revoked'
                      AND quota_exhausted_at IS NOT NULL
                    LIMIT 1""",
                (str(subscription_id),),
            ).fetchone()
        return row is not None

    def key_is_exhausted(self, *, server_id: str, local_key_ref: str) -> bool:
        """Return the aggregate-quota stop for a legacy/free key binding."""
        source_ref = f"key:{server_id}:{local_key_ref}"
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM entitlements
                    WHERE source_ref = ? AND status = 'revoked'
                      AND quota_exhausted_at IS NOT NULL
                    LIMIT 1""",
                (source_ref,),
            ).fetchone()
        return row is not None

    def quota_snapshot(self, entitlement_id: str) -> dict[str, Any] | None:
        """Return aggregate quota state without exposing any credential secret."""
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT e.entitlement_id, e.account_id, e.kind, e.quota_bytes,
                          e.consumed_bytes, e.status, e.expires_at, e.quota_exhausted_at,
                          COALESCE((SELECT SUM(lease_bytes - used_bytes)
                                      FROM quota_leases q
                                     WHERE q.entitlement_id = e.entitlement_id
                                       AND q.status = 'active'), 0) AS reserved_bytes,
                          COALESCE((SELECT COUNT(*) FROM entitlement_usage_epochs u
                                     WHERE u.entitlement_id = e.entitlement_id), 0) AS epoch_count
                     FROM entitlements e WHERE e.entitlement_id = ?""",
                (str(entitlement_id),),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["remaining_bytes"] = max(0, int(value["quota_bytes"]) - int(value["consumed_bytes"] or 0))
        value["reserved_bytes"] = max(0, int(value["reserved_bytes"] or 0))
        value["epoch_count"] = int(value["epoch_count"] or 0)
        return value

    def record_lease_usage(self, lease_id: str, used_bytes: int, *, now: str | None = None) -> dict[str, Any]:
        if int(used_bytes) < 0 or int(used_bytes) > 10 * 1024 * 1024 * 1024:
            raise IdentityError("reported lease usage is invalid")
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                """SELECT q.*, e.quota_bytes, e.consumed_bytes, e.status AS entitlement_status
                     FROM quota_leases q JOIN entitlements e ON e.entitlement_id = q.entitlement_id
                    WHERE q.lease_id = ?""",
                (str(lease_id),),
            ).fetchone()
            if row is None:
                raise IdentityError("lease does not exist")
            self._lock_entitlement(connection, str(row["entitlement_id"]))
            previous = int(row["used_bytes"] or 0)
            current = min(max(previous, int(used_bytes)), int(row["lease_bytes"]))
            delta = max(0, current - previous)
            quota = int(row["quota_bytes"])
            consumed_before = int(row["consumed_bytes"] or 0)
            credited = min(delta, max(0, quota - consumed_before))
            consumed_after = min(quota, consumed_before + credited)
            expired = _parse_time(str(row["expires_at"])) <= _parse_time(timestamp)
            status = (
                "exhausted" if current >= int(row["lease_bytes"])
                else "expired" if expired and str(row["status"]) == "active"
                else str(row["status"])
            )
            connection.execute(
                "UPDATE quota_leases SET used_bytes = ?, status = ?, released_at = CASE WHEN ? = 'exhausted' THEN COALESCE(released_at, ?) ELSE released_at END WHERE lease_id = ?",
                (current, status, status, timestamp, str(lease_id)),
            )
            if credited:
                connection.execute(
                    "UPDATE entitlements SET consumed_bytes = ?, updated_at = ? WHERE entitlement_id = ?",
                    (consumed_after, timestamp, str(row["entitlement_id"])),
                )
                self._append_quota_ledger(
                    connection,
                    entitlement_id=str(row["entitlement_id"]),
                    generation_id=str(row["generation_id"]) if row["generation_id"] else None,
                    endpoint_id=str(row["endpoint_id"]),
                    lease_id=str(lease_id),
                    event_type="usage",
                    bytes_value=credited,
                    consumed_bytes=consumed_after,
                    remaining_bytes=max(0, quota - consumed_after),
                    idempotency_key=f"lease-usage:{lease_id}:{current}",
                    details={"reported_used_bytes": int(used_bytes)},
                    now=timestamp,
                )
            exhausted = consumed_after >= quota
            if exhausted and str(row["entitlement_status"]) == "active":
                self._mark_entitlement_exhausted_locked(
                    connection,
                    str(row["entitlement_id"]),
                    now=timestamp,
                    generation_id=str(row["generation_id"]) if row["generation_id"] else None,
                    endpoint_id=str(row["endpoint_id"]),
                    reason="lease_usage",
                )
        return {
            "lease_id": str(lease_id),
            "used_bytes": current,
            "credited_bytes": credited,
            "consumed_bytes": consumed_after,
            "remaining_bytes": max(0, quota - consumed_after),
            "status": status,
            "exhausted": exhausted,
        }

    def lease_snapshot(self, telegram_id: int) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT q.lease_id, q.entitlement_id, q.endpoint_id, q.lease_bytes,
                          q.used_bytes, q.expires_at, q.status
                     FROM quota_leases q JOIN entitlements e ON e.entitlement_id = q.entitlement_id
                     JOIN account_identities i ON i.account_id = e.account_id
                    WHERE i.identity_type = 'telegram' AND i.identity_value = ?
                    ORDER BY q.created_at""",
                (str(int(telegram_id)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def routes_for_account(self, account_id: str) -> list[dict[str, Any]]:
        """Return non-secret route metadata suitable for a signed manifest."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT g.generation_id, g.entitlement_id, g.generation_no,
                          e.endpoint_id, sr.route_id, r.display_name AS region,
                          sr.protocol, t.display_name AS transport,
                          g.credential_id
                     FROM credential_generations g
                     JOIN entitlements en ON en.entitlement_id = g.entitlement_id
                     JOIN connectivity_endpoints e ON e.endpoint_id = g.endpoint_id
                     JOIN connectivity_credentials c
                       ON c.credential_id = g.credential_id
                      AND c.endpoint_id = g.endpoint_id
                      AND c.status = 'active'
                     JOIN connectivity_regions r ON r.region_id = e.region_id
                     JOIN connectivity_routes sr ON sr.route_id = g.route_id
                     JOIN connectivity_transports t ON t.transport_id = e.transport_id
                    WHERE en.account_id = ? AND en.status = 'active' AND g.status = 'active'
                      AND sr.status IN ('active', 'degraded')
                      AND e.status IN ('active', 'degraded')
                    ORDER BY r.display_name, g.generation_no""",
                (str(account_id),),
            ).fetchall()
        return [
            {
                # ``route_id`` remains the generation-scoped public selector
                # for device API compatibility; ``service_route_id`` is the
                # protocol-level route identity used by orchestration.
                "route_id": str(row["generation_id"]),
                "service_route_id": str(row["route_id"] or ""),
                "entitlement_id": str(row["entitlement_id"]),
                "endpoint_id": str(row["endpoint_id"]),
                "region": str(row["region"]),
                "protocol": str(row["protocol"]),
                "transport": str(row["transport"]),
                "credential_ref": str(row["credential_id"] or ""),
                "generation": int(row["generation_no"]),
            }
            for row in rows
        ]

    def route_secret_record(self, account_id: str, route_id: str) -> dict[str, Any] | None:
        """Return one account-owned credential record for the device API.

        The ciphertext is intentionally kept behind this narrow method. The
        manifest path never selects it, and callers must decrypt it before
        returning a customer configuration.
        """
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT g.generation_id, g.entitlement_id, g.generation_no,
                          en.kind, en.quota_bytes, en.expires_at,
                          e.endpoint_id, e.outline_server_id, sr.route_id,
                          r.display_name AS region, sr.protocol,
                          t.display_name AS transport, c.credential_id,
                          c.external_id, c.secret_ciphertext
                     FROM credential_generations g
                     JOIN entitlements en ON en.entitlement_id = g.entitlement_id
                     JOIN connectivity_endpoints e ON e.endpoint_id = g.endpoint_id
                     JOIN connectivity_routes sr ON sr.route_id = g.route_id
                     JOIN connectivity_regions r ON r.region_id = e.region_id
                     JOIN connectivity_transports t ON t.transport_id = e.transport_id
                     JOIN connectivity_credentials c ON c.credential_id = g.credential_id
                    WHERE en.account_id = ? AND en.status = 'active'
                      AND g.generation_id = ? AND g.status = 'active'
                      AND sr.status IN ('active', 'degraded')
                      AND e.status IN ('active', 'degraded')
                      AND c.status = 'active'""",
                (str(account_id), str(route_id)),
            ).fetchone()
        return dict(row) if row is not None else None
