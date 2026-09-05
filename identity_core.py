"""Shared transactional primitives for identity mixins."""

from __future__ import annotations

import json
import secrets
from typing import Any

from identity_support import IdentityError


class IdentityCoreMixin:
    @staticmethod
    def _lock_entitlement(connection: Any, entitlement_id: str) -> None:
        """Serialize aggregate quota mutations on PostgreSQL."""
        if connection.__class__.__name__ == "_PostgresConnection":
            connection.execute(
                "SELECT entitlement_id FROM entitlements WHERE entitlement_id = ? FOR UPDATE",
                (str(entitlement_id),),
            ).fetchone()

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
        if event_type not in {
            "grant",
            "usage",
            "release",
            "exhaust",
            "counter_reset",
            "reconcile",
        }:
            raise IdentityError("quota ledger event type is invalid")
        result = connection.execute(
            """INSERT INTO entitlement_quota_ledger
               (entry_id, entitlement_id, generation_id, endpoint_id, lease_id, epoch_id,
                event_type, bytes, consumed_bytes, remaining_bytes, idempotency_key,
                details_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(idempotency_key) DO NOTHING""",
            (
                f"ledger-{secrets.token_hex(16)}",
                str(entitlement_id),
                str(generation_id) if generation_id is not None else None,
                str(endpoint_id) if endpoint_id is not None else None,
                str(lease_id) if lease_id is not None else None,
                str(epoch_id) if epoch_id is not None else None,
                event_type,
                max(0, int(bytes_value)),
                max(0, int(consumed_bytes)),
                max(0, int(remaining_bytes)),
                str(idempotency_key),
                json.dumps(details or {}, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
                str(now),
            ),
        )
        return int(getattr(result, "rowcount", 0) or 0) == 1
