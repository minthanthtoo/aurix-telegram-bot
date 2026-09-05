"""Explicit mapping from worker ports to responsibility-owned handlers."""

from __future__ import annotations

from typing import Any

from commerce_worker_capacity import (
    _metrics_by_server,
    _record_scale_observation,
    _required_scale_observations,
    _scale_advice,
    _scale_observation_interval_seconds,
    capacity_snapshot,
    enforce_quotas,
    queue_quota_warnings,
)
from commerce_worker_job_operations import (
    _claim_job,
    _job_done,
    _job_failed,
    failed_jobs,
    retry_failed_job,
    retry_job,
)
from commerce_worker_lifecycle import (
    _expire,
    _find_key,
    _provision,
    _revoke,
    _revoke_legacy_free_keys,
    expire_and_process,
    process_jobs,
)
from commerce_worker_migrations import (
    _claim_endpoint_migration,
    _create_migration_key,
    _delete_migration_source,
    _endpoint_migration_completed,
    _endpoint_migration_failed,
    _mark_migration_source_delete_retry,
    _metric_for_key,
    _process_endpoint_migration,
    process_endpoint_migrations,
)
from commerce_worker_notifications import (
    _hydrate_notifications,
    claim_pending_notifications,
    mark_notification_failed,
    mark_notification_sent,
    pending_notifications,
)
from commerce_worker_repairs import (
    _claim_managed_key_repair,
    _managed_repair_failed,
    _managed_repair_manual,
    _managed_repair_max_attempts,
    _mark_managed_repair_converged,
    _process_managed_key_repair,
    _repair_key_idempotent,
    _sync_identity_binding,
    process_managed_key_repairs,
)


WORKER_IMPLEMENTATIONS: dict[str, Any] = {
    name: implementation
    for name, implementation in {
        "_metrics_by_server": _metrics_by_server,
        "_record_scale_observation": _record_scale_observation,
        "_required_scale_observations": _required_scale_observations,
        "_scale_advice": _scale_advice,
        "_scale_observation_interval_seconds": _scale_observation_interval_seconds,
        "capacity_snapshot": capacity_snapshot,
        "enforce_quotas": enforce_quotas,
        "queue_quota_warnings": queue_quota_warnings,
        "_claim_job": _claim_job,
        "_job_done": _job_done,
        "_job_failed": _job_failed,
        "failed_jobs": failed_jobs,
        "retry_failed_job": retry_failed_job,
        "retry_job": retry_job,
        "_expire": _expire,
        "_find_key": _find_key,
        "_provision": _provision,
        "_revoke": _revoke,
        "_revoke_legacy_free_keys": _revoke_legacy_free_keys,
        "expire_and_process": expire_and_process,
        "process_jobs": process_jobs,
        "_claim_endpoint_migration": _claim_endpoint_migration,
        "_create_migration_key": _create_migration_key,
        "_delete_migration_source": _delete_migration_source,
        "_endpoint_migration_completed": _endpoint_migration_completed,
        "_endpoint_migration_failed": _endpoint_migration_failed,
        "_mark_migration_source_delete_retry": _mark_migration_source_delete_retry,
        "_metric_for_key": _metric_for_key,
        "_process_endpoint_migration": _process_endpoint_migration,
        "process_endpoint_migrations": process_endpoint_migrations,
        "_hydrate_notifications": _hydrate_notifications,
        "claim_pending_notifications": claim_pending_notifications,
        "mark_notification_failed": mark_notification_failed,
        "mark_notification_sent": mark_notification_sent,
        "pending_notifications": pending_notifications,
        "_claim_managed_key_repair": _claim_managed_key_repair,
        "_managed_repair_failed": _managed_repair_failed,
        "_managed_repair_manual": _managed_repair_manual,
        "_managed_repair_max_attempts": _managed_repair_max_attempts,
        "_mark_managed_repair_converged": _mark_managed_repair_converged,
        "_process_managed_key_repair": _process_managed_key_repair,
        "_repair_key_idempotent": _repair_key_idempotent,
        "_sync_identity_binding": _sync_identity_binding,
        "process_managed_key_repairs": process_managed_key_repairs,
    }.items()
}

STATIC_WORKER_IMPLEMENTATIONS = {
    "_managed_repair_max_attempts",
    "_metric_for_key",
    "_required_scale_observations",
    "_scale_advice",
    "_scale_observation_interval_seconds",
}
