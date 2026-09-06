"""Endpoint migration request workflow."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from commerce_models import UTC, CommerceError, _new_id, _now_text
from connectivity_registry import ConnectivityRegistry
from commerce_endpoint_migration_repository import EndpointMigrationRepository


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
        row = EndpointMigrationRepository.request_context(
            connection,
            target=target,
            source=source,
            external_id=external_id,
        )
        if row is None:
            raise CommerceError("Active credential is not registered on the source endpoint")
        if str(row["credential_status"]) != "active":
            raise CommerceError("Only an active credential can be migrated")
        if str(row["target_status"]) != "active" or not int(row["accepts_new_keys"] or 0):
            raise CommerceError("Target endpoint is not healthy and accepting new keys")
        kind = str(row["profile_kind"])
        entitlement = None
        if kind == "paid":
            entitlement = EndpointMigrationRepository.paid_entitlement(
                connection,
                subscription_id=str(row["subscription_id"]),
                source=source,
                external_id=external_id,
            )
            if (
                entitlement is None
                or str(entitlement["status"]) != "active"
                or str(entitlement["subscription_status"]) != "active"
            ):
                raise CommerceError("Paid entitlement is not active on the source endpoint")
        else:
            entitlement = (
                EndpointMigrationRepository.free_entitlement(
                    connection,
                    source=source,
                    external_id=external_id,
                )
                if self._table_exists(connection, "keys")
                else None
            )
            if entitlement is None or str(entitlement["status"]) not in {"active", "revoke_failed"}:
                raise CommerceError(
                    "Free/trial/promo entitlement is not active on the source endpoint"
                )
        try:
            expires_at = datetime.fromisoformat(str(entitlement["expires_at"])).astimezone(UTC)
        except (TypeError, ValueError) as exc:
            raise CommerceError("Entitlement expiry is invalid") from exc
        if expires_at <= current:
            raise CommerceError("Expired credentials cannot be migrated")
        quota_bytes = int(entitlement["quota_bytes"] or 0)
        if quota_bytes <= 0:
            raise CommerceError("Entitlement quota is invalid")
        existing = EndpointMigrationRepository.existing_request(
            connection,
            credential_id=str(row["credential_id"]),
            target_endpoint_id=str(row["target_endpoint"]),
        )
        if existing is not None and str(existing["status"]) in {
            "pending",
            "creating",
            "source_delete_pending",
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
        EndpointMigrationRepository.insert_request(
            connection,
            job_id=job_id,
            profile_id=str(row["profile_id"]),
            credential_id=str(row["credential_id"]),
            source_endpoint_id=str(row["source_endpoint"]),
            target_endpoint_id=str(row["target_endpoint"]),
            source=source,
            target=target,
            source_external_id=external_id,
            target_external_id=target_external_id,
            target_name=target_name[:128],
            profile_kind=kind,
            telegram_id=int(row["telegram_id"]),
            quota_bytes=quota_bytes,
            expires_at=expires_at.isoformat(),
            next_attempt_at=now_text,
            requested_by=int(requested_by),
            created_at=now_text,
            updated_at=now_text,
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
