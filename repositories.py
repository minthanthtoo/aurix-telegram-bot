"""Structural repository contracts used while the modular monolith is extracted."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class RepositoryDatabase(Protocol):
    """Minimum transaction boundary required by domain services."""

    def connect(self) -> AbstractContextManager[Any]: ...

    def begin_write(self, connection: Any) -> None: ...

    def is_integrity_error(self, error: Exception) -> bool: ...

    def initialize(self) -> None: ...


@runtime_checkable
class HostedRepositoryDatabase(RepositoryDatabase, Protocol):
    """Repository whose external connection resources need process shutdown."""

    def close(self) -> None: ...


@runtime_checkable
class OrderRepositoryPort(Protocol):
    """Transaction-neutral queries required by the commerce order workflow."""

    def get(self, connection: Any, order_id: str) -> Any: ...

    def get_owned(self, connection: Any, order_id: str, telegram_id: int) -> Any: ...

    def find_open_for_user(self, connection: Any, telegram_id: int) -> Any: ...

    def get_payment_context(self, connection: Any, order_id: str) -> Any: ...


@runtime_checkable
class PaymentRepositoryPort(Protocol):
    """Payment/evidence reads used by order workflows."""

    def activity_counts(self, connection: Any, order_id: str) -> tuple[int, int]: ...

    def has_evidence(self, connection: Any, order_id: str) -> bool: ...

    def payments_for_order(self, connection: Any, order_id: str) -> list[Any]: ...

    def latest_payment(self, connection: Any, order_id: str) -> Any: ...

    def latest_eligible_payment(self, connection: Any, order_id: str) -> Any: ...

    def verified_payment(self, connection: Any, order_id: str) -> Any: ...

    def verified_evidence(self, connection: Any, order_id: str) -> Any: ...

    def latest_evidence_review(self, connection: Any, order_id: str) -> Any: ...

    def latest_evidence_for_approval(self, connection: Any, order_id: str) -> Any: ...

    def verified_evidence_amount(self, connection: Any, order_id: str) -> Any: ...

    def find_exact_duplicate(
        self,
        connection: Any,
        image_sha256: str,
        file_unique_id: str | None,
        exclude_order_id: str | None = None,
    ) -> list[Any]: ...

    def prior_phash_rows(self, connection: Any, exclude_order_id: str | None = None) -> list[Any]: ...

    def existing_evidence(self, connection: Any, order_id: str, image_sha256: str) -> Any: ...

    def transaction_candidates(
        self,
        connection: Any,
        exclude_order_id: str,
        exclude_evidence_id: str | None = None,
    ) -> list[Any]: ...

    def list_pending_receipts(self, connection: Any, limit: int) -> list[Any]: ...

    def get_receipt(self, connection: Any, evidence_id: str) -> Any: ...


@runtime_checkable
class WalletApprovalRepositoryPort(Protocol):
    """Persistence commands used inside one order-approval transaction."""

    def subscription_id_for_order(self, connection: Any, order_id: str) -> str | None: ...

    def wallet_reservation(self, connection: Any, order_id: str) -> Any: ...

    def plan(self, connection: Any, plan_code: str) -> Any: ...

    def ensure_wallet(
        self, connection: Any, *, telegram_id: int, currency: str, now_text: str
    ) -> None: ...

    def credit_once(
        self,
        connection: Any,
        *,
        entry_id: str,
        telegram_id: int,
        amount_minor: int,
        currency: str,
        reference_type: str,
        reference_id: str,
        idempotency_key: str,
        now_text: str,
    ) -> bool: ...

    def reserve_once(
        self,
        connection: Any,
        *,
        ledger_entry_id: str,
        reservation_id: str,
        telegram_id: int,
        order_id: str,
        amount_minor: int,
        currency: str,
        idempotency_key: str,
        now_text: str,
    ) -> bool: ...

    def capture_once(
        self,
        connection: Any,
        *,
        ledger_entry_id: str,
        reservation_id: str,
        telegram_id: int,
        order_id: str,
        amount_minor: int,
        currency: str,
        idempotency_key: str,
        now_text: str,
    ) -> None: ...

    def mark_order_approved(
        self, connection: Any, order_id: str, approved_at: str
    ) -> None: ...

    def mark_payment_verified(
        self, connection: Any, payment_id: str, verified_at: str
    ) -> None: ...

    def create_subscription(
        self,
        connection: Any,
        *,
        subscription_id: str,
        order_id: str,
        telegram_id: int,
        plan_code: str,
        starts_at: str,
        expires_at: str,
        plan_name: str,
        quota_bytes: int | None,
        duration_days: int,
        server_id: str | None,
    ) -> None: ...

    def queue_provisioning(
        self,
        connection: Any,
        *,
        job_id: str,
        subscription_id: str,
        next_attempt_at: str,
        created_at: str,
    ) -> None: ...

    def queue_notification(
        self,
        connection: Any,
        *,
        notification_id: str,
        dedupe_key: str,
        telegram_id: int,
        kind: str,
        text: str,
        now_text: str,
    ) -> None: ...


@runtime_checkable
class IdentityUsageRepositoryPort(Protocol):
    """Persistence operations used inside one remote-usage transaction."""

    def active_binding(self, connection: Any, endpoint_id: str, external_id: str) -> Any: ...

    def active_epoch(self, connection: Any, **criteria: Any) -> Any: ...

    def latest_epoch_no(self, connection: Any, **criteria: Any) -> int: ...

    def create_epoch(self, connection: Any, **values: Any) -> None: ...

    def mark_epoch_reset(self, connection: Any, epoch_id: str, now_text: str) -> None: ...

    def update_epoch_remote(self, connection: Any, **values: Any) -> None: ...

    def duplicate_sample(self, connection: Any, **criteria: Any) -> Any: ...

    def record_sample(self, connection: Any, **values: Any) -> None: ...

    def active_leases(self, connection: Any, **criteria: Any) -> list[dict[str, Any]]: ...

    def create_lease(self, connection: Any, **values: Any) -> None: ...

    def consume_lease(self, connection: Any, **values: Any) -> None: ...

    def credit_epoch(self, connection: Any, **values: Any) -> None: ...

    def set_entitlement_consumed(self, connection: Any, **values: Any) -> None: ...


@runtime_checkable
class InventoryReconciliationRepositoryPort(Protocol):
    """Persistence operations used during remote inventory reconciliation."""

    def enabled_server_ids(self, connection: Any) -> list[str]: ...

    def remote_key_ledger(
        self, connection: Any, server_id: str
    ) -> dict[str, dict[str, Any]]: ...

    def managed_key_ids(
        self, connection: Any, server_id: str, *, include_free_keys: bool
    ) -> set[str]: ...

    def mark_present_keys_missing(self, connection: Any, server_id: str) -> None: ...

    def upsert_present_key(self, connection: Any, **values: Any) -> None: ...

    def update_managed_usage(self, connection: Any, **values: Any) -> None: ...

    def upsert_missing_key(self, connection: Any, **values: Any) -> None: ...

    def unreviewed_orphan_count(self, connection: Any, server_id: str) -> int: ...

    def update_server_metrics(self, connection: Any, **values: Any) -> None: ...

    def lifecycle_state(self, connection: Any, server_id: str) -> str: ...


@runtime_checkable
class CapacitySnapshotRepositoryPort(Protocol):
    """Persistence read model for capacity and admission snapshots."""

    def snapshot_inputs(self, connection: Any, **criteria: Any) -> dict[str, Any]: ...

    def server_commitments(self, connection: Any, **criteria: Any) -> dict[str, int]: ...


@runtime_checkable
class ProvisioningRepositoryPort(Protocol):
    """Persistence operations for one paid provisioning job."""

    def context(self, connection: Any, subscription_id: str) -> tuple[Any, Any]: ...

    def defer_job(self, connection: Any, job_id: str, next_attempt_at: str) -> None: ...

    def expire_subscription(
        self, connection: Any, subscription_id: str, expected_status: str
    ) -> None: ...

    def mark_job_expired(self, connection: Any, job_id: str) -> None: ...

    def insert_key(self, connection: Any, **values: Any) -> None: ...

    def activate_subscription(self, connection: Any, **values: Any) -> None: ...

    def queue_ready_notification(self, connection: Any, **values: Any) -> None: ...

    def mark_job_done(self, connection: Any, job_id: str) -> None: ...


@runtime_checkable
class FleetHealthRepositoryPort(Protocol):
    """Persistence operations for endpoint lifecycle and health transitions."""

    def server(self, connection: Any, server_id: str) -> Any: ...

    def retirement_counts(self, connection: Any, server_id: str, **flags: Any) -> dict[str, int]: ...

    def set_lifecycle(self, connection: Any, **values: Any) -> None: ...

    def health_state_for_update(self, connection: Any, server_id: str) -> Any: ...

    def duplicate_observation(
        self, connection: Any, server_id: str, observed_at: str
    ) -> Any: ...

    def update_health(self, connection: Any, **values: Any) -> None: ...

    def record_observation(self, connection: Any, **values: Any) -> None: ...
