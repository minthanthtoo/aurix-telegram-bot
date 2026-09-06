"""Commit a normalized usage sample against entitlement lease capacity."""

from __future__ import annotations

import secrets
from typing import Any

from identity_usage_lease_accounting import consume_usage_leases, prepare_usage_leases
from identity_usage_crediting_steps import persist_usage_credit


def _record_usage_credit(
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
    return persist_usage_credit(
        service,
        repository,
        connection,
        binding=binding,
        endpoint_id=endpoint_id,
        external_id=external_id,
        quota=quota,
        epoch=epoch,
        reported=reported,
        timestamp=timestamp,
        observed_text=observed_text,
        credited=credited,
        consumed_after=consumed_after,
        primary_lease_id=primary_lease_id,
    )


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
    epoch: Any,
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
    credited = min(epoch.delta, max(0, quota - consumed_before))
    lease_rows = prepare_usage_leases(
        service,
        repository,
        connection,
        binding=binding,
        endpoint_id=endpoint_id,
        quota=quota,
        consumed_before=consumed_before,
        epoch_id=epoch.epoch_id,
        credited=credited,
        timestamp=timestamp,
    )
    if not lease_rows:
        return _record_missing_lease(
            service,
            repository,
            connection,
            binding=binding,
            endpoint_id=endpoint_id,
            external_id=external_id,
            epoch=epoch,
            reported=reported,
            timestamp=timestamp,
            observed_text=observed_text,
        )
    consumed_after, primary_lease_id = consume_usage_leases(
        repository,
        connection,
        lease_rows,
        credited=credited,
        quota=quota,
        consumed_before=consumed_before,
        timestamp=timestamp,
    )
    return _record_usage_credit(
        service,
        repository,
        connection,
        binding=binding,
        endpoint_id=endpoint_id,
        external_id=external_id,
        quota=quota,
        epoch=epoch,
        reported=reported,
        timestamp=timestamp,
        observed_text=observed_text,
        credited=credited,
        consumed_after=consumed_after,
        primary_lease_id=primary_lease_id,
    )


def _record_missing_lease(
    service: Any,
    repository: Any,
    connection: Any,
    *,
    binding: dict[str, Any],
    endpoint_id: str,
    external_id: str,
    epoch: Any,
    reported: int,
    timestamp: str,
    observed_text: str,
) -> dict[str, Any]:
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
