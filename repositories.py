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
