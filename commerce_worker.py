"""Compatibility facade for the decomposed commerce worker handlers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from commerce_worker_api import CommerceWorkerApi
from commerce_worker_dispatch import WORKER_IMPLEMENTATIONS


class CommerceWorkerMixin(CommerceWorkerApi):
    """Preserve the historical mixin API for integrations during migration."""

    @staticmethod
    def _managed_repair_max_attempts() -> int:
        return WORKER_IMPLEMENTATIONS["_managed_repair_max_attempts"]()

    @staticmethod
    def _metric_for_key(metrics: Any, key_id: str) -> int | None:
        return WORKER_IMPLEMENTATIONS["_metric_for_key"](metrics, key_id)

    @staticmethod
    def _required_scale_observations() -> int:
        return WORKER_IMPLEMENTATIONS["_required_scale_observations"]()

    @staticmethod
    def _scale_advice(servers: list[dict[str, Any]]) -> dict[str, Any]:
        return WORKER_IMPLEMENTATIONS["_scale_advice"](servers)

    @staticmethod
    def _scale_observation_interval_seconds() -> int:
        return WORKER_IMPLEMENTATIONS["_scale_observation_interval_seconds"]()

    def process_jobs(self, now: datetime | None = None, max_jobs: int = 10) -> int:
        return WORKER_IMPLEMENTATIONS["process_jobs"](self, now, max_jobs)

    def expire_and_process(self, now: datetime | None = None) -> int:
        return WORKER_IMPLEMENTATIONS["expire_and_process"](self, now)

    def enforce_quotas(
        self,
        now: datetime | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> int:
        return WORKER_IMPLEMENTATIONS["enforce_quotas"](self, now, metrics)

    def queue_quota_warnings(
        self,
        now: datetime | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> int:
        return WORKER_IMPLEMENTATIONS["queue_quota_warnings"](self, now, metrics)

    def process_managed_key_repairs(
        self, now: datetime | None = None, max_jobs: int = 5
    ) -> int:
        return WORKER_IMPLEMENTATIONS["process_managed_key_repairs"](self, now, max_jobs)

    def process_endpoint_migrations(
        self, now: datetime | None = None, max_jobs: int = 5
    ) -> int:
        return WORKER_IMPLEMENTATIONS["process_endpoint_migrations"](self, now, max_jobs)

    def failed_jobs(
        self, limit: int = 20, include_nonterminal: bool = False
    ) -> list[dict[str, Any]]:
        return WORKER_IMPLEMENTATIONS["failed_jobs"](self, limit, include_nonterminal)

    def retry_job(self, job_id: str, admin_id: int, now: datetime | None = None) -> str:
        return WORKER_IMPLEMENTATIONS["retry_job"](self, job_id, admin_id, now)

    def retry_failed_job(
        self,
        order_id: str,
        admin_id: int,
        now: datetime | None = None,
        operation: str | None = None,
    ) -> str:
        return WORKER_IMPLEMENTATIONS["retry_failed_job"](self, order_id, admin_id, now, operation)

    def capacity_snapshot(
        self, now: datetime | None = None, *, refresh_inventory: bool = True
    ) -> dict[str, Any]:
        return WORKER_IMPLEMENTATIONS["capacity_snapshot"](self, now, refresh_inventory=refresh_inventory)

    def pending_notifications(
        self, now: datetime | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        return WORKER_IMPLEMENTATIONS["pending_notifications"](self, now, limit)

    def claim_pending_notifications(
        self,
        now: datetime | None = None,
        limit: int = 20,
        lease_seconds: int = 120,
    ) -> list[dict[str, Any]]:
        return WORKER_IMPLEMENTATIONS["claim_pending_notifications"](self, now, limit, lease_seconds)

    def mark_notification_sent(
        self, notification_id: str, now: datetime | None = None
    ) -> None:
        WORKER_IMPLEMENTATIONS["mark_notification_sent"](self, notification_id, now)

    def mark_notification_failed(
        self, notification_id: str, now: datetime | None = None
    ) -> None:
        WORKER_IMPLEMENTATIONS["mark_notification_failed"](self, notification_id, now)
