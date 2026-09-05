"""Transaction-aware persistence for remote entitlement usage accounting."""

from __future__ import annotations

from typing import Any


class IdentityUsageRepository:
    """SQL boundary used inside one identity usage transaction."""

    @staticmethod
    def active_binding(connection: Any, endpoint_id: str, external_id: str) -> Any:
        return connection.execute(
            """SELECT c.credential_id, c.endpoint_id, c.external_id,
                      g.generation_id, g.entitlement_id, e.subscription_id,
                      e.source_ref, e.quota_bytes, e.consumed_bytes, e.status, e.expires_at
                 FROM connectivity_credentials c
                 JOIN credential_generations g
                   ON g.credential_id = c.credential_id
                  AND g.endpoint_id = c.endpoint_id AND g.status = 'active'
                 JOIN entitlements e ON e.entitlement_id = g.entitlement_id
                WHERE c.endpoint_id = ? AND c.external_id = ? AND c.status = 'active'
                  AND e.status = 'active'
                ORDER BY g.generation_no DESC LIMIT 1""",
            (endpoint_id, external_id),
        ).fetchone()

    @staticmethod
    def active_epoch(
        connection: Any,
        *,
        entitlement_id: str,
        generation_id: str,
        endpoint_id: str,
        external_id: str,
    ) -> Any:
        return connection.execute(
            """SELECT * FROM entitlement_usage_epochs
                WHERE entitlement_id = ? AND generation_id = ? AND endpoint_id = ?
                  AND source_external_id = ? AND status = 'active'
                ORDER BY epoch_no DESC LIMIT 1""",
            (entitlement_id, generation_id, endpoint_id, external_id),
        ).fetchone()

    @staticmethod
    def latest_epoch_no(
        connection: Any,
        *,
        entitlement_id: str,
        generation_id: str,
        endpoint_id: str,
        external_id: str,
    ) -> int:
        row = connection.execute(
            """SELECT COALESCE(MAX(epoch_no), 0) AS latest
                 FROM entitlement_usage_epochs
                WHERE entitlement_id = ? AND generation_id = ?
                  AND endpoint_id = ? AND source_external_id = ?""",
            (entitlement_id, generation_id, endpoint_id, external_id),
        ).fetchone()
        return int(row["latest"] or 0)

    @staticmethod
    def create_epoch(
        connection: Any,
        *,
        epoch_id: str,
        entitlement_id: str,
        generation_id: str,
        endpoint_id: str,
        external_id: str,
        epoch_no: int,
        remote_bytes: int,
        reset_count: int,
        observed_at: str,
        now_text: str,
    ) -> None:
        connection.execute(
            """INSERT INTO entitlement_usage_epochs
               (epoch_id, entitlement_id, generation_id, endpoint_id, source_external_id,
                epoch_no, last_remote_bytes, credited_bytes, reset_count, status,
                last_observed_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 'active', ?, ?, ?)""",
            (
                epoch_id,
                entitlement_id,
                generation_id,
                endpoint_id,
                external_id,
                epoch_no,
                remote_bytes,
                reset_count,
                observed_at,
                now_text,
                now_text,
            ),
        )

    @staticmethod
    def mark_epoch_reset(connection: Any, epoch_id: str, now_text: str) -> None:
        connection.execute(
            """UPDATE entitlement_usage_epochs SET status = 'reset', updated_at = ?
                WHERE epoch_id = ? AND status = 'active'""",
            (now_text, epoch_id),
        )

    @staticmethod
    def update_epoch_remote(
        connection: Any,
        *,
        epoch_id: str,
        remote_bytes: int,
        observed_at: str,
        now_text: str,
    ) -> None:
        connection.execute(
            """UPDATE entitlement_usage_epochs
                  SET last_remote_bytes = ?, last_observed_at = ?, updated_at = ?
                WHERE epoch_id = ?""",
            (remote_bytes, observed_at, now_text, epoch_id),
        )

    @staticmethod
    def duplicate_sample(
        connection: Any, *, epoch_id: str, observed_at: str, remote_bytes: int
    ) -> Any:
        return connection.execute(
            """SELECT sample_id, accepted, reason, delta_bytes
                 FROM entitlement_usage_samples
                WHERE epoch_id = ? AND observed_at = ? AND remote_bytes = ?""",
            (epoch_id, observed_at, remote_bytes),
        ).fetchone()

    @staticmethod
    def record_sample(
        connection: Any,
        *,
        sample_id: str,
        epoch_id: str,
        entitlement_id: str,
        generation_id: str,
        endpoint_id: str,
        external_id: str,
        lease_id: str | None,
        remote_bytes: int,
        delta_bytes: int,
        accepted: bool,
        reason: str,
        observed_at: str,
        now_text: str,
        ignore_duplicate: bool = False,
    ) -> None:
        conflict = (
            " ON CONFLICT(epoch_id, observed_at, remote_bytes) DO NOTHING"
            if ignore_duplicate
            else ""
        )
        connection.execute(
            """INSERT INTO entitlement_usage_samples
               (sample_id, epoch_id, entitlement_id, generation_id, endpoint_id,
                source_external_id, lease_id, remote_bytes, delta_bytes, accepted,
                reason, observed_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
            + conflict,
            (
                sample_id,
                epoch_id,
                entitlement_id,
                generation_id,
                endpoint_id,
                external_id,
                lease_id,
                remote_bytes,
                delta_bytes,
                int(accepted),
                reason,
                observed_at,
                now_text,
            ),
        )

    @staticmethod
    def active_leases(
        connection: Any,
        *,
        entitlement_id: str,
        generation_id: str,
        now_text: str,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT * FROM quota_leases
                WHERE entitlement_id = ? AND generation_id = ?
                  AND status = 'active' AND expires_at > ?
                ORDER BY created_at, lease_id""",
            (entitlement_id, generation_id, now_text),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def create_lease(
        connection: Any,
        *,
        lease_id: str,
        entitlement_id: str,
        generation_id: str,
        endpoint_id: str,
        lease_bytes: int,
        expires_at: str,
        now_text: str,
    ) -> None:
        connection.execute(
            """INSERT INTO quota_leases
               (lease_id, entitlement_id, generation_id, endpoint_id,
                lease_bytes, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                lease_id,
                entitlement_id,
                generation_id,
                endpoint_id,
                lease_bytes,
                expires_at,
                now_text,
            ),
        )

    @staticmethod
    def consume_lease(
        connection: Any,
        *,
        lease_id: str,
        used_bytes: int,
        status: str,
        now_text: str,
    ) -> None:
        connection.execute(
            """UPDATE quota_leases
                  SET used_bytes = ?, status = ?,
                      released_at = CASE WHEN ? = 'exhausted'
                                         THEN COALESCE(released_at, ?) ELSE released_at END
                WHERE lease_id = ?""",
            (used_bytes, status, status, now_text, lease_id),
        )

    @staticmethod
    def credit_epoch(
        connection: Any,
        *,
        epoch_id: str,
        remote_bytes: int,
        credited_bytes: int,
        observed_at: str,
        now_text: str,
    ) -> None:
        connection.execute(
            """UPDATE entitlement_usage_epochs
                  SET last_remote_bytes = ?, credited_bytes = credited_bytes + ?,
                      last_observed_at = ?, updated_at = ?
                WHERE epoch_id = ?""",
            (remote_bytes, credited_bytes, observed_at, now_text, epoch_id),
        )

    @staticmethod
    def set_entitlement_consumed(
        connection: Any, *, entitlement_id: str, consumed_bytes: int, now_text: str
    ) -> None:
        connection.execute(
            """UPDATE entitlements SET consumed_bytes = ?, updated_at = ?
                WHERE entitlement_id = ?""",
            (consumed_bytes, now_text, entitlement_id),
        )

    @staticmethod
    def subscription_is_exhausted(connection: Any, subscription_id: str) -> bool:
        return connection.execute(
            """SELECT 1 FROM entitlements
                WHERE subscription_id = ? AND status = 'revoked'
                  AND quota_exhausted_at IS NOT NULL LIMIT 1""",
            (subscription_id,),
        ).fetchone() is not None

    @staticmethod
    def source_is_exhausted(connection: Any, source_ref: str) -> bool:
        return connection.execute(
            """SELECT 1 FROM entitlements
                WHERE source_ref = ? AND status = 'revoked'
                  AND quota_exhausted_at IS NOT NULL LIMIT 1""",
            (source_ref,),
        ).fetchone() is not None

    @staticmethod
    def quota_snapshot(connection: Any, entitlement_id: str) -> Any:
        return connection.execute(
            """SELECT e.entitlement_id, e.account_id, e.kind, e.quota_bytes,
                      e.consumed_bytes, e.status, e.expires_at, e.quota_exhausted_at,
                      COALESCE((SELECT SUM(lease_bytes - used_bytes) FROM quota_leases q
                                 WHERE q.entitlement_id = e.entitlement_id AND q.status = 'active'), 0) AS reserved_bytes,
                      COALESCE((SELECT COUNT(*) FROM entitlement_usage_epochs u
                                 WHERE u.entitlement_id = e.entitlement_id), 0) AS epoch_count
                 FROM entitlements e WHERE e.entitlement_id = ?""",
            (entitlement_id,),
        ).fetchone()

    @staticmethod
    def lease_usage_row(connection: Any, lease_id: str) -> Any:
        return connection.execute(
            """SELECT q.*, e.quota_bytes, e.consumed_bytes, e.status AS entitlement_status
                 FROM quota_leases q JOIN entitlements e ON e.entitlement_id = q.entitlement_id
                WHERE q.lease_id = ?""",
            (lease_id,),
        ).fetchone()

    @staticmethod
    def update_lease_usage(connection: Any, **values: Any) -> None:
        connection.execute(
            """UPDATE quota_leases SET used_bytes = ?, status = ?,
                    released_at = CASE WHEN ? = 'exhausted' THEN COALESCE(released_at, ?) ELSE released_at END
                WHERE lease_id = ?""",
            (
                values["used_bytes"], values["status"], values["status"],
                values["now_text"], values["lease_id"],
            ),
        )

    @staticmethod
    def lease_snapshot(connection: Any, telegram_id: int) -> list[Any]:
        return connection.execute(
            """SELECT q.lease_id, q.entitlement_id, q.endpoint_id, q.lease_bytes,
                      q.used_bytes, q.expires_at, q.status
                 FROM quota_leases q JOIN entitlements e ON e.entitlement_id = q.entitlement_id
                 JOIN account_identities i ON i.account_id = e.account_id
                WHERE i.identity_type = 'telegram' AND i.identity_value = ?
                ORDER BY q.created_at""",
            (str(int(telegram_id)),),
        ).fetchall()

    @staticmethod
    def routes_for_account(connection: Any, account_id: str) -> list[Any]:
        return connection.execute(
            """SELECT g.generation_id, g.entitlement_id, g.generation_no,
                      e.endpoint_id, sr.route_id, r.display_name AS region,
                      sr.protocol, t.display_name AS transport, g.credential_id
                 FROM credential_generations g
                 JOIN entitlements en ON en.entitlement_id = g.entitlement_id
                 JOIN connectivity_endpoints e ON e.endpoint_id = g.endpoint_id
                 JOIN connectivity_credentials c ON c.credential_id = g.credential_id
                    AND c.endpoint_id = g.endpoint_id AND c.status = 'active'
                 JOIN connectivity_regions r ON r.region_id = e.region_id
                 JOIN connectivity_routes sr ON sr.route_id = g.route_id
                 JOIN connectivity_transports t ON t.transport_id = e.transport_id
                WHERE en.account_id = ? AND en.status = 'active' AND g.status = 'active'
                  AND sr.status IN ('active', 'degraded') AND e.status IN ('active', 'degraded')
                ORDER BY r.display_name, g.generation_no""",
            (account_id,),
        ).fetchall()

    @staticmethod
    def route_secret(connection: Any, account_id: str, route_id: str) -> Any:
        return connection.execute(
            """SELECT g.generation_id, g.entitlement_id, g.generation_no,
                      en.kind, en.quota_bytes, en.expires_at, e.endpoint_id,
                      e.outline_server_id, sr.route_id, r.display_name AS region,
                      sr.protocol, t.display_name AS transport, c.credential_id,
                      c.external_id, c.secret_ciphertext
                 FROM credential_generations g
                 JOIN entitlements en ON en.entitlement_id = g.entitlement_id
                 JOIN connectivity_endpoints e ON e.endpoint_id = g.endpoint_id
                 JOIN connectivity_routes sr ON sr.route_id = g.route_id
                 JOIN connectivity_regions r ON r.region_id = e.region_id
                 JOIN connectivity_transports t ON t.transport_id = e.transport_id
                 JOIN connectivity_credentials c ON c.credential_id = g.credential_id
                WHERE en.account_id = ? AND en.status = 'active' AND g.generation_id = ?
                  AND g.status = 'active' AND sr.status IN ('active', 'degraded')
                  AND e.status IN ('active', 'degraded') AND c.status = 'active'""",
            (account_id, route_id),
        ).fetchone()
