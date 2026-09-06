"""Endpoint migration operations."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from typing import Any
from connectivity_registry import ConnectivityRegistry
from commerce_endpoint_migration_repository import EndpointMigrationRepository
from commerce_worker_migration_persistence import persist_migration_cutover
from commerce_models import (
    UTC,
    CommerceError,
    _new_id,
    _now_text,
)


def _claim_endpoint_migration(self, now: datetime) -> dict[str, Any] | None:
    """Lease one durable credential migration for the maintenance worker."""
    now_text = _now_text(now)
    stale_before = _now_text(now - timedelta(minutes=10))
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        EndpointMigrationRepository.recover_stale(
            connection,
            now_text=now_text,
            stale_before=stale_before,
        )
        row = EndpointMigrationRepository.next_request(
            connection,
            now_text=now_text,
            limit=1,
        )
        if row is None:
            return None
        claimed = EndpointMigrationRepository.mark_claimed(
            connection,
            locked_at=now_text,
            updated_at=now_text,
            job_id=str(row["id"]),
        )
        if claimed.rowcount != 1:
            return None
        result = dict(row)
        result["attempts"] = int(row["attempts"] or 0) + 1
        result["migration_phase"] = str(row["status"])
        result["status"] = "creating"
        return result


def _endpoint_migration_failed(
    self, job_id: str, error: Exception, now: datetime, *, attempt: int, terminal: bool = False
) -> None:
    safe_error = f"{type(error).__name__}: {str(error)[:500]}"
    current = (now or datetime.now(UTC)).astimezone(UTC)
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        row = EndpointMigrationRepository.attempts(
            connection,
            job_id=str(job_id),
        )
        attempts = int(row["attempts"] or 0) if row else 0
        done = bool(terminal or attempts >= 8)
        EndpointMigrationRepository.mark_failed(
            connection,
            status="failed" if done else "pending",
            next_attempt_at="9999-12-31T00:00:00+00:00"
            if done
            else _now_text(current + timedelta(minutes=1)),
            error=safe_error,
            now_text=_now_text(current),
            job_id=str(job_id),
            attempt=attempt,
        )


def _endpoint_migration_completed(self, job_id: str, now: datetime, *, attempt: int) -> None:
    now_text = _now_text(now)
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        EndpointMigrationRepository.complete(
            connection,
            completed_at=now_text,
            updated_at=now_text,
            job_id=str(job_id),
            attempt=attempt,
        )


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


def _mark_migration_source_delete_retry(
    self, job_id: str, error: Exception, now: datetime, *, attempt: int
) -> None:
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        EndpointMigrationRepository.defer_source_delete(
            connection,
            next_attempt_at=_now_text(
                (now or datetime.now(UTC)).astimezone(UTC) + timedelta(minutes=1)
            ),
            error=f"{type(error).__name__}: {str(error)[:500]}",
            now_text=_now_text(now),
            job_id=str(job_id),
            attempt=attempt,
        )


def _delete_migration_source(self, job: dict[str, Any], now: datetime) -> bool:
    source = self._outline_client(str(job["source_server_id"]))
    try:
        source.delete_key(str(job["source_external_id"]))
        getter = getattr(source, "get_key", None)
        if callable(getter) and getter(str(job["source_external_id"])) is not None:
            raise CommerceError("Source Outline key still exists after migration delete")
    except Exception as exc:
        self._mark_migration_source_delete_retry(str(job["id"]), exc, now, attempt=int(job["attempts"]))
        return False
    self._endpoint_migration_completed(str(job["id"]), now, attempt=int(job["attempts"]))
    return True


def _migration_source_state(
    self, job: dict[str, Any], current: datetime
) -> tuple[Any, Any, int, int]:
    try:
        expires_at = datetime.fromisoformat(str(job["expires_at"])).astimezone(UTC)
    except (TypeError, ValueError) as exc:
        raise CommerceError("Migration expiry is invalid") from exc
    if expires_at <= current:
        raise CommerceError("Migration entitlement expired before replacement creation")
    source = self._outline_client(str(job["source_server_id"]))
    target = self._outline_client(str(job["target_server_id"]))
    used = self._metric_for_key(
        source.transfer_metrics(), str(job["source_external_id"])
    )
    if used is None:
        raise CommerceError("Fresh source usage is unavailable; migration is safely deferred")
    quota = int(job["quota_bytes"] or 0)
    remaining = quota - used
    if remaining <= 0:
        raise CommerceError("Source credential has no remaining quota")
    return source, target, used, remaining


def _persist_migration_cutover(
    self,
    job: dict[str, Any],
    current: datetime,
    *,
    target_id: str,
    encrypted: str,
    used: int,
    remaining: int,
) -> tuple[bool, str | None]:
    return persist_migration_cutover(
        self,
        EndpointMigrationRepository,
        job,
        current,
        target_id=target_id,
        encrypted=encrypted,
        used=used,
        remaining=remaining,
    )


def _process_endpoint_migration(self, job: dict[str, Any], now: datetime) -> None:
    if str(job.get("migration_phase") or job.get("status")) == "source_delete_pending":
        self._delete_migration_source(job, now)
        return
    current = (now or datetime.now(UTC)).astimezone(UTC)
    source, target, used, remaining = _migration_source_state(self, job, current)
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
    persisted, subscription_id = _persist_migration_cutover(
        self,
        job,
        current,
        target_id=target_id,
        encrypted=encrypted,
        used=used,
        remaining=remaining,
    )
    if not persisted:
        return
    self._delete_migration_source({**job, "target_external_id": target_id}, current)
    try:
        self._sync_identity_binding(
            telegram_id=int(job["telegram_id"]),
            kind=str(job["profile_kind"]),
            quota_bytes=int(remaining),
            expires_at=str(job["expires_at"]),
            server_id=str(job["target_server_id"]),
            external_id=target_id,
            subscription_id=subscription_id,
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
            self._endpoint_migration_failed(str(job["id"]), exc, current, attempt=int(job["attempts"]))
            print(f"endpoint migration error: {type(exc).__name__}", file=sys.stderr)
        processed += 1
    return processed
