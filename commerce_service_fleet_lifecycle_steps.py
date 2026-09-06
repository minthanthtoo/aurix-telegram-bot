"""Validation and persistence steps for endpoint lifecycle transitions."""

from __future__ import annotations

from typing import Any

from commerce_models import CommerceError, _now_text
from connectivity_registry import ConnectivityRegistry
from lifecycle_policy import normalize_lifecycle_state


def validate_lifecycle_request(
    lifecycle_state: str,
    server_id: str,
    reason: str | None,
) -> tuple[str, str, str, str | None]:
    try:
        state = normalize_lifecycle_state(lifecycle_state, strict=True)
    except ValueError as exc:
        raise CommerceError("Endpoint lifecycle must be active, draining or retired") from exc
    server = str(server_id or "").strip()
    if not server:
        raise CommerceError("Endpoint identity is required")
    now_text = _now_text()
    clean_reason = str(reason or "").strip()[:512] or None
    return state, server, now_text, clean_reason


def assert_retirement_ready(
    fleet_health: Any, service: Any, connection: Any, row: Any, server: str
) -> None:
    blockers: list[str] = []
    counts = fleet_health.retirement_counts(
        connection,
        server,
        include_free_keys=service._table_exists(connection, "keys"),
        include_free_intents=service._table_exists(
            connection, "free_provisioning_intents"
        ),
    )
    if counts["active_free"]:
        blockers.append(f"{counts['active_free']} active free/promo key(s)")
    if counts["active_paid"]:
        blockers.append(f"{counts['active_paid']} active paid key(s)")
    if counts["pending_orders"]:
        blockers.append(f"{counts['pending_orders']} open order(s)")
    if counts["pending_subscriptions"]:
        blockers.append(
            f"{counts['pending_subscriptions']} active/pending subscription(s)"
        )
    if counts["pending_intents"]:
        blockers.append(f"{counts['pending_intents']} pending provisioning intent(s)")
    remote_count = row["remote_key_count"]
    orphan_count = int(row["remote_orphan_key_count"] or 0)
    if remote_count is None:
        blockers.append("remote inventory has not been reconciled")
    elif int(remote_count or 0) != 0:
        blockers.append(f"remote inventory still has {int(remote_count)} key(s)")
    if orphan_count:
        blockers.append(f"{orphan_count} unreviewed remote key(s)")
    if blockers:
        raise CommerceError("Endpoint cannot be retired yet: " + "; ".join(blockers))


def persist_lifecycle_change(
    fleet_health: Any,
    service: Any,
    connection: Any,
    *,
    row: Any,
    server: str,
    state: str,
    previous: str,
    actor_id: int,
    clean_reason: str | None,
    now_text: str,
) -> None:
    fleet_health.set_lifecycle(
        connection,
        server_id=server,
        enabled=state in {"active", "draining"},
        state=state,
        reason=clean_reason,
        now_text=now_text,
    )
    ConnectivityRegistry.sync_outline_health(
        connection,
        server_id=server,
        lifecycle_state=state,
        health_status=str(row["health_status"] or "unknown"),
        now_text=now_text,
    )
    service._audit(
        connection,
        "server_lifecycle_changed",
        "outline_server",
        server,
        "owner",
        str(actor_id),
        {
            "previous_state": previous,
            "lifecycle_state": state,
            "reason": clean_reason,
        },
    )
