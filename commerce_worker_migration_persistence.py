"""Transaction phases for endpoint migration cutover persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from commerce_endpoint_migration_repository import EndpointMigrationRepository
from commerce_models import CommerceError, _new_id
from connectivity_registry import ConnectivityRegistry


@dataclass(frozen=True, slots=True)
class CutoverContext:
    kind: str
    subscription_id: str | None


def validate_cutover(
    repository: Any,
    connection: Any,
    job: dict[str, Any],
) -> CutoverContext | None:
    """Confirm the lease is still current and resolve the paid profile."""
    current_job = repository.current_state(connection, job_id=str(job["id"]))
    if current_job is None or str(current_job["status"]) != "creating" or int(
        current_job["attempts"]
    ) != int(job["attempts"]):
        return None
    kind = str(job["profile_kind"])
    profile_row = repository.profile_subscription(
        connection, profile_id=str(job["profile_id"])
    )
    return CutoverContext(
        kind=kind,
        subscription_id=(str(profile_row["subscription_id"]) if profile_row and profile_row["subscription_id"] else None),
    )


def move_local_credential(
    service: Any,
    repository: Any,
    connection: Any,
    *,
    job: dict[str, Any],
    context: CutoverContext,
    target_id: str,
    encrypted: str,
    remaining: int,
) -> None:
    """Conditionally move the legacy paid/free key row to the target endpoint."""
    if context.kind == "paid":
        changed = repository.move_paid_key(
            connection,
            target_server_id=str(job["target_server_id"]),
            target_id=target_id,
            encrypted=encrypted,
            remaining=remaining,
            subscription_id=context.subscription_id,
            source_server_id=str(job["source_server_id"]),
            source_external_id=str(job["source_external_id"]),
        )
        if int(getattr(changed, "rowcount", 0) or 0) != 1:
            changed = repository.move_paid_key(
                connection,
                target_server_id=str(job["target_server_id"]),
                target_id=target_id,
                encrypted=encrypted,
                remaining=remaining,
                subscription_id=context.subscription_id,
                source_server_id=str(job["source_server_id"]),
                source_external_id=str(job["source_external_id"]),
            )
        if int(getattr(changed, "rowcount", 0) or 0) != 1:
            raise CommerceError("Paid entitlement changed before migration cutover")
        return
    changed = (
        repository.move_free_key(
            connection,
            target_server_id=str(job["target_server_id"]),
            target_id=target_id,
            remaining=remaining,
            source_server_id=str(job["source_server_id"]),
            source_external_id=str(job["source_external_id"]),
        )
        if service._table_exists(connection, "keys")
        else None
    )
    if changed is None or int(getattr(changed, "rowcount", 0) or 0) != 1:
        raise CommerceError("Free entitlement changed before migration cutover")


def record_cutover(
    service: Any,
    repository: Any,
    connection: Any,
    *,
    job: dict[str, Any],
    context: CutoverContext,
    target_id: str,
    encrypted: str,
    used: int,
    remaining: int,
    now_text: str,
) -> None:
    """Bind the target credential, notify the user, audit, and advance the job."""
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
        profile_kind=context.kind,
        subscription_id=context.subscription_id if context.kind == "paid" else None,
    )
    repository.notify_cutover(
        connection,
        notification_id=_new_id(),
        dedupe_key=f"endpoint-migration:{job['id']}",
        telegram_id=int(job["telegram_id"]),
        text="Your AuriX VPN access was moved to a healthier endpoint.\n"
        f"Remaining quota: {remaining} bytes\nExpires: {str(job['expires_at'])}",
        encrypted=encrypted,
        next_attempt_at=now_text,
        created_at=now_text,
    )
    service._audit(
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
    repository.mark_cutover(
        connection,
        used=used,
        target_id=target_id,
        encrypted=encrypted,
        next_attempt_at=now_text,
        updated_at=now_text,
        job_id=str(job["id"]),
    )


def persist_migration_cutover(
    service: Any,
    repository: Any,
    job: dict[str, Any],
    current: Any,
    *,
    target_id: str,
    encrypted: str,
    used: int,
    remaining: int,
) -> tuple[bool, str | None]:
    """Persist one conditional migration cutover in one transaction."""
    now_text = current.isoformat()
    with service.database.connect() as connection:
        service.database.begin_write(connection)
        context = validate_cutover(repository, connection, job)
        if context is None:
            return False, None
        move_local_credential(
            service,
            repository,
            connection,
            job=job,
            context=context,
            target_id=target_id,
            encrypted=encrypted,
            remaining=remaining,
        )
        record_cutover(
            service,
            repository,
            connection,
            job=job,
            context=context,
            target_id=target_id,
            encrypted=encrypted,
            used=used,
            remaining=remaining,
            now_text=now_text,
        )
    return True, context.subscription_id
