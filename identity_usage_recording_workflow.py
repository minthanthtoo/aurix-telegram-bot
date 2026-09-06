"""Transactional stages for remote usage recording."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from identity_support import _parse_time
from identity_usage_crediting import credit_usage_sample


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
