"""Persistence boundary for staff roles, control groups, and audit state."""

from __future__ import annotations

import json
from typing import Any


class AccessControlRepository:
    """Keep authorization state storage separate from authorization policy."""

    @staticmethod
    def audit(
        connection: Any,
        action: str,
        target_id: int,
        actor_id: int | None,
        metadata: dict[str, Any] | None,
        now_text: str,
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
                    now_text,
                ),
            )
        except Exception:
            pass

    @staticmethod
    def active_owner(connection: Any) -> Any:
        return connection.execute(
            "SELECT telegram_id FROM staff_accounts WHERE role = 'owner' AND status = 'active'"
        ).fetchone()

    @staticmethod
    def insert_owner(connection: Any, **values: Any) -> None:
        connection.execute(
            """INSERT INTO staff_accounts
               (telegram_id, role, status, display_name, username, source,
                added_by, added_at, access_version)
               VALUES (?, 'owner', 'active', ?, ?, ?, ?, ?, 1)
               ON CONFLICT(telegram_id) DO UPDATE SET
                 role = 'owner', status = 'active', revoked_by = NULL,
                 revoked_at = NULL, access_version = staff_accounts.access_version + 1""",
            (values["telegram_id"], values["display_name"], values["username"], values["source"], values["added_by"], values["now_text"]),
        )

    @staticmethod
    def active_admin_count(connection: Any) -> int:
        return int(
            connection.execute(
                "SELECT COUNT(*) AS count FROM staff_accounts WHERE role = 'admin' AND status = 'active'"
            ).fetchone()["count"]
        )

    @staticmethod
    def insert_admin(connection: Any, **values: Any) -> None:
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
            (values["telegram_id"], values["display_name"], values["username"], values["source"], values["added_by"], values["now_text"]),
        )

    @staticmethod
    def role(connection: Any, telegram_id: int) -> Any:
        return connection.execute(
            "SELECT role FROM staff_accounts WHERE telegram_id = ? AND status = 'active'",
            (int(telegram_id),),
        ).fetchone()

    @staticmethod
    def control_group(connection: Any) -> Any:
        return connection.execute(
            "SELECT control_group_id, title, bound_by, bound_at, source FROM staff_control_group WHERE id = 1"
        ).fetchone()

    @staticmethod
    def bind_control_group(connection: Any, **values: Any) -> None:
        connection.execute(
            """INSERT INTO staff_control_group
               (id, control_group_id, title, bound_by, bound_at, source)
               VALUES (1, ?, ?, ?, ?, 'telegram_chat_shared')
               ON CONFLICT(id) DO UPDATE SET
                 control_group_id = excluded.control_group_id,
                 title = excluded.title,
                 bound_by = excluded.bound_by,
                 bound_at = excluded.bound_at,
                 source = excluded.source""",
            (values["group_id"], values["title"], values["owner_id"], values["bound_at"]),
        )

    @staticmethod
    def owner(connection: Any) -> Any:
        return connection.execute(
            "SELECT telegram_id FROM staff_accounts WHERE role = 'owner' AND status = 'active'"
        ).fetchone()

    @staticmethod
    def active_staff_ids(connection: Any) -> list[Any]:
        return connection.execute(
            "SELECT telegram_id FROM staff_accounts WHERE status = 'active'"
        ).fetchall()

    @staticmethod
    def list_staff(connection: Any) -> list[Any]:
        return connection.execute(
            """SELECT s.*, COALESCE(NULLIF(s.display_name, ''), u.first_name, '') AS effective_name,
                      COALESCE(NULLIF(s.username, ''), u.username) AS effective_username
               FROM staff_accounts s LEFT JOIN users u ON u.telegram_id = s.telegram_id
               WHERE s.status = 'active'
               ORDER BY CASE WHEN s.role = 'owner' THEN 0 ELSE 1 END,
                        s.added_at, s.telegram_id"""
        ).fetchall()

    @staticmethod
    def notification_preferences(connection: Any, telegram_id: int) -> list[Any]:
        return connection.execute(
            """SELECT event_type, enabled FROM staff_notification_preferences
               WHERE telegram_id = ?""",
            (int(telegram_id),),
        ).fetchall()

    @staticmethod
    def set_notification_preference(
        connection: Any, telegram_id: int, event_type: str, enabled: bool, now_text: str
    ) -> None:
        connection.execute(
            """INSERT INTO staff_notification_preferences
               (telegram_id, event_type, enabled, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(telegram_id, event_type) DO UPDATE SET
                 enabled = excluded.enabled, updated_at = excluded.updated_at""",
            (int(telegram_id), event_type, 1 if enabled else 0, now_text),
        )

    @staticmethod
    def user(connection: Any, telegram_id: int) -> Any:
        return connection.execute(
            "SELECT first_name, username FROM users WHERE telegram_id = ?", (int(telegram_id),)
        ).fetchone()

    @staticmethod
    def revoke_admin(connection: Any, telegram_id: int, owner_id: int, now_text: str) -> Any:
        return connection.execute(
            """UPDATE staff_accounts
               SET status = 'revoked', revoked_by = ?, revoked_at = ?,
                   access_version = access_version + 1
               WHERE telegram_id = ? AND role = 'admin' AND status = 'active'""",
            (int(owner_id), now_text, int(telegram_id)),
        )

    @staticmethod
    def cancel_challenges(connection: Any, telegram_id: int, now_text: str) -> None:
        connection.execute(
            """UPDATE admin_action_challenges SET status = 'cancelled', cancelled_at = ?
               WHERE admin_id = ? AND status = 'pending'""",
            (now_text, int(telegram_id)),
        )

    @staticmethod
    def insert_sync_run(connection: Any, **values: Any) -> None:
        connection.execute(
            """INSERT INTO staff_sync_runs
               (id, control_group_id, requested_by, source, status, snapshot_json, created_at)
               VALUES (?, ?, ?, 'telegram_group', 'previewed', ?, ?)""",
            (values["run_id"], int(values["control_group_id"]), int(values["owner_id"]), json.dumps(values["snapshot"]), values["now_text"]),
        )
