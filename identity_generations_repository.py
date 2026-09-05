"""Persistence boundary for credential generations and quota leases."""

from __future__ import annotations

import json
import secrets
from datetime import timedelta
from typing import Any

from identity_support import IdentityError, _parse_time


class IdentityGenerationsRepository:
    """SQL operations for generation cutover and bounded quota leases."""

    @staticmethod
    def _lock_entitlement(connection: Any, entitlement_id: str) -> None:
        if connection.__class__.__name__ == "_PostgresConnection":
            connection.execute(
                "SELECT entitlement_id FROM entitlements WHERE entitlement_id = ? FOR UPDATE",
                (str(entitlement_id),),
            ).fetchone()

    @staticmethod
    def _append_quota_ledger(
        connection: Any,
        *,
        entitlement_id: str,
        event_type: str,
        bytes_value: int,
        consumed_bytes: int,
        remaining_bytes: int,
        idempotency_key: str,
        now: str,
        generation_id: str | None = None,
        endpoint_id: str | None = None,
        lease_id: str | None = None,
        epoch_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        if event_type not in {
            "grant",
            "usage",
            "release",
            "exhaust",
            "counter_reset",
            "reconcile",
        }:
            raise IdentityError("quota ledger event type is invalid")
        result = connection.execute(
            """INSERT INTO entitlement_quota_ledger
               (entry_id, entitlement_id, generation_id, endpoint_id, lease_id, epoch_id,
                event_type, bytes, consumed_bytes, remaining_bytes, idempotency_key,
                details_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(idempotency_key) DO NOTHING""",
            (
                f"ledger-{secrets.token_hex(16)}",
                str(entitlement_id),
                str(generation_id) if generation_id is not None else None,
                str(endpoint_id) if endpoint_id is not None else None,
                str(lease_id) if lease_id is not None else None,
                str(epoch_id) if epoch_id is not None else None,
                event_type,
                max(0, int(bytes_value)),
                max(0, int(consumed_bytes)),
                max(0, int(remaining_bytes)),
                str(idempotency_key),
                json.dumps(details or {}, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
                str(now),
            ),
        )
        return int(getattr(result, "rowcount", 0) or 0) == 1

    @staticmethod
    def ensure_generation_for_credential(
        connection: Any,
        *,
        entitlement_id: str,
        endpoint_id: str,
        credential_id: str,
        status: str,
        timestamp: str,
    ) -> str:
        entitlement = connection.execute(
            "SELECT 1 FROM entitlements WHERE entitlement_id = ?",
            (str(entitlement_id),),
        ).fetchone()
        credential = connection.execute(
            """SELECT endpoint_id, route_id FROM connectivity_credentials
                WHERE credential_id = ? AND endpoint_id = ?""",
            (str(credential_id), str(endpoint_id)),
        ).fetchone()
        if entitlement is None or credential is None:
            raise IdentityError("generation references an unknown entitlement or credential")
        route_id = str(credential["route_id"]) if credential["route_id"] else None
        existing = connection.execute(
            """SELECT generation_id, status FROM credential_generations
                WHERE entitlement_id = ? AND credential_id = ?
                ORDER BY generation_no DESC LIMIT 1""",
            (str(entitlement_id), str(credential_id)),
        ).fetchone()
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
                str(credential_id),
                int(latest["latest"] or 0) + 1,
                status,
                timestamp,
            ),
        )
        return generation_id

    @staticmethod
    def active_lease_for_generation(connection: Any, generation_id: str) -> str | None:
        row = connection.execute(
            """SELECT lease_id FROM quota_leases
                WHERE generation_id = ? AND status = 'active'
                ORDER BY created_at DESC LIMIT 1""",
            (str(generation_id),),
        ).fetchone()
        return str(row["lease_id"]) if row is not None else None

    @staticmethod
    def create_generation(
        connection: Any,
        *,
        entitlement_id: str,
        endpoint_id: str,
        credential_id: str | None,
        route_id: str | None,
        status: str,
        timestamp: str,
    ) -> str:
        if connection.execute(
            "SELECT 1 FROM entitlements WHERE entitlement_id = ?", (str(entitlement_id),)
        ).fetchone() is None:
            raise IdentityError("entitlement does not exist")
        if connection.execute(
            "SELECT 1 FROM connectivity_endpoints WHERE endpoint_id = ?", (str(endpoint_id),)
        ).fetchone() is None:
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
                int(row["latest"] or 0) + 1,
                status,
                timestamp,
            ),
        )
        return generation_id

    @staticmethod
    def revoke_generation(connection: Any, generation_id: str, timestamp: str) -> bool:
        updated = connection.execute(
            """UPDATE credential_generations SET status = 'revoked', revoked_at = ?
                WHERE generation_id = ? AND status != 'revoked'""",
            (timestamp, str(generation_id)),
        )
        return int(getattr(updated, "rowcount", 0) or 0) == 1

    @classmethod
    def grant_lease(
        cls,
        connection: Any,
        *,
        entitlement_id: str,
        endpoint_id: str,
        lease_bytes: int,
        generation_id: str | None,
        ttl_seconds: int,
        timestamp: str,
    ) -> str:
        cls._lock_entitlement(connection, entitlement_id)
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
            existing = cls.active_lease_for_generation(connection, generation_id)
            if existing is not None:
                return existing
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
        cls._append_quota_ledger(
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

    @classmethod
    def mark_entitlement_exhausted(
        cls,
        connection: Any,
        entitlement_id: str,
        *,
        now: str,
        generation_id: str | None = None,
        endpoint_id: str | None = None,
        epoch_id: str | None = None,
        reason: str = "aggregate_quota",
    ) -> bool:
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
            cls._append_quota_ledger(
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

    @classmethod
    def release_lease(
        cls, connection: Any, lease_id: str, *, timestamp: str, reason: str
    ) -> bool:
        row = connection.execute(
            """SELECT q.*, e.consumed_bytes, e.quota_bytes
                 FROM quota_leases q JOIN entitlements e ON e.entitlement_id = q.entitlement_id
                WHERE q.lease_id = ?""",
            (str(lease_id),),
        ).fetchone()
        if row is None:
            raise IdentityError("lease does not exist")
        cls._lock_entitlement(connection, str(row["entitlement_id"]))
        if str(row["status"]) != "active":
            return False
        unused = max(0, int(row["lease_bytes"]) - int(row["used_bytes"] or 0))
        connection.execute(
            """UPDATE quota_leases SET status = 'released', released_at = ?
                WHERE lease_id = ? AND status = 'active'""",
            (timestamp, str(lease_id)),
        )
        cls._append_quota_ledger(
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
