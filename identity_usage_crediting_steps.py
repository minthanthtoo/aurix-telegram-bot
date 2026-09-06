"""Persistence and entitlement accounting for accepted usage samples."""

from __future__ import annotations

import secrets
from typing import Any


def persist_usage_credit(
    service: Any,
    repository: Any,
    connection: Any,
    *,
    binding: dict[str, Any],
    endpoint_id: str,
    external_id: str,
    quota: int,
    epoch: Any,
    reported: int,
    timestamp: str,
    observed_text: str,
    credited: int,
    consumed_after: int,
    primary_lease_id: str | None,
) -> dict[str, Any]:
    """Record the sample, ledger event, and exhaustion state atomically."""
    sample_reason = (
        epoch.reason
        if not epoch.delta
        else "quota_exhausted" if credited < epoch.delta else epoch.reason
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
