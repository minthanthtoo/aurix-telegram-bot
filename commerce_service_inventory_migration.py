"""Endpoint migration request workflow."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from commerce_models import UTC, CommerceError, _new_id, _now_text
from connectivity_registry import ConnectivityRegistry


def queue_endpoint_migration(
    self,
    source_server_id: str,
    outline_key_id: str,
    target_server_id: str,
    requested_by: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Queue an idempotent credential move to a healthy endpoint.

    This records intent only.  The maintenance worker performs the remote
    create/cutover/delete sequence and never calls a provider VM API.
    Quota and expiry are copied from the authoritative local entitlement;
    the worker subtracts fresh source usage before creating the target.
    """
    source = str(source_server_id or "").strip()
    external_id = str(outline_key_id or "").strip()
    target = str(target_server_id or "").strip()
    if not source or not external_id or not target or source == target:
        raise CommerceError("Source, target, and Outline key identity are required")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    now_text = _now_text(current)
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        if not ConnectivityRegistry.available(connection):
            raise CommerceError("Connectivity registry is not initialized")
        row = connection.execute(
            """SELECT c.credential_id, c.profile_id, c.status AS credential_status,
                      c.external_id, p.profile_kind, p.telegram_id, p.subscription_id,
                      se.outline_server_id AS source_server, se.endpoint_id AS source_endpoint,
                      te.outline_server_id AS target_server, te.endpoint_id AS target_endpoint,
                      te.status AS target_status, te.accepts_new_keys
                 FROM connectivity_credentials c
                 JOIN connectivity_profiles p ON p.profile_id = c.profile_id
                 JOIN connectivity_endpoints se ON se.endpoint_id = c.endpoint_id
                 JOIN connectivity_endpoints te ON te.outline_server_id = ?
                WHERE se.outline_server_id = ? AND c.external_id = ?
                LIMIT 1""",
            (target, source, external_id),
        ).fetchone()
        if row is None:
            raise CommerceError("Active credential is not registered on the source endpoint")
        if str(row["credential_status"]) != "active":
            raise CommerceError("Only an active credential can be migrated")
        if str(row["target_status"]) != "active" or not int(row["accepts_new_keys"] or 0):
            raise CommerceError("Target endpoint is not healthy and accepting new keys")
        kind = str(row["profile_kind"])
        entitlement = None
        if kind == "paid":
            entitlement = connection.execute(
                """SELECT k.outline_key_id, k.status, k.quota_bytes,
                          s.expires_at, s.status AS subscription_status
                     FROM paid_vpn_keys k JOIN subscriptions s ON s.id = k.subscription_id
                    WHERE k.subscription_id = ? AND k.server_id = ? AND k.outline_key_id = ?""",
                (str(row["subscription_id"]), source, external_id),
            ).fetchone()
            if entitlement is None or str(entitlement["status"]) != "active" or str(entitlement["subscription_status"]) != "active":
                raise CommerceError("Paid entitlement is not active on the source endpoint")
        else:
            entitlement = connection.execute(
                """SELECT outline_key_id, status, data_limit_bytes AS quota_bytes, expires_at
                     FROM keys
                    WHERE server_id = ? AND outline_key_id = ?""",
                (source, external_id),
            ).fetchone() if self._table_exists(connection, "keys") else None
            if entitlement is None or str(entitlement["status"]) not in {"active", "revoke_failed"}:
                raise CommerceError("Free/trial/promo entitlement is not active on the source endpoint")
        try:
            expires_at = datetime.fromisoformat(str(entitlement["expires_at"])).astimezone(UTC)
        except (TypeError, ValueError) as exc:
            raise CommerceError("Entitlement expiry is invalid") from exc
        if expires_at <= current:
            raise CommerceError("Expired credentials cannot be migrated")
        quota_bytes = int(entitlement["quota_bytes"] or 0)
        if quota_bytes <= 0:
            raise CommerceError("Entitlement quota is invalid")
        existing = connection.execute(
            """SELECT id, status, target_server_id FROM connectivity_migration_jobs
                WHERE credential_id = ? AND target_endpoint_id = ?
                ORDER BY created_at DESC LIMIT 1""",
            (str(row["credential_id"]), str(row["target_endpoint"])),
        ).fetchone()
        if existing is not None and str(existing["status"]) in {
            "pending", "creating", "source_delete_pending"
        }:
            return {
                "job_id": str(existing["id"]),
                "status": str(existing["status"]),
                "source_server_id": source,
                "target_server_id": target,
                "idempotent": True,
            }
        job_id = _new_id()
        target_external_id = f"aurix-mig-{job_id[:24]}"
        target_name = f"AuriX-MIG-{str(row['telegram_id'])}-{target[:16]}-{job_id[:8]}"
        connection.execute(
            """INSERT INTO connectivity_migration_jobs
               (id, profile_id, credential_id, source_endpoint_id, target_endpoint_id,
                source_server_id, target_server_id, source_external_id, target_external_id,
                target_name, profile_kind, telegram_id, quota_bytes, expires_at,
                status, attempts, next_attempt_at, requested_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)""",
            (
                job_id,
                str(row["profile_id"]),
                str(row["credential_id"]),
                str(row["source_endpoint"]),
                str(row["target_endpoint"]),
                source,
                target,
                external_id,
                target_external_id,
                target_name[:128],
                kind,
                int(row["telegram_id"]),
                quota_bytes,
                expires_at.isoformat(),
                now_text,
                int(requested_by),
                now_text,
                now_text,
            ),
        )
        self._audit(
            connection,
            "endpoint_migration_queued",
            "connectivity_migration",
            job_id,
            "owner",
            str(requested_by),
            {
                "source_server_id": source,
                "target_server_id": target,
                "source_external_id": external_id,
                "profile_kind": kind,
                "expires_at": expires_at.isoformat(),
            },
        )
    return {
        "job_id": job_id,
        "status": "pending",
        "source_server_id": source,
        "target_server_id": target,
        "profile_kind": kind,
        "expires_at": expires_at.isoformat(),
        "idempotent": False,
    }
