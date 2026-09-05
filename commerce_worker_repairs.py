"""Managed credential repair operations."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from typing import Any
from commerce_repositories import _PostgresConnection
from connectivity_registry import ConnectivityRegistry
from identity import IdentityError
from identity import IdentityService
from commerce_models import (
    JOB_RETRY_DELAY,
    UTC,
    CommerceError,
    _human_bytes,
    _new_id,
    _now_text,
)
from commerce_repairs_repository import ManagedRepairRepository


_REPAIRS = ManagedRepairRepository()


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
                row = _REPAIRS.free_key_ref(connection, str(server_id), str(external_id))
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
        credential = _REPAIRS.active_credential(connection, str(server_id), str(external_id))
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
        _REPAIRS.reset_stale_repairs(connection, stale_before)
        row, updated_ok = _REPAIRS.claim_repair(
            connection, max_attempts=self._managed_repair_max_attempts(), now_text=now_text
        )
        if row is None:
            return None
        if not updated_ok:
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
        row = _REPAIRS.repair_attempts(connection, str(job_id))
        attempts = int(row["attempts"] or 0) if row else self._managed_repair_max_attempts()
        terminal = attempts >= self._managed_repair_max_attempts()
        _REPAIRS.update_failed(
            connection,
            job_id=str(job_id),
            status="manual" if terminal else "failed",
            next_attempt_at=(
                _now_text(current + JOB_RETRY_DELAY)
                if not terminal else "9999-12-31T00:00:00+00:00"
            ),
            error=safe_error,
        )

def _managed_repair_manual(self, job: dict[str, Any], reason: str, now: datetime) -> None:
    """Escalate an unsafe repair without mutating local entitlement state."""
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        if _REPAIRS.mark_manual(connection, str(job["id"]), str(reason)[:500]):
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
        row = _REPAIRS.repair_entitlement(
            connection, kind=kind, local_ref=local_ref, server_id=server_id
        )
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
        current_job = _REPAIRS.current_repair(connection, str(job["id"]))
        if current_job is None or str(current_job["status"]) != "running":
            return False
        if kind == "paid":
            changed = _REPAIRS.update_paid(
                connection,
                (remote_id, encrypted, remaining, now_text, local_ref, server_id, source_id),
            )
            if not changed:
                raise CommerceError("Paid entitlement changed before repair cutover")
            subscription_id = str(row["subscription_id"])
            profile_kind = "paid"
        else:
            changed = _REPAIRS.update_free(
                connection,
                (remote_id, remaining, local_ref, server_id, source_id),
            )
            if not changed:
                raise CommerceError("Free entitlement changed before repair cutover")
            subscription_id = None
            profile_kind = (
                "promo" if row.get("campaign_code")
                else "trial" if str(row.get("key_type") or "") == "monthly_trial"
                else "free"
            )
            if self._table_exists(connection, "free_provisioning_intents"):
                _REPAIRS.update_free_intent(
                    connection, remote_id, int(row["id"]), server_id
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
            _REPAIRS.mark_remote_missing(connection, server_id, source_id)
            _REPAIRS.upsert_remote_present(connection, server_id, remote_id, name, now_text)
        _REPAIRS.complete_repair(
            connection, str(job["id"]), remote_id, remaining, usage, now_text
        )
        notification_key = f"managed-key-repaired:{kind}:{local_ref}"
        _REPAIRS.queue_repaired_notification(
            connection,
            notification_id=_new_id(),
            dedupe_key=notification_key,
            telegram_id=int(row["telegram_id"]),
            text=(
                "Your AuriX VPN key was safely refreshed after the previous remote credential disappeared.\n"
                f"Remaining quota preserved: {_human_bytes(remaining)}\n"
                f"Expires: {row['expires_at']}\n"
                "The previous key is no longer active in AuriX."
            ),
            encrypted=encrypted,
            now_text=now_text,
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
        _REPAIRS.mark_converged(connection, str(job["id"]), now_text)
        if self._table_exists(connection, "outline_remote_keys"):
            _REPAIRS.mark_remote_present(
                connection,
                name=str(key.get("name") or "")[:256] or None,
                now_text=now_text,
                server_id=str(job["server_id"]),
                external_id=str(key.get("id") or job["source_external_id"]),
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
