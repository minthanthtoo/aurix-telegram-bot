"""Explicit private handler surface for commerce worker composition."""

from __future__ import annotations

from typing import Any

from commerce_worker_dispatch import WORKER_IMPLEMENTATIONS


class CommerceWorkerApi:
    """Expose every responsibility-owned worker helper as a real method."""

    def _invoke_worker(self, handler_name: str, *args: Any, **kwargs: Any) -> Any:
        return WORKER_IMPLEMENTATIONS[handler_name](self, *args, **kwargs)

    def _metrics_by_server(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_metrics_by_server", *args, **kwargs)

    def _record_scale_observation(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_record_scale_observation", *args, **kwargs)

    @staticmethod
    def _required_scale_observations(*args: Any, **kwargs: Any) -> Any:
        return WORKER_IMPLEMENTATIONS["_required_scale_observations"](*args, **kwargs)

    @staticmethod
    def _scale_advice(*args: Any, **kwargs: Any) -> Any:
        return WORKER_IMPLEMENTATIONS["_scale_advice"](*args, **kwargs)

    @staticmethod
    def _scale_observation_interval_seconds(*args: Any, **kwargs: Any) -> Any:
        return WORKER_IMPLEMENTATIONS["_scale_observation_interval_seconds"](
            *args, **kwargs
        )

    def _claim_job(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_claim_job", *args, **kwargs)

    def _job_done(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_job_done", *args, **kwargs)

    def _job_failed(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_job_failed", *args, **kwargs)

    def _expire(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_expire", *args, **kwargs)

    def _find_key(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_find_key", *args, **kwargs)

    def _provision(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_provision", *args, **kwargs)

    def _revoke(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_revoke", *args, **kwargs)

    def _revoke_legacy_free_keys(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_revoke_legacy_free_keys", *args, **kwargs)

    def _claim_endpoint_migration(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_claim_endpoint_migration", *args, **kwargs)

    def _create_migration_key(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_create_migration_key", *args, **kwargs)

    def _delete_migration_source(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_delete_migration_source", *args, **kwargs)

    def _endpoint_migration_completed(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_endpoint_migration_completed", *args, **kwargs)

    def _endpoint_migration_failed(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_endpoint_migration_failed", *args, **kwargs)

    def _mark_migration_source_delete_retry(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_mark_migration_source_delete_retry", *args, **kwargs)

    @staticmethod
    def _metric_for_key(*args: Any, **kwargs: Any) -> Any:
        return WORKER_IMPLEMENTATIONS["_metric_for_key"](*args, **kwargs)

    def _process_endpoint_migration(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_process_endpoint_migration", *args, **kwargs)

    def _hydrate_notifications(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_hydrate_notifications", *args, **kwargs)

    def _claim_managed_key_repair(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_claim_managed_key_repair", *args, **kwargs)

    def _managed_repair_failed(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_managed_repair_failed", *args, **kwargs)

    def _managed_repair_manual(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_managed_repair_manual", *args, **kwargs)

    @staticmethod
    def _managed_repair_max_attempts(*args: Any, **kwargs: Any) -> Any:
        return WORKER_IMPLEMENTATIONS["_managed_repair_max_attempts"](*args, **kwargs)

    def _mark_managed_repair_converged(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_mark_managed_repair_converged", *args, **kwargs)

    def _process_managed_key_repair(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_process_managed_key_repair", *args, **kwargs)

    def _repair_key_idempotent(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_repair_key_idempotent", *args, **kwargs)

    def _sync_identity_binding(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke_worker("_sync_identity_binding", *args, **kwargs)
