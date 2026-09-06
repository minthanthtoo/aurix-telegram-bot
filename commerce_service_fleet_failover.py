"""Route failover observation and execution workflow."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from typing import Any

from commerce_models import UTC, _now_text
from commerce_service_fleet_failover_steps import (
    commit_route_failover,
    prepare_route_failover,
    provision_failover_target,
)


def _failover_target_has_fresh_probe(self, server_id: str, now: datetime) -> bool:
    stale_after = int(getattr(self.probe_service, "stale_after_seconds", 900) or 900)
    cutoff = (now.astimezone(UTC) - timedelta(seconds=max(30, stale_after))).isoformat()
    with self.database.connect() as connection:
        return self.failover_reads.target_has_fresh_probe(connection, server_id, cutoff)


def _failover_source_record(self, decision: dict[str, Any]) -> dict[str, Any] | None:
    with self.database.connect() as connection:
        return self.failover_reads.source_record(connection, str(decision["decision_id"]))


def process_route_failovers(
    self, now: datetime | None = None, max_jobs: int = 5
) -> int:
    """Execute only claimed, verified failover decisions.

    A target is provisioned with a small lease, checked through the
    adapter and fresh server-side probe evidence, then committed as a new
    credential generation. The source credential is intentionally not
    deleted: manual exports remain valid until their normal revoke path.
    """
    current = (now or datetime.now(UTC)).astimezone(UTC)
    processed = 0
    while processed < max(1, int(max_jobs)):
        decision = self.failover.claim(now=_now_text(current))
        if decision is None:
            break
        preparation = None
        target_grant: dict[str, Any] | None = None
        try:
            preparation = prepare_route_failover(self, decision, current)
            target_grant = provision_failover_target(preparation, decision)
            commit_route_failover(self, decision, preparation, target_grant, current)
        except Exception as exc:
            if (
                preparation is not None
                and target_grant is not None
                and bool(target_grant.get("created"))
            ):
                try:
                    preparation.target_adapter.revoke_auth(target_grant)
                except Exception:
                    pass
            self.failover.mark_failed(str(decision["decision_id"]), exc, now=_now_text(current))
            print(f"route failover error: {type(exc).__name__}", file=sys.stderr)
        processed += 1
    return processed
