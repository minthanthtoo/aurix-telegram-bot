"""Remote inventory, endpoint migration and managed-repair review use cases."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any
from commerce_models import UTC
from commerce_models import CommerceError
from commerce_models import _new_id
from commerce_models import _human_bytes
from commerce_models import _now_text
from connectivity_registry import ConnectivityRegistry
from commerce_inventory_operations_repository import InventoryOperationsRepository


_INVENTORY = InventoryOperationsRepository()


def endpoint_migration_jobs(
    self, *, limit: int = 50, include_completed: bool = False
) -> list[dict[str, Any]]:
    """Return safe endpoint-migration operations for the admin console.

    This is intentionally an operational view: it contains no management
    URLs, certificates, access URLs, or secret ciphertext.  Completed rows
    are omitted by default so the panel stays focused on work that still
    needs observation or retry.
    """
    try:
        page_limit = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        page_limit = 50
    with self.database.connect() as connection:
        if not _INVENTORY.table_exists(connection, "connectivity_migration_jobs"):
            return []
        return _INVENTORY.endpoint_migration_jobs(
            connection, limit=page_limit, include_completed=include_completed
        )

from commerce_service_inventory_migration import queue_endpoint_migration
def migratable_credentials(self, source_server_id: str) -> list[dict[str, Any]]:
    """List active managed credentials on a source endpoint, sans secrets."""
    source = str(source_server_id or "").strip()
    if not source:
        raise CommerceError("Source endpoint is required")
    with self.database.connect() as connection:
        if not ConnectivityRegistry.available(connection):
            return []
        return _INVENTORY.migratable_credentials(connection, source)

from commerce_service_inventory_reconciliation import refresh_server_inventory
def ensure_managed_key_repair_notifications(
    self, now: datetime | None = None
) -> int:
    """Backfill staff alerts for every still-open managed-key repair.

    Inventory normally opens the alert in the same transaction as the
    repair decision. This second, cheap pass also covers repairs created
    during startup before staff bootstrap completed, and repairs created
    by an older release. It is idempotent per repair/staff pair and never
    calls Outline or changes entitlement state.
    """
    created_at = _now_text(now)
    queued = 0
    with self.database.connect() as connection:
        if not _INVENTORY.table_exists(connection, "managed_key_repair_jobs"):
            return 0
        rows = _INVENTORY.open_managed_repairs(connection)
        for row in rows:
            repair_id = str(row["id"])
            exists = _INVENTORY.repair_notification_exists(connection, repair_id)
            usage_text = "unknown (fresh Outline telemetry unavailable)"
            if row["used_bytes"] is not None:
                usage_text = f"{_human_bytes(max(0, int(row['used_bytes'])))} observed"
            if not exists:
                self._queue_staff_notification(
                    connection,
                    "key_repairs",
                    repair_id,
                    "🧩 MANAGED KEY MISSING\n\n"
                    f"Repair: #{repair_id[:8]}\n"
                    f"Customer: tg:{int(row['telegram_id'])}\n"
                    f"Endpoint: {str(row['server_id'])[:32]}\n"
                    f"Old key: {str(row['source_external_id'])[:32]}\n"
                    f"Usage: {usage_text}\n"
                    f"Decision: {str(row['status']).replace('_', ' ')}\n\n"
                    "Open Key Repairs to review. AuriX will not recreate this key or reset quota without the required owner decision.",
                    created_at,
                )
                queued += 1
            self._queue_customer_repair_notification(
                connection,
                repair_id,
                int(row["telegram_id"]),
                str(row["status"]),
                str(row["server_id"]),
                created_at,
            )
    return queued

def remote_key_inventory(
    self,
    server_id: str,
    *,
    status: str = "present",
    managed: bool | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return the safe, operator-facing remote-key audit ledger.

    Only Outline's key id, display name, management classification,
    lifecycle status and observed telemetry are returned. Access URLs are
    never read by this method, so the admin inventory panel cannot
    accidentally disclose a usable VPN credential.
    """
    normalized_status = str(status or "present").strip().lower()
    if normalized_status not in {"present", "missing", "all"}:
        raise CommerceError("Remote inventory status must be present, missing or all")
    try:
        page_limit = max(1, min(500, int(limit)))
    except (TypeError, ValueError):
        page_limit = 100
    with self.database.connect() as connection:
        if not _INVENTORY.table_exists(connection, "outline_remote_keys"):
            return []
        try:
            return _INVENTORY.remote_key_inventory(
                connection,
                str(server_id),
                status=normalized_status,
                managed=managed,
                limit=page_limit,
            )
        except ValueError as exc:
            raise CommerceError(str(exc)) from exc

