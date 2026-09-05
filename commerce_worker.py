"""Durable provisioning, revocation, quota, and notification worker boundary."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Any

from commerce_models import (
    JOB_RETRY_DELAY,
    NOTIFICATION_RETRY_DELAY,
    UTC,
    CommerceError,
    _human_bytes,
    _new_id,
    _now_text,
    _paid_outline_key_name,
)
from commerce_repositories import _PostgresConnection
from connectivity_registry import ConnectivityRegistry
from identity import IdentityError, IdentityService
from quota_alerts import get_quota_alert_preferences, reached_alert


class CommerceWorkerMixin:
    """Reliable-worker operations sharing the service transaction boundary."""

    def _sync_identity_binding(
        self,
        *,
        telegram_id: int,
        kind: str,
        quota_bytes: int,
        expires_at: str,
        server_id: str,
        external_id: str,
        subscription_id: str | None = None,
        local_key_ref: str | None = None,
    ) -> None:
        """Converge a legacy credential into account/generation/lease state.

        This is deliberately called after the commerce transaction commits:
        the identity service owns its own transaction and must never nest a
        second SQLite write transaction inside Outline provisioning.
        """
        identity = IdentityService(self.database)
        normalized_kind = str(kind).lower()
        if normalized_kind == "paid":
            if not subscription_id:
                raise IdentityError("paid credential is missing its subscription")
            entitlement_id = identity.ensure_subscription_entitlement(
                int(telegram_id),
                str(subscription_id),
                kind="paid",
                quota_bytes=int(quota_bytes),
                expires_at=str(expires_at),
                status="active",
            )
        else:
            ref = local_key_ref
            if ref is None:
                with self.database.connect() as connection:
                    row = connection.execute(
                        """SELECT id FROM keys
                            WHERE server_id = ? AND outline_key_id = ?
                            ORDER BY id DESC LIMIT 1""",
                        (str(server_id), str(external_id)),
                    ).fetchone()
                ref = str(row["id"]) if row is not None else None
            if ref is None:
                raise IdentityError("free credential is missing its local key reference")
            entitlement_id = identity.ensure_key_entitlement(
                int(telegram_id),
                server_id=str(server_id),
                local_key_ref=str(ref),
                kind=normalized_kind if normalized_kind in {"free", "trial", "promo"} else "free",
                quota_bytes=int(quota_bytes),
                expires_at=str(expires_at),
                status="active",
            )
        with self.database.connect() as connection:
            credential = connection.execute(
                """SELECT c.credential_id, c.endpoint_id
                     FROM connectivity_credentials c
                     JOIN connectivity_endpoints e ON e.endpoint_id = c.endpoint_id
                    WHERE e.outline_server_id = ? AND c.external_id = ?
                      AND c.status = 'active'""",
                (str(server_id), str(external_id)),
            ).fetchone()
        if credential is None:
            return
        generation_id = identity.ensure_generation_for_credential(
            entitlement_id,
            str(credential["endpoint_id"]),
            credential_id=str(credential["credential_id"]),
        )
        identity.ensure_generation_lease(
            entitlement_id,
            generation_id,
            str(credential["endpoint_id"]),
            int(quota_bytes),
            str(expires_at),
        )

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

    @staticmethod
    def _managed_repair_max_attempts() -> int:
        try:
            return max(1, min(20, int(os.environ.get("AURIX_KEY_REPAIR_MAX_ATTEMPTS", "8"))))
        except (TypeError, ValueError):
            return 8

    def _claim_managed_key_repair(self, now: datetime) -> dict[str, Any] | None:
        """Lease one missing-managed-key repair for an external-effect call."""
        now_text = _now_text(now)
        stale_before = _now_text(now - timedelta(minutes=10))
        with self.database.connect() as connection:
            if not self._table_exists(connection, "managed_key_repair_jobs"):
                return None
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE managed_key_repair_jobs
                      SET status = 'pending', locked_at = NULL
                    WHERE status = 'running' AND locked_at < ?""",
                (stale_before,),
            )
            lock_clause = (
                " FOR UPDATE SKIP LOCKED" if isinstance(connection, _PostgresConnection) else ""
            )
            row = connection.execute(
                """SELECT * FROM managed_key_repair_jobs
                    WHERE status IN ('pending', 'failed')
                      AND attempts < ? AND next_attempt_at <= ?
                    ORDER BY created_at LIMIT 1"""
                + lock_clause,
                (self._managed_repair_max_attempts(), now_text),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """UPDATE managed_key_repair_jobs
                      SET status = 'running', attempts = attempts + 1, locked_at = ?
                    WHERE id = ? AND status IN ('pending', 'failed')""",
                (now_text, row["id"]),
            )
            if isinstance(connection, _PostgresConnection) and int(updated.rowcount or 0) != 1:
                return None
            result = dict(row)
            result["status"] = "running"
            result["attempts"] = int(row["attempts"] or 0) + 1
            result["locked_at"] = now_text
            return result

    def _managed_repair_failed(self, job_id: str, error: Exception, now: datetime) -> None:
        safe_error = f"{type(error).__name__}: {str(error)[:500]}"
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                "SELECT attempts FROM managed_key_repair_jobs WHERE id = ?",
                (str(job_id),),
            ).fetchone()
            attempts = int(row["attempts"] or 0) if row else self._managed_repair_max_attempts()
            terminal = attempts >= self._managed_repair_max_attempts()
            connection.execute(
                """UPDATE managed_key_repair_jobs
                      SET status = ?, next_attempt_at = ?, locked_at = NULL, last_error = ?
                    WHERE id = ?""",
                (
                    "manual" if terminal else "failed",
                    _now_text(current + JOB_RETRY_DELAY)
                    if not terminal
                    else "9999-12-31T00:00:00+00:00",
                    safe_error,
                    str(job_id),
                ),
            )

    def _managed_repair_manual(self, job: dict[str, Any], reason: str, now: datetime) -> None:
        """Escalate an unsafe repair without mutating local entitlement state."""
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            changed = connection.execute(
                """UPDATE managed_key_repair_jobs
                      SET status = 'manual', locked_at = NULL,
                          next_attempt_at = '9999-12-31T00:00:00+00:00', last_error = ?
                    WHERE id = ? AND status = 'running'""",
                (str(reason)[:500], str(job["id"])),
            )
            if int(getattr(changed, "rowcount", 0) or 0) == 1:
                self._audit(
                    connection,
                    "managed_key_repair_escalated",
                    "managed_key_repair",
                    str(job["id"]),
                    "system",
                    None,
                    {
                        "server_id": str(job["server_id"]),
                        "kind": str(job["kind"]),
                        "local_key_ref": str(job["local_key_ref"]),
                        "reason": str(reason)[:500],
                    },
                )

    def _repair_key_idempotent(
        self,
        outline: Any,
        *,
        key_id: str,
        name: str,
        limit_bytes: int,
    ) -> tuple[dict[str, Any], bool]:
        """Recreate one key while recovering ambiguous PUT/POST outcomes."""
        getter = getattr(outline, "get_key", None)

        def validate(candidate: Any) -> dict[str, Any]:
            if not isinstance(candidate, dict) or not candidate.get("id") or not candidate.get("accessUrl"):
                raise CommerceError("Outline repair response lacks id or accessUrl")
            remote_name = str(candidate.get("name") or "").strip()
            if remote_name and remote_name != name:
                raise CommerceError("Outline repair key belongs to another entitlement")
            return candidate

        if callable(getter):
            existing = getter(str(key_id))
            if existing is not None:
                return validate(existing), False
        creator = getattr(outline, "create_key_with_id", None)
        if callable(creator):
            try:
                return validate(creator(str(key_id), name, limit_bytes)), True
            except Exception as exc:
                if callable(getter):
                    try:
                        recovered = getter(str(key_id))
                    except Exception:
                        recovered = None
                    if recovered is not None:
                        return validate(recovered), False
                if getattr(exc, "status", None) not in (404, 405, 501):
                    raise
        # Legacy adapters choose the ID on POST.  Recover a unique named key
        # before creating another one; an ambiguous timeout is never retried
        # blindly.
        existing_by_name = self._find_key(name, outline)
        if existing_by_name is not None:
            return validate(existing_by_name), False
        return validate(outline.create_key(name, limit_bytes)), True

    def _process_managed_key_repair(self, job: dict[str, Any], now: datetime) -> bool:
        """Repair one active entitlement, preserving observed remaining quota."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        kind = str(job["kind"])
        server_id = str(job["server_id"])
        source_id = str(job["source_external_id"])
        local_ref = str(job["local_key_ref"])
        with self.database.connect() as connection:
            if kind == "paid":
                row = connection.execute(
                    """SELECT k.*, s.status AS subscription_status, s.expires_at,
                              s.plan_name, s.plan_code, s.duration_days, u.username
                         FROM paid_vpn_keys k
                         JOIN subscriptions s ON s.id = k.subscription_id
                         JOIN users u ON u.telegram_id = k.telegram_id
                        WHERE CAST(k.id AS TEXT) = ? AND k.server_id = ?""",
                    (local_ref, server_id),
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT k.*, u.username, g.campaign_code,
                              COALESCE(c.duration_days,
                                       CASE WHEN k.key_type = 'monthly_trial' THEN 30 ELSE 1 END)
                                  AS duration_days
                         FROM keys k
                         JOIN users u ON u.telegram_id = k.telegram_id
                         LEFT JOIN giveaway_claims g ON g.key_id = k.id
                         LEFT JOIN giveaway_campaigns c ON c.code = g.campaign_code
                        WHERE CAST(k.id AS TEXT) = ? AND k.server_id = ?""",
                    (local_ref, server_id),
                ).fetchone()
        if row is None:
            self._managed_repair_manual(job, "managed entitlement no longer exists", current)
            return False
        row = dict(row)
        if str(row.get("outline_key_id") or "") != source_id:
            # Another repair or an operator reconciliation already converged it.
            self._managed_repair_manual(job, "managed entitlement changed before repair", current)
            return False
        if str(row.get("status") or "") != "active" or (
            kind == "paid" and str(row.get("subscription_status") or "") != "active"
        ):
            self._managed_repair_manual(job, "managed entitlement is no longer active", current)
            return False
        try:
            expires_at = datetime.fromisoformat(str(row["expires_at"])).astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            self._managed_repair_manual(job, "managed entitlement expiry is invalid", current)
            return False
        if expires_at <= current:
            self._managed_repair_manual(job, "managed entitlement expired before repair", current)
            return False

        outline = self._outline_client(server_id)
        # Inventory can lag a concurrent manual repair.  Re-read the exact key
        # before creating anything; existence is convergence, not a reason to
        # issue a second credential.
        getter = getattr(outline, "get_key", None)
        if callable(getter):
            existing = getter(source_id)
            if existing is not None:
                self._mark_managed_repair_converged(job, current, existing)
                return True
        usage: int | None = None
        try:
            metrics = outline.transfer_metrics()
            by_key = metrics.get("bytesTransferredByUserId", {}) if isinstance(metrics, dict) else {}
            if isinstance(by_key, dict) and source_id in by_key:
                usage = max(0, int(by_key.get(source_id) or 0))
        except Exception:
            usage = None
        if usage is None and self._managed_repair_cached_usage_is_recent(row, current):
            try:
                usage = max(0, int(row.get("last_usage_bytes")))
            except (TypeError, ValueError):
                usage = None
        owner_usage_override = str(job.get("last_error") or "") == "owner_approved_unknown_usage"
        if usage is None and owner_usage_override:
            usage = 0
        if usage is None and os.environ.get(
            "AURIX_KEY_REPAIR_ALLOW_STALE_USAGE", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}:
            try:
                usage = max(0, int(job.get("used_bytes"))) if job.get("used_bytes") is not None else None
            except (TypeError, ValueError):
                usage = None
        if usage is None and not self._managed_repair_allow_unknown_usage():
            self._managed_repair_manual(job, "usage_observation_required", current)
            return False
        quota = int(row.get("quota_bytes") or row.get("data_limit_bytes") or job["quota_bytes"])
        if usage is None:
            usage = 0
        if usage >= quota:
            self._managed_repair_manual(job, "quota_already_exhausted", current)
            return False
        remaining = quota - usage
        name = str(job.get("key_name") or "").strip()[:128]
        key, _created_remote = self._repair_key_idempotent(
            outline,
            key_id=str(job["target_external_id"]),
            name=name,
            limit_bytes=remaining,
        )
        remote_id = str(key.get("id") or "").strip()
        access_url = str(key.get("accessUrl") or "")
        if not remote_id or not access_url:
            raise CommerceError("Outline repair response lacks id or accessUrl")
        outline.set_data_limit(remote_id, remaining)
        encrypted = self._encrypt_access_url(access_url)
        now_text = _now_text(current)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            current_job = connection.execute(
                "SELECT status FROM managed_key_repair_jobs WHERE id = ?",
                (str(job["id"]),),
            ).fetchone()
            if current_job is None or str(current_job["status"]) != "running":
                return False
            if kind == "paid":
                changed = connection.execute(
                    """UPDATE paid_vpn_keys
                          SET outline_key_id = ?, access_url = ?, quota_bytes = ?,
                              last_usage_bytes = 0, last_usage_observed_at = ?, quota_reason = NULL
                        WHERE CAST(id AS TEXT) = ? AND server_id = ?
                          AND outline_key_id = ? AND status = 'active'""",
                    (remote_id, encrypted, remaining, now_text, local_ref, server_id, source_id),
                )
                if int(getattr(changed, "rowcount", 0) or 0) != 1:
                    raise CommerceError("Paid entitlement changed before repair cutover")
                subscription_id = str(row["subscription_id"])
                profile_kind = "paid"
            else:
                changed = connection.execute(
                    """UPDATE keys
                          SET outline_key_id = ?, data_limit_bytes = ?,
                              last_usage_bytes = 0, quota_reason = NULL
                        WHERE CAST(id AS TEXT) = ? AND server_id = ?
                          AND outline_key_id = ? AND status = 'active'""",
                    (remote_id, remaining, local_ref, server_id, source_id),
                )
                if int(getattr(changed, "rowcount", 0) or 0) != 1:
                    raise CommerceError("Free entitlement changed before repair cutover")
                subscription_id = None
                profile_kind = (
                    "promo" if row.get("campaign_code")
                    else "trial" if str(row.get("key_type") or "") == "monthly_trial"
                    else "free"
                )
                if self._table_exists(connection, "free_provisioning_intents"):
                    connection.execute(
                        """UPDATE free_provisioning_intents
                              SET outline_key_id = ?
                            WHERE key_id = ? AND server_id = ?""",
                        (remote_id, int(row["id"]), server_id),
                    )
            ConnectivityRegistry.revoke_credential(
                connection,
                server_id=server_id,
                external_id=source_id,
                now_text=now_text,
            )
            ConnectivityRegistry.bind_credential(
                connection,
                telegram_id=int(row["telegram_id"]),
                server_id=server_id,
                external_id=remote_id,
                secret_ciphertext=encrypted,
                now_text=now_text,
                profile_kind=profile_kind,
                subscription_id=subscription_id,
            )
            if self._table_exists(connection, "outline_remote_keys"):
                connection.execute(
                    """UPDATE outline_remote_keys
                          SET status = 'missing', managed = 1,
                              missing_observation_count = 0,
                              missing_since_at = NULL, last_missing_at = NULL
                        WHERE server_id = ? AND outline_key_id = ?""",
                    (server_id, source_id),
                )
                connection.execute(
                    """INSERT INTO outline_remote_keys
                       (server_id, outline_key_id, remote_name, managed, status,
                        first_seen_at, last_seen_at, last_usage_bytes,
                        missing_observation_count, missing_since_at, last_missing_at)
                       VALUES (?, ?, ?, 1, 'present', ?, ?, 0, 0, NULL, NULL)
                       ON CONFLICT(server_id, outline_key_id) DO UPDATE SET
                         remote_name = excluded.remote_name, managed = 1,
                         status = 'present', last_seen_at = excluded.last_seen_at,
                         last_usage_bytes = excluded.last_usage_bytes,
                         missing_observation_count = 0,
                         missing_since_at = NULL, last_missing_at = NULL""",
                    (server_id, remote_id, name, now_text, now_text),
                )
            connection.execute(
                """UPDATE managed_key_repair_jobs
                      SET source_external_id = ?, target_external_id = ?,
                          quota_bytes = ?, used_bytes = ?, status = 'done',
                          locked_at = NULL, last_error = NULL, completed_at = ?
                    WHERE id = ? AND status = 'running'""",
                (remote_id, remote_id, remaining, usage, now_text, str(job["id"])),
            )
            notification_key = f"managed-key-repaired:{kind}:{local_ref}"
            connection.execute(
                """INSERT INTO notifications
                   (id, dedupe_key, telegram_id, kind, text, access_url_ciphertext,
                    status, next_attempt_at, created_at)
                   VALUES (?, ?, ?, 'vpn_key_repaired', ?, ?, 'pending', ?, ?)
                   ON CONFLICT(dedupe_key) DO NOTHING""",
                (
                    _new_id(), notification_key, int(row["telegram_id"]),
                    "Your AuriX VPN key was safely refreshed after the previous remote credential disappeared.\n"
                    f"Remaining quota preserved: {_human_bytes(remaining)}\n"
                    f"Expires: {row['expires_at']}\n"
                    "The previous key is no longer active in AuriX.",
                    encrypted,
                    now_text,
                    now_text,
                ),
            )
            self._audit(
                connection,
                "managed_key_repaired",
                "managed_key",
                f"{server_id}:{kind}:{local_ref}",
                "system",
                None,
                {
                    "old_external_id": source_id,
                    "new_external_id": remote_id,
                    "observed_usage_bytes": usage,
                    "previous_quota_bytes": quota,
                    "remaining_quota_bytes": remaining,
                    "expires_at": str(row["expires_at"]),
                },
            )
        try:
            self._sync_identity_binding(
                telegram_id=int(row["telegram_id"]),
                kind=profile_kind,
                quota_bytes=int(remaining),
                expires_at=str(row["expires_at"]),
                server_id=server_id,
                external_id=remote_id,
                subscription_id=subscription_id,
                local_key_ref=local_ref if profile_kind != "paid" else None,
            )
        except Exception as exc:
            print(f"identity repair sync error: {type(exc).__name__}", file=sys.stderr)
        return True

    def _mark_managed_repair_converged(
        self, job: dict[str, Any], now: datetime, key: dict[str, Any]
    ) -> None:
        """Close a repair when another worker already restored the exact key."""
        now_text = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE managed_key_repair_jobs
                      SET status = 'done', locked_at = NULL, last_error = NULL,
                          completed_at = ?
                    WHERE id = ? AND status = 'running'""",
                (now_text, str(job["id"])),
            )
            if self._table_exists(connection, "outline_remote_keys"):
                connection.execute(
                    """UPDATE outline_remote_keys
                          SET status = 'present', managed = 1,
                              remote_name = COALESCE(?, remote_name),
                              missing_observation_count = 0,
                              missing_since_at = NULL, last_missing_at = NULL,
                              last_seen_at = ?
                        WHERE server_id = ? AND outline_key_id = ?""",
                    (str(key.get("name") or "")[:256] or None, now_text,
                     str(job["server_id"]), str(key.get("id") or job["source_external_id"])),
                )

    def process_managed_key_repairs(
        self, now: datetime | None = None, max_jobs: int = 5
    ) -> int:
        """Run a bounded, restart-safe queue for missing active credentials."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        processed = 0
        while processed < max(1, int(max_jobs)):
            job = self._claim_managed_key_repair(current)
            if job is None:
                break
            try:
                self._process_managed_key_repair(job, current)
            except Exception as exc:
                self._managed_repair_failed(str(job["id"]), exc, current)
                print(f"managed key repair error: {type(exc).__name__}", file=sys.stderr)
            processed += 1
        return processed

    def _claim_endpoint_migration(self, now: datetime) -> dict[str, Any] | None:
        """Lease one durable credential migration for the maintenance worker."""
        now_text = _now_text(now)
        stale_before = _now_text(now - timedelta(minutes=10))
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE connectivity_migration_jobs
                      SET status = 'pending', locked_at = NULL,
                          updated_at = ?
                    WHERE status = 'creating' AND locked_at < ?""",
                (now_text, stale_before),
            )
            lock_clause = (
                " FOR UPDATE SKIP LOCKED" if isinstance(connection, _PostgresConnection) else ""
            )
            row = connection.execute(
                """SELECT * FROM connectivity_migration_jobs
                    WHERE status IN ('pending', 'failed', 'source_delete_pending') AND next_attempt_at <= ?
                    ORDER BY created_at LIMIT ?""" + lock_clause,
                (now_text, 1),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """UPDATE connectivity_migration_jobs
                      SET status = 'creating', attempts = attempts + 1,
                          locked_at = ?, updated_at = ?
                    WHERE id = ? AND status IN ('pending', 'failed', 'source_delete_pending')""",
                (now_text, now_text, str(row["id"])),
            )
            result = dict(row)
            result["attempts"] = int(row["attempts"] or 0) + 1
            result["migration_phase"] = str(row["status"])
            result["status"] = "creating"
            return result

    def _endpoint_migration_failed(
        self, job_id: str, error: Exception, now: datetime, *, terminal: bool = False
    ) -> None:
        safe_error = f"{type(error).__name__}: {str(error)[:500]}"
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                "SELECT attempts FROM connectivity_migration_jobs WHERE id = ?",
                (str(job_id),),
            ).fetchone()
            attempts = int(row["attempts"] or 0) if row else 0
            done = bool(terminal or attempts >= 8)
            connection.execute(
                """UPDATE connectivity_migration_jobs
                      SET status = ?, next_attempt_at = ?, locked_at = NULL,
                          last_error = ?, updated_at = ?
                    WHERE id = ?""",
                (
                    "failed" if done else "pending",
                    "9999-12-31T00:00:00+00:00"
                    if done
                    else _now_text(current + timedelta(minutes=1)),
                    safe_error,
                    _now_text(current),
                    str(job_id),
                ),
            )

    def _endpoint_migration_completed(self, job_id: str, now: datetime) -> None:
        now_text = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE connectivity_migration_jobs
                      SET status = 'completed', locked_at = NULL,
                          last_error = NULL, completed_at = ?, updated_at = ?
                    WHERE id = ?""",
                (now_text, now_text, str(job_id)),
            )

    @staticmethod
    def _metric_for_key(metrics: Any, key_id: str) -> int | None:
        by_key = metrics.get("bytesTransferredByUserId", {}) if isinstance(metrics, dict) else {}
        if not isinstance(by_key, dict) or key_id not in by_key:
            return None
        try:
            return max(0, int(by_key.get(key_id) or 0))
        except (TypeError, ValueError):
            return None

    def _create_migration_key(
        self, outline: Any, key_id: str, name: str, limit_bytes: int
    ) -> dict[str, Any]:
        """Create one replacement key with timeout-safe deterministic recovery."""
        getter = getattr(outline, "get_key", None)
        existing = None
        if callable(getter):
            try:
                existing = getter(key_id)
            except Exception:
                existing = None
        if existing is not None:
            return existing
        creator = getattr(outline, "create_key_with_id", None)
        if callable(creator):
            try:
                created = creator(key_id, name, limit_bytes)
            except Exception as exc:
                recovered = None
                if callable(getter):
                    try:
                        recovered = getter(key_id)
                    except Exception:
                        recovered = None
                if recovered is not None:
                    return recovered
                if getattr(exc, "status", None) not in (404, 405, 501):
                    raise
                created = None
            if created is not None:
                return created
        # POST-only adapters are rare and cannot choose the external id.  A
        # name lookup is the only safe recovery after an ambiguous response.
        existing_by_name = self._find_key(name, outline)
        if existing_by_name is not None:
            return existing_by_name
        created = outline.create_key(name, limit_bytes)
        if not isinstance(created, dict) or not created.get("id") or not created.get("accessUrl"):
            raise CommerceError("Replacement Outline key response lacks id or accessUrl")
        return created

    def _mark_migration_source_delete_retry(self, job_id: str, error: Exception, now: datetime) -> None:
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE connectivity_migration_jobs
                      SET status = 'source_delete_pending', locked_at = NULL,
                          next_attempt_at = ?, last_error = ?, updated_at = ?
                    WHERE id = ?""",
                (
                    _now_text((now or datetime.now(UTC)).astimezone(UTC) + timedelta(minutes=1)),
                    f"{type(error).__name__}: {str(error)[:500]}",
                    _now_text(now),
                    str(job_id),
                ),
            )

    def _delete_migration_source(self, job: dict[str, Any], now: datetime) -> bool:
        source = self._outline_client(str(job["source_server_id"]))
        try:
            source.delete_key(str(job["source_external_id"]))
            getter = getattr(source, "get_key", None)
            if callable(getter) and getter(str(job["source_external_id"])) is not None:
                raise CommerceError("Source Outline key still exists after migration delete")
        except Exception as exc:
            self._mark_migration_source_delete_retry(str(job["id"]), exc, now)
            return False
        self._endpoint_migration_completed(str(job["id"]), now)
        return True

    def _process_endpoint_migration(self, job: dict[str, Any], now: datetime) -> None:
        if str(job.get("migration_phase") or job.get("status")) == "source_delete_pending":
            self._delete_migration_source(job, now)
            return
        current = (now or datetime.now(UTC)).astimezone(UTC)
        try:
            expires_at = datetime.fromisoformat(str(job["expires_at"])).astimezone(UTC)
        except (TypeError, ValueError) as exc:
            raise CommerceError("Migration expiry is invalid") from exc
        if expires_at <= current:
            raise CommerceError("Migration entitlement expired before replacement creation")
        source = self._outline_client(str(job["source_server_id"]))
        target = self._outline_client(str(job["target_server_id"]))
        metrics = source.transfer_metrics()
        used = self._metric_for_key(metrics, str(job["source_external_id"]))
        if used is None:
            raise CommerceError("Fresh source usage is unavailable; migration is safely deferred")
        quota = int(job["quota_bytes"] or 0)
        remaining = quota - used
        if remaining <= 0:
            raise CommerceError("Source credential has no remaining quota")
        target_key = self._create_migration_key(
            target,
            str(job["target_external_id"]),
            str(job["target_name"]),
            remaining,
        )
        target_id = str(target_key.get("id") or "")
        access_url = str(target_key.get("accessUrl") or "")
        if not target_id or not access_url:
            raise CommerceError("Replacement Outline key response lacks id or accessUrl")
        target.set_data_limit(target_id, remaining)
        encrypted = self._encrypt_access_url(access_url)
        now_text = _now_text(current)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            current_job = connection.execute(
                "SELECT status FROM connectivity_migration_jobs WHERE id = ?",
                (str(job["id"]),),
            ).fetchone()
            if current_job is None or str(current_job["status"]) != "creating":
                return
            kind = str(job["profile_kind"])
            profile_row = connection.execute(
                "SELECT subscription_id FROM connectivity_profiles WHERE profile_id = ?",
                (str(job["profile_id"]),),
            ).fetchone()
            subscription_id = profile_row["subscription_id"] if profile_row else None
            if kind == "paid":
                changed = connection.execute(
                    """UPDATE paid_vpn_keys SET server_id = ?, outline_key_id = ?,
                              access_url = ?, quota_bytes = ?
                         WHERE subscription_id = ? AND server_id = ?
                           AND outline_key_id = ? AND status = 'active'""",
                    (
                        str(job["target_server_id"]), target_id, encrypted, remaining,
                        subscription_id, str(job["source_server_id"]),
                        str(job["source_external_id"]),
                    ),
                )
                if int(getattr(changed, "rowcount", 0) or 0) != 1:
                    # Older deployments may not use profile_id as the
                    # subscription id; resolve it through the registry row.
                    changed = connection.execute(
                        """UPDATE paid_vpn_keys SET server_id = ?, outline_key_id = ?,
                                  access_url = ?, quota_bytes = ?
                             WHERE subscription_id = ? AND server_id = ?
                               AND outline_key_id = ? AND status = 'active'""",
                        (
                            str(job["target_server_id"]), target_id, encrypted, remaining,
                            subscription_id, str(job["source_server_id"]),
                            str(job["source_external_id"]),
                        ),
                    )
                if int(getattr(changed, "rowcount", 0) or 0) != 1:
                    raise CommerceError("Paid entitlement changed before migration cutover")
            else:
                changed = connection.execute(
                    """UPDATE keys SET server_id = ?, outline_key_id = ?, data_limit_bytes = ?
                         WHERE server_id = ? AND outline_key_id = ?
                           AND status IN ('active', 'revoke_failed')""",
                    (
                        str(job["target_server_id"]), target_id, remaining,
                        str(job["source_server_id"]), str(job["source_external_id"]),
                    ),
                ) if self._table_exists(connection, "keys") else None
                if changed is None or int(getattr(changed, "rowcount", 0) or 0) != 1:
                    raise CommerceError("Free entitlement changed before migration cutover")
            ConnectivityRegistry.revoke_credential(
                connection,
                server_id=str(job["source_server_id"]),
                external_id=str(job["source_external_id"]),
                now_text=now_text,
            )
            ConnectivityRegistry.bind_credential(
                connection,
                telegram_id=int(job["telegram_id"]),
                server_id=str(job["target_server_id"]),
                external_id=target_id,
                secret_ciphertext=encrypted,
                now_text=now_text,
                profile_kind=kind,
                subscription_id=(str(subscription_id) if kind == "paid" and subscription_id else None),
            )
            connection.execute(
                """INSERT INTO notifications
                   (id, dedupe_key, telegram_id, kind, text, access_url_ciphertext,
                    status, next_attempt_at, created_at)
                   VALUES (?, ?, ?, 'vpn_migrated', ?, ?, 'pending', ?, ?)
                   ON CONFLICT(dedupe_key) DO NOTHING""",
                (
                    _new_id(), f"endpoint-migration:{job['id']}", int(job["telegram_id"]),
                    "Your AuriX VPN access was moved to a healthier endpoint.\n"
                    f"Remaining quota: {remaining} bytes\nExpires: {str(job['expires_at'])}",
                    encrypted, now_text, now_text,
                ),
            )
            self._audit(
                connection,
                "endpoint_migration_cutover",
                "connectivity_migration",
                str(job["id"]),
                "system",
                None,
                {
                    "source_server_id": str(job["source_server_id"]),
                    "target_server_id": str(job["target_server_id"]),
                    "source_external_id": str(job["source_external_id"]),
                    "target_external_id": target_id,
                    "source_used_bytes": used,
                    "remaining_quota_bytes": remaining,
                },
            )
            connection.execute(
                """UPDATE connectivity_migration_jobs
                      SET source_used_bytes = ?, target_external_id = ?,
                          target_access_url_ciphertext = ?, status = 'source_delete_pending',
                          locked_at = NULL, last_error = NULL, next_attempt_at = ?, updated_at = ?
                    WHERE id = ?""",
                (used, target_id, encrypted, now_text, now_text, str(job["id"])),
            )
        self._delete_migration_source(
            {**job, "target_external_id": target_id}, current
        )
        try:
            self._sync_identity_binding(
                telegram_id=int(job["telegram_id"]),
                kind=str(job["profile_kind"]),
                quota_bytes=int(remaining),
                expires_at=str(job["expires_at"]),
                server_id=str(job["target_server_id"]),
                external_id=target_id,
                subscription_id=str(subscription_id) if subscription_id else None,
            )
        except Exception as exc:
            print(f"identity migration sync error: {type(exc).__name__}", file=sys.stderr)

    def process_endpoint_migrations(self, now: datetime | None = None, max_jobs: int = 5) -> int:
        """Run bounded replacement-key migrations after durable owner intent."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        try:
            with self.database.connect() as connection:
                if not self._table_exists(connection, "connectivity_migration_jobs"):
                    return 0
        except Exception:
            return 0
        processed = 0
        while processed < max(1, int(max_jobs)):
            job = self._claim_endpoint_migration(current)
            if job is None:
                break
            try:
                self._process_endpoint_migration(job, current)
            except Exception as exc:
                self._endpoint_migration_failed(str(job["id"]), exc, current)
                print(f"endpoint migration error: {type(exc).__name__}", file=sys.stderr)
            processed += 1
        return processed

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
                        local = connection.execute(
                            "SELECT id, telegram_id, server_id, data_limit_bytes, expires_at FROM keys WHERE outline_key_id = ?",
                            (str(item["id"]),),
                        ).fetchone()
                        if local is not None:
                            connection.execute(
                                "UPDATE keys SET status = 'revoked' WHERE id = ?", (local["id"],)
                            )
                            ConnectivityRegistry.revoke_credential(
                                connection,
                                server_id=str(local["server_id"]),
                                external_id=str(item["id"]),
                                now_text=_now_text(),
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
                            "SELECT id, telegram_id, server_id, data_limit_bytes, expires_at FROM keys WHERE outline_key_id = ?",
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
                connection.execute(
                    """UPDATE paid_vpn_keys SET status = 'revoked', revoked_at = ?
                       WHERE id = ?""",
                    (_now_text(now), key["id"]),
                )
                ConnectivityRegistry.revoke_credential(
                    connection,
                    server_id=str(server_id),
                    external_id=str(key["outline_key_id"]),
                    now_text=_now_text(now),
                )
                connection.execute(
                    "UPDATE provisioning_jobs SET status = 'done', locked_at = NULL WHERE id = ?",
                    (job["id"],),
                )
                quota_reason = key["quota_reason"] if "quota_reason" in key.keys() else None
                quota_event = connection.execute(
                    """SELECT observed_bytes, quota_bytes, observed_at FROM quota_events
                       WHERE subscription_id = ? AND reason IN ('quota', 'aggregate_quota')
                       ORDER BY observed_at DESC LIMIT 1""",
                    (job["subscription_id"],),
                ).fetchone()
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

    @staticmethod
    def _required_scale_observations() -> int:
        """Return the minimum independent scale observations required to queue."""
        try:
            return max(2, min(10, int(os.environ.get("AURIX_SCALE_REQUIRED_OBSERVATIONS", "2"))))
        except (TypeError, ValueError):
            return 2

    @staticmethod
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

    def capacity_snapshot(
        self, now: datetime | None = None, *, refresh_inventory: bool = True
    ) -> dict[str, Any]:
        """Return declared capacity beside observed remote inventory/telemetry."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        expiring_at = _now_text(current + timedelta(hours=24))
        if refresh_inventory:
            try:
                self.refresh_server_inventory(current)
            except Exception:
                pass
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
                """SELECT outline_key_id, telegram_id, quota_bytes, server_id
                   FROM paid_vpn_keys WHERE status = 'active'"""
            ).fetchall()
            server_rows = connection.execute(
                "SELECT * FROM outline_servers ORDER BY enabled DESC, label, server_id"
            ).fetchall()
            registry_by_server = {
                str(item["outline_server_id"]): item
                for item in ConnectivityRegistry.endpoint_snapshot(connection)
            }
            allocation_rows = connection.execute(
                """SELECT a.server_id, a.plan_code, a.slot_limit, p.name,
                          (SELECT COUNT(*) FROM subscriptions s
                            WHERE s.server_id = a.server_id AND s.plan_code = a.plan_code
                              AND s.status IN ('pending', 'active')) AS active_count,
                          (SELECT COUNT(*) FROM orders o
                            WHERE o.server_id = a.server_id AND o.plan_code = a.plan_code
                              AND (o.status = 'payment_submitted' OR
                                   (o.status = 'awaiting_payment' AND o.capacity_reserved_until > ?))) AS reserved_count
                   FROM server_plan_allocations a JOIN plans p ON p.code = a.plan_code
                   ORDER BY a.server_id, p.price_minor""",
                (_now_text(current),),
            ).fetchall()
            tier_allocation_rows = connection.execute(
                """SELECT server_id, tier_code, slot_limit
                   FROM server_tier_allocations ORDER BY server_id, tier_code"""
            ).fetchall()
            free_key_rows = (
                connection.execute(
                    """SELECT k.server_id, k.key_type,
                              CASE WHEN g.key_id IS NULL THEN 0 ELSE 1 END AS is_promo
                       FROM keys k LEFT JOIN giveaway_claims g ON g.key_id = k.id
                       WHERE k.status IN ('active', 'revoke_failed')"""
                ).fetchall()
                if self._table_exists(connection, "keys")
                else []
            )
        default_server_id = getattr(self.outline, "default_server_id", None)
        metrics_by_server = (
            dict(getattr(self, "_server_metrics_cache", {}))
            if server_rows
            else self._metrics_by_server()
        )
        usage = []
        for row in key_rows:
            server_id = row["server_id"] or default_server_id
            by_key = metrics_by_server.get(server_id, {})
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
                    "server_id": server_id,
                }
            )
        allocations_by_server: dict[str, list[dict[str, Any]]] = {}
        for row in allocation_rows:
            item = dict(row)
            item["remaining_slots"] = max(
                0,
                int(item["slot_limit"]) - int(item["active_count"]) - int(item["reserved_count"]),
            )
            allocations_by_server.setdefault(str(item["server_id"]), []).append(item)
        free_counts: dict[tuple[str, str], int] = {}
        for row in free_key_rows:
            tier_code = (
                "PROMO"
                if int(row["is_promo"])
                else "FREE300MB"
                if row["key_type"] == "daily_free"
                else "FREE3GB"
            )
            identity = (str(row["server_id"] or default_server_id), tier_code)
            free_counts[identity] = free_counts.get(identity, 0) + 1
        tier_allocations_by_server: dict[str, list[dict[str, Any]]] = {}
        for row in tier_allocation_rows:
            item = dict(row)
            active_count = free_counts.get((str(item["server_id"]), str(item["tier_code"])), 0)
            item["active_count"] = active_count
            item["remaining_slots"] = max(0, int(item["slot_limit"]) - active_count)
            tier_allocations_by_server.setdefault(str(item["server_id"]), []).append(item)
        servers = []
        strict_allocations = os.environ.get(
            "AURIX_FLEET_STRICT_ALLOCATION_VALIDATION", ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        for row in server_rows:
            item = dict(row)
            registry = registry_by_server.get(str(item["server_id"]))
            if registry:
                item["connectivity"] = {
                    "endpoint_id": registry["endpoint_id"],
                    "provider_id": registry["provider_id"],
                    "provider_name": registry["provider_name"],
                    "region_id": registry["region_id"],
                    "region_name": registry["region_name"],
                    "transport_id": registry["transport_id"],
                    "protocol": registry["protocol"],
                    "transport_name": registry["transport_name"],
                    "status": registry["status"],
                    "accepts_new_keys": bool(registry["accepts_new_keys"]),
                    "updated_at": registry["updated_at"],
                }
            max_keys = item.get("max_keys")
            usable = (
                None
                if max_keys is None
                else max(0, int(max_keys) - int(item.get("reserved_keys") or 0))
            )
            remote = int(item.get("remote_key_count") or 0)
            with self.database.connect() as connection:
                reserved_orders = int(
                    connection.execute(
                        """SELECT COUNT(*) AS n FROM orders WHERE server_id = ?
                           AND (status = 'payment_submitted' OR
                                (status = 'awaiting_payment' AND capacity_reserved_until > ?))""",
                        (item["server_id"], _now_text(current)),
                    ).fetchone()["n"]
                )
                pending_keys = int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM subscriptions WHERE server_id = ? AND status = 'pending'",
                        (item["server_id"],),
                    ).fetchone()["n"]
                )
                committed_traffic = int(
                    connection.execute(
                        """SELECT
                           COALESCE((SELECT SUM(COALESCE(quota_bytes, 0)) FROM subscriptions
                             WHERE server_id = ? AND status IN ('pending', 'active')), 0) +
                           COALESCE((SELECT SUM(COALESCE(quota_bytes_snapshot, 0)) FROM orders
                             WHERE server_id = ? AND (status = 'payment_submitted' OR
                               (status = 'awaiting_payment' AND capacity_reserved_until > ?))), 0) AS n""",
                        (item["server_id"], item["server_id"], _now_text(current)),
                    ).fetchone()["n"]
                )
            item["reserved_order_count"] = reserved_orders
            item["pending_key_count"] = pending_keys
            item["committed_traffic_bytes"] = committed_traffic
            # Drain/retirement evidence is kept beside capacity so the owner
            # can see exactly why an endpoint is still unsafe to remove.
            with self.database.connect() as readiness_connection:
                item["active_free_key_count"] = int(
                    readiness_connection.execute(
                        "SELECT COUNT(*) AS n FROM keys WHERE server_id = ? AND status IN ('active', 'revoke_failed')",
                        (item["server_id"],),
                    ).fetchone()["n"]
                ) if self._table_exists(readiness_connection, "keys") else 0
                item["active_paid_key_count"] = int(
                    readiness_connection.execute(
                        "SELECT COUNT(*) AS n FROM paid_vpn_keys WHERE server_id = ? AND status IN ('active', 'revoke_failed')",
                        (item["server_id"],),
                    ).fetchone()["n"]
                )
                item["open_order_count"] = int(
                    readiness_connection.execute(
                        "SELECT COUNT(*) AS n FROM orders WHERE server_id = ? AND status IN ('awaiting_payment', 'payment_submitted')",
                        (item["server_id"],),
                    ).fetchone()["n"]
                )
                item["pending_provisioning_count"] = int(
                    readiness_connection.execute(
                        "SELECT COUNT(*) AS n FROM free_provisioning_intents WHERE server_id = ? AND status IN ('pending', 'running')",
                        (item["server_id"],),
                    ).fetchone()["n"]
                ) if self._table_exists(readiness_connection, "free_provisioning_intents") else 0
            item["drain_ready_to_retire"] = not any(
                (
                    item["active_free_key_count"],
                    item["active_paid_key_count"],
                    item["open_order_count"],
                    item["pending_provisioning_count"],
                    item.get("remote_key_count") is None,
                    int(item.get("remote_key_count") or 0),
                    int(item.get("remote_orphan_key_count") or 0),
                )
            )
            item["remaining_traffic_bytes"] = (
                None
                if item.get("monthly_traffic_bytes") is None
                else max(0, int(item["monthly_traffic_bytes"]) - committed_traffic)
            )
            item["remaining_key_slots"] = (
                None if usable is None else max(0, usable - remote - reserved_orders - pending_keys)
            )
            item["saleable_key_capacity"] = usable
            item["key_demand"] = remote + reserved_orders + pending_keys
            item["key_utilization_percent"] = (
                None
                if usable is None or usable <= 0
                else min(100.0, (item["key_demand"] / usable) * 100.0)
            )
            item["allocations"] = allocations_by_server.get(str(item["server_id"]), [])
            item["tier_allocations"] = tier_allocations_by_server.get(
                str(item["server_id"]), []
            )
            allocation_total = sum(
                int(allocation.get("slot_limit") or 0) for allocation in item["allocations"]
            ) + sum(
                int(allocation.get("slot_limit") or 0)
                for allocation in item["tier_allocations"]
            )
            orphan_count = int(item.get("remote_orphan_key_count") or 0)
            allocation_gap = None if usable is None else int(usable) - allocation_total
            policy_blockers: list[str] = []
            if allocation_gap is not None and allocation_gap < 0:
                policy_blockers.append("overallocated")
            if orphan_count:
                policy_blockers.append("untracked_remote_keys")
            item["allocation_total_slots"] = allocation_total
            item["allocation_remaining_slots"] = allocation_gap
            item["allocation_policy_status"] = (
                "overallocated"
                if allocation_gap is not None and allocation_gap < 0
                else "audit_required"
                if orphan_count
                else "ready"
                if usable is not None
                else "unconfigured"
            )
            item["allocation_policy_blockers"] = policy_blockers
            admission_blockers: list[str] = []
            if not int(item.get("enabled") or 0):
                admission_blockers.append("disabled")
            lifecycle = str(item.get("lifecycle_state") or "active")
            if lifecycle == "draining":
                admission_blockers.append("draining")
            elif lifecycle == "retired":
                admission_blockers.append("retired")
            if str(item.get("health_status") or "") != "healthy":
                admission_blockers.append("unhealthy")
            if item.get("last_synced_at") is None:
                admission_blockers.append("no_inventory")
            else:
                try:
                    synced_at = datetime.fromisoformat(str(item["last_synced_at"])).astimezone(UTC)
                    age_limit = max(
                        30,
                        int(os.environ.get("AURIX_SERVER_HEALTH_MAX_AGE_SECONDS", "900")),
                    )
                    if current - synced_at > timedelta(seconds=age_limit):
                        admission_blockers.append("stale_inventory")
                except (TypeError, ValueError, OverflowError):
                    admission_blockers.append("invalid_inventory_time")
            if item.get("remaining_key_slots") is not None and int(item["remaining_key_slots"]) <= 0:
                admission_blockers.append("key_capacity")
            item["admission_status"] = "blocked" if admission_blockers else "eligible"
            item["admission_blockers"] = admission_blockers
            servers.append(item)
        outline_version = "multi" if len(servers) > 1 else "unknown"
        if not servers:
            try:
                outline_version = str(self.outline.server_info().get("version", "unknown"))[:64]
            except Exception:
                pass
        advice = self._scale_advice(servers)
        advice.update(self._record_scale_observation(current, servers, advice))
        return {
            **dict(counts),
            "outline_version": outline_version,
            "usage": usage,
            "servers": servers,
            "strict_allocation_validation": strict_allocations,
            "scale_advice": advice,
        }

    @staticmethod
    def _scale_advice(servers: list[dict[str, Any]]) -> dict[str, Any]:
        """Return a non-mutating fleet posture from declared saleable capacity."""

        def threshold(name: str, default: int) -> int:
            try:
                return max(1, min(100, int(os.environ.get(name, str(default)))))
            except (TypeError, ValueError):
                return default

        declared = [
            item
            for item in servers
            if item.get("enabled") and item.get("saleable_key_capacity") is not None
        ]
        configured = [
            item
            for item in declared
            if str(item.get("lifecycle_state") or "active") == "active"
        ]
        healthy = [item for item in configured if item.get("health_status") == "healthy"]
        if not configured:
            if declared:
                return {
                    "status": "blocked",
                    "utilization_percent": None,
                    "remaining_slots": 0,
                    "message": "All declared endpoints are draining or retired; no new allocation is allowed.",
                }
            return {
                "status": "unconfigured",
                "utilization_percent": None,
                "remaining_slots": None,
                "message": "Declare key capacity before making a scaling decision.",
            }
        if not healthy:
            return {
                "status": "blocked",
                "utilization_percent": None,
                "remaining_slots": 0,
                "message": "No healthy declared server can accept new keys.",
            }
        total_capacity = sum(max(0, int(item["saleable_key_capacity"])) for item in healthy)
        total_demand = sum(max(0, int(item.get("key_demand") or 0)) for item in healthy)
        remaining = max(0, total_capacity - total_demand)
        utilization = (
            100.0 if total_capacity <= 0 else min(100.0, total_demand / total_capacity * 100)
        )
        traffic_ratios = [
            min(
                100.0,
                max(0, int(item.get("committed_traffic_bytes") or 0))
                / max(1, int(item["monthly_traffic_bytes"]))
                * 100,
            )
            for item in healthy
            if item.get("monthly_traffic_bytes") is not None
        ]
        traffic_utilization = max(traffic_ratios, default=None)
        prepare_at = threshold("AURIX_SCALE_PREPARE_UTILIZATION_PERCENT", 75)
        urgent_at = max(
            prepare_at,
            threshold("AURIX_SCALE_URGENT_UTILIZATION_PERCENT", 90),
        )
        traffic_prepare_at = threshold("AURIX_SCALE_PREPARE_TRAFFIC_PERCENT", prepare_at)
        traffic_urgent_at = max(
            traffic_prepare_at,
            threshold("AURIX_SCALE_URGENT_TRAFFIC_PERCENT", urgent_at),
        )
        urgent_traffic = traffic_utilization is not None and traffic_utilization >= traffic_urgent_at
        prepare_traffic = traffic_utilization is not None and traffic_utilization >= traffic_prepare_at
        if utilization >= urgent_at or urgent_traffic or remaining <= 1:
            status = "urgent"
            message = "Add and verify another Outline node before accepting more demand."
        elif utilization >= prepare_at or prepare_traffic:
            status = "prepare"
            message = "Prepare and verify the next Outline node now."
        else:
            status = "stable"
            message = "Current declared fleet headroom is sufficient."
        return {
            "status": status,
            "utilization_percent": round(utilization, 1),
            "remaining_slots": remaining,
            "saleable_capacity": total_capacity,
            "traffic_utilization_percent": (
                None if traffic_utilization is None else round(traffic_utilization, 1)
            ),
            "message": message,
        }

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
        return self._hydrate_notifications(rows)

    def claim_pending_notifications(
        self,
        now: datetime | None = None,
        limit: int = 20,
        lease_seconds: int = 120,
    ) -> list[dict[str, Any]]:
        """Claim due notifications with crash recovery through a short lease.

        ``next_attempt_at`` doubles as a lease because notification rows
        predate a dedicated worker-lock column. A second worker cannot select a
        leased row, while a process that dies before marking it sent exposes the
        row again after the lease. Delivery remains at-least-once, but normal
        concurrent workers no longer duplicate the same notification.
        """

        current = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = _now_text(current)
        lease_until = _now_text(current + timedelta(seconds=max(30, int(lease_seconds))))
        page_limit = max(1, min(int(limit), 100))
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            lock_clause = (
                " FOR UPDATE SKIP LOCKED" if isinstance(connection, _PostgresConnection) else ""
            )
            rows = connection.execute(
                """SELECT * FROM notifications
                   WHERE status IN ('pending', 'failed')
                     AND dead_lettered_at IS NULL
                     AND next_attempt_at <= ?
                   ORDER BY created_at LIMIT ?"""
                + lock_clause,
                (now_text, page_limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """UPDATE notifications SET next_attempt_at = ?
                       WHERE id = ? AND status IN ('pending', 'failed')
                         AND dead_lettered_at IS NULL AND next_attempt_at <= ?""",
                    (lease_until, row["id"], now_text),
                )
        return self._hydrate_notifications(rows)

    def _hydrate_notifications(self, rows: Any) -> list[dict[str, Any]]:
        notifications = []
        for row in rows:
            notification = dict(row)
            access_url = self._decrypt_access_url(notification.get("access_url_ciphertext"))
            if access_url:
                notification["text"] += f"\n\nYour Outline key:\n{access_url}"
                notification["access_url"] = access_url
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
