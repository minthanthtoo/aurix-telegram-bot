"""Credential-generation and bounded lease lifecycle handlers."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from identity_support import IdentityError, _now_text, _parse_time


class IdentityGenerationsMixin:
    def ensure_generation_for_credential(
        self,
        entitlement_id: str,
        endpoint_id: str,
        *,
        credential_id: str,
        status: str = "active",
        now: str | None = None,
    ) -> str:
        """Converge one entitlement onto its current credential generation.

        A retry for the same credential is idempotent. A changed credential
        creates a new generation and revokes the previous active generation
        on that endpoint only, preserving an auditable cutover boundary while
        allowing one pooled entitlement to keep routes on other servers.
        """
        if status not in {"pending", "active", "revoked", "failed"}:
            raise IdentityError("credential generation status is invalid")
        credential_id = str(credential_id or "").strip()
        if not credential_id:
            raise IdentityError("credential id is required")
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            entitlement = connection.execute(
                "SELECT 1 FROM entitlements WHERE entitlement_id = ?",
                (str(entitlement_id),),
            ).fetchone()
            credential = connection.execute(
                """SELECT endpoint_id, route_id FROM connectivity_credentials
                    WHERE credential_id = ? AND endpoint_id = ?""",
                (credential_id, str(endpoint_id)),
            ).fetchone()
            if entitlement is None or credential is None:
                raise IdentityError("generation references an unknown entitlement or credential")
            route_id = str(credential["route_id"]) if credential["route_id"] else None
            existing = connection.execute(
                """SELECT generation_id, status FROM credential_generations
                    WHERE entitlement_id = ? AND credential_id = ?
                    ORDER BY generation_no DESC LIMIT 1""",
                (str(entitlement_id), credential_id),
            ).fetchone()
            # A revoked/failed generation is historical state.  Reusing it
            # would make a reissued credential appear to have uninterrupted
            # validity and would weaken cutover/audit semantics.  Cutover is
            # endpoint-scoped: one entitlement may legitimately have one
            # active route on every healthy server, while a replacement on
            # Singapore-A must not revoke its still-valid Bangkok route.
            if existing is not None and str(existing["status"]) in {"pending", "active"}:
                connection.execute(
                    """UPDATE credential_generations
                          SET status = ?, revoked_at = CASE WHEN ? = 'revoked' THEN COALESCE(revoked_at, ?) ELSE revoked_at END
                        WHERE generation_id = ?""",
                    (status, status, timestamp, str(existing["generation_id"])),
                )
                return str(existing["generation_id"])
            if route_id:
                connection.execute(
                    """UPDATE credential_generations
                          SET status = 'revoked', revoked_at = COALESCE(revoked_at, ?)
                        WHERE entitlement_id = ? AND endpoint_id = ? AND route_id = ?
                          AND status = 'active'""",
                    (timestamp, str(entitlement_id), str(endpoint_id), route_id),
                )
            else:
                connection.execute(
                    """UPDATE credential_generations
                          SET status = 'revoked', revoked_at = COALESCE(revoked_at, ?)
                        WHERE entitlement_id = ? AND endpoint_id = ? AND status = 'active'""",
                    (timestamp, str(entitlement_id), str(endpoint_id)),
                )
            latest = connection.execute(
                "SELECT COALESCE(MAX(generation_no), 0) AS latest FROM credential_generations WHERE entitlement_id = ?",
                (str(entitlement_id),),
            ).fetchone()
            generation_id = f"generation-{secrets.token_hex(16)}"
            connection.execute(
                """INSERT INTO credential_generations
                   (generation_id, entitlement_id, endpoint_id, route_id, credential_id,
                    generation_no, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    generation_id,
                    str(entitlement_id),
                    str(endpoint_id),
                    route_id,
                    credential_id,
                    int(latest["latest"] or 0) + 1,
                    status,
                    timestamp,
                ),
            )
        return generation_id

    def _active_lease_for_generation(self, generation_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT lease_id FROM quota_leases
                    WHERE generation_id = ? AND status = 'active'
                    ORDER BY created_at DESC LIMIT 1""",
                (str(generation_id),),
            ).fetchone()
        return str(row["lease_id"]) if row is not None else None

    def ensure_generation_lease(
        self,
        entitlement_id: str,
        generation_id: str,
        endpoint_id: str,
        quota_bytes: int,
        entitlement_expires_at: str,
        *,
        now: str | None = None,
    ) -> str:
        """Reserve a bounded first quota block for a live generation."""
        existing = self._active_lease_for_generation(generation_id)
        if existing is not None:
            return existing
        current = _parse_time(str(now or _now_text()))
        expiry = _parse_time(entitlement_expires_at)
        ttl = max(30, min(2_592_000, int((expiry - current).total_seconds())))
        # Leases are deliberately bounded blocks. Large entitlements are
        # renewed with additional blocks as usage is observed rather than
        # reserving the entire customer quota in one transaction.
        lease_bytes = min(int(quota_bytes), 10 * 1024 * 1024 * 1024)
        return self.grant_lease(
            entitlement_id,
            endpoint_id,
            lease_bytes=lease_bytes,
            generation_id=generation_id,
            ttl_seconds=ttl,
            now=current.isoformat(),
        )

    def create_generation(
        self,
        entitlement_id: str,
        endpoint_id: str,
        *,
        credential_id: str | None = None,
        route_id: str | None = None,
        status: str = "active",
        now: str | None = None,
    ) -> str:
        if status not in {"pending", "active", "revoked", "failed"}:
            raise IdentityError("credential generation status is invalid")
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            if connection.execute("SELECT 1 FROM entitlements WHERE entitlement_id = ?", (str(entitlement_id),)).fetchone() is None:
                raise IdentityError("entitlement does not exist")
            if connection.execute("SELECT 1 FROM connectivity_endpoints WHERE endpoint_id = ?", (str(endpoint_id),)).fetchone() is None:
                raise IdentityError("endpoint does not exist")
            if route_id is None:
                route = connection.execute(
                    """SELECT route_id FROM connectivity_routes
                        WHERE endpoint_id = ? AND route_name = 'primary'""",
                    (str(endpoint_id),),
                ).fetchone()
                route_id = str(route["route_id"]) if route is not None else None
            row = connection.execute(
                "SELECT COALESCE(MAX(generation_no), 0) AS latest FROM credential_generations WHERE entitlement_id = ?",
                (str(entitlement_id),),
            ).fetchone()
            generation_no = int(row["latest"] or 0) + 1
            generation_id = f"generation-{secrets.token_hex(16)}"
            connection.execute(
                """INSERT INTO credential_generations
                   (generation_id, entitlement_id, endpoint_id, route_id, credential_id,
                    generation_no, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (generation_id, str(entitlement_id), str(endpoint_id), route_id, credential_id, generation_no, status, timestamp),
            )
        return generation_id

    def revoke_generation(self, generation_id: str, *, now: str | None = None) -> bool:
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            updated = connection.execute(
                """UPDATE credential_generations SET status = 'revoked', revoked_at = ?
                    WHERE generation_id = ? AND status != 'revoked'""",
                (timestamp, str(generation_id)),
            )
        return int(getattr(updated, "rowcount", 0) or 0) == 1

    def grant_lease(
        self,
        entitlement_id: str,
        endpoint_id: str,
        *,
        lease_bytes: int,
        generation_id: str | None = None,
        ttl_seconds: int = 900,
        now: str | None = None,
    ) -> str:
        if int(lease_bytes) <= 0 or int(lease_bytes) > 10 * 1024 * 1024 * 1024:
            raise IdentityError("lease size is invalid")
        if not 30 <= int(ttl_seconds) <= 2_592_000:
            raise IdentityError("lease TTL is outside the allowed range")
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_entitlement(connection, entitlement_id)
            entitlement = connection.execute(
                "SELECT quota_bytes, consumed_bytes, status, expires_at FROM entitlements WHERE entitlement_id = ?",
                (str(entitlement_id),),
            ).fetchone()
            endpoint = connection.execute(
                "SELECT 1 FROM connectivity_endpoints WHERE endpoint_id = ?", (str(endpoint_id),)
            ).fetchone()
            if entitlement is None or endpoint is None:
                raise IdentityError("lease references an unknown entitlement or endpoint")
            if str(entitlement["status"]) != "active" or _parse_time(str(entitlement["expires_at"])) <= _parse_time(timestamp):
                raise IdentityError("entitlement is not active")
            expires = min(
                _parse_time(timestamp) + timedelta(seconds=int(ttl_seconds)),
                _parse_time(str(entitlement["expires_at"])),
            ).isoformat()
            if generation_id is not None:
                generation = connection.execute(
                    """SELECT generation_id, endpoint_id, status
                         FROM credential_generations
                        WHERE generation_id = ? AND entitlement_id = ?""",
                    (str(generation_id), str(entitlement_id)),
                ).fetchone()
                if generation is None or str(generation["endpoint_id"]) != str(endpoint_id):
                    raise IdentityError("lease generation is unknown")
                if str(generation["status"]) != "active":
                    raise IdentityError("lease generation is not active")
                existing = connection.execute(
                    """SELECT lease_id FROM quota_leases
                        WHERE generation_id = ? AND status = 'active'
                        ORDER BY created_at DESC LIMIT 1""",
                    (str(generation_id),),
                ).fetchone()
                if existing is not None:
                    return str(existing["lease_id"])
            connection.execute(
                """UPDATE quota_leases SET status = 'expired', released_at = COALESCE(released_at, ?)
                    WHERE entitlement_id = ? AND status = 'active' AND expires_at <= ?""",
                (timestamp, str(entitlement_id), timestamp),
            )
            usage = connection.execute(
                """SELECT COALESCE(SUM(used_bytes), 0) AS consumed,
                          COALESCE(SUM(CASE WHEN status = 'active' THEN lease_bytes - used_bytes ELSE 0 END), 0) AS reserved
                     FROM quota_leases WHERE entitlement_id = ?""",
                (str(entitlement_id),),
            ).fetchone()
            consumed = max(int(entitlement["consumed_bytes"] or 0), int(usage["consumed"] or 0))
            if consumed != int(entitlement["consumed_bytes"] or 0):
                connection.execute(
                    "UPDATE entitlements SET consumed_bytes = ?, updated_at = ? WHERE entitlement_id = ?",
                    (consumed, timestamp, str(entitlement_id)),
                )
            available = int(entitlement["quota_bytes"]) - consumed - int(usage["reserved"] or 0)
            if int(lease_bytes) > available:
                raise IdentityError("entitlement has insufficient unreserved quota")
            lease_id = f"lease-{secrets.token_hex(16)}"
            connection.execute(
                """INSERT INTO quota_leases
                   (lease_id, entitlement_id, generation_id, endpoint_id, lease_bytes, expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (lease_id, str(entitlement_id), generation_id, str(endpoint_id), int(lease_bytes), expires, timestamp),
            )
            self._append_quota_ledger(
                connection,
                entitlement_id=str(entitlement_id),
                generation_id=generation_id,
                endpoint_id=str(endpoint_id),
                lease_id=lease_id,
                event_type="grant",
                bytes_value=int(lease_bytes),
                consumed_bytes=consumed,
                remaining_bytes=max(0, int(entitlement["quota_bytes"]) - consumed),
                idempotency_key=f"grant:{lease_id}",
                details={"expires_at": expires},
                now=timestamp,
            )
        return lease_id

    def _mark_entitlement_exhausted_locked(
        self,
        connection: Any,
        entitlement_id: str,
        *,
        now: str,
        generation_id: str | None = None,
        endpoint_id: str | None = None,
        epoch_id: str | None = None,
        reason: str = "aggregate_quota",
    ) -> bool:
        """Revoke every route when an entitlement reaches its aggregate cap."""
        row = connection.execute(
            """SELECT quota_bytes, consumed_bytes, status, quota_exhausted_at
                 FROM entitlements WHERE entitlement_id = ?""",
            (str(entitlement_id),),
        ).fetchone()
        if row is None or int(row["consumed_bytes"] or 0) < int(row["quota_bytes"]):
            return False
        first_exhaustion = not row["quota_exhausted_at"]
        connection.execute(
            """UPDATE entitlements
                  SET status = CASE WHEN status IN ('active', 'pending') THEN 'revoked' ELSE status END,
                      quota_exhausted_at = COALESCE(quota_exhausted_at, ?),
                      updated_at = ?
                WHERE entitlement_id = ?""",
            (str(now), str(now), str(entitlement_id)),
        )
        connection.execute(
            """UPDATE credential_generations
                  SET status = 'revoked', revoked_at = COALESCE(revoked_at, ?)
                WHERE entitlement_id = ? AND status IN ('pending', 'active')""",
            (str(now), str(entitlement_id)),
        )
        connection.execute(
            """UPDATE quota_leases
                  SET status = 'released', released_at = COALESCE(released_at, ?)
                WHERE entitlement_id = ? AND status = 'active'""",
            (str(now), str(entitlement_id)),
        )
        if first_exhaustion:
            self._append_quota_ledger(
                connection,
                entitlement_id=str(entitlement_id),
                generation_id=generation_id,
                endpoint_id=endpoint_id,
                epoch_id=epoch_id,
                event_type="exhaust",
                bytes_value=0,
                consumed_bytes=int(row["consumed_bytes"] or 0),
                remaining_bytes=0,
                idempotency_key=f"exhaust:{entitlement_id}",
                details={"reason": str(reason)},
                now=str(now),
            )
        return first_exhaustion

    def release_lease(self, lease_id: str, *, now: str | None = None, reason: str = "released") -> bool:
        """Release unconsumed reservation while retaining its usage history."""
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                """SELECT q.*, e.consumed_bytes, e.quota_bytes
                     FROM quota_leases q JOIN entitlements e ON e.entitlement_id = q.entitlement_id
                    WHERE q.lease_id = ?""",
                (str(lease_id),),
            ).fetchone()
            if row is None:
                raise IdentityError("lease does not exist")
            self._lock_entitlement(connection, str(row["entitlement_id"]))
            if str(row["status"]) != "active":
                return False
            unused = max(0, int(row["lease_bytes"]) - int(row["used_bytes"] or 0))
            connection.execute(
                """UPDATE quota_leases SET status = 'released', released_at = ?
                    WHERE lease_id = ? AND status = 'active'""",
                (timestamp, str(lease_id)),
            )
            self._append_quota_ledger(
                connection,
                entitlement_id=str(row["entitlement_id"]),
                generation_id=str(row["generation_id"]) if row["generation_id"] else None,
                endpoint_id=str(row["endpoint_id"]),
                lease_id=str(lease_id),
                event_type="release",
                bytes_value=unused,
                consumed_bytes=int(row["consumed_bytes"] or 0),
                remaining_bytes=max(0, int(row["quota_bytes"]) - int(row["consumed_bytes"] or 0)),
                idempotency_key=f"release:{lease_id}",
                details={"reason": str(reason)},
                now=timestamp,
            )
        return True
