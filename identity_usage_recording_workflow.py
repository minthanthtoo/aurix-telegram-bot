"""Transactional stages for remote usage recording."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from identity_support import _parse_time


@dataclass(frozen=True, slots=True)
class UsageEpochState:
    epoch_id: str
    delta: int
    reason: str
    reset: bool


def resolve_usage_epoch(
    service: Any,
    repository: Any,
    connection: Any,
    *,
    binding: dict[str, Any],
    endpoint_id: str,
    external_id: str,
    reported: int,
    timestamp: str,
    observed_text: str,
    observed_time: datetime,
    consumed_before: int,
    quota: int,
) -> tuple[UsageEpochState | None, dict[str, Any] | None]:
    entitlement_id = str(binding["entitlement_id"])
    generation_id = str(binding["generation_id"])
    epoch = repository.active_epoch(
        connection,
        entitlement_id=entitlement_id,
        generation_id=generation_id,
        endpoint_id=endpoint_id,
        external_id=external_id,
    )
    if epoch is None:
        epoch_id = f"epoch-{secrets.token_hex(16)}"
        epoch_no = repository.latest_epoch_no(
            connection,
            entitlement_id=entitlement_id,
            generation_id=generation_id,
            endpoint_id=endpoint_id,
            external_id=external_id,
        ) + 1
        repository.create_epoch(
            connection,
            epoch_id=epoch_id,
            entitlement_id=entitlement_id,
            generation_id=generation_id,
            endpoint_id=endpoint_id,
            external_id=external_id,
            epoch_no=epoch_no,
            remote_bytes=reported,
            reset_count=0,
            observed_at=observed_text,
            now_text=timestamp,
        )
        return UsageEpochState(epoch_id, reported, "initial_sample", False), None

    epoch_id = str(epoch["epoch_id"])
    previous_observed = _parse_time(str(epoch["last_observed_at"]))
    if observed_time < previous_observed:
        sample_id = f"sample-{secrets.token_hex(16)}"
        repository.record_sample(
            connection,
            sample_id=sample_id,
            epoch_id=epoch_id,
            entitlement_id=entitlement_id,
            generation_id=generation_id,
            endpoint_id=endpoint_id,
            external_id=external_id,
            lease_id=None,
            remote_bytes=reported,
            delta_bytes=0,
            accepted=False,
            reason="stale_sample",
            observed_at=observed_text,
            now_text=timestamp,
            ignore_duplicate=True,
        )
        return None, {
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
        repository.mark_epoch_reset(connection, epoch_id, timestamp)
        epoch_id = f"epoch-{secrets.token_hex(16)}"
        epoch_reset_count = int(epoch["reset_count"] or 0) + 1
        repository.create_epoch(
            connection,
            epoch_id=epoch_id,
            entitlement_id=entitlement_id,
            generation_id=generation_id,
            endpoint_id=endpoint_id,
            external_id=external_id,
            epoch_no=int(epoch["epoch_no"]) + 1,
            remote_bytes=reported,
            reset_count=epoch_reset_count,
            observed_at=observed_text,
            now_text=timestamp,
        )
        service._append_quota_ledger(
            connection,
            entitlement_id=entitlement_id,
            generation_id=generation_id,
            endpoint_id=endpoint_id,
            epoch_id=epoch_id,
            event_type="counter_reset",
            bytes_value=0,
            consumed_bytes=consumed_before,
            remaining_bytes=max(0, quota - consumed_before),
            idempotency_key=f"counter-reset:{epoch_id}",
            details={"previous_remote_bytes": previous_remote, "current_remote_bytes": reported},
            now=timestamp,
        )
        return UsageEpochState(epoch_id, reported, "counter_reset", True), None

    delta = reported - previous_remote
    repository.update_epoch_remote(
        connection,
        epoch_id=epoch_id,
        remote_bytes=reported,
        observed_at=observed_text,
        now_text=timestamp,
    )
    return UsageEpochState(epoch_id, delta, "monotonic" if delta else "no_delta", False), None


def credit_usage_sample(
    service: Any,
    repository: Any,
    connection: Any,
    *,
    binding: dict[str, Any],
    endpoint_id: str,
    external_id: str,
    quota: int,
    consumed_before: int,
    epoch: UsageEpochState,
    reported: int,
    timestamp: str,
    observed_text: str,
) -> dict[str, Any]:
    duplicate = repository.duplicate_sample(
        connection,
        epoch_id=epoch.epoch_id,
        observed_at=observed_text,
        remote_bytes=reported,
    )
    if duplicate is not None:
        return {
            "accepted": bool(duplicate["accepted"]),
            "duplicate": True,
            "reason": str(duplicate["reason"]),
            "delta_bytes": int(duplicate["delta_bytes"] or 0),
            "epoch_id": epoch.epoch_id,
        }
    lease_rows = repository.active_leases(
        connection,
        entitlement_id=str(binding["entitlement_id"]),
        generation_id=str(binding["generation_id"]),
        now_text=timestamp,
    )
    credited = min(epoch.delta, max(0, quota - consumed_before))
    available_capacity = sum(
        max(0, int(item["lease_bytes"]) - int(item["used_bytes"] or 0))
        for item in lease_rows
    )
    while available_capacity < credited:
        block = min(10 * 1024 * 1024 * 1024, credited - available_capacity)
        lease_rows.append(
            _create_runtime_lease(
                service,
                repository,
                connection,
                binding=binding,
                endpoint_id=endpoint_id,
                block_bytes=block,
                quota=quota,
                consumed_before=consumed_before,
                epoch_id=epoch.epoch_id,
                timestamp=timestamp,
            )
        )
        available_capacity += block
    if not lease_rows and consumed_before < quota:
        block = min(10 * 1024 * 1024 * 1024, max(1, quota - consumed_before))
        lease_rows.append(
            _create_runtime_lease(
                service,
                repository,
                connection,
                binding=binding,
                endpoint_id=endpoint_id,
                block_bytes=block,
                quota=quota,
                consumed_before=consumed_before,
                epoch_id=epoch.epoch_id,
                timestamp=timestamp,
            )
        )
    if not lease_rows:
        repository.record_sample(
            connection,
            sample_id=f"sample-{secrets.token_hex(16)}",
            epoch_id=epoch.epoch_id,
            entitlement_id=str(binding["entitlement_id"]),
            generation_id=str(binding["generation_id"]),
            endpoint_id=endpoint_id,
            external_id=external_id,
            lease_id=None,
            remote_bytes=reported,
            delta_bytes=epoch.delta,
            accepted=False,
            reason="no_active_lease",
            observed_at=observed_text,
            now_text=timestamp,
        )
        service._mark_entitlement_exhausted_locked(
            connection,
            str(binding["entitlement_id"]),
            now=timestamp,
            generation_id=str(binding["generation_id"]),
            endpoint_id=endpoint_id,
            epoch_id=epoch.epoch_id,
            reason="missing_active_lease_fail_closed",
        )
        return {
            "accepted": False,
            "reason": "no_active_lease",
            "entitlement_id": binding["entitlement_id"],
            "generation_id": binding["generation_id"],
            "subscription_id": binding["subscription_id"],
            "source_ref": binding["source_ref"],
            "epoch_id": epoch.epoch_id,
            "exhausted": True,
        }
    consumed_after, primary_lease_id = _consume_leases(
        repository,
        connection,
        lease_rows,
        credited=credited,
        quota=quota,
        consumed_before=consumed_before,
        timestamp=timestamp,
    )
    sample_reason = (
        epoch.reason if not epoch.delta else "quota_exhausted" if credited < epoch.delta else epoch.reason
    )
    sample_id = f"sample-{secrets.token_hex(16)}"
    repository.record_sample(
        connection,
        sample_id=sample_id,
        epoch_id=epoch.epoch_id,
        entitlement_id=str(binding["entitlement_id"]),
        generation_id=str(binding["generation_id"]),
        endpoint_id=endpoint_id,
        external_id=external_id,
        lease_id=primary_lease_id,
        remote_bytes=reported,
        delta_bytes=epoch.delta,
        accepted=bool(credited),
        reason=sample_reason,
        observed_at=observed_text,
        now_text=timestamp,
    )
    repository.credit_epoch(
        connection,
        epoch_id=epoch.epoch_id,
        remote_bytes=reported,
        credited_bytes=credited,
        observed_at=observed_text,
        now_text=timestamp,
    )
    if credited:
        repository.set_entitlement_consumed(
            connection,
            entitlement_id=str(binding["entitlement_id"]),
            consumed_bytes=consumed_after,
            now_text=timestamp,
        )
        service._append_quota_ledger(
            connection,
            entitlement_id=str(binding["entitlement_id"]),
            generation_id=str(binding["generation_id"]),
            endpoint_id=endpoint_id,
            lease_id=primary_lease_id,
            epoch_id=epoch.epoch_id,
            event_type="usage",
            bytes_value=credited,
            consumed_bytes=consumed_after,
            remaining_bytes=max(0, quota - consumed_after),
            idempotency_key=f"usage-sample:{sample_id}",
            details={"remote_delta_bytes": epoch.delta, "reset": epoch.reset},
            now=timestamp,
        )
    exhausted = consumed_after >= quota
    if exhausted:
        service._mark_entitlement_exhausted_locked(
            connection,
            str(binding["entitlement_id"]),
            now=timestamp,
            generation_id=str(binding["generation_id"]),
            endpoint_id=endpoint_id,
            epoch_id=epoch.epoch_id,
            reason="aggregate_quota_reached",
        )
    return {
        "accepted": bool(credited),
        "duplicate": False,
        "reason": sample_reason,
        "delta_bytes": epoch.delta,
        "credited_bytes": credited,
        "consumed_bytes": consumed_after,
        "remaining_bytes": max(0, quota - consumed_after),
        "entitlement_id": binding["entitlement_id"],
        "generation_id": binding["generation_id"],
        "subscription_id": binding["subscription_id"],
        "source_ref": binding["source_ref"],
        "epoch_id": epoch.epoch_id,
        "reset": epoch.reset,
        "exhausted": exhausted,
    }


def _create_runtime_lease(
    service: Any,
    repository: Any,
    connection: Any,
    *,
    binding: dict[str, Any],
    endpoint_id: str,
    block_bytes: int,
    quota: int,
    consumed_before: int,
    epoch_id: str,
    timestamp: str,
) -> dict[str, Any]:
    lease_id = f"lease-{secrets.token_hex(16)}"
    lease_expires = min(
        _parse_time(str(binding["expires_at"])),
        _parse_time(timestamp) + timedelta(days=30),
    ).isoformat()
    repository.create_lease(
        connection,
        lease_id=lease_id,
        entitlement_id=str(binding["entitlement_id"]),
        generation_id=str(binding["generation_id"]),
        endpoint_id=endpoint_id,
        lease_bytes=int(block_bytes),
        expires_at=lease_expires,
        now_text=timestamp,
    )
    service._append_quota_ledger(
        connection,
        entitlement_id=str(binding["entitlement_id"]),
        generation_id=str(binding["generation_id"]),
        endpoint_id=endpoint_id,
        lease_id=lease_id,
        event_type="grant",
        bytes_value=int(block_bytes),
        consumed_bytes=consumed_before,
        remaining_bytes=max(0, quota - consumed_before),
        idempotency_key=f"grant:{lease_id}",
        details={"reason": "usage_observation", "expires_at": lease_expires},
        now=timestamp,
    )
    return {"lease_id": lease_id, "lease_bytes": int(block_bytes), "used_bytes": 0}


def _consume_leases(
    repository: Any,
    connection: Any,
    lease_rows: list[dict[str, Any]],
    *,
    credited: int,
    quota: int,
    consumed_before: int,
    timestamp: str,
) -> tuple[int, str | None]:
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
            repository.consume_lease(
                connection,
                lease_id=str(lease["lease_id"]),
                used_bytes=new_used,
                status=lease_status,
                now_text=timestamp,
            )
            lease_remaining -= allocation
        if lease_remaining <= 0:
            break
    return min(quota, consumed_before + credited), primary_lease_id
