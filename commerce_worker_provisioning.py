"""Paid subscription provisioning job orchestration."""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Any

from commerce_models import CommerceError, _paid_outline_key_name
from commerce_worker_provisioning_lifecycle import (
    finish_existing_provisioning,
    prepare_provisioning,
)
from commerce_worker_provisioning_steps import (
    commit_provisioning,
    resolve_remote_provisioning_key,
)

def _provision(self, job: dict[str, Any], now: datetime) -> None:
    """Lease one paid entitlement and converge remote and local state."""
    preparation = prepare_provisioning(self, job, now)
    if preparation is None:
        return
    (
        subscription,
        existing,
        desired_quota,
        desired_plan_name,
        server_id,
        outline,
        current_dt,
    ) = preparation
    if finish_existing_provisioning(
        self, job, subscription, existing, desired_quota, server_id
    ):
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
            desired_quota=desired_quota,
            desired_plan_name=desired_plan_name,
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
