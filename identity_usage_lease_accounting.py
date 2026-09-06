"""Lease allocation and consumption steps for usage accounting."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from identity_support import _parse_time


def prepare_usage_leases(
    service: Any,
    repository: Any,
    connection: Any,
    *,
    binding: dict[str, Any],
    endpoint_id: str,
    quota: int,
    consumed_before: int,
    epoch_id: str,
    credited: int,
    timestamp: str,
) -> list[dict[str, Any]]:
    """Ensure enough lease capacity exists for a usage sample."""
    lease_rows = repository.active_leases(
        connection,
        entitlement_id=str(binding["entitlement_id"]),
        generation_id=str(binding["generation_id"]),
        now_text=timestamp,
    )
    available_capacity = sum(
        max(0, int(item["lease_bytes"]) - int(item["used_bytes"] or 0))
        for item in lease_rows
    )
    while available_capacity < credited:
        block = min(10 * 1024 * 1024 * 1024, credited - available_capacity)
        lease_rows.append(
            _create_runtime_lease(
                service,
                repository,
                connection,
                binding=binding,
                endpoint_id=endpoint_id,
                block_bytes=block,
                quota=quota,
                consumed_before=consumed_before,
                epoch_id=epoch_id,
                timestamp=timestamp,
            )
        )
        available_capacity += block
    if not lease_rows and consumed_before < quota:
        block = min(10 * 1024 * 1024 * 1024, max(1, quota - consumed_before))
        lease_rows.append(
            _create_runtime_lease(
                service,
                repository,
                connection,
                binding=binding,
                endpoint_id=endpoint_id,
                block_bytes=block,
                quota=quota,
                consumed_before=consumed_before,
                epoch_id=epoch_id,
                timestamp=timestamp,
            )
        )
    return lease_rows


def consume_usage_leases(
    repository: Any,
    connection: Any,
    lease_rows: list[dict[str, Any]],
    *,
    credited: int,
    quota: int,
    consumed_before: int,
    timestamp: str,
) -> tuple[int, str | None]:
    """Consume the credited bytes in lease order and return the primary lease."""
    lease_remaining = credited
    primary_lease_id: str | None = None
    for lease in lease_rows:
        available = max(0, int(lease["lease_bytes"]) - int(lease["used_bytes"] or 0))
        allocation = min(lease_remaining, available)
        if allocation:
            if primary_lease_id is None:
                primary_lease_id = str(lease["lease_id"])
            new_used = int(lease["used_bytes"] or 0) + allocation
            lease_status = "exhausted" if new_used >= int(lease["lease_bytes"]) else "active"
            repository.consume_lease(
                connection,
                lease_id=str(lease["lease_id"]),
                used_bytes=new_used,
                status=lease_status,
                now_text=timestamp,
            )
            lease_remaining -= allocation
        if lease_remaining <= 0:
            break
    return min(quota, consumed_before + credited), primary_lease_id


def _create_runtime_lease(
    service: Any,
    repository: Any,
    connection: Any,
    *,
    binding: dict[str, Any],
    endpoint_id: str,
    block_bytes: int,
    quota: int,
    consumed_before: int,
    epoch_id: str,
    timestamp: str,
) -> dict[str, Any]:
    lease_id = f"lease-{secrets.token_hex(16)}"
    lease_expires = min(
        _parse_time(str(binding["expires_at"])),
        _parse_time(timestamp) + timedelta(days=30),
    ).isoformat()
    repository.create_lease(
        connection,
        lease_id=lease_id,
        entitlement_id=str(binding["entitlement_id"]),
        generation_id=str(binding["generation_id"]),
        endpoint_id=endpoint_id,
        lease_bytes=int(block_bytes),
        expires_at=lease_expires,
        now_text=timestamp,
    )
    service._append_quota_ledger(
        connection,
        entitlement_id=str(binding["entitlement_id"]),
        generation_id=str(binding["generation_id"]),
        endpoint_id=endpoint_id,
        lease_id=lease_id,
        event_type="grant",
        bytes_value=int(block_bytes),
        consumed_bytes=consumed_before,
        remaining_bytes=max(0, quota - consumed_before),
        idempotency_key=f"grant:{lease_id}",
        details={"reason": "usage_observation", "expires_at": lease_expires},
        now=timestamp,
    )
    return {"lease_id": lease_id, "lease_bytes": int(block_bytes), "used_bytes": 0}
