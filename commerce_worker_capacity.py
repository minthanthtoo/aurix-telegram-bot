"""Quota enforcement and capacity observation operations."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any
from capacity_policy import compute_scale_advice
from connectivity_registry import ConnectivityRegistry
from quota_alerts import get_quota_alert_preferences
from quota_alerts import reached_alert
from commerce_models import (
    UTC,
    _human_bytes,
    _new_id,
    _now_text,
)


def _metrics_by_server(self) -> dict[str | None, dict[str, Any]]:
    server_ids = (
        self.outline.server_ids()
        if callable(getattr(self.outline, "server_ids", None))
        else (None,)
    )
    result: dict[str | None, dict[str, Any]] = {}
    for server_id in server_ids:
        try:
            client = self._outline_client(server_id)
            payload = client.transfer_metrics()
            by_key = (
                payload.get("bytesTransferredByUserId", {}) if isinstance(payload, dict) else {}
            )
            result[server_id] = by_key if isinstance(by_key, dict) else {}
        except Exception:
            continue
    return result

def queue_quota_warnings(
    self,
    now: datetime | None = None,
    metrics: dict[str, Any] | None = None,
) -> int:
    """Queue one Telegram warning as each remaining-quota threshold is crossed."""
    default_server_id = getattr(self.outline, "default_server_id", None)
    if metrics is None:
        metrics_by_server = self._metrics_by_server()
    else:
        by_key = (
            metrics.get("bytesTransferredByUserId", {}) if isinstance(metrics, dict) else {}
        )
        metrics_by_server = {default_server_id: by_key if isinstance(by_key, dict) else {}}
    current = (now or datetime.now(UTC)).astimezone(UTC)
    now_text = _now_text(current)
    queued = 0
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        rows = connection.execute(
            """SELECT k.id, k.subscription_id, k.telegram_id, k.outline_key_id,
                      k.quota_bytes, k.status, k.quota_warning_percent,
                      k.server_id, s.plan_code, s.expires_at
               FROM paid_vpn_keys k JOIN subscriptions s ON s.id = k.subscription_id
               WHERE k.status = 'active' AND s.status = 'active'
                 AND k.quota_bytes IS NOT NULL"""
        ).fetchall()
        for row in rows:
            try:
                server_key = row["server_id"] or default_server_id
                by_key = metrics_by_server.get(server_key, {})
                used = max(0, int(by_key.get(str(row["outline_key_id"]), 0) or 0))
                quota = int(row["quota_bytes"])
            except (TypeError, ValueError):
                continue
            if quota <= 0 or used >= quota:
                continue
            remaining = quota - used
            preferences = get_quota_alert_preferences(self.database, int(row["telegram_id"]))
            reached = reached_alert(preferences, quota, remaining)
            if reached is None:
                continue
            threshold_bytes, threshold_label = reached
            remaining_percent = remaining * 100 / quota
            dedupe_key = (
                f"quota-warning:paid:{row['subscription_id']}:"
                f"v{preferences.get('version', 1)}:{threshold_bytes}"
            )
            try:
                existing = connection.execute(
                    "SELECT id FROM notifications WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
                if existing is None:
                    text = (
                        f"📶 VPN usage alert: your AuriX {row['plan_code']} key has "
                        f"{_human_bytes(remaining)} remaining "
                        f"({remaining_percent:.1f}% of {_human_bytes(quota)}).\n"
                        f"Your configured alert level: {threshold_label} remaining.\n"
                        "This is based on Outline's trailing-30-day usage. "
                        "When no quota remains, the key will be blocked and deleted. "
                        f"Expires: {row['expires_at']}"
                    )
                    connection.execute(
                        """INSERT INTO notifications
                           (id, dedupe_key, telegram_id, kind, text, status,
                            next_attempt_at, created_at)
                           VALUES (?, ?, ?, 'quota_warning', ?, 'pending', ?, ?)""",
                        (_new_id(), dedupe_key, row["telegram_id"], text, now_text, now_text),
                    )
                    queued += 1
                connection.execute(
                    "UPDATE paid_vpn_keys SET quota_warning_percent = ? WHERE id = ?",
                    (int(remaining_percent), row["id"]),
                )
            except Exception as exc:
                if self.database.is_integrity_error(exc):
                    continue
                raise
    return queued

def enforce_quotas(
    self,
    now: datetime | None = None,
    metrics: dict[str, Any] | None = None,
) -> int:
    """Observe Outline transfer metrics and queue one idempotent hard revoke.

    Aggregate entitlement state is the authoritative safety brake. Outline's
    per-key data limit remains the immediate remote safety brake. Metrics are
    only an observation; once ``used >= quota`` is seen we fail closed in
    AuriX and delete the known remote key.  Missing/stale metrics never
    restore or disable a key.
    """
    current = (now or datetime.now(UTC)).astimezone(UTC)
    default_server_id = getattr(self.outline, "default_server_id", None)
    if metrics is None:
        metrics_by_server = self._metrics_by_server()
    else:
        by_key = (
            metrics.get("bytesTransferredByUserId", {}) if isinstance(metrics, dict) else {}
        )
        metrics_by_server = {default_server_id: by_key if isinstance(by_key, dict) else {}}
    try:
        self.queue_quota_warnings(current, metrics)
    except Exception as exc:
        # A notification outage must never delay the hard quota revoke.
        print(f"paid quota warning error: {type(exc).__name__}", file=sys.stderr)
    with self.database.connect() as connection:
        rows = connection.execute(
            """SELECT k.id, k.subscription_id, k.outline_key_id, k.quota_bytes, k.server_id,
                      k.status, s.status AS subscription_status
               FROM paid_vpn_keys k JOIN subscriptions s ON s.id = k.subscription_id
               WHERE k.status = 'active' AND s.status = 'active' AND k.quota_bytes IS NOT NULL"""
        ).fetchall()
    scheduled = 0
    for row in rows:
        quota = int(row["quota_bytes"])
        aggregate_exhausted = False
        identity = getattr(self, "identity", None)
        if identity is not None:
            try:
                aggregate_exhausted = identity.subscription_is_exhausted(str(row["subscription_id"]))
            except Exception:
                aggregate_exhausted = False
        if aggregate_exhausted:
            # Aggregate ledger state remains authoritative even when the
            # endpoint that served the credential is currently unreachable.
            used = quota
            quota_reason = "aggregate_quota"
        else:
            try:
                server_key = row["server_id"] or default_server_id
                by_key = metrics_by_server.get(server_key, {})
                used = int(by_key.get(str(row["outline_key_id"]), 0) or 0)
            except (TypeError, ValueError):
                continue
            if used < quota:
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE paid_vpn_keys SET last_usage_bytes = ?, last_usage_observed_at = ? WHERE id = ?",
                        (used, _now_text(current), row["id"]),
                    )
                continue
            quota_reason = "quota"
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            existing = connection.execute(
                "SELECT id FROM quota_events WHERE subscription_id = ? AND reason = ?",
                (row["subscription_id"], quota_reason),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO quota_events
                       (id, subscription_id, reason, observed_bytes, quota_bytes, observed_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (_new_id(), row["subscription_id"], quota_reason, used, quota, _now_text(current)),
                )
                scheduled += 1
            connection.execute(
                """UPDATE paid_vpn_keys SET status = 'active',
                          last_usage_bytes = ?, last_usage_observed_at = ?, quota_reason = ?
                   WHERE id = ? AND status = 'active'""",
                (used, _now_text(current), quota_reason, row["id"]),
            )
            connection.execute(
                "UPDATE subscriptions SET status = 'revoked' WHERE id = ? AND status = 'active'",
                (row["subscription_id"],),
            )
            connection.execute(
                """INSERT INTO provisioning_jobs
                   (id, subscription_id, operation, status, next_attempt_at, created_at)
                   VALUES (?, ?, 'revoke', 'pending', ?, ?)
                   ON CONFLICT(subscription_id, operation) DO NOTHING""",
                (_new_id(), row["subscription_id"], _now_text(current), _now_text(current)),
            )
    return scheduled

def _required_scale_observations() -> int:
    """Return the minimum independent scale observations required to queue."""
    try:
        return max(2, min(10, int(os.environ.get("AURIX_SCALE_REQUIRED_OBSERVATIONS", "2"))))
    except (TypeError, ValueError):
        return 2

def _scale_observation_interval_seconds() -> int:
    """Bound the interval so repeated UI refreshes cannot fake a window."""
    try:
        return max(
            0,
            min(
                86_400,
                int(os.environ.get("AURIX_SCALE_OBSERVATION_INTERVAL_SECONDS", "300")),
            ),
        )
    except (TypeError, ValueError):
        return 300

def _record_scale_observation(
    self,
    current: datetime,
    servers: list[dict[str, Any]],
    advice: dict[str, Any],
) -> dict[str, Any]:
    """Persist a non-secret fleet posture and calculate its consecutive gate.

    A capacity button may be pressed repeatedly, so observations are
    rate-limited and idempotent. Only distinct time windows count toward a
    scale-out intent; a stable/blocked observation resets the consecutive
    qualifying count. This is evidence collection only and never calls a
    provider.
    """
    required = self._required_scale_observations()
    interval = self._scale_observation_interval_seconds()
    observed_at = _now_text(current)
    fingerprint_payload = [
        {
            "server_id": str(item.get("server_id") or ""),
            "enabled": int(item.get("enabled") or 0),
            "health_status": str(item.get("health_status") or ""),
            "saleable_key_capacity": item.get("saleable_key_capacity"),
            "key_demand": item.get("key_demand"),
            "remaining_key_slots": item.get("remaining_key_slots"),
            "monthly_traffic_bytes": item.get("monthly_traffic_bytes"),
            "committed_traffic_bytes": item.get("committed_traffic_bytes"),
        }
        for item in sorted(servers, key=lambda value: str(value.get("server_id") or ""))
    ]
    fleet_fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    healthy_count = sum(1 for item in servers if item.get("health_status") == "healthy")
    status = str(advice.get("status") or "unconfigured")
    with self.database.connect() as connection:
        if not self._table_exists(connection, "scale_observations"):
            return {
                "consecutive_observations": 0,
                "required_observations": required,
                "observation_ready": False,
                "last_observed_at": None,
            }
        self.database.begin_write(connection)
        latest = connection.execute(
            """SELECT observed_at FROM scale_observations
               ORDER BY observed_at DESC LIMIT 1"""
        ).fetchone()
        should_insert = True
        latest_at: datetime | None = None
        if latest is not None and latest["observed_at"]:
            try:
                latest_at = datetime.fromisoformat(str(latest["observed_at"])).astimezone(UTC)
            except (TypeError, ValueError):
                latest_at = None
            if latest_at is not None:
                current_utc = current.astimezone(UTC)
                should_insert = current_utc > latest_at + timedelta(seconds=interval)
        if should_insert:
            connection.execute(
                """INSERT INTO scale_observations
                   (id, fleet_fingerprint, observed_at, status,
                    utilization_percent, remaining_slots, saleable_capacity,
                    traffic_utilization_percent, healthy_server_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(fleet_fingerprint, observed_at) DO NOTHING""",
                (
                    _new_id(),
                    fleet_fingerprint,
                    observed_at,
                    status,
                    advice.get("utilization_percent"),
                    advice.get("remaining_slots"),
                    advice.get("saleable_capacity"),
                    advice.get("traffic_utilization_percent"),
                    healthy_count,
                    observed_at,
                ),
            )
        rows = connection.execute(
            """SELECT observed_at, status FROM scale_observations
               ORDER BY observed_at DESC LIMIT 10"""
        ).fetchall()
    consecutive = 0
    for row in rows:
        if str(row["status"]) not in {"prepare", "urgent"}:
            break
        consecutive += 1
    return {
        "consecutive_observations": consecutive,
        "required_observations": required,
        "observation_ready": status in {"prepare", "urgent"} and consecutive >= required,
        "last_observed_at": rows[0]["observed_at"] if rows else None,
        "observation_interval_seconds": interval,
    }

from commerce_worker_capacity_snapshot import capacity_snapshot
def _scale_advice(servers: list[dict[str, Any]]) -> dict[str, Any]:
    return compute_scale_advice(servers)
