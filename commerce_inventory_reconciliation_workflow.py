"""Focused remote inventory observation and persistence steps."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from commerce_models import UTC
from connectivity_registry import ConnectivityRegistry


def collect_remote_inventory(client: Any, metric_bytes: Any) -> dict[str, Any]:
    """Observe one Outline server without opening a database transaction."""
    info = client.server_info()
    inventory = client.list_keys()
    keys = inventory.get("accessKeys", []) if isinstance(inventory, dict) else []
    remote_items = [
        item
        for item in (keys if isinstance(keys, list) else [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    transfer = client.transfer_metrics()
    by_key = transfer.get("bytesTransferredByUserId", {}) if isinstance(transfer, dict) else {}
    total_transfer = 0
    for value in by_key.values() if isinstance(by_key, dict) else ():
        parsed = _safe_int(value)
        if parsed is not None:
            total_transfer += max(0, parsed)
    current_bandwidth = peak_bandwidth = None
    experimental = 0
    experimental_method = getattr(client, "experimental_metrics", None)
    if callable(experimental_method):
        try:
            detailed = experimental_method("30d")
            server_metrics = detailed.get("server", {}) if isinstance(detailed, dict) else {}
            bandwidth = server_metrics.get("bandwidth", {}) if isinstance(server_metrics, dict) else {}
            current_bandwidth = metric_bytes(bandwidth.get("current"))
            peak_bandwidth = metric_bytes(bandwidth.get("peak"))
            experimental = 1
        except Exception:
            pass
    return {
        "info": info,
        "remote_items": remote_items,
        "by_key": dict(by_key) if isinstance(by_key, dict) else {},
        "total_transfer": total_transfer,
        "current_bandwidth": current_bandwidth,
        "peak_bandwidth": peak_bandwidth,
        "experimental": experimental,
    }


def _safe_int(value: Any) -> int | None:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return None


def reconcile_observed_inventory(
    service: Any,
    repository: Any,
    *,
    server_id: str,
    observed_at: str,
    probe_started: float,
    observation: dict[str, Any],
) -> dict[str, Any]:
    """Persist one successful observation and return its public summary."""
    remote_items = observation["remote_items"]
    by_key = observation["by_key"]
    managed_rows: list[dict[str, Any]] = []
    managed_missing_count = 0
    repair_jobs_queued = 0
    repair_manual_count = 0
    usage_snapshots_recorded = 0
    orphan_count = 0
    with service.database.connect() as connection:
        service.database.begin_write(connection)
        if service._table_exists(connection, "outline_remote_keys"):
            managed_rows = service._managed_repair_rows(connection, server_id)
            for managed_row in managed_rows:
                managed_row["server_id"] = server_id
            usage_snapshots_recorded = service._record_usage_snapshots(
                connection,
                server_id=server_id,
                observed_at=observed_at,
                managed_rows=managed_rows,
                by_key=by_key,
            )
            ledger_rows = repository.remote_key_ledger(connection, server_id)
            has_free_keys = service._table_exists(connection, "keys")
            managed_ids = repository.managed_key_ids(
                connection, server_id, include_free_keys=has_free_keys
            )
            repository.mark_present_keys_missing(connection, server_id)
            _record_present_keys(
                repository,
                connection,
                server_id,
                observed_at,
                remote_items,
                managed_ids,
                by_key,
                service._metric_bytes,
            )
            _update_managed_usage(
                service,
                repository,
                connection,
                server_id,
                observed_at,
                managed_rows,
                by_key,
                has_free_keys,
            )
            missing = _record_missing_keys(
                service,
                repository,
                connection,
                server_id,
                observed_at,
                managed_rows,
                ledger_rows,
                remote_items,
                by_key,
            )
            managed_missing_count = missing["managed_missing_count"]
            repair_jobs_queued = missing["repair_jobs_queued"]
            repair_manual_count = missing["repair_manual_count"]
            orphan_count = repository.unreviewed_orphan_count(connection, server_id)
        latency_ms = round((time.perf_counter() - probe_started) * 1000, 3)
        health = service._record_endpoint_health(
            connection,
            server_id,
            observed_at,
            observed_status="healthy",
            latency_ms=latency_ms,
            remote_key_count=len(remote_items),
        )
        repository.update_server_metrics(
            connection,
            server_id=server_id,
            remote_key_count=len(remote_items),
            remote_transfer_bytes=observation["total_transfer"],
            current_bandwidth_bytes=observation["current_bandwidth"],
            peak_bandwidth_bytes=observation["peak_bandwidth"],
            telemetry_experimental=bool(observation["experimental"]),
            remote_orphan_key_count=orphan_count,
        )
        ConnectivityRegistry.sync_outline_health(
            connection,
            server_id=server_id,
            lifecycle_state=repository.lifecycle_state(connection, server_id),
            health_status=str(health["state"]),
            now_text=observed_at,
        )
    aggregate_usage = service._record_aggregate_usage(
        server_id=server_id,
        managed_rows=managed_rows,
        by_key=by_key,
        observed_at=observed_at,
    )
    return {
        "server_id": server_id,
        "status": health["state"],
        "observed_status": "healthy",
        "latency_ms": latency_ms,
        "health_success_streak": health["success_streak"],
        "version": observation["info"].get("version"),
        "remote_key_count": len(remote_items),
        "remote_orphan_key_count": orphan_count,
        "managed_missing_key_count": managed_missing_count,
        "repair_jobs_queued": repair_jobs_queued,
        "repair_manual_count": repair_manual_count,
        "usage_snapshots_recorded": usage_snapshots_recorded,
        "aggregate_usage_recorded": aggregate_usage["recorded"],
        "aggregate_exhausted": aggregate_usage["exhausted"],
        "aggregate_usage_errors": aggregate_usage["errors"],
    }


def _record_present_keys(
    repository: Any,
    connection: Any,
    server_id: str,
    observed_at: str,
    remote_items: list[dict[str, Any]],
    managed_ids: set[str],
    by_key: dict[str, Any],
    metric_bytes: Any,
) -> None:
    for item in remote_items:
        outline_key_id = str(item["id"]).strip()
        repository.upsert_present_key(
            connection,
            server_id=server_id,
            outline_key_id=outline_key_id,
            remote_name=str(item.get("name") or "").strip()[:256] or None,
            managed=outline_key_id in managed_ids,
            observed_at=observed_at,
            usage_bytes=metric_bytes(by_key.get(outline_key_id)),
        )


def _update_managed_usage(
    service: Any,
    repository: Any,
    connection: Any,
    server_id: str,
    observed_at: str,
    managed_rows: list[dict[str, Any]],
    by_key: dict[str, Any],
    has_free_keys: bool,
) -> None:
    for managed_row in managed_rows:
        external_id = str(managed_row["source_external_id"])
        if external_id not in by_key:
            continue
        usage_bytes = service._metric_bytes(by_key.get(external_id))
        if usage_bytes is None:
            continue
        kind = str(managed_row["kind"])
        if kind == "paid" or has_free_keys:
            repository.update_managed_usage(
                connection,
                kind=kind,
                local_id=str(managed_row["local_id"]),
                server_id=server_id,
                usage_bytes=usage_bytes,
                observed_at=observed_at,
            )


def _record_missing_keys(
    service: Any,
    repository: Any,
    connection: Any,
    server_id: str,
    observed_at: str,
    managed_rows: list[dict[str, Any]],
    ledger_rows: dict[str, Any],
    remote_items: list[dict[str, Any]],
    by_key: dict[str, Any],
) -> dict[str, int]:
    remote_ids = {str(item["id"]).strip() for item in remote_items}
    required_observations = service._managed_repair_required_observations()
    interval_seconds = service._managed_repair_observation_interval_seconds()
    current_observed_dt = datetime.fromisoformat(observed_at).astimezone(UTC)
    counts = {"managed_missing_count": 0, "repair_jobs_queued": 0, "repair_manual_count": 0}
    for managed_row in managed_rows:
        source_id = str(managed_row["source_external_id"])
        if source_id in remote_ids:
            continue
        counts["managed_missing_count"] += 1
        previous = ledger_rows.get(source_id)
        previous_status = str(previous.get("status") or "") if previous else ""
        previous_count = int(previous.get("missing_observation_count") or 0) if previous else 0
        previous_last_missing = str(previous.get("last_missing_at") or "") if previous else ""
        count = 1
        missing_since = observed_at
        should_increment = True
        if previous_status == "missing":
            count = max(1, previous_count)
            missing_since = str(previous.get("missing_since_at") or observed_at)
            if previous_last_missing:
                try:
                    elapsed = current_observed_dt - datetime.fromisoformat(
                        previous_last_missing
                    ).astimezone(UTC)
                    should_increment = elapsed.total_seconds() >= interval_seconds
                except (TypeError, ValueError, OverflowError):
                    should_increment = True
            if should_increment:
                count += 1
        usage_bytes = (
            service._metric_bytes(by_key.get(source_id))
            if isinstance(by_key, dict)
            else managed_row.get("last_usage_bytes")
        )
        repository.upsert_missing_key(
            connection,
            server_id=server_id,
            outline_key_id=source_id,
            observed_at=observed_at,
            usage_bytes=usage_bytes,
            observation_count=count,
            missing_since=missing_since,
        )
        if count < required_observations or not (
            previous_status != "missing" or should_increment or previous_count < required_observations
        ):
            continue
        repair_status = service._enqueue_managed_key_repair(
            connection,
            managed_row,
            observed_at=observed_at,
            missing_observation_count=count,
            previous_name=str(previous.get("remote_name") or "") if previous else None,
            usage_bytes=service._metric_bytes(by_key.get(source_id)),
        )
        if repair_status == "pending":
            counts["repair_jobs_queued"] += 1
        elif repair_status == "manual":
            counts["repair_manual_count"] += 1
    return counts


def record_unreachable_inventory(
    service: Any,
    repository: Any,
    *,
    server_id: str,
    observed_at: str,
    probe_started: float,
    error: Exception,
) -> dict[str, Any]:
    latency_ms = round((time.perf_counter() - probe_started) * 1000, 3)
    with service.database.connect() as connection:
        service.database.begin_write(connection)
        health = service._record_endpoint_health(
            connection,
            server_id,
            observed_at,
            observed_status="unreachable",
            latency_ms=latency_ms,
            error_type=type(error).__name__,
        )
        ConnectivityRegistry.sync_outline_health(
            connection,
            server_id=server_id,
            lifecycle_state=repository.lifecycle_state(connection, server_id),
            health_status=str(health["state"]),
            now_text=observed_at,
        )
    return {
        "server_id": server_id,
        "status": health["state"],
        "observed_status": "unreachable",
        "latency_ms": latency_ms,
        "health_failure_streak": health["failure_streak"],
        "managed_missing_key_count": 0,
        "repair_jobs_queued": 0,
        "repair_manual_count": 0,
    }
