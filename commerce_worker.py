"""Durable provisioning, revocation, quota, and notification worker boundary."""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from typing import Any

from commerce_models import (
    JOB_RETRY_DELAY,
    NOTIFICATION_RETRY_DELAY,
    QUOTA_WARNING_THRESHOLDS,
    UTC,
    CommerceError,
    _human_bytes,
    _new_id,
    _now_text,
    _paid_outline_key_name,
)
from commerce_repositories import _PostgresConnection


class CommerceWorkerMixin:
    """Reliable-worker operations sharing the service transaction boundary."""

    def _claim_job(self, operation: str, now: datetime) -> dict[str, Any] | None:
        now_text = _now_text(now)
        stale_before = _now_text(now - timedelta(minutes=5))
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE provisioning_jobs SET status = 'pending', locked_at = NULL
                   WHERE status = 'running' AND locked_at < ?""",
                (stale_before,),
            )
            lock_clause = (
                " FOR UPDATE SKIP LOCKED" if isinstance(connection, _PostgresConnection) else ""
            )
            row = connection.execute(
                """SELECT * FROM provisioning_jobs
                   WHERE operation = ? AND status = 'pending' AND next_attempt_at <= ?
                   ORDER BY created_at LIMIT 1"""
                + lock_clause,
                (operation, now_text),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """UPDATE provisioning_jobs
                   SET status = 'running', attempts = attempts + 1, locked_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (now_text, row["id"]),
            )
            result = dict(row)
            result["attempts"] = row["attempts"] + 1
            return result

    def _job_done(self, job_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE provisioning_jobs SET status = 'done', locked_at = NULL, last_error = NULL
                   WHERE id = ?""",
                (job_id,),
            )

    def _job_failed(self, job_id: str, error: Exception, now: datetime) -> None:
        safe_error = f"{type(error).__name__}: {str(error)[:500]}"
        next_attempt = _now_text(now + JOB_RETRY_DELAY)
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE provisioning_jobs
                   SET status = CASE WHEN attempts >= 8 THEN 'failed' ELSE 'pending' END,
                       next_attempt_at = ?, locked_at = NULL, last_error = ?
                   WHERE id = ?""",
                (next_attempt, safe_error, job_id),
            )

    def failed_jobs(
        self, limit: int = 20, include_nonterminal: bool = False
    ) -> list[dict[str, Any]]:
        """Return worker operations needing attention.

        The default remains terminal-only for API compatibility; operators can
        request pending/running retries so a silent revoke failure is visible
        before the eighth attempt.
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT j.id AS job_id, j.operation, j.attempts, j.last_error,
                          j.next_attempt_at, j.status AS job_status, s.order_id, s.telegram_id,
                          s.plan_code, s.status AS subscription_status
                   FROM provisioning_jobs j
                   JOIN subscriptions s ON s.id = j.subscription_id
                   WHERE j.status = 'failed' OR (? = 1 AND j.status IN ('pending', 'running'))
                   ORDER BY j.created_at LIMIT ?""",
                (1 if include_nonterminal else 0, max(1, min(limit, 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def retry_job(self, job_id: str, admin_id: int, now: datetime | None = None) -> str:
        """Requeue one exact failed job (avoids ambiguous order-level retries)."""
        current = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                "SELECT id, operation, subscription_id FROM provisioning_jobs WHERE id = ? AND status = 'failed'",
                (job_id,),
            ).fetchone()
            if row is None:
                raise CommerceError("No terminal worker failure exists for that job")
            connection.execute(
                """UPDATE provisioning_jobs SET status = 'pending', attempts = 0,
                          next_attempt_at = ?, locked_at = NULL, last_error = NULL
                   WHERE id = ? AND status = 'failed'""",
                (current, job_id),
            )
            self._audit(
                connection,
                "job_retried",
                "provisioning_job",
                job_id,
                "admin",
                str(admin_id),
                {"operation": row["operation"]},
            )
        return str(row["operation"])

    def retry_failed_job(
        self,
        order_id: str,
        admin_id: int,
        now: datetime | None = None,
        operation: str | None = None,
    ) -> str:
        """Requeue one terminal job after an operator has reviewed its error."""
        current = _now_text(now)
        if operation not in (None, "provision", "revoke"):
            raise CommerceError("Unknown worker operation")
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                """SELECT j.id, j.operation, s.id AS subscription_id
                   FROM provisioning_jobs j JOIN subscriptions s
                     ON s.id = j.subscription_id
                   WHERE s.order_id = ? AND j.status = 'failed'
                     AND (? IS NULL OR j.operation = ?)
                   ORDER BY j.created_at DESC LIMIT 1""",
                (order_id, operation, operation),
            ).fetchone()
            if row is None:
                raise CommerceError("No terminal worker failure exists for this order")
            connection.execute(
                """UPDATE provisioning_jobs
                   SET status = 'pending', attempts = 0, next_attempt_at = ?,
                       locked_at = NULL, last_error = NULL
                   WHERE id = ? AND status = 'failed'""",
                (current, row["id"]),
            )
            self._audit(
                connection,
                "job_retried",
                "subscription",
                str(row["subscription_id"]),
                "admin",
                str(admin_id),
                {"operation": row["operation"], "order_id": order_id},
            )
        return str(row["operation"])

    def _find_key(self, name: str) -> dict[str, Any] | None:
        result = self.outline.list_keys()
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
                        local = connection.execute(
                            "SELECT id, telegram_id, data_limit_bytes, expires_at FROM keys WHERE outline_key_id = ?",
                            (str(item["id"]),),
                        ).fetchone()
                        if local is not None:
                            connection.execute(
                                "UPDATE keys SET status = 'revoked' WHERE id = ?", (local["id"],)
                            )
                            connection.execute(
                                """INSERT INTO key_termination_events
                                   (key_id, telegram_id, outline_key_id, reason, quota_bytes,
                                    expires_at, detected_at, remote_state, delete_attempts,
                                    deletion_verified_at)
                                   VALUES (?, ?, ?, 'paid_upgrade_cleanup', ?, ?, ?, 'delete_accepted', 1, ?)
                                   ON CONFLICT(key_id, reason) DO UPDATE SET
                                      remote_state = excluded.remote_state,
                                      delete_attempts = key_termination_events.delete_attempts + 1,
                                      deletion_verified_at = excluded.deletion_verified_at""",
                                (
                                    local["id"],
                                    local["telegram_id"],
                                    str(item["id"]),
                                    local["data_limit_bytes"],
                                    local["expires_at"],
                                    _now_text(),
                                    _now_text(),
                                ),
                            )
                except Exception as exc:
                    with self.database.connect() as connection:
                        self.database.begin_write(connection)
                        local = connection.execute(
                            "SELECT id, telegram_id, data_limit_bytes, expires_at FROM keys WHERE outline_key_id = ?",
                            (str(item.get("id")),),
                        ).fetchone()
                        if local is not None:
                            connection.execute(
                                """INSERT INTO key_termination_events
                                   (key_id, telegram_id, outline_key_id, reason, quota_bytes,
                                    expires_at, detected_at, remote_state, delete_attempts, last_error)
                                   VALUES (?, ?, ?, 'paid_upgrade_cleanup', ?, ?, ?, 'retrying', 1, ?)
                                   ON CONFLICT(key_id, reason) DO UPDATE SET
                                      remote_state = 'retrying', delete_attempts = key_termination_events.delete_attempts + 1,
                                      last_error = excluded.last_error""",
                                (
                                    local["id"],
                                    local["telegram_id"],
                                    str(item.get("id")),
                                    local["data_limit_bytes"],
                                    local["expires_at"],
                                    _now_text(),
                                    type(exc).__name__[:128],
                                ),
                            )

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
            self._job_done(job["id"])
            return
        key_name = _paid_outline_key_name(subscription)
        key = None
        deterministic_id = f"aurix-{subscription['id']}"
        getter = getattr(self.outline, "get_key", None)
        if callable(getter):
            try:
                key = getter(deterministic_id)
            except Exception:
                key = None
        if key is None:
            key = self._find_key(key_name)
        if key is None:
            legacy_key = self._find_key(f"aurix-sub-{subscription['id']}")
            if legacy_key is not None:
                key = legacy_key
                rename = getattr(self.outline, "rename_key", None)
                if callable(rename):
                    rename(str(key["id"]), key_name)
        created_remote = False
        if key is None:
            deterministic_create = getattr(self.outline, "create_key_with_id", None)
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
                        key = self.outline.create_key(key_name, desired_quota)
                    else:
                        raise
            else:
                key = self.outline.create_key(key_name, desired_quota)
            created_remote = True
        try:
            if not isinstance(key, dict) or not key.get("id") or not key.get("accessUrl"):
                raise CommerceError("Outline key response lacks id or accessUrl")
            if desired_quota is not None:
                self.outline.set_data_limit(str(key["id"]), desired_quota)
            created_at = _now_text(now)
            activated_at = current_dt.isoformat()
            activated_expires_at = (
                current_dt + timedelta(days=int(subscription["duration_days"] or 0))
            ).isoformat()
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                connection.execute(
                    """INSERT INTO paid_vpn_keys
                       (id, subscription_id, telegram_id, outline_key_id, access_url,
                        quota_bytes, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'active', ?)""",
                    (
                        _new_id(),
                        subscription["id"],
                        subscription["telegram_id"],
                        str(key["id"]),
                        self._encrypt_access_url(str(key["accessUrl"])),
                        desired_quota,
                        created_at,
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
                        self._encrypt_access_url(str(key["accessUrl"])),
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
                    {"outline_key_id": str(key["id"]), "activated_at": activated_at},
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
        except Exception:
            if created_remote and isinstance(key, dict) and key.get("id"):
                try:
                    self.outline.delete_key(str(key["id"]))
                except Exception:
                    pass
            raise

    def _expire(self, now: datetime) -> int:
        now_text = _now_text(now)
        count = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            rows = connection.execute(
                """SELECT id FROM subscriptions
                   WHERE status = 'active' AND expires_at <= ?""",
                (now_text,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE subscriptions SET status = 'expired' WHERE id = ?",
                    (row["id"],),
                )
                self._audit(
                    connection,
                    "subscription_expired",
                    "subscription",
                    row["id"],
                    "system",
                    None,
                    {"detected_at": now_text},
                )
                connection.execute(
                    """INSERT INTO provisioning_jobs
                       (id, subscription_id, operation, status, next_attempt_at, created_at)
                       VALUES (?, ?, 'revoke', 'pending', ?, ?)
                       ON CONFLICT(subscription_id, operation) DO NOTHING""",
                    (_new_id(), row["id"], now_text, now_text),
                )
                count += 1
        return count

    def _revoke(self, job: dict[str, Any], now: datetime) -> None:
        with self.database.connect() as connection:
            key = connection.execute(
                """SELECT k.*, s.status AS subscription_status,
                          o.id AS order_id, o.refund_status
                   FROM paid_vpn_keys k
                   JOIN subscriptions s ON s.id = k.subscription_id
                   JOIN orders o ON o.id = s.order_id
                   WHERE k.subscription_id = ?""",
                (job["subscription_id"],),
            ).fetchone()
        if key is None or key["status"] == "revoked":
            self._job_done(job["id"])
            return
        try:
            self.outline.delete_key(key["outline_key_id"])
            getter = getattr(self.outline, "get_key", None)
            remote_state = "delete_accepted"
            if callable(getter):
                if getter(str(key["outline_key_id"])) is not None:
                    raise CommerceError("Outline key still exists after delete")
                remote_state = "deleted_verified"
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                connection.execute(
                    """UPDATE paid_vpn_keys SET status = 'revoked', revoked_at = ?
                       WHERE id = ?""",
                    (_now_text(now), key["id"]),
                )
                connection.execute(
                    "UPDATE provisioning_jobs SET status = 'done', locked_at = NULL WHERE id = ?",
                    (job["id"],),
                )
                quota_reason = key["quota_reason"] if "quota_reason" in key.keys() else None
                quota_event = connection.execute(
                    """SELECT observed_bytes, quota_bytes, observed_at FROM quota_events
                       WHERE subscription_id = ? AND reason = 'quota'""",
                    (job["subscription_id"],),
                ).fetchone()
                if key["refund_status"] == "refunded":
                    notice = "Your AuriX order was refunded to your wallet and its VPN access was terminated."
                    notice_kind = "payment_refunded"
                elif quota_reason == "quota":
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
                connection.execute(
                    """INSERT INTO notifications
                       (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
                       VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                       ON CONFLICT(dedupe_key) DO NOTHING""",
                    (
                        _new_id(),
                        (
                            f"access-revoked:{key['order_id']}"
                            if key["refund_status"] == "refunded"
                            else f"vpn-{notice_kind}:{job['subscription_id']}"
                        ),
                        key["telegram_id"],
                        notice_kind,
                        notice,
                        _now_text(now),
                        _now_text(now),
                    ),
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

    def queue_quota_warnings(
        self,
        now: datetime | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> int:
        """Queue one Telegram warning as each remaining-quota threshold is crossed."""
        if metrics is None:
            try:
                metrics = self.outline.transfer_metrics()
            except Exception:
                return 0
        by_key = metrics.get("bytesTransferredByUserId", {}) if isinstance(metrics, dict) else {}
        if not isinstance(by_key, dict):
            return 0
        current = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = _now_text(current)
        queued = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            rows = connection.execute(
                """SELECT k.id, k.subscription_id, k.telegram_id, k.outline_key_id,
                          k.quota_bytes, k.status, k.quota_warning_percent,
                          s.plan_code, s.expires_at
                   FROM paid_vpn_keys k JOIN subscriptions s ON s.id = k.subscription_id
                   WHERE k.status = 'active' AND s.status = 'active'
                     AND k.quota_bytes IS NOT NULL"""
            ).fetchall()
            for row in rows:
                try:
                    used = max(0, int(by_key.get(str(row["outline_key_id"]), 0) or 0))
                    quota = int(row["quota_bytes"])
                except (TypeError, ValueError):
                    continue
                if quota <= 0 or used >= quota:
                    continue
                remaining = quota - used
                reached = next(
                    (
                        percent
                        for percent, fraction in reversed(QUOTA_WARNING_THRESHOLDS)
                        if remaining <= quota * fraction
                    ),
                    None,
                )
                if reached is None:
                    continue
                previous = row["quota_warning_percent"]
                if previous is not None and int(previous) <= reached:
                    continue
                dedupe_key = f"quota-warning:paid:{row['subscription_id']}:{reached}"
                try:
                    existing = connection.execute(
                        "SELECT id FROM notifications WHERE dedupe_key = ?",
                        (dedupe_key,),
                    ).fetchone()
                    if existing is None:
                        remaining_percent = remaining * 100 / quota
                        text = (
                            f"Quota warning: your AuriX {row['plan_code']} key has "
                            f"{_human_bytes(remaining)} remaining "
                            f"({remaining_percent:.1f}% of {_human_bytes(quota)}).\n"
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
                        (reached, row["id"]),
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

        Outline's per-key data limit is the immediate safety brake.  Metrics are
        only an observation; once ``used >= quota`` is seen we fail closed in
        AuriX and delete the known remote key.  Missing/stale metrics never
        restore or disable a key.
        """
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if metrics is None:
            try:
                metrics = self.outline.transfer_metrics()
            except Exception:
                return 0
        by_key = metrics.get("bytesTransferredByUserId", {}) if isinstance(metrics, dict) else {}
        if not isinstance(by_key, dict):
            return 0
        try:
            self.queue_quota_warnings(current, metrics)
        except Exception as exc:
            # A notification outage must never delay the hard quota revoke.
            print(f"paid quota warning error: {type(exc).__name__}", file=sys.stderr)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT k.id, k.subscription_id, k.outline_key_id, k.quota_bytes,
                          k.status, s.status AS subscription_status
                   FROM paid_vpn_keys k JOIN subscriptions s ON s.id = k.subscription_id
                   WHERE k.status = 'active' AND s.status = 'active' AND k.quota_bytes IS NOT NULL"""
            ).fetchall()
        scheduled = 0
        for row in rows:
            try:
                used = int(by_key.get(str(row["outline_key_id"]), 0) or 0)
            except (TypeError, ValueError):
                continue
            quota = int(row["quota_bytes"])
            if used < quota:
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE paid_vpn_keys SET last_usage_bytes = ?, last_usage_observed_at = ? WHERE id = ?",
                        (used, _now_text(current), row["id"]),
                    )
                continue
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                existing = connection.execute(
                    "SELECT id FROM quota_events WHERE subscription_id = ? AND reason = 'quota'",
                    (row["subscription_id"],),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """INSERT INTO quota_events
                           (id, subscription_id, reason, observed_bytes, quota_bytes, observed_at)
                           VALUES (?, ?, 'quota', ?, ?, ?)""",
                        (_new_id(), row["subscription_id"], used, quota, _now_text(current)),
                    )
                    scheduled += 1
                connection.execute(
                    """UPDATE paid_vpn_keys SET status = 'active',
                              last_usage_bytes = ?, last_usage_observed_at = ?, quota_reason = 'quota'
                       WHERE id = ? AND status = 'active'""",
                    (used, _now_text(current), row["id"]),
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

    def capacity_snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        """Return operator-only counts and mapped transfer usage, never access URLs."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        expiring_at = _now_text(current + timedelta(hours=24))
        server = self.outline.server_info()
        outline_version = str(server.get("version", "unknown"))[:64]
        with self.database.connect() as connection:
            counts = connection.execute(
                """SELECT
                       (SELECT COUNT(*) FROM subscriptions WHERE status = 'active') AS active_subscriptions,
                       (SELECT COUNT(*) FROM paid_vpn_keys WHERE status = 'active') AS active_keys,
                       (SELECT COUNT(*) FROM provisioning_jobs WHERE status IN ('pending', 'running')) AS pending_jobs,
                       (SELECT COUNT(*) FROM provisioning_jobs WHERE status = 'failed') AS failed_jobs,
                       (SELECT COUNT(*) FROM subscriptions
                          WHERE status = 'active'
                            AND expires_at <= ?) AS expiring_24h""",
                (expiring_at,),
            ).fetchone()
            key_rows = connection.execute(
                """SELECT outline_key_id, telegram_id, quota_bytes
                   FROM paid_vpn_keys WHERE status = 'active'"""
            ).fetchall()
        metrics = self.outline.transfer_metrics()
        by_key = metrics.get("bytesTransferredByUserId", {})
        if not isinstance(by_key, dict):
            by_key = {}
        usage = []
        for row in key_rows:
            raw_used = by_key.get(str(row["outline_key_id"]), 0)
            try:
                used_bytes = max(0, int(raw_used or 0))
            except (TypeError, ValueError):
                used_bytes = 0
            usage.append(
                {
                    "outline_key_id": row["outline_key_id"],
                    "telegram_id": row["telegram_id"],
                    "quota_bytes": row["quota_bytes"],
                    "used_bytes": used_bytes,
                }
            )
        return {**dict(counts), "outline_version": outline_version, "usage": usage}

    def pending_notifications(
        self, now: datetime | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        now_text = _now_text(now)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM notifications
                   WHERE status IN ('pending', 'failed')
                     AND dead_lettered_at IS NULL
                     AND next_attempt_at <= ?
                   ORDER BY created_at LIMIT ?""",
                (now_text, max(1, min(limit, 100))),
            ).fetchall()
        notifications = []
        for row in rows:
            notification = dict(row)
            access_url = self._decrypt_access_url(notification.get("access_url_ciphertext"))
            if access_url:
                notification["text"] += f"\n\nYour Outline key:\n{access_url}"
            elif notification.get("access_url_ciphertext"):
                notification["secret_unavailable"] = True
            notifications.append(notification)
        return notifications

    def mark_notification_sent(self, notification_id: str, now: datetime | None = None) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE notifications SET status = 'sent', sent_at = ? WHERE id = ?""",
                (_now_text(now), notification_id),
            )

    def mark_notification_failed(self, notification_id: str, now: datetime | None = None) -> None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE notifications
                   SET status = 'failed', attempts = attempts + 1,
                       dead_lettered_at = CASE WHEN attempts + 1 >= 8 THEN ? ELSE dead_lettered_at END,
                       next_attempt_at = CASE WHEN attempts + 1 >= 8 THEN '9999-12-31T00:00:00+00:00' ELSE ? END
                   WHERE id = ?""",
                (
                    _now_text(current),
                    _now_text(current + NOTIFICATION_RETRY_DELAY),
                    notification_id,
                ),
            )
