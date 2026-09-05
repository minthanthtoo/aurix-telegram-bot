"""Customer usage and VPN presentation use cases."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from commerce_models import UTC
from commerce_models import CommerceError
from commerce_models import _now_text


def user_usage(self, telegram_id: int, usage_by_key: dict[str, Any]) -> list[dict[str, Any]]:
    """Return paid key usage belonging to one Telegram user."""
    with self.database.connect() as connection:
        rows = self.customer_reads.user_usage(connection, telegram_id)
    result = []
    for row in rows:
        server_id = str(
            row["server_id"] or getattr(self.outline, "default_server_id", "default")
        )
        key_id = str(row["outline_key_id"])
        scoped = usage_by_key.get("byServer") if isinstance(usage_by_key, dict) else None
        if isinstance(scoped, dict):
            server_usage = scoped.get(server_id)
            server_usage = server_usage if isinstance(server_usage, dict) else {}
            server_observed = server_id in scoped
        else:
            server_usage = usage_by_key if isinstance(usage_by_key, dict) else {}
            server_observed = True
        observed = server_observed and key_id in server_usage
        raw_used = server_usage.get(key_id, row["last_usage_bytes"] or 0)
        try:
            used = max(0, int(raw_used or 0))
        except (TypeError, ValueError):
            used = max(0, int(row["last_usage_bytes"] or 0))
            observed = False
        quota = int(row["quota_bytes"] or 0)
        if quota <= 0:
            continue
        result.append(
            {
                "outline_key_id": key_id,
                "server_id": server_id,
                "server_label": row["server_label"],
                "server_health_status": row["server_health_status"],
                "tier": row["plan_name"] or row["plan_code"],
                "used_bytes": used,
                "quota_bytes": quota,
                "remaining_bytes": max(0, quota - used),
                "usage_observed": observed,
                "expires_at": row["expires_at"],
                "status": "quota exhausted"
                if row["quota_reason"] == "quota"
                else (
                    "revocation failed"
                    if row["revocation_status"] == "failed"
                    else (
                        "revocation pending"
                        if row["subscription_status"] != "active" and row["status"] == "active"
                        else (
                            "revocation pending"
                            if row["status"] == "revoke_failed"
                            else row["status"]
                        )
                    )
                ),
                "repair_status": row["repair_status"],
                "repair_reason": row["repair_reason"],
                "created_at": row["created_at"],
            }
        )
    return result

def user_migrated_usage(self, telegram_id: int) -> int:
    """Return usage already consumed by this user before key turnover.

    Endpoint migration replaces the local credential row with the target
    key so the new key can be delivered normally.  The migration ledger is
    the durable link to the source key's measured traffic; include only
    migrations that reached cutover/source-delete phase.  A job that failed
    before cutover still has the original key and must not be counted a
    second time.
    """
    with self.database.connect() as connection:
        if not self.customer_reads.table_exists(connection, "connectivity_migration_jobs"):
            return 0
        row = self.customer_reads.migrated_usage(connection, telegram_id)
    try:
        return max(0, int(row["used"] if row is not None else 0))
    except (KeyError, TypeError, ValueError):
        return 0

def prune_usage_snapshots(
    self,
    now: datetime | None = None,
    *,
    retention_days: int | None = None,
) -> int:
    """Bound telemetry storage while keeping a useful audit window."""
    if not self._usage_snapshot_table_available():
        return 0
    if retention_days is None:
        try:
            retention_days = int(os.environ.get("AURIX_USAGE_SNAPSHOT_RETENTION_DAYS", "90"))
        except (TypeError, ValueError):
            retention_days = 90
    retention_days = max(7, min(3_650, int(retention_days)))
    current = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = (current - timedelta(days=retention_days)).isoformat()
    with self.database.connect() as connection:
        return self.customer_reads.delete_usage_snapshots(connection, cutoff)

def _usage_snapshot_table_available(self) -> bool:
    with self.database.connect() as connection:
        return self.customer_reads.usage_snapshot_table_available(connection)

def user_vpns(self, telegram_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """Return all of a user's paid entitlements without exposing secrets.

    A customer may own multiple active keys (for devices or parallel
    plans). Access URLs are decrypted only for active, non-expired keys.
    """
    with self.database.connect() as connection:
        rows = self.customer_reads.user_vpns(
            connection, telegram_id, max(1, min(int(limit), 100))
        )
    now_text = _now_text()
    results = []
    for row in rows:
        result = dict(row)
        result["access_url"] = self._decrypt_access_url(result.get("access_url"))
        if (
            result.get("status") != "active"
            or result.get("key_status") != "active"
            or self._repair_blocks_access(result.get("repair_status"))
            or str(result.get("expires_at") or "") <= now_text
        ):
            result["access_url"] = None
        results.append(result)
    return results

def user_vpn(self, telegram_id: int) -> dict[str, Any] | None:
    """Backward-compatible latest/most relevant paid entitlement view."""
    subscriptions = self.user_vpns(telegram_id, limit=1)
    return subscriptions[0] if subscriptions else None

def user_vpn_detail(
    self, telegram_id: int, subscription_id: str
) -> dict[str, Any] | None:
    """Return one customer-owned paid entitlement for a focused key view."""
    with self.database.connect() as connection:
        row = self.customer_reads.user_vpn_detail(connection, telegram_id, subscription_id)
    if row is None:
        return None
    result = dict(row)
    result["access_url"] = self._decrypt_access_url(result.get("access_url"))
    if (
        result.get("status") != "active"
        or result.get("key_status") != "active"
        or self._repair_blocks_access(result.get("repair_status"))
        or str(result.get("expires_at") or "") <= _now_text()
    ):
        result["access_url"] = None
    return result

def receipt_policy(self) -> dict[str, Any]:
    with self.database.connect() as connection:
        row = self.customer_reads.receipt_policy(connection)
    if row is None:
        return {"mode": "manual", "version": 0, "updated_at": None}
    return dict(row)

def set_receipt_mode(
    self,
    mode: str,
    admin_id: int,
    *,
    expected_version: int | None = None,
    reason: str = "changed from Telegram admin panel",
) -> dict[str, Any]:
    normalized = str(mode).strip().lower()
    if normalized not in {"manual", "assisted"}:
        raise CommerceError(
            "Automatic approval requires an authoritative payment verifier; choose manual or assisted"
        )
    now_text = _now_text()
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        current = self.customer_reads.receipt_policy_version(connection)
        if current is None:
            raise CommerceError("Receipt verification policy is unavailable")
        if expected_version is not None and int(current["version"]) != int(expected_version):
            raise CommerceError("Receipt mode changed while you were reviewing it; refresh first")
        old_mode = str(current["mode"])
        self.customer_reads.update_receipt_mode(
            connection, normalized, admin_id, now_text, reason
        )
        self._audit(
            connection,
            "receipt_mode_changed",
            "receipt_policy",
            "1",
            "admin",
            str(admin_id),
            {"old_mode": old_mode, "new_mode": normalized},
        )
    return self.receipt_policy()
