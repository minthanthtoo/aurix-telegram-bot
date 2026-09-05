"""Remote server inventory reconciliation workflow."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from commerce_models import UTC, _now_text
from connectivity_registry import ConnectivityRegistry


def refresh_server_inventory(self, now: datetime | None = None) -> list[dict[str, Any]]:
    """Reconcile remote inventory and telemetry without storing access URLs."""
    observed_at = _now_text(now)
    with self.database.connect() as connection:
        rows = connection.execute(
            "SELECT server_id FROM outline_servers WHERE enabled = 1 ORDER BY server_id"
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        server_id = str(row["server_id"])
        client = self._outline_client(server_id)
        probe_started = time.perf_counter()
        try:
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
                try:
                    total_transfer += max(0, int(value or 0))
                except (TypeError, ValueError):
                    continue
            self._server_metrics_cache[server_id] = (
                dict(by_key) if isinstance(by_key, dict) else {}
            )
            current_bandwidth = peak_bandwidth = None
            experimental = 0
            experimental_method = getattr(client, "experimental_metrics", None)
            if callable(experimental_method):
                try:
                    detailed = experimental_method("30d")
                    server_metrics = detailed.get("server", {}) if isinstance(detailed, dict) else {}
                    bandwidth = server_metrics.get("bandwidth", {}) if isinstance(server_metrics, dict) else {}
                    current_bandwidth = self._metric_bytes(bandwidth.get("current"))
                    peak_bandwidth = self._metric_bytes(bandwidth.get("peak"))
                    experimental = 1
                except Exception:
                    pass
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                managed_rows: list[dict[str, Any]] = []
                orphan_count = 0
                managed_missing_count = 0
                repair_jobs_queued = 0
                repair_manual_count = 0
                usage_snapshots_recorded = 0
                if self._table_exists(connection, "outline_remote_keys"):
                    managed_rows = self._managed_repair_rows(connection, server_id)
                    for managed_row in managed_rows:
                        managed_row["server_id"] = server_id
                    usage_snapshots_recorded = self._record_usage_snapshots(
                        connection,
                        server_id=server_id,
                        observed_at=observed_at,
                        managed_rows=managed_rows,
                        by_key=by_key,
                    )
                    ledger_rows = {
                        str(item["outline_key_id"]): dict(item)
                        for item in connection.execute(
                            """SELECT * FROM outline_remote_keys
                               WHERE server_id = ?""",
                            (server_id,),
                        ).fetchall()
                    }
                    managed_ids = {
                        str(item["outline_key_id"])
                        for item in connection.execute(
                            """SELECT outline_key_id FROM paid_vpn_keys
                               WHERE server_id = ? AND outline_key_id IS NOT NULL""",
                            (server_id,),
                        ).fetchall()
                    }
                    if self._table_exists(connection, "keys"):
                        managed_ids.update(
                            str(item["outline_key_id"])
                            for item in connection.execute(
                                """SELECT outline_key_id FROM keys
                                   WHERE server_id = ? AND outline_key_id IS NOT NULL""",
                                (server_id,),
                            ).fetchall()
                        )
                    # A successful inventory containing zero keys is authoritative:
                    # previously observed records become missing, but remain in the
                    # audit ledger for later reconciliation.
                    connection.execute(
                        """UPDATE outline_remote_keys
                           SET status = 'missing'
                           WHERE server_id = ? AND status = 'present'""",
                        (server_id,),
                    )
                    for item in remote_items:
                        outline_key_id = str(item["id"]).strip()
                        remote_name = str(item.get("name") or "").strip()[:256] or None
                        usage_bytes = self._metric_bytes(by_key.get(outline_key_id)) if isinstance(by_key, dict) else None
                        connection.execute(
                            """INSERT INTO outline_remote_keys
                               (server_id, outline_key_id, remote_name, managed, status,
                                first_seen_at, last_seen_at, last_usage_bytes,
                                missing_observation_count, missing_since_at, last_missing_at)
                               VALUES (?, ?, ?, ?, 'present', ?, ?, ?, 0, NULL, NULL)
                               ON CONFLICT(server_id, outline_key_id) DO UPDATE SET
                                 remote_name = excluded.remote_name,
                                 managed = excluded.managed,
                                 status = 'present',
                                 last_seen_at = excluded.last_seen_at,
                                 last_usage_bytes = COALESCE(
                                     excluded.last_usage_bytes,
                                     outline_remote_keys.last_usage_bytes
                                 ),
                                 missing_observation_count = 0,
                                 missing_since_at = NULL,
                                 last_missing_at = NULL""",
                            (
                                server_id,
                                outline_key_id,
                                remote_name,
                                1 if outline_key_id in managed_ids else 0,
                                observed_at,
                                observed_at,
                                usage_bytes,
                            ),
                        )
                    # Persist only metrics that explicitly contain the
                    # external id.  A missing map entry is ambiguous (it
                    # may mean zero traffic or a deleted key), so it must
                    # never refresh the cached-usage freshness window.
                    if isinstance(by_key, dict):
                        for managed_row in managed_rows:
                            external_id = str(managed_row["source_external_id"])
                            if external_id not in by_key:
                                continue
                            usage_bytes = self._metric_bytes(by_key.get(external_id))
                            if usage_bytes is None:
                                continue
                            if str(managed_row["kind"]) == "paid":
                                connection.execute(
                                    """UPDATE paid_vpn_keys
                                          SET last_usage_bytes = ?, last_usage_observed_at = ?
                                        WHERE id = ? AND server_id = ?""",
                                    (
                                        usage_bytes,
                                        observed_at,
                                        managed_row["local_id"],
                                        server_id,
                                    ),
                                )
                            elif self._table_exists(connection, "keys"):
                                connection.execute(
                                    """UPDATE keys
                                          SET last_usage_bytes = ?, last_usage_observed_at = ?
                                        WHERE id = ? AND server_id = ?""",
                                    (
                                        usage_bytes,
                                        observed_at,
                                        managed_row["local_id"],
                                        server_id,
                                    ),
                                )
                    remote_ids = {
                        str(item["id"]).strip()
                        for item in remote_items
                        if str(item.get("id") or "").strip()
                    }
                    required_observations = self._managed_repair_required_observations()
                    interval_seconds = self._managed_repair_observation_interval_seconds()
                    current_observed_dt = datetime.fromisoformat(observed_at).astimezone(UTC)
                    for managed_row in managed_rows:
                        source_id = str(managed_row["source_external_id"])
                        if source_id in remote_ids:
                            continue
                        managed_missing_count += 1
                        previous = ledger_rows.get(source_id)
                        previous_status = str(previous.get("status") or "") if previous else ""
                        previous_count = int(
                            previous.get("missing_observation_count") or 0
                        ) if previous else 0
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
                        connection.execute(
                            """INSERT INTO outline_remote_keys
                               (server_id, outline_key_id, remote_name, managed, status,
                                first_seen_at, last_seen_at, last_usage_bytes,
                                missing_observation_count, missing_since_at, last_missing_at)
                               VALUES (?, ?, NULL, 1, 'missing', ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(server_id, outline_key_id) DO UPDATE SET
                                 managed = 1,
                                 status = 'missing',
                                 last_usage_bytes = COALESCE(
                                     excluded.last_usage_bytes,
                                     outline_remote_keys.last_usage_bytes
                                 ),
                                 missing_observation_count = excluded.missing_observation_count,
                                 missing_since_at = excluded.missing_since_at,
                                 last_missing_at = excluded.last_missing_at""",
                            (
                                server_id,
                                source_id,
                                observed_at,
                                observed_at,
                                self._metric_bytes(by_key.get(source_id))
                                if isinstance(by_key, dict)
                                else managed_row.get("last_usage_bytes"),
                                count,
                                missing_since,
                                observed_at,
                            ),
                        )
                        if count >= required_observations and (
                            previous_status != "missing"
                            or should_increment
                            or previous_count < required_observations
                        ):
                            repair_status = self._enqueue_managed_key_repair(
                                connection,
                                managed_row,
                                observed_at=observed_at,
                                missing_observation_count=count,
                                previous_name=(
                                    str(previous.get("remote_name") or "") if previous else None
                                ),
                                usage_bytes=(
                                    self._metric_bytes(by_key.get(source_id))
                                    if isinstance(by_key, dict)
                                    else None
                                ),
                            )
                            if repair_status == "pending":
                                repair_jobs_queued += 1
                            elif repair_status == "manual":
                                repair_manual_count += 1
                    orphan_count = int(
                        connection.execute(
                            """SELECT COUNT(*) AS n FROM outline_remote_keys
                               WHERE server_id = ? AND status = 'present' AND managed = 0
                                 AND COALESCE(
                                       (SELECT review_state
                                          FROM outline_remote_key_reviews r
                                         WHERE r.server_id = outline_remote_keys.server_id
                                           AND r.outline_key_id = outline_remote_keys.outline_key_id),
                                       'unreviewed'
                                     ) = 'unreviewed'""",
                            (server_id,),
                        ).fetchone()["n"]
                    )
                latency_ms = round((time.perf_counter() - probe_started) * 1000, 3)
                health = self._record_endpoint_health(
                    connection,
                    server_id,
                    observed_at,
                    observed_status="healthy",
                    latency_ms=latency_ms,
                    remote_key_count=len(remote_items),
                )
                connection.execute(
                    """UPDATE outline_servers SET remote_key_count = ?, remote_transfer_bytes = ?,
                              current_bandwidth_bytes = ?, peak_bandwidth_bytes = ?,
                              telemetry_experimental = ?, remote_orphan_key_count = ?
                           WHERE server_id = ?""",
                    (
                        len(remote_items),
                        total_transfer,
                        current_bandwidth,
                        peak_bandwidth,
                        experimental,
                        orphan_count,
                        server_id,
                    ),
                )
                lifecycle_row = connection.execute(
                    "SELECT lifecycle_state FROM outline_servers WHERE server_id = ?",
                    (server_id,),
                ).fetchone()
                ConnectivityRegistry.sync_outline_health(
                    connection,
                    server_id=server_id,
                    lifecycle_state=str(lifecycle_row["lifecycle_state"] if lifecycle_row else "active"),
                    health_status=str(health["state"]),
                    now_text=observed_at,
                )
            aggregate_usage = self._record_aggregate_usage(
                server_id=server_id,
                managed_rows=managed_rows,
                by_key=by_key,
                observed_at=observed_at,
            )
            results.append(
                {
                    "server_id": server_id,
                    "status": health["state"],
                    "observed_status": "healthy",
                    "latency_ms": latency_ms,
                    "health_success_streak": health["success_streak"],
                    "version": info.get("version"),
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
            )
        except Exception as exc:
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                latency_ms = round((time.perf_counter() - probe_started) * 1000, 3)
                health = self._record_endpoint_health(
                    connection,
                    server_id,
                    observed_at,
                    observed_status="unreachable",
                    latency_ms=latency_ms,
                    error_type=type(exc).__name__,
                )
                lifecycle_row = connection.execute(
                    "SELECT lifecycle_state FROM outline_servers WHERE server_id = ?",
                    (server_id,),
                ).fetchone()
                ConnectivityRegistry.sync_outline_health(
                    connection,
                    server_id=server_id,
                    lifecycle_state=str(lifecycle_row["lifecycle_state"] if lifecycle_row else "active"),
                    health_status=str(health["state"]),
                    now_text=observed_at,
                )
            results.append(
                {
                    "server_id": server_id,
                    "status": health["state"],
                    "observed_status": "unreachable",
                    "latency_ms": latency_ms,
                    "health_failure_streak": health["failure_streak"],
                    "managed_missing_key_count": 0,
                    "repair_jobs_queued": 0,
                    "repair_manual_count": 0,
                }
            )
    return results

