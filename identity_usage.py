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
            return self.usage_recording.subscription_is_exhausted(connection, str(subscription_id))

    def key_is_exhausted(self, *, server_id: str, local_key_ref: str) -> bool:
        """Return the aggregate-quota stop for a legacy/free key binding."""
        source_ref = f"key:{server_id}:{local_key_ref}"
        with self.database.connect() as connection:
            return self.usage_recording.source_is_exhausted(connection, source_ref)

    def quota_snapshot(self, entitlement_id: str) -> dict[str, Any] | None:
        """Return aggregate quota state without exposing any credential secret."""
        with self.database.connect() as connection:
            row = self.usage_recording.quota_snapshot(connection, str(entitlement_id))
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
            row = self.usage_recording.lease_usage_row(connection, str(lease_id))
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
            self.usage_recording.update_lease_usage(
                connection,
                used_bytes=current,
                status=status,
                now_text=timestamp,
                lease_id=str(lease_id),
            )
            if credited:
                self.usage_recording.set_entitlement_consumed(
                    connection,
                    entitlement_id=str(row["entitlement_id"]),
                    consumed_bytes=consumed_after,
                    now_text=timestamp,
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
            rows = self.usage_recording.lease_snapshot(connection, telegram_id)
        return [dict(row) for row in rows]

    def routes_for_account(self, account_id: str) -> list[dict[str, Any]]:
        """Return non-secret route metadata suitable for a signed manifest."""
        with self.database.connect() as connection:
            rows = self.usage_recording.routes_for_account(connection, str(account_id))
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
            row = self.usage_recording.route_secret(connection, str(account_id), str(route_id))
        return dict(row) if row is not None else None
