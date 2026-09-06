"""Lifecycle gates and idempotent handling for paid provisioning jobs."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any

from commerce_provisioning_repository import ProvisioningRepository


_PROVISIONING = ProvisioningRepository()
UTC = timezone.utc


def prepare_provisioning(
    self: Any, job: dict[str, Any], now: datetime
) -> tuple[Any, Any, int, str, str | None, Any, datetime] | None:
    """Load a subscription and resolve defer/expiry gates before remote work."""
    with self.database.connect() as connection:
        subscription, existing = _PROVISIONING.context(
            connection, str(job["subscription_id"])
        )
    if subscription is None:
        self._job_done(job["id"])
        return None

    desired_quota = (
        subscription["quota_bytes"]
        if subscription["quota_bytes"] is not None
        else subscription["catalog_quota_bytes"]
    )
    desired_plan_name = subscription["plan_name"] or subscription["catalog_plan_name"]
    server_id = subscription["server_id"] if "server_id" in subscription.keys() else None
    outline = self._outline_client(server_id)
    current_dt = (now or datetime.now(UTC)).astimezone(UTC)
    starts_dt = datetime.fromisoformat(subscription["starts_at"])
    expires_dt = datetime.fromisoformat(subscription["expires_at"])
    if current_dt < starts_dt:
        with self.database.connect() as connection:
            _PROVISIONING.defer_job(
                connection, str(job["id"]), str(subscription["starts_at"])
            )
        return None
    if subscription["status"] not in ("pending", "active"):
        _expire_job(self, job, subscription, "pending")
        return None
    if subscription["status"] == "active" and current_dt >= expires_dt:
        _expire_job(self, job, subscription, "active")
        return None
    return (
        subscription,
        existing,
        int(desired_quota),
        str(desired_plan_name),
        server_id,
        outline,
        current_dt,
    )


def finish_existing_provisioning(
    self: Any,
    job: dict[str, Any],
    subscription: Any,
    existing: Any,
    desired_quota: int,
    server_id: str | None,
) -> bool:
    """Refresh identity for an existing key and close the idempotent job."""
    if existing is None:
        return False
    if str(subscription["status"]) == "active":
        try:
            self._sync_identity_binding(
                telegram_id=int(subscription["telegram_id"]),
                kind="paid",
                quota_bytes=int(existing["quota_bytes"] or desired_quota),
                expires_at=str(subscription["expires_at"]),
                server_id=str(existing["server_id"] or server_id),
                external_id=str(existing["outline_key_id"]),
                subscription_id=str(subscription["id"]),
            )
        except Exception as exc:
            print(f"identity binding sync error: {type(exc).__name__}", file=sys.stderr)
    self._job_done(job["id"])
    return True


def _expire_job(self: Any, job: dict[str, Any], subscription: Any, status: str) -> None:
    """Persist subscription expiry and close its provisioning job."""
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        _PROVISIONING.expire_subscription(
            connection, str(subscription["id"]), status
        )
        _PROVISIONING.mark_job_expired(connection, str(job["id"]))
