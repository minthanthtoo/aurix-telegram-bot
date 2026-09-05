"""Endpoint migration operations."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from typing import Any
from commerce_repositories import _PostgresConnection
from connectivity_registry import ConnectivityRegistry
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
