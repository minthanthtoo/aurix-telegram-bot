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
