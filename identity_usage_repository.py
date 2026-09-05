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
