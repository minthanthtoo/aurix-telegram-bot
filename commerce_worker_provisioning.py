"""Paid subscription provisioning job orchestration."""

from __future__ import annotations

import sys
from datetime import datetime, UTC
from typing import Any

from commerce_models import CommerceError, _paid_outline_key_name
from commerce_provisioning_repository import ProvisioningRepository
from commerce_worker_provisioning_steps import (
    commit_provisioning,
    resolve_remote_provisioning_key,
)


_PROVISIONING = ProvisioningRepository()


def _provision(self, job: dict[str, Any], now: datetime) -> None:
    """Lease one paid entitlement and converge remote and local state."""
    with self.database.connect() as connection:
        subscription, existing = _PROVISIONING.context(
            connection, str(job["subscription_id"])
        )
    if subscription is None:
        self._job_done(job["id"])
        return

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
            _PROVISIONING.defer_job(connection, str(job["id"]), str(subscription["starts_at"]))
        return
    if subscription["status"] not in ("pending", "active"):
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            _PROVISIONING.expire_subscription(
                connection, str(subscription["id"]), "pending"
            )
            _PROVISIONING.mark_job_expired(connection, str(job["id"]))
        return
    if subscription["status"] == "active" and current_dt >= expires_dt:
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            _PROVISIONING.expire_subscription(
                connection, str(subscription["id"]), "active"
            )
            _PROVISIONING.mark_job_expired(connection, str(job["id"]))
        return
    if existing is not None:
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
        return

    key_name = _paid_outline_key_name(subscription)
    created_remote = False
    key: dict[str, Any] | None = None
    try:
        key, created_remote = resolve_remote_provisioning_key(
            self, outline, subscription, key_name, int(desired_quota)
        )
        if not isinstance(key, dict) or not key.get("id") or not key.get("accessUrl"):
            raise CommerceError("Outline key response lacks id or accessUrl")
        activated_expires_at = commit_provisioning(
            self,
            job=job,
            subscription=subscription,
            key=key,
            desired_quota=int(desired_quota),
            desired_plan_name=str(desired_plan_name),
            server_id=server_id,
            outline=outline,
            now=now,
            current_dt=current_dt,
        )
        # Paid access supersedes free/trial access; cleanup is retryable.
        self._revoke_legacy_free_keys(
            subscription["telegram_id"], str(key["id"]), subscription["username"]
        )
        try:
            self._sync_identity_binding(
                telegram_id=int(subscription["telegram_id"]),
                kind="paid",
                quota_bytes=int(desired_quota),
                expires_at=str(activated_expires_at),
                server_id=str(server_id),
                external_id=str(key["id"]),
                subscription_id=str(subscription["id"]),
            )
        except Exception as exc:
            print(f"identity binding sync error: {type(exc).__name__}", file=sys.stderr)
    except Exception:
        if created_remote and key is not None and key.get("id"):
            try:
                outline.delete_key(str(key["id"]))
            except Exception:
                pass
        raise
