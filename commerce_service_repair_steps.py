"""Decision, persistence, and notification steps for managed-key repair."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from commerce_models import _human_bytes, _new_id
from commerce_repairs_repository import ManagedRepairRepository


_REPAIRS = ManagedRepairRepository()


@dataclass(frozen=True, slots=True)
class ManagedRepairDecision:
    repair_id: str
    local_ref: str
    server_id: str
    kind: str
    quota: int
    name: str
    effective_usage: int | None
    status: str
    error: str | None
    job_state: str
    alert_needed: bool


def resolve_repair_decision(
    service: Any,
    row: dict[str, Any],
    *,
    observed_at: str,
    usage_bytes: int | None,
    repair_id: str,
    previous_name: str | None,
) -> tuple[str, int | None, str, str | None, str]:
    """Resolve bounded usage evidence into a conservative repair status."""
    quota = int(row["quota_bytes"] or 0)
    effective_usage = usage_bytes
    usage_is_fresh = usage_bytes is not None
    if effective_usage is None:
        try:
            effective_usage = int(row.get("last_usage_bytes"))
        except (TypeError, ValueError):
            effective_usage = None
        usage_is_fresh = False
    if not usage_is_fresh and service._managed_repair_cached_usage_is_recent(row, observed_at):
        usage_is_fresh = effective_usage is not None
    if not usage_is_fresh and os.environ.get(
        "AURIX_KEY_REPAIR_ALLOW_STALE_USAGE", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}:
        usage_is_fresh = effective_usage is not None
    if effective_usage is not None:
        effective_usage = max(0, effective_usage)
    if not usage_is_fresh and not service._managed_repair_allow_unknown_usage():
        status = "manual"
        error = "usage_observation_required"
    elif effective_usage is not None and effective_usage >= quota:
        status = "manual"
        error = "quota_already_exhausted"
    else:
        status = "pending"
        error = None
    return (
        service._managed_repair_key_name(row, previous_name),
        effective_usage,
        status,
        error,
        repair_id,
    )


def persist_repair_decision(
    service: Any,
    connection: Any,
    row: dict[str, Any],
    *,
    observed_at: str,
    missing_observation_count: int,
    previous_name: str | None,
    usage_bytes: int | None,
) -> ManagedRepairDecision:
    """Persist or reopen one repair episode and record its audit event."""
    local_ref = str(row["local_key_ref"])
    server_id = str(row["server_id"])
    kind = str(row["kind"])
    quota = int(row["quota_bytes"] or 0)
    existing = _REPAIRS.existing_repair(connection, server_id, kind, local_ref)
    repair_id = str(existing["id"]) if existing is not None else _new_id()
    name, effective_usage, status, error, repair_id = resolve_repair_decision(
        service,
        row,
        observed_at=observed_at,
        usage_bytes=usage_bytes,
        repair_id=repair_id,
        previous_name=previous_name,
    )
    if existing is None:
        _REPAIRS.insert_repair(
            connection,
            (
                repair_id, kind, server_id, int(row["telegram_id"]), local_ref,
                str(row["source_external_id"]), str(row["source_external_id"]), name,
                quota, effective_usage, str(row["expires_at"]), status,
                observed_at, error, observed_at, observed_at,
            ),
        )
        job_state = "created"
    else:
        current_status = str(existing["status"] or "")
        reopen = current_status in {"done", "cancelled"} or str(
            existing["source_external_id"]
        ) != str(row["source_external_id"])
        if reopen:
            _REPAIRS.reopen_repair(
                connection,
                (
                    str(row["source_external_id"]), str(row["source_external_id"]), name,
                    quota, effective_usage, str(row["expires_at"]), status, observed_at,
                    error, observed_at, existing["id"],
                ),
            )
            job_state = "reopened"
        else:
            job_state = current_status or "existing"
    alert_needed = job_state in {"created", "reopened"}
    if not alert_needed:
        alert_needed = not _REPAIRS.repair_alert_exists(connection, repair_id)
    if job_state in {"created", "reopened"}:
        service._audit(
            connection,
            "managed_key_missing",
            "managed_key",
            f"{server_id}:{kind}:{local_ref}",
            "system",
            None,
            {
                "source_external_id": str(row["source_external_id"]),
                "missing_observation_count": int(missing_observation_count),
                "repair_status": status,
                "used_bytes": effective_usage,
                "quota_bytes": quota,
                "job_state": job_state,
            },
        )
    return ManagedRepairDecision(
        repair_id=repair_id,
        local_ref=local_ref,
        server_id=server_id,
        kind=kind,
        quota=quota,
        name=name,
        effective_usage=effective_usage,
        status=status,
        error=error,
        job_state=job_state,
        alert_needed=alert_needed,
    )


def notify_repair_decision(
    service: Any,
    connection: Any,
    row: dict[str, Any],
    decision: ManagedRepairDecision,
    *,
    observed_at: str,
) -> str:
    """Queue staff/customer notifications and expose the stable poll state."""
    if decision.alert_needed:
        usage_text = "unknown (fresh Outline telemetry unavailable)"
        if decision.effective_usage is not None:
            usage_text = f"{_human_bytes(decision.effective_usage)} observed"
        service._queue_staff_notification(
            connection,
            "key_repairs",
            decision.repair_id,
            "🧩 MANAGED KEY MISSING\n\n"
            f"Repair: #{decision.repair_id[:8]}\n"
            f"Customer: tg:{int(row['telegram_id'])}\n"
            f"Endpoint: {decision.server_id}\n"
            f"Old key: {str(row['source_external_id'])[:32]}\n"
            f"Usage: {usage_text}\n"
            f"Decision: {decision.status.replace('_', ' ')}\n\n"
            "Open Key Repairs to review. AuriX will not recreate this key or reset quota without the required owner decision.",
            observed_at,
        )
    service._queue_customer_repair_notification(
        connection,
        decision.repair_id,
        int(row["telegram_id"]),
        decision.status,
        decision.server_id,
        observed_at,
    )
    return decision.status if decision.job_state in {"created", "reopened"} else f"existing_{decision.status}"


def enqueue_managed_key_repair(
    service: Any,
    connection: Any,
    row: dict[str, Any],
    *,
    observed_at: str,
    missing_observation_count: int,
    previous_name: str | None,
    usage_bytes: int | None,
) -> str:
    """Coordinate decision, persistence, and notifications for one repair."""
    decision = persist_repair_decision(
        service,
        connection,
        row,
        observed_at=observed_at,
        missing_observation_count=missing_observation_count,
        previous_name=previous_name,
        usage_bytes=usage_bytes,
    )
    return notify_repair_decision(
        service, connection, row, decision, observed_at=observed_at
    )
