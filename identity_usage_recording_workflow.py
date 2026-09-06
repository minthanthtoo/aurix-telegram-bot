"""Transactional stages for remote usage recording."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from identity_support import _parse_time
from identity_usage_crediting import credit_usage_sample
from identity_usage_epoch_steps import (
    UsageEpochState,
    create_initial_epoch,
    create_reset_epoch,
    record_stale_sample,
    update_monotonic_epoch,
)


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
        return (
            create_initial_epoch(
                repository,
                connection,
                entitlement_id=entitlement_id,
                generation_id=generation_id,
                endpoint_id=endpoint_id,
                external_id=external_id,
                reported=reported,
                observed_text=observed_text,
                timestamp=timestamp,
            ),
            None,
        )

    epoch_id = str(epoch["epoch_id"])
    previous_observed = _parse_time(str(epoch["last_observed_at"]))
    if observed_time < previous_observed:
        return None, record_stale_sample(
            repository,
            connection,
            epoch=epoch,
            binding=binding,
            endpoint_id=endpoint_id,
            external_id=external_id,
            reported=reported,
            observed_at=observed_text,
            timestamp=timestamp,
        )

    previous_remote = int(epoch["last_remote_bytes"] or 0)
    if reported < previous_remote:
        return (
            create_reset_epoch(
                service,
                repository,
                connection,
                epoch=epoch,
                entitlement_id=entitlement_id,
                generation_id=generation_id,
                endpoint_id=endpoint_id,
                external_id=external_id,
                reported=reported,
                observed_text=observed_text,
                consumed_before=consumed_before,
                quota=quota,
                timestamp=timestamp,
            ),
            None,
        )
    return (
        update_monotonic_epoch(
            repository,
            connection,
            epoch_id=epoch_id,
            reported=reported,
            previous_remote=previous_remote,
            observed_text=observed_text,
            timestamp=timestamp,
        ),
        None,
    )
