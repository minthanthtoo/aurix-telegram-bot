"""Aggregate identity usage accounting workflow."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from identity_usage_policy import normalize_usage_observation
from identity_usage_recording_workflow import (
    credit_usage_sample,
    resolve_usage_epoch,
)


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
        """Credit remote counters; preserve aggregate use across counter resets."""
        reported, timestamp, observed_text, observed_time = normalize_usage_observation(
            remote_bytes,
            observed_at=observed_at,
            now=now,
        )
        endpoint_id = str(endpoint_id)
        external_id = str(external_id)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            repository = self.usage_recording
            binding = repository.active_binding(connection, endpoint_id, external_id)
            if binding is None:
                return {"accepted": False, "reason": "unbound_or_inactive_credential"}
            entitlement_id = str(binding["entitlement_id"])
            self._lock_entitlement(connection, entitlement_id)
            quota = int(binding["quota_bytes"])
            consumed_before = int(binding["consumed_bytes"] or 0)
            epoch, early_result = resolve_usage_epoch(
                self,
                repository,
                connection,
                binding=binding,
                endpoint_id=endpoint_id,
                external_id=external_id,
                reported=reported,
                timestamp=timestamp,
                observed_text=observed_text,
                observed_time=observed_time,
                consumed_before=consumed_before,
                quota=quota,
            )
            if early_result is not None:
                return early_result
            assert epoch is not None
            return credit_usage_sample(
                self,
                repository,
                connection,
                binding=binding,
                endpoint_id=endpoint_id,
                external_id=external_id,
                quota=quota,
                consumed_before=consumed_before,
                epoch=epoch,
                reported=reported,
                timestamp=timestamp,
                observed_text=observed_text,
            )
