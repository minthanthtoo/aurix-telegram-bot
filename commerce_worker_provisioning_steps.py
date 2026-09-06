"""Remote-key and committed-finalization steps for paid provisioning."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from commerce_models import CommerceError, UTC, _new_id, _now_text
from commerce_provisioning_repository import ProvisioningRepository
from connectivity_registry import ConnectivityRegistry


_PROVISIONING = ProvisioningRepository()


def resolve_remote_provisioning_key(
    self: Any,
    outline: Any,
    subscription: Any,
    key_name: str,
    desired_quota: int,
) -> tuple[Any, bool]:
    """Find or create the deterministic remote credential exactly once."""
    deterministic_id = f"aurix-{subscription['id']}"
    key = None
    getter = getattr(outline, "get_key", None)
    if callable(getter):
        try:
            key = getter(deterministic_id)
        except Exception:
            key = None
    if key is None:
        key = self._find_key(key_name, outline)
    if key is None:
        legacy_key = self._find_key(f"aurix-sub-{subscription['id']}", outline)
        if legacy_key is not None:
            key = legacy_key
            rename = getattr(outline, "rename_key", None)
            if callable(rename):
                rename(str(key["id"]), key_name)
    if key is None:
        deterministic_create = getattr(outline, "create_key_with_id", None)
        if callable(deterministic_create):
            try:
                key = deterministic_create(deterministic_id, key_name, desired_quota)
            except Exception as exc:
                recovered = None
                if callable(getter):
                    try:
                        recovered = getter(deterministic_id)
                    except Exception:
                        recovered = None
                if recovered is not None:
                    key = recovered
                elif getattr(exc, "status", None) in (404, 405, 501):
                    key = outline.create_key(key_name, desired_quota)
                else:
                    raise
        else:
            key = outline.create_key(key_name, desired_quota)
        created_remote = True
    else:
        created_remote = False
    return key, created_remote


def commit_provisioning(
    self: Any,
    *,
    job: dict[str, Any],
    subscription: Any,
    key: dict[str, Any],
    desired_quota: int,
    desired_plan_name: str,
    server_id: str | None,
    outline: Any,
    now: datetime,
    current_dt: datetime,
) -> str:
    """Apply the local activation transaction after remote creation succeeds."""
    outline.set_data_limit(str(key["id"]), desired_quota)
    created_at = _now_text(now)
    activated_at = current_dt.isoformat()
    activated_expires_at = (
        current_dt + timedelta(days=int(subscription["duration_days"] or 0))
    ).isoformat()
    encrypted_access_url = self._encrypt_access_url(str(key["accessUrl"]))
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        _PROVISIONING.insert_key(
            connection,
            key_id=_new_id(),
            subscription_id=str(subscription["id"]),
            telegram_id=int(subscription["telegram_id"]),
            outline_key_id=str(key["id"]),
            access_url=encrypted_access_url,
            quota_bytes=desired_quota,
            created_at=created_at,
            server_id=server_id,
        )
        _PROVISIONING.activate_subscription(
            connection,
            subscription_id=str(subscription["id"]),
            activated_at=activated_at,
            expires_at=activated_expires_at,
        )
        _PROVISIONING.queue_ready_notification(
            connection,
            notification_id=_new_id(),
            dedupe_key=f"vpn-ready:{subscription['id']}",
            telegram_id=int(subscription["telegram_id"]),
            text=f"Your {desired_plan_name} AuriX VPN is ready.\n\nExpires: {activated_expires_at}",
            access_url_ciphertext=encrypted_access_url,
            now_text=created_at,
        )
        self._audit(
            connection,
            "key_provisioned",
            "subscription",
            subscription["id"],
            "system",
            None,
            {
                "outline_key_id": str(key["id"]),
                "activated_at": activated_at,
                "server_id": server_id,
            },
        )
        ConnectivityRegistry.bind_credential(
            connection,
            telegram_id=int(subscription["telegram_id"]),
            server_id=str(server_id),
            external_id=str(key["id"]),
            secret_ciphertext=encrypted_access_url,
            now_text=created_at,
            profile_kind="paid",
            subscription_id=str(subscription["id"]),
        )
        _PROVISIONING.mark_job_done(connection, str(job["id"]))
    return activated_expires_at
