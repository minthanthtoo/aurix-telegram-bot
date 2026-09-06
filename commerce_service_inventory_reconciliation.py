"""Remote server inventory reconciliation workflow."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from commerce_models import _now_text
from commerce_inventory_reconciliation_workflow import (
    collect_remote_inventory,
    reconcile_observed_inventory,
    record_unreachable_inventory,
)


def refresh_server_inventory(self: Any, now: datetime | None = None) -> list[dict[str, Any]]:
    """Reconcile remote inventory and telemetry without storing access URLs."""
    observed_at = _now_text(now)
    repository = self.inventory_reconciliation
    with self.database.connect() as connection:
        server_ids = repository.enabled_server_ids(connection)
    results: list[dict[str, Any]] = []
    for server_id in server_ids:
        probe_started = time.perf_counter()
        try:
            observation = collect_remote_inventory(
                self._outline_client(server_id), self._metric_bytes
            )
            self._server_metrics_cache[server_id] = observation["by_key"]
            results.append(
                reconcile_observed_inventory(
                    self,
                    repository,
                    server_id=server_id,
                    observed_at=observed_at,
                    probe_started=probe_started,
                    observation=observation,
                )
            )
        except Exception as exc:
            results.append(
                record_unreachable_inventory(
                    self,
                    repository,
                    server_id=server_id,
                    observed_at=observed_at,
                    probe_started=probe_started,
                    error=exc,
                )
            )
    return results
