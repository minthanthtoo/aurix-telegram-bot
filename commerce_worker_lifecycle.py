"""Paid credential lifecycle operations."""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from typing import Any
from connectivity_registry import ConnectivityRegistry
from commerce_models import (
    UTC,
    CommerceError,
    _new_id,
    _now_text,
    _paid_outline_key_name,
)


def _find_key(self, name: str, outline: Any | None = None) -> dict[str, Any] | None:
    outline = outline or self.outline
    result = outline.list_keys()
    if isinstance(result, dict):
        keys = result.get("accessKeys", [])
    else:
        keys = result if isinstance(result, list) else []
    if not isinstance(keys, list):
        raise CommerceError("Outline key inventory has an invalid shape")
    matches = [key for key in keys if isinstance(key, dict) and key.get("name") == name]
    if len(matches) > 1:
        raise CommerceError("Outline has multiple keys for one subscription")
    return matches[0] if matches else None

def _revoke_legacy_free_keys(
    self, telegram_id: int, keep_key_id: str, username: str | None = None
) -> None:
    """Remove old free/trial keys when a paid entitlement becomes active."""
    try:
        result = self.outline.list_keys()
    except Exception:
        return
    keys = result.get("accessKeys", []) if isinstance(result, dict) else []
    prefixes = {f"tg-{telegram_id}-", f"{telegram_id}-"}
    if username:
        safe_username = re.sub(r"[^A-Za-z0-9_-]+", "-", str(username).lstrip("@")).strip("-_")[
            :48
        ]
        if safe_username:
            prefixes.add(f"{safe_username}-")
    for item in keys if isinstance(keys, list) else []:
        if not isinstance(item, dict) or str(item.get("id")) == str(keep_key_id):
            continue
        name = str(item.get("name", ""))
        is_new_free = any(name.startswith(prefix) for prefix in prefixes) and (
            "-FREE200MB-" in name
            or "-FREE300MB-" in name
            or "-TRIAL3GB-" in name
            or "-FREE3GB-" in name
        )
        is_legacy_free = name.startswith(f"tg-{telegram_id}-")
        if is_new_free or is_legacy_free:
            try:
                self.outline.delete_key(str(item["id"]))
                with self.database.connect() as connection:
                    self.database.begin_write(connection)
                    local = self.lifecycle.key_for_outline_id(connection, str(item["id"]))
                    if local is not None:
                        self.lifecycle.mark_legacy_key_revoked(connection, local["id"])
                        ConnectivityRegistry.revoke_credential(
                            connection,
                            server_id=str(local["server_id"]),
                            external_id=str(item["id"]),
                            now_text=_now_text(),
                        )
                        self.lifecycle.record_legacy_cleanup(
                            connection,
                            local=local,
                            outline_key_id=str(item["id"]),
                            now_text=_now_text(),
                        )
            except Exception as exc:
                with self.database.connect() as connection:
                    self.database.begin_write(connection)
                    local = self.lifecycle.key_for_outline_id(
                        connection, str(item.get("id"))
                    )
                    if local is not None:
                        self.lifecycle.record_legacy_cleanup_failure(
                            connection,
                            local=local,
                            outline_key_id=str(item.get("id")),
                            error=type(exc).__name__[:128],
                            now_text=_now_text(),
                        )

from commerce_worker_provisioning import _provision
def _expire(self, now: datetime) -> int:
    now_text = _now_text(now)
    count = 0
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        rows = self.lifecycle.expired_subscriptions(connection, now_text)
        for row in rows:
            self.lifecycle.mark_subscription_expired(connection, str(row["id"]))
            self._audit(
                connection,
                "subscription_expired",
                "subscription",
                row["id"],
                "system",
                None,
                {"detected_at": now_text},
            )
            self.lifecycle.insert_revoke_job(
                connection, _new_id(), str(row["id"]), now_text
            )
            count += 1
    return count

