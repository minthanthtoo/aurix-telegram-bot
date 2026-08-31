"""Durable owner and administrator authorization for AuriX."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable


UTC = timezone.utc


def _now() -> str:
    return datetime.now(UTC).isoformat()


class StaffAccessError(RuntimeError):
    """A safe staff-management failure."""


class StaffAccessControl:
    """Database-backed role checks with an immutable bootstrap owner."""

    def __init__(self, database: Any, immutable_owner_id: int | None = None):
        self.database = database
        self.immutable_owner_id = int(immutable_owner_id) if immutable_owner_id else None

    def _audit(
        self,
        connection: Any,
        action: str,
        target_id: int,
        actor_id: int | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            connection.execute(
                """INSERT INTO audit_events
                   (actor_type, actor_id, action, target_type, target_id,
                    metadata_json, created_at)
                   VALUES ('staff', ?, ?, 'staff_account', ?, ?, ?)""",
                (
                    str(actor_id) if actor_id is not None else None,
                    action,
                    str(target_id),
                    json.dumps(metadata or {}, sort_keys=True),
                    _now(),
                ),
            )
        except Exception:
            # Free-only test repositories may initialize before commerce has
            # created the shared audit table. Authorization state still wins.
            pass

    def bootstrap(
        self,
        *,
        owner_id: int | None,
        admin_ids: Iterable[int] = (),
        group_owner: dict[str, Any] | None = None,
        group_admins: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        """Resolve initial staff once without silently replacing an owner."""
        explicit_owner = int(owner_id) if owner_id else None
        group_owner_id = int(group_owner["id"]) if group_owner and group_owner.get("id") else None
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            existing = connection.execute(
                "SELECT telegram_id FROM staff_accounts WHERE role = 'owner' AND status = 'active'"
            ).fetchone()
            existing_owner = int(existing["telegram_id"]) if existing is not None else None
            legacy_admin_ids = sorted({int(value) for value in admin_ids if int(value) > 0})
            legacy_single_owner = legacy_admin_ids[0] if len(legacy_admin_ids) == 1 else None
            selected_owner = explicit_owner or existing_owner or group_owner_id or legacy_single_owner
            if explicit_owner and existing_owner and explicit_owner != existing_owner:
                raise StaffAccessError(
                    "OWNER_TELEGRAM_ID conflicts with the active database owner"
                )
            if selected_owner and existing_owner is None:
                owner_profile = group_owner if group_owner_id == selected_owner else {}
                owner_source = (
                    "environment"
                    if explicit_owner
                    else "telegram_group_bootstrap"
                    if group_owner_id
                    else "legacy_single_admin"
                )
                connection.execute(
                    """INSERT INTO staff_accounts
                       (telegram_id, role, status, display_name, username, source,
                        added_by, added_at, access_version)
                       VALUES (?, 'owner', 'active', ?, ?, ?, ?, ?, 1)
                       ON CONFLICT(telegram_id) DO UPDATE SET
                         role = 'owner', status = 'active', revoked_by = NULL,
                         revoked_at = NULL, access_version = staff_accounts.access_version + 1""",
                    (
                        selected_owner,
                        str(owner_profile.get("display_name") or "")[:128] or None,
                        str(owner_profile.get("username") or "")[:128] or None,
                        owner_source,
                        selected_owner,
                        _now(),
                    ),
                )
                self._audit(
                    connection,
                    "owner_bootstrapped",
                    selected_owner,
                    selected_owner,
                    {"source": owner_source},
                )

            active_admin_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM staff_accounts WHERE role = 'admin' AND status = 'active'"
                ).fetchone()["count"]
            )
            candidates: list[dict[str, Any]] = []
            source = "none"
            if active_admin_count == 0:
                environment_ids = legacy_admin_ids
                if environment_ids:
                    candidates = [{"id": value} for value in environment_ids]
                    source = "environment_legacy"
                else:
                    candidates = [dict(item) for item in group_admins]
                    source = "telegram_group_bootstrap"
            imported = 0
            for candidate in candidates:
                candidate_id = int(candidate.get("id") or 0)
                if candidate_id <= 0 or candidate_id == selected_owner or candidate.get("is_bot"):
                    continue
                connection.execute(
                    """INSERT INTO staff_accounts
                       (telegram_id, role, status, display_name, username, source,
                        added_by, added_at, access_version)
                       VALUES (?, 'admin', 'active', ?, ?, ?, ?, ?, 1)
                       ON CONFLICT(telegram_id) DO UPDATE SET
                         role = 'admin', status = 'active', display_name = excluded.display_name,
                         username = excluded.username, source = excluded.source,
                         revoked_by = NULL, revoked_at = NULL,
                         access_version = staff_accounts.access_version + 1""",
                    (
                        candidate_id,
                        str(candidate.get("display_name") or "")[:128] or None,
                        str(candidate.get("username") or "")[:128] or None,
                        source,
                        selected_owner,
                        _now(),
                    ),
                )
                imported += 1
                self._audit(
                    connection,
                    "admin_bootstrapped",
                    candidate_id,
                    selected_owner,
                    {"source": source},
                )
        self.immutable_owner_id = explicit_owner or existing_owner or group_owner_id or legacy_single_owner
        return {
            "owner_id": self.owner_id(),
            "admin_ids": self.admin_ids(),
            "imported_admins": imported,
        }

    def role_for(self, telegram_id: int) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT role FROM staff_accounts WHERE telegram_id = ? AND status = 'active'",
                (int(telegram_id),),
            ).fetchone()
        return str(row["role"]) if row is not None else None

    def owner_id(self) -> int | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT telegram_id FROM staff_accounts WHERE role = 'owner' AND status = 'active'"
            ).fetchone()
        return int(row["telegram_id"]) if row is not None else None

    def admin_ids(self) -> set[int]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT telegram_id FROM staff_accounts WHERE status = 'active'"
            ).fetchall()
        return {int(row["telegram_id"]) for row in rows}

    def is_admin(self, telegram_id: int) -> bool:
        return self.role_for(telegram_id) in {"owner", "admin"}

    def is_owner(self, telegram_id: int) -> bool:
        return self.role_for(telegram_id) == "owner"

    def require_admin(self, telegram_id: int) -> None:
        if not self.is_admin(telegram_id):
            raise PermissionError("administrator access required")

    def require_owner(self, telegram_id: int) -> None:
        if not self.is_owner(telegram_id):
            raise PermissionError("owner access required")

    def list_staff(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT s.*, COALESCE(NULLIF(s.display_name, ''), u.first_name, '') AS effective_name,
                          COALESCE(NULLIF(s.username, ''), u.username) AS effective_username
                   FROM staff_accounts s LEFT JOIN users u ON u.telegram_id = s.telegram_id
                   WHERE s.status = 'active'
                   ORDER BY CASE WHEN s.role = 'owner' THEN 0 ELSE 1 END,
                            s.added_at, s.telegram_id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def add_admin(self, telegram_id: int, owner_id: int) -> dict[str, Any]:
        self.require_owner(owner_id)
        candidate_id = int(telegram_id)
        if candidate_id <= 0 or candidate_id == self.owner_id():
            raise StaffAccessError("That account cannot be added as an administrator")
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            user = connection.execute(
                "SELECT first_name, username FROM users WHERE telegram_id = ?", (candidate_id,)
            ).fetchone()
            if user is None:
                raise StaffAccessError(
                    "That user must open the bot and use /whoami before being added"
                )
            connection.execute(
                """INSERT INTO staff_accounts
                   (telegram_id, role, status, display_name, username, source,
                    added_by, added_at, access_version)
                   VALUES (?, 'admin', 'active', ?, ?, 'owner_panel', ?, ?, 1)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                     role = 'admin', status = 'active', display_name = excluded.display_name,
                     username = excluded.username, source = 'owner_panel',
                     added_by = excluded.added_by, added_at = excluded.added_at,
                     revoked_by = NULL, revoked_at = NULL,
                     access_version = staff_accounts.access_version + 1""",
                (
                    candidate_id,
                    str(user["first_name"] or "")[:128] or None,
                    str(user["username"] or "")[:128] or None,
                    int(owner_id),
                    _now(),
                ),
            )
            self._audit(connection, "admin_added", candidate_id, owner_id)
        return next(item for item in self.list_staff() if int(item["telegram_id"]) == candidate_id)

    def remove_admin(self, telegram_id: int, owner_id: int) -> None:
        self.require_owner(owner_id)
        target_id = int(telegram_id)
        if target_id == self.owner_id() or target_id == self.immutable_owner_id:
            raise StaffAccessError("The owner cannot be removed or demoted from Telegram")
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            updated = connection.execute(
                """UPDATE staff_accounts
                   SET status = 'revoked', revoked_by = ?, revoked_at = ?,
                       access_version = access_version + 1
                   WHERE telegram_id = ? AND role = 'admin' AND status = 'active'""",
                (int(owner_id), _now(), target_id),
            )
            if int(getattr(updated, "rowcount", 0) or 0) != 1:
                raise StaffAccessError("Active administrator not found")
            connection.execute(
                """UPDATE admin_action_challenges SET status = 'cancelled', cancelled_at = ?
                   WHERE admin_id = ? AND status = 'pending'""",
                (_now(), target_id),
            )
            self._audit(connection, "admin_revoked", target_id, owner_id)

    def group_sync_preview(
        self,
        control_group_id: int,
        owner_id: int,
        group_owner: dict[str, Any] | None,
        group_admins: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        self.require_owner(owner_id)
        current = {int(item["telegram_id"]): item for item in self.list_staff()}
        group_ids = {
            int(item["id"])
            for item in group_admins
            if int(item.get("id") or 0) > 0 and not item.get("is_bot")
        }
        additions = sorted(group_ids - set(current))
        removals = sorted(
            staff_id
            for staff_id, item in current.items()
            if item["role"] == "admin" and staff_id not in group_ids
        )
        snapshot = {
            "group_owner_id": int(group_owner["id"]) if group_owner else None,
            "current_owner_id": self.owner_id(),
            "additions": additions,
            "review_removals": removals,
        }
        run_id = uuid.uuid4().hex
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO staff_sync_runs
                   (id, control_group_id, requested_by, source, status, snapshot_json, created_at)
                   VALUES (?, ?, ?, 'telegram_group', 'previewed', ?, ?)""",
                (run_id, int(control_group_id), int(owner_id), json.dumps(snapshot), _now()),
            )
        return {"run_id": run_id, **snapshot}
