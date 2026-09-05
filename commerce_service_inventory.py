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
    statuses = (
        "('pending', 'creating', 'source_delete_pending', 'failed', 'cancelled')"
        if not include_completed
        else "('pending', 'creating', 'source_delete_pending', 'failed', 'cancelled', 'completed')"
    )
    with self.database.connect() as connection:
        if not self._table_exists(connection, "connectivity_migration_jobs"):
            return []
        rows = connection.execute(
            f"""SELECT id AS job_id, profile_kind, telegram_id,
                              source_server_id, target_server_id,
                              source_external_id, target_external_id,
                              status AS job_status, attempts,
                              source_used_bytes, quota_bytes,
                              next_attempt_at, last_error,
                              requested_by, created_at, updated_at,
                              completed_at
                         FROM connectivity_migration_jobs
                        WHERE status IN {statuses}
                        ORDER BY CASE status
                                   WHEN 'failed' THEN 0
                                   WHEN 'source_delete_pending' THEN 1
                                   WHEN 'creating' THEN 2
                                   WHEN 'pending' THEN 3
                                   ELSE 4
                                 END,
                                 created_at DESC
                        LIMIT ?""",
            (page_limit,),
        ).fetchall()
    return [dict(row) for row in rows]

from commerce_service_inventory_migration import queue_endpoint_migration
def migratable_credentials(self, source_server_id: str) -> list[dict[str, Any]]:
    """List active managed credentials on a source endpoint, sans secrets."""
    source = str(source_server_id or "").strip()
    if not source:
        raise CommerceError("Source endpoint is required")
    with self.database.connect() as connection:
        if not ConnectivityRegistry.available(connection):
            return []
        rows = connection.execute(
            """SELECT c.credential_id, c.external_id, p.profile_kind,
                      p.telegram_id, c.created_at, c.status
                 FROM connectivity_credentials c
                 JOIN connectivity_profiles p ON p.profile_id = c.profile_id
                 JOIN connectivity_endpoints e ON e.endpoint_id = c.endpoint_id
                WHERE e.outline_server_id = ? AND c.status = 'active'
                ORDER BY c.created_at, c.external_id""",
            (source,),
        ).fetchall()
    return [dict(row) for row in rows]

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
        if not self._table_exists(connection, "managed_key_repair_jobs"):
            return 0
        rows = connection.execute(
            """SELECT id, kind, server_id, telegram_id, source_external_id,
                      quota_bytes, used_bytes, status
                 FROM managed_key_repair_jobs
                WHERE status IN ('pending', 'running', 'manual', 'failed')
                ORDER BY created_at, id"""
        ).fetchall()
        for row in rows:
            repair_id = str(row["id"])
            exists = connection.execute(
                "SELECT 1 FROM notifications WHERE dedupe_key LIKE ? LIMIT 1",
                (f"staff:key_repairs:{repair_id}:%",),
            ).fetchone()
            usage_text = "unknown (fresh Outline telemetry unavailable)"
            if row["used_bytes"] is not None:
                usage_text = f"{_human_bytes(max(0, int(row['used_bytes'])))} observed"
            if exists is None:
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
        if not self._table_exists(connection, "outline_remote_keys"):
            return []
        # Validate the server id so a stale button is distinguishable from
        # a server which genuinely has no audit rows.
        if connection.execute(
            "SELECT 1 FROM outline_servers WHERE server_id = ?", (str(server_id),)
        ).fetchone() is None:
            raise CommerceError("Outline server is unavailable")
        clauses = ["outline_remote_keys.server_id = ?"]
        params: list[Any] = [str(server_id)]
        if normalized_status != "all":
            clauses.append("outline_remote_keys.status = ?")
            params.append(normalized_status)
        if managed is not None:
            clauses.append("outline_remote_keys.managed = ?")
            params.append(1 if managed else 0)
        params.append(page_limit)
        rows = connection.execute(
            """SELECT outline_remote_keys.server_id, outline_remote_keys.outline_key_id,
                      outline_remote_keys.remote_name, outline_remote_keys.managed,
                      outline_remote_keys.status, outline_remote_keys.first_seen_at,
                      outline_remote_keys.last_seen_at, outline_remote_keys.last_usage_bytes,
                      COALESCE(r.review_state,
                               CASE WHEN outline_remote_keys.managed = 1
                                    THEN 'managed' ELSE 'unreviewed' END) AS review_state,
                      r.reviewed_by, r.reviewed_at, r.review_note
                 FROM outline_remote_keys
                 LEFT JOIN outline_remote_key_reviews r
                   ON r.server_id = outline_remote_keys.server_id
                  AND r.outline_key_id = outline_remote_keys.outline_key_id
                WHERE """
            + " AND ".join(clauses)
            + """ ORDER BY CASE WHEN outline_remote_keys.status = 'present' THEN 0 ELSE 1 END,
                              outline_remote_keys.last_seen_at DESC,
                              outline_remote_keys.outline_key_id
                LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

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
        row = connection.execute(
            """SELECT managed, status FROM outline_remote_keys
               WHERE server_id = ? AND outline_key_id = ?""",
            (server, key_id),
        ).fetchone()
        if row is None:
            raise CommerceError("Remote key is not present in the audit inventory")
        if int(row["managed"] or 0) == 1:
            raise CommerceError("Managed AuriX keys do not need orphan review")
        connection.execute(
            """INSERT INTO outline_remote_key_reviews
               (server_id, outline_key_id, review_state, reviewed_by, reviewed_at, review_note)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(server_id, outline_key_id) DO UPDATE SET
                 review_state = excluded.review_state,
                 reviewed_by = excluded.reviewed_by,
                 reviewed_at = excluded.reviewed_at,
                 review_note = excluded.review_note""",
            (server, key_id, normalized_state, int(reviewer_id), now_text, clean_note),
        )
        connection.execute(
            """UPDATE outline_servers
                  SET remote_orphan_key_count = (
                      SELECT COUNT(*)
                        FROM outline_remote_keys remote
                       WHERE remote.server_id = outline_servers.server_id
                         AND remote.status = 'present'
                         AND remote.managed = 0
                         AND COALESCE(
                               (SELECT review_state
                                  FROM outline_remote_key_reviews review
                                 WHERE review.server_id = remote.server_id
                                   AND review.outline_key_id = remote.outline_key_id),
                               'unreviewed'
                             ) = 'unreviewed'
                  ),
                      updated_at = ?
                WHERE server_id = ?""",
            (now_text, server),
        )
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
        if not self._table_exists(connection, "managed_key_repair_jobs"):
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if normalized == "open":
            clauses.append("status IN ('pending', 'running', 'failed', 'manual')")
        elif normalized != "all":
            clauses.append("status = ?")
            params.append(normalized)
        params.append(page_limit)
        rows = connection.execute(
            """SELECT id, kind, server_id, telegram_id, local_key_ref,
                      source_external_id, target_external_id, key_name,
                      quota_bytes, used_bytes, expires_at, status, attempts,
                      next_attempt_at, locked_at, last_error, observed_at,
                      created_at, completed_at
                 FROM managed_key_repair_jobs"""
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
            + " ORDER BY CASE status WHEN 'manual' THEN 0 WHEN 'failed' THEN 1 "
              "WHEN 'pending' THEN 2 WHEN 'running' THEN 3 ELSE 4 END, created_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]

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
        row = connection.execute(
            "SELECT * FROM managed_key_repair_jobs WHERE id = ?",
            (repair,),
        ).fetchone()
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
        updated = connection.execute(
            """UPDATE managed_key_repair_jobs
                  SET status = 'pending', attempts = 0, next_attempt_at = ?,
                      locked_at = NULL, used_bytes = ?,
                      last_error = ?, completed_at = NULL
                WHERE id = ? AND status IN ('manual', 'failed')""",
            (
                now_text,
                0 if override else row["used_bytes"],
                "owner_approved_unknown_usage" if override else None,
                repair,
            ),
        )
        if int(getattr(updated, "rowcount", 0) or 0) != 1:
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
