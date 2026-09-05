"""Durable owner and administrator authorization for AuriX."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from access_control_repository import AccessControlRepository


UTC = timezone.utc
STAFF_NOTIFICATION_EVENTS = (
    "order_created",
    "receipt_submitted",
    "rejected",
    "key_repairs",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class StaffAccessError(RuntimeError):
    """A safe staff-management failure."""


class StaffAccessControl:
    """Database-backed role checks with an immutable bootstrap owner."""

    def __init__(self, database: Any, immutable_owner_id: int | None = None):
        self.database = database
        self.repository = AccessControlRepository()
        self.immutable_owner_id = int(immutable_owner_id) if immutable_owner_id else None

    def _audit(
        self,
        connection: Any,
        action: str,
        target_id: int,
        actor_id: int | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.repository.audit(
            connection, action, target_id, actor_id, metadata, _now()
        )

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
            existing = self.repository.active_owner(connection)
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
                self.repository.insert_owner(
                    connection,
                    telegram_id=selected_owner,
                    display_name=str(owner_profile.get("display_name") or "")[:128] or None,
                    username=str(owner_profile.get("username") or "")[:128] or None,
                    source=owner_source,
                    added_by=selected_owner,
                    now_text=_now(),
                )
                self._audit(
                    connection,
                    "owner_bootstrapped",
                    selected_owner,
                    selected_owner,
                    {"source": owner_source},
                )

            active_admin_count = self.repository.active_admin_count(connection)
            candidates: list[dict[str, Any]] = []
            source = "none"
            if active_admin_count == 0:
                environment_ids = [
                    value for value in legacy_admin_ids if value != selected_owner
                ]
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
                self.repository.insert_admin(
                    connection,
                    telegram_id=candidate_id,
                    display_name=str(candidate.get("display_name") or "")[:128] or None,
                    username=str(candidate.get("username") or "")[:128] or None,
                    source=source,
                    added_by=selected_owner,
                    now_text=_now(),
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
            row = self.repository.role(connection, telegram_id)
        return str(row["role"]) if row is not None else None

    def control_group(self) -> dict[str, Any] | None:
        """Return the owner-approved control group, if one has been bound."""
        with self.database.connect() as connection:
            row = self.repository.control_group(connection)
        return dict(row) if row is not None else None

    def bind_control_group(
        self,
        control_group_id: int,
        owner_id: int,
        *,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Persist a Telegram-authenticated group selected by the owner."""
        self.require_owner(owner_id)
        group_id = int(control_group_id)
        if group_id >= 0:
            raise StaffAccessError("The control group must have a negative Telegram chat ID")
        bound_at = _now()
        clean_title = str(title or "").strip()[:128] or None
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self.repository.bind_control_group(
                connection,
                group_id=group_id,
                title=clean_title,
                owner_id=int(owner_id),
                bound_at=bound_at,
            )
            self._audit(
                connection,
                "control_group_bound",
                int(owner_id),
                int(owner_id),
                {"control_group_id": group_id, "title": clean_title},
            )
        return self.control_group() or {}

    def owner_id(self) -> int | None:
        with self.database.connect() as connection:
            row = self.repository.owner(connection)
        return int(row["telegram_id"]) if row is not None else None

    def admin_ids(self) -> set[int]:
        with self.database.connect() as connection:
            rows = self.repository.active_staff_ids(connection)
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
            rows = self.repository.list_staff(connection)
        return [dict(row) for row in rows]

    def notification_preferences(self, telegram_id: int) -> dict[str, bool]:
        """Return one staff member's event choices; missing rows default on."""
        self.require_admin(telegram_id)
        with self.database.connect() as connection:
            rows = self.repository.notification_preferences(connection, telegram_id)
        configured = {str(row["event_type"]): bool(row["enabled"]) for row in rows}
        return {event: configured.get(event, True) for event in STAFF_NOTIFICATION_EVENTS}

    def set_notification_preference(
        self, telegram_id: int, event_type: str, enabled: bool
    ) -> dict[str, bool]:
        """Change the caller's own staff alert choice and audit the change."""
        self.require_admin(telegram_id)
        event = str(event_type).strip().lower()
        if event not in STAFF_NOTIFICATION_EVENTS:
            raise StaffAccessError("That notification type is unavailable")
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            now_text = _now()
            self.repository.set_notification_preference(
                connection, telegram_id, event, enabled, now_text
            )
            self._audit(
                connection,
                "staff_notification_preference_changed",
                int(telegram_id),
                int(telegram_id),
                {"event_type": event, "enabled": bool(enabled)},
            )
        return self.notification_preferences(telegram_id)

    def add_admin(self, telegram_id: int, owner_id: int) -> dict[str, Any]:
        self.require_owner(owner_id)
        candidate_id = int(telegram_id)
        if candidate_id <= 0 or candidate_id == self.owner_id():
            raise StaffAccessError("That account cannot be added as an administrator")
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            user = self.repository.user(connection, candidate_id)
            if user is None:
                raise StaffAccessError(
                    "That user must open the bot and use /whoami before being added"
                )
            self.repository.insert_admin(
                connection,
                telegram_id=candidate_id,
                display_name=str(user["first_name"] or "")[:128] or None,
                username=str(user["username"] or "")[:128] or None,
                source="owner_panel",
                added_by=int(owner_id),
                now_text=_now(),
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
            now_text = _now()
            updated = self.repository.revoke_admin(
                connection, target_id, int(owner_id), now_text
            )
            if int(getattr(updated, "rowcount", 0) or 0) != 1:
                raise StaffAccessError("Active administrator not found")
            self.repository.cancel_challenges(connection, target_id, now_text)
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
            self.repository.insert_sync_run(
                connection,
                run_id=run_id,
                control_group_id=control_group_id,
                owner_id=owner_id,
                snapshot=snapshot,
                now_text=_now(),
            )
        return {"run_id": run_id, **snapshot}
