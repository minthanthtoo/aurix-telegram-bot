"""Paid subscription provisioning job workflow."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from typing import Any

from commerce_models import (
    UTC,
    CommerceError,
    _new_id,
    _now_text,
    _paid_outline_key_name,
)
from connectivity_registry import ConnectivityRegistry


def _provision(self, job: dict[str, Any], now: datetime) -> None:
    with self.database.connect() as connection:
        subscription = connection.execute(
            """SELECT s.*, p.quota_bytes AS catalog_quota_bytes,
                      p.name AS catalog_plan_name, u.username
               FROM subscriptions s JOIN plans p ON p.code = s.plan_code
               JOIN users u ON u.telegram_id = s.telegram_id
               WHERE s.id = ?""",
            (job["subscription_id"],),
        ).fetchone()
        existing = connection.execute(
            "SELECT * FROM paid_vpn_keys WHERE subscription_id = ?",
            (job["subscription_id"],),
        ).fetchone()
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
            connection.execute(
                """UPDATE provisioning_jobs SET status = 'pending', next_attempt_at = ?, locked_at = NULL
                   WHERE id = ?""",
                (subscription["starts_at"], job["id"]),
            )
        return
    if subscription["status"] not in ("pending", "active"):
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                "UPDATE subscriptions SET status = 'expired' WHERE id = ? AND status = 'pending'",
                (subscription["id"],),
            )
            connection.execute(
                "UPDATE provisioning_jobs SET status = 'done', locked_at = NULL, last_error = 'expired before provision' WHERE id = ?",
                (job["id"],),
            )
        return
    # Pending entitlements have no expiry clock yet.  Their planned
    # boundary is only a scheduling hint; paid time starts at successful
    # activation below.  Already-active legacy rows retain their stored
    # expiry and are still protected from late provisioning retries.
    if subscription["status"] == "active" and current_dt >= expires_dt:
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                "UPDATE subscriptions SET status = 'expired' WHERE id = ? AND status = 'active'",
                (subscription["id"],),
            )
            connection.execute(
                "UPDATE provisioning_jobs SET status = 'done', locked_at = NULL, last_error = 'expired before provision' WHERE id = ?",
                (job["id"],),
            )
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
    key = None
    deterministic_id = f"aurix-{subscription['id']}"
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
    created_remote = False
    if key is None:
        deterministic_create = getattr(outline, "create_key_with_id", None)
        if callable(deterministic_create):
            try:
                key = deterministic_create(deterministic_id, key_name, desired_quota)
            except Exception as exc:
                # A timeout may have created the remote key.  Re-read the
                # exact id.  Only an explicit unsupported-endpoint status
                # may fall back to POST; retrying an ambiguous timeout with
                # POST could create a second billable remote credential.
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
    try:
        if not isinstance(key, dict) or not key.get("id") or not key.get("accessUrl"):
            raise CommerceError("Outline key response lacks id or accessUrl")
        if desired_quota is not None:
            outline.set_data_limit(str(key["id"]), desired_quota)
        created_at = _now_text(now)
        activated_at = current_dt.isoformat()
        activated_expires_at = (
            current_dt + timedelta(days=int(subscription["duration_days"] or 0))
        ).isoformat()
        encrypted_access_url = self._encrypt_access_url(str(key["accessUrl"]))
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """INSERT INTO paid_vpn_keys
                   (id, subscription_id, telegram_id, outline_key_id, access_url,
                    quota_bytes, status, created_at, server_id)
                   VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                (
                    _new_id(),
                    subscription["id"],
                    subscription["telegram_id"],
                    str(key["id"]),
                    encrypted_access_url,
                    desired_quota,
                    created_at,
                    server_id,
                ),
            )
            connection.execute(
                """UPDATE subscriptions
                   SET status = 'active', activated_at = ?, starts_at = ?, expires_at = ?
                   WHERE id = ?""",
                (activated_at, activated_at, activated_expires_at, subscription["id"]),
            )
            connection.execute(
                """INSERT INTO notifications
                   (id, dedupe_key, telegram_id, kind, text, access_url_ciphertext,
                    status, next_attempt_at, created_at)
                   VALUES (?, ?, ?, 'vpn_ready', ?, ?, 'pending', ?, ?)
                   ON CONFLICT(dedupe_key) DO NOTHING""",
                (
                    _new_id(),
                    f"vpn-ready:{subscription['id']}",
                    subscription["telegram_id"],
                    f"Your {desired_plan_name} AuriX VPN is ready.\n\nExpires: {activated_expires_at}",
                    encrypted_access_url,
                    created_at,
                    created_at,
                ),
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
            connection.execute(
                """UPDATE provisioning_jobs SET status = 'done', locked_at = NULL, last_error = NULL
                   WHERE id = ?""",
                (job["id"],),
            )
        # A paid account supersedes any free/trial key.  This is best-effort
        # cleanup; the paid key remains authoritative and the next startup
        # reconciliation can retry removal if the inventory call failed.
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
            # The paid key and job are already committed. Startup
            # backfill/maintenance will retry the additive identity view.
            print(f"identity binding sync error: {type(exc).__name__}", file=sys.stderr)
    except Exception:
        if created_remote and isinstance(key, dict) and key.get("id"):
            try:
                outline.delete_key(str(key["id"]))
            except Exception:
                pass
        raise

