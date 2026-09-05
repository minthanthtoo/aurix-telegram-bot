"""Compatibility facade for the decomposed commerce application service."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from cryptography.fernet import Fernet

from commerce_service_dispatch import SERVICE_IMPLEMENTATIONS, STATIC_SERVICE_IMPLEMENTATIONS
from commerce_service_support import LOCAL_PAYMENT_METHODS
from commerce_worker_coordinator import CommerceWorker
from connectivity_adapters import ConnectivityAdapterRegistry
from identity import IdentityService
from lifecycle_policy import normalize_lifecycle_state
from ports import CommerceWorkerPort, OutlineGateway, ReceiptStorageGateway
from receipt_rules import load_recipient_profiles
import repositories
from commerce_order_repository import OrderRepository
from commerce_customer_repository import CustomerRepository
from commerce_failover_repository import FailoverRepository
from commerce_payment_repository import PaymentRepository
from commerce_wallet_read_repository import WalletReadRepository
from commerce_wallet_approval_repository import WalletApprovalRepository
from commerce_inventory_reconciliation_repository import InventoryReconciliationRepository
from route_failover import RouteFailoverService
from supabase_storage import NullReceiptStorage


class CommerceService:
    """Stable application-service facade over responsibility-owned use cases."""

    inventory_reconciliation = InventoryReconciliationRepository()
    wallet_reads = WalletReadRepository()

    def __init__(
        self,
        database: repositories.RepositoryDatabase,
        outline: OutlineGateway,
        access_url_key: bytes | str,
        allow_legacy_text_approval: bool = False,
        receipt_storage: ReceiptStorageGateway | None = None,
        receipt_storage_required: bool = False,
    ):
        self.database = database
        self.outline = outline
        self.orders: repositories.OrderRepositoryPort = OrderRepository()
        self.customer_reads = CustomerRepository()
        self.failover_reads = FailoverRepository()
        self.payments: repositories.PaymentRepositoryPort = PaymentRepository()
        self.wallet_approvals = WalletApprovalRepository()
        # Kept only for migration tests; deployments require evidence or a reservation.
        self.allow_legacy_text_approval = bool(allow_legacy_text_approval)
        self.receipt_storage = receipt_storage or NullReceiptStorage()
        self.receipt_storage_required = bool(receipt_storage_required)
        self.receipt_recipient_profiles = load_recipient_profiles()
        self._server_metrics_cache: dict[str, dict[str, Any]] = {}
        self.probe_service: Any | None = None
        self.identity = IdentityService(database)
        self.adapter_registry = ConnectivityAdapterRegistry()
        self.failover = RouteFailoverService(database)
        self.worker: CommerceWorkerPort = CommerceWorker(self)
        try:
            self.access_url_cipher = Fernet(access_url_key)
        except (TypeError, ValueError) as exc:
            raise ValueError("AURIX_ACCESS_URL_KEY must be a Fernet key") from exc

    @staticmethod
    def _implementation(name: str) -> Any:
        try:
            return SERVICE_IMPLEMENTATIONS[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return self._implementation(name)(self, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        implementation = self._implementation(name)
        if name in STATIC_SERVICE_IMPLEMENTATIONS:
            return implementation
        return implementation.__get__(self, type(self))

    def initialize(self) -> None:
        self._call("initialize")

    def process_jobs(self, now: datetime | None = None, max_jobs: int = 10) -> int:
        return self.worker.process_jobs(now, max_jobs)

    def expire_and_process(self, now: datetime | None = None) -> int:
        return self.worker.expire_and_process(now)

    def enforce_quotas(
        self,
        now: datetime | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> int:
        return self.worker.enforce_quotas(now, metrics)

    def queue_quota_warnings(
        self,
        now: datetime | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> int:
        return self.worker.queue_quota_warnings(now, metrics)

    def process_managed_key_repairs(
        self, now: datetime | None = None, max_jobs: int = 5
    ) -> int:
        return self.worker.process_managed_key_repairs(now, max_jobs)

    def process_endpoint_migrations(
        self, now: datetime | None = None, max_jobs: int = 5
    ) -> int:
        return self.worker.process_endpoint_migrations(now, max_jobs)

    def failed_jobs(
        self, limit: int = 20, include_nonterminal: bool = False
    ) -> list[dict[str, Any]]:
        return self.worker.failed_jobs(limit, include_nonterminal)

    def retry_job(self, job_id: str, admin_id: int, now: datetime | None = None) -> str:
        return self.worker.retry_job(job_id, admin_id, now)

    def retry_failed_job(
        self,
        order_id: str,
        admin_id: int,
        now: datetime | None = None,
        operation: str | None = None,
    ) -> str:
        return self.worker.retry_failed_job(order_id, admin_id, now, operation)

    def capacity_snapshot(
        self, now: datetime | None = None, *, refresh_inventory: bool = True
    ) -> dict[str, Any]:
        return self.worker.capacity_snapshot(now, refresh_inventory=refresh_inventory)

    def pending_notifications(
        self, now: datetime | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        return self.worker.pending_notifications(now, limit)

    def claim_pending_notifications(
        self,
        now: datetime | None = None,
        limit: int = 20,
        lease_seconds: int = 120,
    ) -> list[dict[str, Any]]:
        return self.worker.claim_pending_notifications(now, limit, lease_seconds)

    def mark_notification_sent(
        self, notification_id: str, now: datetime | None = None
    ) -> None:
        self.worker.mark_notification_sent(notification_id, now)

    def mark_notification_failed(
        self, notification_id: str, now: datetime | None = None
    ) -> None:
        self.worker.mark_notification_failed(notification_id, now)

    @staticmethod
    def _scale_advice(servers: list[dict[str, Any]]) -> dict[str, Any]:
        from capacity_policy import compute_scale_advice

        return compute_scale_advice(servers)

    @staticmethod
    def _repair_blocks_access(repair_status: Any) -> bool:
        return SERVICE_IMPLEMENTATIONS["_repair_blocks_access"](repair_status)

    @staticmethod
    def _receipt_storage_extension(mime_type: str) -> str:
        return SERVICE_IMPLEMENTATIONS["_receipt_storage_extension"](mime_type)

    @staticmethod
    def _receipt_storage_path(order_id: str, evidence_id: str, mime_type: str) -> str:
        return SERVICE_IMPLEMENTATIONS["_receipt_storage_path"](order_id, evidence_id, mime_type)

    @staticmethod
    def _lock_order(connection: Any, order_id: str) -> None:
        SERVICE_IMPLEMENTATIONS["_lock_order"](connection, order_id)

    @staticmethod
    def _assert_no_active_promo(connection: Any, telegram_id: int) -> None:
        SERVICE_IMPLEMENTATIONS["_assert_no_active_promo"](connection, telegram_id)

    @staticmethod
    def _table_exists(connection: Any, table_name: str) -> bool:
        return SERVICE_IMPLEMENTATIONS["_table_exists"](connection, table_name)

    @staticmethod
    def _metric_bytes(value: Any) -> int:
        return SERVICE_IMPLEMENTATIONS["_metric_bytes"](value)

    @staticmethod
    def _health_threshold(name: str, default: int) -> int:
        return SERVICE_IMPLEMENTATIONS["_health_threshold"](name, default)

    @staticmethod
    def _receipt_timestamp(value: Any) -> datetime:
        return SERVICE_IMPLEMENTATIONS["_receipt_timestamp"](value)

    @staticmethod
    def _ensure_user(*args: Any, **kwargs: Any) -> None:
        SERVICE_IMPLEMENTATIONS["_ensure_user"](*args, **kwargs)

    @staticmethod
    def _audit(*args: Any, **kwargs: Any) -> None:
        SERVICE_IMPLEMENTATIONS["_audit"](*args, **kwargs)

    @staticmethod
    def _queue_staff_notification(*args: Any, **kwargs: Any) -> None:
        SERVICE_IMPLEMENTATIONS["_queue_staff_notification"](*args, **kwargs)

    @staticmethod
    def _queue_customer_repair_notification(*args: Any, **kwargs: Any) -> None:
        SERVICE_IMPLEMENTATIONS["_queue_customer_repair_notification"](*args, **kwargs)

    @staticmethod
    def _queue_receipt_extraction(*args: Any, **kwargs: Any) -> None:
        SERVICE_IMPLEMENTATIONS["_queue_receipt_extraction"](*args, **kwargs)

    @classmethod
    def _managed_repair_cached_usage_is_recent(
        cls, row: Any, observed_at: Any
    ) -> bool:
        return SERVICE_IMPLEMENTATIONS["_managed_repair_cached_usage_is_recent"](
            cls, row, observed_at
        )

    @staticmethod
    def _managed_repair_allow_unknown_usage() -> bool:
        return SERVICE_IMPLEMENTATIONS["_managed_repair_allow_unknown_usage"]()

    @staticmethod
    def _managed_repair_cached_usage_max_age_seconds() -> int:
        return SERVICE_IMPLEMENTATIONS["_managed_repair_cached_usage_max_age_seconds"]()

    @staticmethod
    def _managed_repair_key_name(*args: Any, **kwargs: Any) -> str:
        return SERVICE_IMPLEMENTATIONS["_managed_repair_key_name"](*args, **kwargs)

    @staticmethod
    def _managed_repair_observation_interval_seconds() -> int:
        return SERVICE_IMPLEMENTATIONS["_managed_repair_observation_interval_seconds"]()

    @staticmethod
    def _managed_repair_required_observations() -> int:
        return SERVICE_IMPLEMENTATIONS["_managed_repair_required_observations"]()

    @staticmethod
    def _usage_snapshot_interval_seconds() -> int:
        return SERVICE_IMPLEMENTATIONS["_usage_snapshot_interval_seconds"]()

    @staticmethod
    def _validate_server_allocation_capacity(*args: Any, **kwargs: Any) -> None:
        SERVICE_IMPLEMENTATIONS["_validate_server_allocation_capacity"](*args, **kwargs)
