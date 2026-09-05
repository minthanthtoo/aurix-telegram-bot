"""Managed-key repair observation and usage accounting use cases."""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from commerce_models import UTC
from commerce_models import _new_id
from commerce_models import _human_bytes
from commerce_repairs_repository import ManagedRepairRepository


_REPAIRS = ManagedRepairRepository()


def _managed_repair_required_observations() -> int:
    """Require independent missing-key observations before recreating access."""
    try:
        return max(
            2,
            min(10, int(os.environ.get("AURIX_KEY_REPAIR_REQUIRED_OBSERVATIONS", "2"))),
        )
    except (TypeError, ValueError):
        return 2

def _managed_repair_observation_interval_seconds() -> int:
    """Keep repeated UI refreshes from fabricating independent observations."""
    try:
        return max(
            0,
            min(
                86_400,
                int(os.environ.get("AURIX_KEY_REPAIR_OBSERVATION_INTERVAL_SECONDS", "60")),
            ),
        )
    except (TypeError, ValueError):
        return 60

def _usage_snapshot_interval_seconds() -> int:
    """Throttle durable usage samples without reducing live enforcement."""
    try:
        return max(
            60,
            min(
                86_400,
                int(os.environ.get("AURIX_USAGE_SNAPSHOT_INTERVAL_SECONDS", "300")),
            ),
        )
    except (TypeError, ValueError):
        return 300

def _managed_repair_cached_usage_max_age_seconds() -> int:
    """Bound how long a cached usage sample may authorize a repair.

    A missing Outline key can no longer be queried for its historical
    transfer count.  A recent, persisted sample is safe to reuse because
    it is still a lower-bound observation from the same external key; an
    old sample is escalated instead of silently restoring quota.
    """
    try:
        return max(
            0,
            min(
                86_400,
                int(os.environ.get("AURIX_KEY_REPAIR_CACHED_USAGE_MAX_AGE_SECONDS", "900")),
            ),
        )
    except (TypeError, ValueError):
        return 900

def _managed_repair_cached_usage_is_recent(self, row: Any, observed_at: Any) -> bool:
    """Return whether the entitlement has a bounded, trustworthy sample."""
    raw_observed = row.get("last_usage_observed_at") if hasattr(row, "get") else None
    if not raw_observed:
        return False
    try:
        usage_at = (
            raw_observed
            if isinstance(raw_observed, datetime)
            else datetime.fromisoformat(str(raw_observed))
        ).astimezone(UTC)
        current = (
            observed_at
            if isinstance(observed_at, datetime)
            else datetime.fromisoformat(str(observed_at))
        ).astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return False
    age = (current - usage_at).total_seconds()
    return 0 <= age <= self._managed_repair_cached_usage_max_age_seconds()

