"""Aggregate identity usage accounting workflow."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from identity_support import IdentityError, _now_text, _parse_time


class IdentityUsageRecordingMixin:
    def record_remote_usage(
        self,
        endpoint_id: str,
        external_id: str,
        remote_bytes: int,
        *,
        observed_at: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Credit monotonic remote counters without allowing counter resets to bypass quota.

        Outline exposes a cumulative counter per remote key, but counters can
        reset after a server restart, metrics-window rollover, or key reuse.
        Each reset starts a new local epoch; aggregate entitlement consumption
        is never reset and every accepted delta is preserved in both a sample
        and an immutable ledger entry.
        """
        try:
            reported = int(remote_bytes)
        except (TypeError, ValueError) as exc:
            raise IdentityError("remote usage is invalid") from exc
        if reported < 0 or reported > 100 * 1024 * 1024 * 1024 * 1024:
            raise IdentityError("remote usage is outside the allowed range")
        timestamp = str(now or _now_text())
        observed_text = str(observed_at or timestamp)
        observed_time = _parse_time(observed_text)
        now_time = _parse_time(timestamp)
        if observed_time > now_time + timedelta(minutes=5):
            raise IdentityError("remote usage timestamp is too far in the future")
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            binding = connection.execute(
                """SELECT c.credential_id, c.endpoint_id, c.external_id,
                          g.generation_id, g.entitlement_id, e.subscription_id,
                          e.source_ref,
                          e.quota_bytes, e.consumed_bytes, e.status, e.expires_at
                     FROM connectivity_credentials c
                     JOIN credential_generations g
                       ON g.credential_id = c.credential_id
                      AND g.endpoint_id = c.endpoint_id
                      AND g.status = 'active'
                     JOIN entitlements e ON e.entitlement_id = g.entitlement_id
                    WHERE c.endpoint_id = ? AND c.external_id = ? AND c.status = 'active'
                      AND e.status = 'active'
                    ORDER BY g.generation_no DESC
                    LIMIT 1""",
                (str(endpoint_id), str(external_id)),
            ).fetchone()
            if binding is None:
                return {"accepted": False, "reason": "unbound_or_inactive_credential"}
            entitlement_id = str(binding["entitlement_id"])
            generation_id = str(binding["generation_id"])
            self._lock_entitlement(connection, entitlement_id)
            quota = int(binding["quota_bytes"])
            consumed_before = int(binding["consumed_bytes"] or 0)
            epoch = connection.execute(
                """SELECT * FROM entitlement_usage_epochs
                    WHERE entitlement_id = ? AND generation_id = ? AND endpoint_id = ?
                      AND source_external_id = ? AND status = 'active'
                    ORDER BY epoch_no DESC LIMIT 1""",
                (entitlement_id, generation_id, str(endpoint_id), str(external_id)),
            ).fetchone()
            reset = False
            delta = 0
            reason = "no_delta"
            if epoch is None:
                latest = connection.execute(
                    """SELECT COALESCE(MAX(epoch_no), 0) AS latest
                         FROM entitlement_usage_epochs
                        WHERE entitlement_id = ? AND generation_id = ?
                          AND endpoint_id = ? AND source_external_id = ?""",
                    (entitlement_id, generation_id, str(endpoint_id), str(external_id)),
                ).fetchone()
                epoch_id = f"epoch-{secrets.token_hex(16)}"
                epoch_no = int(latest["latest"] or 0) + 1
                connection.execute(
                    """INSERT INTO entitlement_usage_epochs
                       (epoch_id, entitlement_id, generation_id, endpoint_id, source_external_id,
                        epoch_no, last_remote_bytes, credited_bytes, reset_count, status,
                        last_observed_at, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 'active', ?, ?, ?)""",
                    (epoch_id, entitlement_id, generation_id, str(endpoint_id), str(external_id),
                     epoch_no, reported, observed_text, timestamp, timestamp),
                )
                delta = reported
                reason = "initial_sample"
                epoch_reset_count = 0
            else:
                epoch_id = str(epoch["epoch_id"])
                epoch_reset_count = int(epoch["reset_count"] or 0)
                previous_observed = _parse_time(str(epoch["last_observed_at"]))
                if observed_time < previous_observed:
                    sample_id = f"sample-{secrets.token_hex(16)}"
                    connection.execute(
                        """INSERT INTO entitlement_usage_samples
                           (sample_id, epoch_id, entitlement_id, generation_id, endpoint_id,
                           source_external_id, lease_id, remote_bytes, delta_bytes, accepted,
                           reason, observed_at, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 0, 0, 'stale_sample', ?, ?)
                           ON CONFLICT(epoch_id, observed_at, remote_bytes) DO NOTHING""",
                        (sample_id, epoch_id, entitlement_id, generation_id, str(endpoint_id),
                         str(external_id), reported, observed_text, timestamp),
                    )
                    return {
                        "accepted": False,
                        "reason": "stale_sample",
                        "epoch_id": epoch_id,
                        "entitlement_id": entitlement_id,
                        "generation_id": generation_id,
                        "subscription_id": binding["subscription_id"],
                        "source_ref": binding["source_ref"],
                    }
                previous_remote = int(epoch["last_remote_bytes"] or 0)
                if reported < previous_remote:
                    connection.execute(
                        """UPDATE entitlement_usage_epochs SET status = 'reset', updated_at = ?
                            WHERE epoch_id = ? AND status = 'active'""",
                        (timestamp, epoch_id),
                    )
                    epoch_id = f"epoch-{secrets.token_hex(16)}"
                    epoch_no = int(epoch["epoch_no"]) + 1
                    epoch_reset_count += 1
                    connection.execute(
                        """INSERT INTO entitlement_usage_epochs
                           (epoch_id, entitlement_id, generation_id, endpoint_id, source_external_id,
                            epoch_no, last_remote_bytes, credited_bytes, reset_count, status,
                            last_observed_at, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 'active', ?, ?, ?)""",
                        (epoch_id, entitlement_id, generation_id, str(endpoint_id), str(external_id),
                         epoch_no, reported, epoch_reset_count, observed_text, timestamp, timestamp),
                    )
                    reset = True
                    delta = reported
                    reason = "counter_reset"
                    self._append_quota_ledger(
                        connection,
                        entitlement_id=entitlement_id,
                        generation_id=generation_id,
                        endpoint_id=str(endpoint_id),
                        epoch_id=epoch_id,
                        event_type="counter_reset",
                        bytes_value=0,
                        consumed_bytes=consumed_before,
                        remaining_bytes=max(0, quota - consumed_before),
                        idempotency_key=f"counter-reset:{epoch_id}",
                        details={"previous_remote_bytes": previous_remote, "current_remote_bytes": reported},
                        now=timestamp,
                    )
                else:
                    delta = reported - previous_remote
                    reason = "monotonic" if delta else "no_delta"
                    connection.execute(
                        """UPDATE entitlement_usage_epochs
                              SET last_remote_bytes = ?, last_observed_at = ?, updated_at = ?
                            WHERE epoch_id = ?""",
                        (reported, observed_text, timestamp, epoch_id),
                    )
            duplicate = connection.execute(
                """SELECT sample_id, accepted, reason, delta_bytes
                     FROM entitlement_usage_samples
                    WHERE epoch_id = ? AND observed_at = ? AND remote_bytes = ?""",
                (epoch_id, observed_text, reported),
            ).fetchone()
            if duplicate is not None:
                return {
                    "accepted": bool(duplicate["accepted"]),
                    "duplicate": True,
                    "reason": str(duplicate["reason"]),
                    "delta_bytes": int(duplicate["delta_bytes"] or 0),
                    "epoch_id": epoch_id,
                }
            credited = min(delta, max(0, quota - consumed_before))
            lease_rows = [dict(row) for row in connection.execute(
                """SELECT * FROM quota_leases
                    WHERE entitlement_id = ? AND generation_id = ?
                      AND status = 'active' AND expires_at > ?
                    ORDER BY created_at, lease_id""",
                (entitlement_id, generation_id, timestamp),
            ).fetchall()]

            def create_runtime_lease(block_bytes: int) -> dict[str, Any]:
                lease_id = f"lease-{secrets.token_hex(16)}"
                lease_expires = min(
                    _parse_time(str(binding["expires_at"])),
                    _parse_time(timestamp) + timedelta(days=30),
                ).isoformat()
                connection.execute(
                    """INSERT INTO quota_leases
                       (lease_id, entitlement_id, generation_id, endpoint_id,
                        lease_bytes, expires_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (lease_id, entitlement_id, generation_id, str(endpoint_id),
                     int(block_bytes), lease_expires, timestamp),
                )
                self._append_quota_ledger(
                    connection,
                    entitlement_id=entitlement_id,
                    generation_id=generation_id,
                    endpoint_id=str(endpoint_id),
                    lease_id=lease_id,
                    event_type="grant",
                    bytes_value=int(block_bytes),
                    consumed_bytes=consumed_before,
                    remaining_bytes=max(0, quota - consumed_before),
                    idempotency_key=f"grant:{lease_id}",
                    details={"reason": "usage_observation", "expires_at": lease_expires},
                    now=timestamp,
                )
                return {
                    "lease_id": lease_id,
                    "lease_bytes": int(block_bytes),
                    "used_bytes": 0,
                }

            available_capacity = sum(
                max(0, int(item["lease_bytes"]) - int(item["used_bytes"] or 0))
                for item in lease_rows
            )
            while available_capacity < credited:
                block = min(10 * 1024 * 1024 * 1024, credited - available_capacity)
                lease_rows.append(create_runtime_lease(block))
                available_capacity += block
            if not lease_rows and consumed_before < quota:
                # A healthy active generation should normally have a lease. A
                # delayed worker may have let it expire, so restore only a
                # bounded block; missing leases are not a reason to reset use.
                block = min(10 * 1024 * 1024 * 1024, max(1, quota - consumed_before))
                lease_rows.append(create_runtime_lease(block))
            if not lease_rows:
                sample_id = f"sample-{secrets.token_hex(16)}"
                connection.execute(
                    """INSERT INTO entitlement_usage_samples
                       (sample_id, epoch_id, entitlement_id, generation_id, endpoint_id,
                        source_external_id, lease_id, remote_bytes, delta_bytes, accepted,
                        reason, observed_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 0, 'no_active_lease', ?, ?)""",
                    (sample_id, epoch_id, entitlement_id, generation_id, str(endpoint_id),
                     str(external_id), reported, delta, observed_text, timestamp),
                )
                self._mark_entitlement_exhausted_locked(
                    connection,
                    entitlement_id,
                    now=timestamp,
                    generation_id=generation_id,
                    endpoint_id=str(endpoint_id),
                    epoch_id=epoch_id,
                    reason="missing_active_lease_fail_closed",
                )
                return {
                    "accepted": False,
                    "reason": "no_active_lease",
                    "entitlement_id": entitlement_id,
                    "generation_id": generation_id,
                    "subscription_id": binding["subscription_id"],
                    "source_ref": binding["source_ref"],
                    "epoch_id": epoch_id,
                    "exhausted": True,
                }
            consumed_after = min(quota, consumed_before + credited)
            lease_remaining = credited
            primary_lease_id: str | None = None
            for lease in lease_rows:
                available = max(0, int(lease["lease_bytes"]) - int(lease["used_bytes"] or 0))
                allocation = min(lease_remaining, available)
                if allocation:
                    if primary_lease_id is None:
                        primary_lease_id = str(lease["lease_id"])
                    new_used = int(lease["used_bytes"] or 0) + allocation
                    lease_status = "exhausted" if new_used >= int(lease["lease_bytes"]) else "active"
                    connection.execute(
                        """UPDATE quota_leases
                              SET used_bytes = ?, status = ?,
                                  released_at = CASE WHEN ? = 'exhausted' THEN COALESCE(released_at, ?) ELSE released_at END
                            WHERE lease_id = ?""",
                        (new_used, lease_status, lease_status, timestamp, str(lease["lease_id"])),
                    )
                    lease_remaining -= allocation
                if lease_remaining <= 0:
                    break
            if not delta:
                sample_reason = reason
            elif credited < delta:
                sample_reason = "quota_exhausted"
            else:
                sample_reason = reason
            sample_id = f"sample-{secrets.token_hex(16)}"
            connection.execute(
                """INSERT INTO entitlement_usage_samples
                   (sample_id, epoch_id, entitlement_id, generation_id, endpoint_id,
                    source_external_id, lease_id, remote_bytes, delta_bytes, accepted,
                    reason, observed_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sample_id, epoch_id, entitlement_id, generation_id, str(endpoint_id),
                 str(external_id), primary_lease_id, reported, delta, int(bool(credited)),
                 sample_reason, observed_text, timestamp),
            )
            connection.execute(
                """UPDATE entitlement_usage_epochs
                      SET last_remote_bytes = ?, credited_bytes = credited_bytes + ?,
                          last_observed_at = ?, updated_at = ?
                    WHERE epoch_id = ?""",
                (reported, credited, observed_text, timestamp, epoch_id),
            )
            if credited:
                connection.execute(
                    """UPDATE entitlements SET consumed_bytes = ?, updated_at = ?
                        WHERE entitlement_id = ?""",
                    (consumed_after, timestamp, entitlement_id),
                )
                self._append_quota_ledger(
                    connection,
                    entitlement_id=entitlement_id,
                    generation_id=generation_id,
                    endpoint_id=str(endpoint_id),
                    lease_id=primary_lease_id,
                    epoch_id=epoch_id,
                    event_type="usage",
                    bytes_value=credited,
                    consumed_bytes=consumed_after,
                    remaining_bytes=max(0, quota - consumed_after),
                    idempotency_key=f"usage-sample:{sample_id}",
                    details={"remote_delta_bytes": delta, "reset": reset},
                    now=timestamp,
                )
            exhausted = consumed_after >= quota
            if exhausted:
                self._mark_entitlement_exhausted_locked(
                    connection,
                    entitlement_id,
                    now=timestamp,
                    generation_id=generation_id,
                    endpoint_id=str(endpoint_id),
                    epoch_id=epoch_id,
                    reason="aggregate_quota_reached",
                )
        return {
            "accepted": bool(credited),
            "duplicate": False,
            "reason": sample_reason,
            "delta_bytes": delta,
            "credited_bytes": credited,
            "consumed_bytes": consumed_after,
            "remaining_bytes": max(0, quota - consumed_after),
            "entitlement_id": entitlement_id,
            "generation_id": generation_id,
            "subscription_id": binding["subscription_id"],
            "source_ref": binding["source_ref"],
            "epoch_id": epoch_id,
            "reset": reset,
            "exhausted": exhausted,
        }