def review_remote_key(
    self,
    server_id: str,
    outline_key_id: str,
    review_state: str,
    reviewer_id: int,
    *,
    note: str | None = None,
) -> dict[str, Any]:
    """Record an explicit owner review of an untracked remote key.

    ``accepted_external`` means the key was inspected and intentionally
    remains outside AuriX lifecycle management. It does not make the key
    saleable or hide it from remote capacity counts. Resetting to
    ``unreviewed`` re-opens the migration blocker. No remote key is
    deleted or adopted by this operation.
    """
    normalized_state = str(review_state or "").strip().lower()
    if normalized_state not in {"unreviewed", "accepted_external"}:
        raise CommerceError("Remote key review state is invalid")
    server = str(server_id or "").strip()
    key_id = str(outline_key_id or "").strip()
    if not server or not key_id:
        raise CommerceError("Remote key identity is required")
    now_text = _now_text()
    clean_note = str(note or "").strip()[:512] or None
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        row = _INVENTORY.remote_key_for_review(connection, server, key_id)
        if row is None:
            raise CommerceError("Remote key is not present in the audit inventory")
        if int(row["managed"] or 0) == 1:
            raise CommerceError("Managed AuriX keys do not need orphan review")
        _INVENTORY.save_remote_key_review(
            connection,
            server_id=server,
            key_id=key_id,
            review_state=normalized_state,
            reviewer_id=int(reviewer_id),
            reviewed_at=now_text,
            note=clean_note,
        )
        _INVENTORY.recompute_orphan_count(connection, server, now_text)
        self._audit(
            connection,
            "remote_key_reviewed",
            "outline_remote_key",
            f"{server}:{key_id}",
            "staff",
            str(reviewer_id),
            {
                "review_state": normalized_state,
                "remote_status": str(row["status"] or ""),
                "note": clean_note,
            },
        )
    return {
        "server_id": server,
        "outline_key_id": key_id,
        "review_state": normalized_state,
        "reviewed_by": int(reviewer_id),
        "reviewed_at": now_text,
        "review_note": clean_note,
    }

def managed_key_repair_jobs(
    self, *, status: str = "open", limit: int = 100
) -> list[dict[str, Any]]:
    """Return safe operator-facing managed-key repair decisions.

    Secrets are deliberately excluded. ``open`` includes pending, failed,
    and manual decisions; completed history remains available through the
    audit ledger and an explicit ``status='all'`` query.
    """
    normalized = str(status or "open").strip().lower()
    if normalized not in {"open", "pending", "failed", "manual", "done", "cancelled", "all"}:
        raise CommerceError("Repair status must be open, pending, failed, manual, done, cancelled or all")
    try:
        page_limit = max(1, min(500, int(limit)))
    except (TypeError, ValueError):
        page_limit = 100
    with self.database.connect() as connection:
        if not _INVENTORY.table_exists(connection, "managed_key_repair_jobs"):
            return []
        return _INVENTORY.managed_key_repair_jobs(
            connection, status=normalized, limit=page_limit
        )

def approve_managed_key_repair(
    self,
    repair_id: str,
    owner_id: int,
    *,
    allow_unknown_usage: bool = False,
) -> dict[str, Any]:
    """Owner-approve a manual repair after an explicit quota decision.

    When Outline no longer exposes the old key's traffic, approving a
    full-quota replacement is intentionally a separate, auditable action.
    The normal automatic worker never takes this path.
    """
    repair = str(repair_id or "").strip()
    if not repair:
        raise CommerceError("Repair identity is required")
    now_text = _now_text()
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        row = _INVENTORY.managed_key_repair(connection, repair)
        if row is None:
            raise CommerceError("Managed-key repair was not found")
        if str(row["status"]) not in {"manual", "failed"}:
            raise CommerceError("This repair is not awaiting owner review")
        reason = str(row["last_error"] or "")
        if reason == "quota_already_exhausted":
            raise CommerceError("This key has exhausted its quota and cannot be restored")
        if reason == "usage_observation_required" and not allow_unknown_usage:
            raise CommerceError(
                "Explicitly confirm full-quota restoration when Outline usage is unavailable"
            )
        override = reason == "usage_observation_required" and allow_unknown_usage
        updated = _INVENTORY.approve_managed_key_repair(
            connection,
            repair_id=repair,
            now_text=now_text,
            used_bytes=0 if override else row["used_bytes"],
            last_error="owner_approved_unknown_usage" if override else None,
        )
        if not updated:
            raise CommerceError("Managed-key repair changed before owner approval")
        self._audit(
            connection,
            "managed_key_repair_approved",
            "managed_key_repair",
            repair,
            "owner",
            str(owner_id),
            {
                "allow_unknown_usage": bool(allow_unknown_usage),
                "usage_override": bool(override),
                "previous_status": str(row["status"]),
                "previous_error": reason,
            },
        )
    return {"repair_id": repair, "status": "pending", "usage_override": override}