def _managed_repair_allow_unknown_usage() -> bool:
    """Return whether an operator explicitly permits a full-quota repair.

    A missing remote key has no trustworthy usage value after an out-of-band
    deletion.  The safe default is to escalate for review rather than reset
    the entitlement to its original quota.  A controlled owner-only repair
    can opt in through the deployment environment when the operator has
    independently verified that no traffic was served.
    """
    return os.environ.get("AURIX_KEY_REPAIR_ALLOW_UNKNOWN_USAGE", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

def _managed_repair_key_name(row: Any, previous_name: str | None = None) -> str:
    """Keep a stable, human-readable name when a key is recreated."""
    if previous_name and str(previous_name).strip():
        return str(previous_name).strip()[:128]
    raw_identity = str(row.get("username") if hasattr(row, "get") else row["username"] or "")
    raw_identity = raw_identity.lstrip("@") or str(row["telegram_id"])
    identity = re.sub(r"[^A-Za-z0-9_-]+", "-", raw_identity).strip("-_")[:48]
    identity = identity or str(row["telegram_id"])
    kind = str(row["kind"] or "free").lower()
    if kind == "paid":
        plan_name = str(row.get("plan_name") or row.get("plan_code") or "paid")
        match = re.search(r"\b(\d+)\s*GB\b", plan_name, re.IGNORECASE)
        tier = f"PAID{match.group(1)}GB" if match else str(row.get("plan_code") or "PAID")
    elif row.get("campaign_code"):
        tier = f"PROMO-{row['campaign_code']}"
    elif str(row.get("key_type") or "") == "monthly_trial":
        tier = "FREE3GB"
    else:
        tier = "FREE300MB"
    duration = f"{int(row.get('duration_days') or (30 if tier == 'FREE3GB' else 1))}day"
    created_at = str(row.get("created_at") or "")
    try:
        started = datetime.fromisoformat(created_at).astimezone(UTC)
        timestamp = started.strftime("%Y%m%d%H%M")
    except (TypeError, ValueError, OverflowError):
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M")
    return f"{identity}-{tier}-{duration}-{timestamp}"[:128]

def _record_usage_snapshots(
    self,
    connection: Any,
    *,
    server_id: str,
    observed_at: str,
    managed_rows: list[dict[str, Any]],
    by_key: dict[str, Any],
) -> int:
    """Persist bounded, non-secret transfer evidence for managed keys."""
    if not self._table_exists(connection, "usage_snapshots") or not isinstance(by_key, dict):
        return 0
    try:
        current = datetime.fromisoformat(str(observed_at)).astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return 0
    cutoff = (current - timedelta(seconds=self._usage_snapshot_interval_seconds())).isoformat()
    recent = _REPAIRS.recent_snapshots(connection, str(server_id), cutoff)
    recorded = 0
    for item in managed_rows:
        external_id = str(item.get("source_external_id") or "").strip()
        if not external_id or external_id not in by_key:
            continue
        try:
            used = self._metric_bytes(by_key.get(external_id))
            quota = int(item.get("quota_bytes") or 0)
            telegram_id = int(item["telegram_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if used is None or quota <= 0:
            continue
        kind = str(item.get("kind") or "free").lower()
        local_ref = str(item.get("local_key_ref") or "").strip()
        if kind not in {"free", "paid", "trial", "promo"} or not local_ref:
            continue
        if (kind, local_ref) in recent:
            continue
        _REPAIRS.insert_snapshot(
            connection,
            (
                _new_id(), telegram_id, kind, local_ref, str(server_id),
                external_id, str(observed_at), int(used), quota,
            ),
        )
        recent.add((kind, local_ref))
        recorded += 1
    return recorded

def _record_aggregate_usage(
    self,
    *,
    server_id: str,
    managed_rows: list[dict[str, Any]],
    by_key: dict[str, Any],
    observed_at: str,
) -> dict[str, int]:
    """Converge Outline counters into account-level quota epochs."""
    if not isinstance(by_key, dict) or not managed_rows:
        return {"recorded": 0, "exhausted": 0, "errors": 0}
    with self.database.connect() as connection:
        if not self._table_exists(connection, "entitlement_usage_epochs"):
            return {"recorded": 0, "exhausted": 0, "errors": 0}
        endpoint = _REPAIRS.endpoint(connection, str(server_id))
    if endpoint is None:
        return {"recorded": 0, "exhausted": 0, "errors": 0}
    endpoint_id = str(endpoint["endpoint_id"])
    recorded = exhausted = errors = 0
    for item in managed_rows:
        external_id = str(item.get("source_external_id") or "").strip()
        if not external_id or external_id not in by_key:
            continue
        usage_bytes = self._metric_bytes(by_key.get(external_id))
        if usage_bytes is None:
            continue
        try:
            result = self.identity.record_remote_usage(
                endpoint_id,
                external_id,
                usage_bytes,
                observed_at=observed_at,
                now=observed_at,
            )
            if result.get("accepted") or result.get("duplicate") or result.get("exhausted"):
                recorded += 1
            if result.get("exhausted"):
                exhausted += 1
                self._queue_aggregate_revocation(
                    kind=str(item.get("kind") or "free"),
                    local_id=item.get("local_id"),
                    subscription_id=result.get("subscription_id"),
                    server_id=server_id,
                    external_id=external_id,
                    used_bytes=usage_bytes,
                    quota_bytes=int(item.get("quota_bytes") or 0),
                    observed_at=observed_at,
                )
        except Exception as exc:
            errors += 1
            print(
                f"aggregate usage error server={server_id} key={external_id[:32]}: {type(exc).__name__}",
                file=sys.stderr,
            )
    return {"recorded": recorded, "exhausted": exhausted, "errors": errors}

def _queue_aggregate_revocation(
    self,
    *,
    kind: str,
    local_id: Any,
    subscription_id: Any,
    server_id: str,
    external_id: str,
    used_bytes: int,
    quota_bytes: int,
    observed_at: str,
) -> None:
    """Bridge the additive aggregate stop into legacy remote-delete workers."""
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        if str(kind).lower() == "paid" and subscription_id:
            _REPAIRS.aggregate_paid(
                connection, str(subscription_id), str(server_id), str(external_id), str(observed_at)
            )
        elif local_id is not None and self._table_exists(connection, "keys"):
            # Free/trial/promo credentials use the ClaimService
            # termination worker rather than a paid provisioning job.
            # Persist the enforcement event now so a process restart
            # between inventory and the next maintenance stage cannot
            # lose the observed quota hit or its staff/customer notices.
            _REPAIRS.aggregate_free(
                connection,
                local_id=local_id,
                server_id=str(server_id),
                external_id=str(external_id),
                used_bytes=used_bytes,
                quota_bytes=quota_bytes,
                observed_at=str(observed_at),
            )

def _managed_repair_rows(self, connection: Any, server_id: str) -> list[dict[str, Any]]:
    """Return active managed entitlements that may need remote repair."""
    rows: list[dict[str, Any]] = _REPAIRS.managed_paid_rows(connection, server_id)
    if self._table_exists(connection, "keys"):
        rows.extend(_REPAIRS.managed_free_rows(connection, server_id))
    # A missing key cannot be queried for historical transfer usage.  If
    # the durable telemetry ledger has a newer sample than the live key
    # row, use that same-key lower bound for repair decisions.  This keeps
    # a recent observation useful across a restart or an inventory race,
    # while the bounded freshness check below still escalates old data.
    snapshot_by_ref: dict[tuple[str, str], dict[str, Any]] = {}
    if self._table_exists(connection, "usage_snapshots"):
        for snapshot in _REPAIRS.usage_snapshots(connection, server_id):
            key = (str(snapshot["entitlement_kind"]), str(snapshot["local_key_ref"]))
            snapshot_by_ref.setdefault(key, dict(snapshot))
    for row in rows:
        row["source_external_id"] = str(row.get("source_external_id") or "").strip()
        snapshot = snapshot_by_ref.get((str(row["kind"]), str(row["local_key_ref"])))
        if snapshot:
            live_observed = row.get("last_usage_observed_at")
            use_snapshot = not live_observed
            if not use_snapshot:
                try:
                    use_snapshot = datetime.fromisoformat(
                        str(snapshot["observed_at"])
                    ).astimezone(UTC) > datetime.fromisoformat(
                        str(live_observed)
                    ).astimezone(UTC)
                except (TypeError, ValueError, OverflowError):
                    use_snapshot = False
            if use_snapshot:
                row["last_usage_bytes"] = snapshot["used_bytes"]
                row["last_usage_observed_at"] = snapshot["observed_at"]
        row["key_name"] = self._managed_repair_key_name(row)
    return [row for row in rows if row["source_external_id"]]

def _enqueue_managed_key_repair(
    self,
    connection: Any,
    row: dict[str, Any],
    *,
    observed_at: str,
    missing_observation_count: int,
    previous_name: str | None,
    usage_bytes: int | None,
) -> str:
    """Create/update one durable repair decision without calling Outline."""
    local_ref = str(row["local_key_ref"])
    server_id = str(row["server_id"])
    kind = str(row["kind"])
    quota = int(row["quota_bytes"] or 0)
    name = self._managed_repair_key_name(row, previous_name)
    existing = _REPAIRS.existing_repair(connection, server_id, kind, local_ref)
    repair_id = str(existing["id"]) if existing is not None else _new_id()
    allow_unknown = self._managed_repair_allow_unknown_usage()
    effective_usage = usage_bytes
    usage_is_fresh = usage_bytes is not None
    if effective_usage is None:
        try:
            effective_usage = int(row.get("last_usage_bytes"))
        except (TypeError, ValueError):
            effective_usage = None
        usage_is_fresh = False
    if not usage_is_fresh and self._managed_repair_cached_usage_is_recent(row, observed_at):
        usage_is_fresh = effective_usage is not None
    if not usage_is_fresh and os.environ.get(
        "AURIX_KEY_REPAIR_ALLOW_STALE_USAGE", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}:
        usage_is_fresh = effective_usage is not None
    if effective_usage is not None:
        effective_usage = max(0, effective_usage)
    if not usage_is_fresh and not allow_unknown:
        status = "manual"
        error = "usage_observation_required"
    elif effective_usage is not None and effective_usage >= quota:
        status = "manual"
        error = "quota_already_exhausted"
    else:
        status = "pending"
        error = None
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
        # A completed/cancelled job belongs to an earlier disappearance.
        # Re-open it only after a new two-observation episode, preserving
        # the same durable row and audit history without duplicate jobs.
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
        # Backfill the alert for a repair that was opened by an older
        # release before staff key-repair notifications existed. The
        # per-staff dedupe key makes this safe across every poll.
        alert_needed = not _REPAIRS.repair_alert_exists(connection, repair_id)
    if job_state in {"created", "reopened"}:
        self._audit(
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
    if alert_needed:
        # A missing managed key is an operational incident, not a silent
        # customer-facing outage. Queue one durable, preference-aware
        # staff alert for this repair episode; the notification dedupe
        # key prevents repeated inventory polls from spamming staff.
        usage_text = "unknown (fresh Outline telemetry unavailable)"
        if effective_usage is not None:
            usage_text = f"{_human_bytes(effective_usage)} observed"
        self._queue_staff_notification(
            connection,
            "key_repairs",
            repair_id,
            "🧩 MANAGED KEY MISSING\n\n"
            f"Repair: #{repair_id[:8]}\n"
            f"Customer: tg:{int(row['telegram_id'])}\n"
            f"Endpoint: {server_id}\n"
            f"Old key: {str(row['source_external_id'])[:32]}\n"
            f"Usage: {usage_text}\n"
            f"Decision: {status.replace('_', ' ')}\n\n"
            "Open Key Repairs to review. AuriX will not recreate this key or reset quota without the required owner decision.",
            observed_at,
        )
    self._queue_customer_repair_notification(
        connection,
        repair_id,
        int(row["telegram_id"]),
        status,
        server_id,
        observed_at,
    )
    # Let the caller distinguish a newly opened repair episode from a
    # repeated observation of the same queued/manual decision.  This keeps
    # operator counters and audit volume stable during frequent refreshes.
    return status if job_state in {"created", "reopened"} else f"existing_{status}"
