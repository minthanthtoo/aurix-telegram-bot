"""Persistence branches for remote usage epoch resolution."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class UsageEpochState:
    epoch_id: str
    delta: int
    reason: str
    reset: bool


def create_initial_epoch(
    repository: Any,
    connection: Any,
    *,
    entitlement_id: str,
    generation_id: str,
    endpoint_id: str,
    external_id: str,
    reported: int,
    observed_text: str,
    timestamp: str,
) -> UsageEpochState:
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
    return UsageEpochState(epoch_id, reported, "initial_sample", False)


def record_stale_sample(
    repository: Any,
    connection: Any,
    *,
    epoch: Any,
    binding: dict[str, Any],
    endpoint_id: str,
    external_id: str,
    reported: int,
    observed_text: str,
    timestamp: str,
) -> dict[str, Any]:
    epoch_id = str(epoch["epoch_id"])
    sample_id = f"sample-{secrets.token_hex(16)}"
    repository.record_sample(
        connection,
        sample_id=sample_id,
        epoch_id=epoch_id,
        entitlement_id=str(binding["entitlement_id"]),
        generation_id=str(binding["generation_id"]),
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
    return {
        "accepted": False,
        "reason": "stale_sample",
        "epoch_id": epoch_id,
        "entitlement_id": str(binding["entitlement_id"]),
        "generation_id": str(binding["generation_id"]),
        "subscription_id": binding["subscription_id"],
        "source_ref": binding["source_ref"],
    }


def create_reset_epoch(
    service: Any,
    repository: Any,
    connection: Any,
    *,
    epoch: Any,
    entitlement_id: str,
    generation_id: str,
    endpoint_id: str,
    external_id: str,
    reported: int,
    observed_text: str,
    consumed_before: int,
    quota: int,
    timestamp: str,
) -> UsageEpochState:
    repository.mark_epoch_reset(connection, str(epoch["epoch_id"]), timestamp)
    epoch_id = f"epoch-{secrets.token_hex(16)}"
    repository.create_epoch(
        connection,
        epoch_id=epoch_id,
        entitlement_id=entitlement_id,
        generation_id=generation_id,
        endpoint_id=endpoint_id,
        external_id=external_id,
        epoch_no=int(epoch["epoch_no"]) + 1,
        remote_bytes=reported,
        reset_count=int(epoch["reset_count"] or 0) + 1,
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
        details={"previous_remote_bytes": int(epoch["last_remote_bytes"] or 0), "current_remote_bytes": reported},
        now=timestamp,
    )
    return UsageEpochState(epoch_id, reported, "counter_reset", True)


def update_monotonic_epoch(
    repository: Any,
    connection: Any,
    *,
    epoch_id: str,
    reported: int,
    previous_remote: int,
    observed_text: str,
    timestamp: str,
) -> UsageEpochState:
    repository.update_epoch_remote(
        connection,
        epoch_id=epoch_id,
        remote_bytes=reported,
        observed_at=observed_text,
        now_text=timestamp,
    )
    delta = reported - previous_remote
    return UsageEpochState(epoch_id, delta, "monotonic" if delta else "no_delta", False)
