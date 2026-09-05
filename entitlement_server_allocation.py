"""Capacity-aware server allocation for entitlement provisioning."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any

from entitlement_models import OutlineError
from entitlement_support import UTC, new_id as _new_id


def _select_server_for_tier(
    self,
    connection: Any,
    tier_code: str,
    quota_bytes: int,
    now: datetime,
    *,
    telegram_id: int | None = None,
) -> str:
    """Select a fresh, healthy server using shared key and traffic headroom."""
    if not self._server_tables_exist(connection):
        return self._default_server_id()
    if int(connection.execute("SELECT COUNT(*) AS n FROM outline_servers").fetchone()["n"]) == 0:
        # Standalone/local free-access databases have no registered fleet.
        # Their single configured adapter remains the authoritative target.
        return self._default_server_id()
    max_age = max(30, int(os.environ.get("AURIX_SERVER_HEALTH_MAX_AGE_SECONDS", "900")))
    fresh_after = (now - timedelta(seconds=max_age)).astimezone(UTC).isoformat()
    servers = connection.execute(
        """SELECT * FROM outline_servers
           WHERE enabled = 1 AND lifecycle_state = 'active'
             AND health_status = 'healthy'
             AND last_synced_at IS NOT NULL AND last_synced_at >= ?
           ORDER BY server_id""",
        (fresh_after,),
    ).fetchall()
    has_tier_allocations = int(
        connection.execute(
            "SELECT COUNT(*) AS n FROM server_tier_allocations WHERE tier_code = ?",
            (tier_code,),
        ).fetchone()["n"]
    ) > 0
    candidates: list[tuple[float, int, float, str]] = []
    for server in servers:
        server_id = str(server["server_id"])
        probe_status = "unknown"
        probe_score = -1.0
        probe = (
            connection.execute(
                """SELECT status, score, last_observed_at
                     FROM route_health_snapshots WHERE server_id = ?""",
                (server_id,),
            ).fetchone()
            if self._table_exists(connection, "route_health_snapshots")
            else None
        )
        if probe is not None and probe["last_observed_at"] is not None:
            try:
                probe_fresh = datetime.fromisoformat(str(probe["last_observed_at"])).astimezone(UTC) >= now - timedelta(seconds=max_age)
            except (TypeError, ValueError, OverflowError):
                probe_fresh = False
            if probe_fresh:
                probe_status = str(probe["status"] or "unknown")
                probe_score = float(probe["score"]) if probe["score"] is not None else -1.0
                if probe_status == "unreachable":
                    continue
                if os.environ.get("AURIX_REQUIRE_PROBE_EVIDENCE_FOR_ISSUANCE", "0").strip().lower() in {"1", "true", "yes", "on"} and probe_status == "unknown":
                    continue
        allocation = connection.execute(
            """SELECT slot_limit FROM server_tier_allocations
               WHERE server_id = ? AND tier_code = ?""",
            (server_id, tier_code),
        ).fetchone()
        if has_tier_allocations and allocation is None:
            continue
        active_tier = connection.execute(
            """SELECT COUNT(*) AS n FROM keys k
               LEFT JOIN giveaway_claims g ON g.key_id = k.id
               WHERE k.server_id = ? AND k.status IN ('active', 'revoke_failed')
                 AND CASE
                   WHEN ? = 'FREE300MB' THEN k.key_type = 'daily_free'
                   WHEN ? = 'FREE3GB' THEN k.key_type = 'monthly_trial' AND g.key_id IS NULL
                   ELSE g.key_id IS NOT NULL
                 END""",
            (server_id, tier_code, tier_code),
        ).fetchone()["n"]
        pending_tier = 0
        if self._intent_tables_exist(connection):
            pending_kind = {
                "FREE300MB": "daily",
                "FREE3GB": "trial",
                "PROMO": "promo",
            }.get(tier_code)
            if pending_kind:
                pending_tier = int(
                    connection.execute(
                        """SELECT COUNT(*) AS n FROM free_provisioning_intents
                           WHERE server_id = ? AND kind = ?
                             AND status IN ('pending', 'running')""",
                        (server_id, pending_kind),
                    ).fetchone()["n"]
                )
        active_tier = int(active_tier) + pending_tier
        if allocation is not None and int(active_tier) >= int(allocation["slot_limit"]):
            continue
        remote_keys = int(server["remote_key_count"] or 0) + pending_tier
        max_keys = server["max_keys"]
        usable = None if max_keys is None else max(
            0, int(max_keys) - int(server["reserved_keys"] or 0)
        )
        if usable is not None and remote_keys >= usable:
            continue
        traffic_budget = server["monthly_traffic_bytes"]
        if traffic_budget is not None:
            free_committed = connection.execute(
                """SELECT COALESCE(SUM(data_limit_bytes), 0) AS n FROM keys
                   WHERE server_id = ? AND status IN ('active', 'revoke_failed')""",
                (server_id,),
            ).fetchone()["n"]
            paid_committed = connection.execute(
                """SELECT COALESCE(SUM(COALESCE(quota_bytes, 0)), 0) AS n
                   FROM subscriptions WHERE server_id = ? AND status IN ('pending', 'active')""",
                (server_id,),
            ).fetchone()["n"]
            if int(free_committed or 0) + int(paid_committed or 0) + quota_bytes > int(
                traffic_budget
            ):
                continue
        denominator = (
            int(allocation["slot_limit"])
            if allocation is not None and int(allocation["slot_limit"])
            else (usable or max(1, remote_keys + 1))
        )
        probe_rank = {"healthy": 0, "degraded": 1, "unknown": 2}.get(probe_status, 2)
        candidates.append((int(active_tier) / max(1, denominator), probe_rank, -probe_score, server_id))
    if not candidates:
        raise OutlineError("No healthy VPN server currently has capacity for this tier")
    selected = min(candidates)
    if self._table_exists(connection, "route_decisions"):
        selected_score = -selected[2] if selected[2] >= 0 else None
        connection.execute(
            """INSERT INTO route_decisions
               (decision_id, telegram_id, entitlement_ref, requested_region,
                selected_server_id, decision_mode, score, evidence_json, created_at)
               VALUES (?, ?, NULL, NULL, ?, 'automatic', ?, ?, ?)""",
            (
                f"decision-{_new_id()}",
                telegram_id,
                selected[3],
                selected_score,
                json.dumps(
                    {
                        "basis": "capacity_and_fresh_probe",
                        "tier_code": str(tier_code),
                        "probe_rank": selected[1],
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                now.astimezone(UTC).isoformat(),
            ),
        )
    return selected[3]