def _revoke(self, job: dict[str, Any], now: datetime) -> None:
    with self.database.connect() as connection:
        key = self.lifecycle.revoke_context(connection, str(job["subscription_id"]))
    if key is None or key["status"] == "revoked":
        self._job_done(job["id"])
        return
    server_id = key["server_id"] if "server_id" in key.keys() else None
    outline = self._outline_client(server_id)
    try:
        outline.delete_key(key["outline_key_id"])
        getter = getattr(outline, "get_key", None)
        remote_state = "delete_accepted"
        if callable(getter):
            if getter(str(key["outline_key_id"])) is not None:
                raise CommerceError("Outline key still exists after delete")
            remote_state = "deleted_verified"
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self.lifecycle.mark_paid_key_revoked(connection, key["id"], _now_text(now))
            ConnectivityRegistry.revoke_credential(
                connection,
                server_id=str(server_id),
                external_id=str(key["outline_key_id"]),
                now_text=_now_text(now),
            )
            self.lifecycle.mark_job_done(connection, str(job["id"]))
            quota_reason = key["quota_reason"] if "quota_reason" in key.keys() else None
            quota_event = self.lifecycle.quota_event(
                connection, str(job["subscription_id"])
            )
            if key["refund_status"] == "refunded":
                notice = "Your AuriX order was refunded to your wallet and its VPN access was terminated."
                notice_kind = "payment_refunded"
            elif quota_reason in {"quota", "aggregate_quota"}:
                usage = (
                    f" Observed usage: {int(quota_event['observed_bytes']):,} / "
                    f"{int(quota_event['quota_bytes']):,} bytes."
                    if quota_event is not None
                    else ""
                )
                notice = (
                    "Your AuriX VPN key reached its data limit and was terminated."
                    + usage
                    + " Renew to receive a new key."
                )
                notice_kind = "vpn_quota"
            else:
                notice = "Your AuriX VPN subscription expired and its key was terminated. Renew to restore access."
                notice_kind = "vpn_expired"
            if remote_state == "deleted_verified":
                notice += " Outline confirmed the credential is deleted."
            self.lifecycle.notification(
                connection,
                id=_new_id(),
                dedupe_key=(
                    f"access-revoked:{key['order_id']}"
                    if key["refund_status"] == "refunded"
                    else f"vpn-{notice_kind}:{job['subscription_id']}"
                ),
                telegram_id=key["telegram_id"],
                kind=notice_kind,
                text=notice,
                now_text=_now_text(now),
            )
            self._audit(
                connection,
                "key_revoked",
                "subscription",
                job["subscription_id"],
                "system",
                None,
                {
                    "outline_key_id": key["outline_key_id"],
                    "reason": (
                        "refund"
                        if key["refund_status"] == "refunded"
                        else (quota_reason or "expiry")
                    ),
                    "remote_state": remote_state,
                    "last_usage_bytes": key["last_usage_bytes"],
                    "quota_bytes": key["quota_bytes"],
                },
            )
    except Exception:
        # Keep the entitlement marked active until the remote delete is
        # actually confirmed. The job status/attempts are the retry state;
        # exposing ``revoke_failed`` as an access state made customers and
        # operators believe a credential had already been revoked.
        raise

def process_jobs(self, now: datetime | None = None, max_jobs: int = 10) -> int:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    processed = 0
    # Revokes run first so an expired/quota-exhausted key is removed before
    # a scheduled renewal provisions its replacement.
    while processed < max_jobs:
        job = self._claim_job("revoke", current)
        if job is None:
            break
        try:
            self._revoke(job, current)
        except Exception as exc:
            self._job_failed(job["id"], exc, current)
        processed += 1
    while processed < max_jobs:
        job = self._claim_job("provision", current)
        if job is None:
            break
        try:
            self._provision(job, current)
        except Exception as exc:
            self._job_failed(job["id"], exc, current)
        processed += 1
    return processed

def expire_and_process(self, now: datetime | None = None) -> int:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    self.release_expired_wallet_reservations(current)
    self.expire_open_orders(current)
    self._expire(current)
    return self.process_jobs(current)
