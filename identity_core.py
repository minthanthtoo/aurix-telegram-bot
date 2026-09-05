"""Shared transactional primitives for identity mixins."""

from __future__ import annotations

from typing import Any

from identity_generations_repository import IdentityGenerationsRepository


class IdentityCoreMixin:
    quota_repository = IdentityGenerationsRepository()

    @staticmethod
    def _lock_entitlement(connection: Any, entitlement_id: str) -> None:
        """Serialize aggregate quota mutations on PostgreSQL."""
        IdentityGenerationsRepository._lock_entitlement(connection, entitlement_id)

    @staticmethod
    def _append_quota_ledger(
        connection: Any,
        *,
        entitlement_id: str,
        event_type: str,
        bytes_value: int,
        consumed_bytes: int,
        remaining_bytes: int,
        idempotency_key: str,
        now: str,
        generation_id: str | None = None,
        endpoint_id: str | None = None,
        lease_id: str | None = None,
        epoch_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        """Append one immutable quota event, returning whether it was new."""
        return IdentityGenerationsRepository._append_quota_ledger(
            connection,
            entitlement_id=entitlement_id,
            generation_id=generation_id,
            endpoint_id=endpoint_id,
            lease_id=lease_id,
            epoch_id=epoch_id,
            event_type=event_type,
            bytes_value=bytes_value,
            consumed_bytes=consumed_bytes,
            remaining_bytes=remaining_bytes,
            idempotency_key=idempotency_key,
            details=details,
            now=now,
        )
