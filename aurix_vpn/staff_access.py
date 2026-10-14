"""Durable staff roles, scoped customer assignments, and access audit."""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Iterable

from migrations import Migration, apply_migrations

from .commerce_repositories import PostgresCommerceDatabase


UTC = timezone.utc
MAX_TELEGRAM_ID = 9_223_372_036_854_775_807
ROLES = frozenset({"seller", "admin", "owner"})
CAPABILITIES = frozenset(
    {
        "sales.read",
        "sales.decide",
        "recovery.manage",
        "fleet.read",
        "fleet.manage",
        "accounts.read",
        "accounts.manage",
        "promotions.manage",
        "wallet.read",
        "sales.assigned.read",
    }
)
ADMIN_CAPABILITIES = CAPABILITIES - {"sales.assigned.read"}
DEFAULT_CAPABILITIES = {
    "seller": frozenset({"sales.assigned.read"}),
    "admin": frozenset(ADMIN_CAPABILITIES),
    "owner": frozenset(CAPABILITIES),
}


class StaffAccessError(PermissionError):
    """Raised when a staff action is outside the actor's current authority."""


class StaffAccessConfigurationError(RuntimeError):
    """Raised when role mode cannot establish an unambiguous Owner."""


STAFF_ACCESS_MIGRATIONS = (
    Migration(
        1,
        "durable_staff_roles_scopes_and_audit",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS staff_memberships (
                   telegram_id INTEGER PRIMARY KEY CHECK (telegram_id > 0),
                   role TEXT NOT NULL CHECK (role IN ('seller', 'admin', 'owner')),
                   active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                   granted_by INTEGER,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   revoked_at TEXT
               )""",
            "CREATE INDEX IF NOT EXISTS staff_memberships_active ON staff_memberships(active, role, telegram_id)",
            """CREATE TABLE IF NOT EXISTS staff_customer_assignments (
                   assignment_id TEXT PRIMARY KEY,
                   seller_telegram_id INTEGER NOT NULL REFERENCES staff_memberships(telegram_id),
                   customer_telegram_id INTEGER NOT NULL CHECK (customer_telegram_id > 0),
                   assigned_by INTEGER NOT NULL,
                   assigned_at TEXT NOT NULL,
                   active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                   revoked_at TEXT
               )""",
            "CREATE UNIQUE INDEX IF NOT EXISTS staff_customer_one_active_seller ON staff_customer_assignments(customer_telegram_id) WHERE active = 1",
            "CREATE INDEX IF NOT EXISTS staff_customer_seller_active ON staff_customer_assignments(seller_telegram_id, active, customer_telegram_id)",
            """CREATE TABLE IF NOT EXISTS staff_capability_overrides (
                   telegram_id INTEGER NOT NULL REFERENCES staff_memberships(telegram_id),
                   capability TEXT NOT NULL,
                   effect TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
                   changed_by INTEGER NOT NULL,
                   changed_at TEXT NOT NULL,
                   PRIMARY KEY (telegram_id, capability)
               )""",
            """CREATE TABLE IF NOT EXISTS staff_access_audit (
                   audit_id TEXT PRIMARY KEY,
                   actor_id INTEGER NOT NULL,
                   target_id INTEGER,
                   action TEXT NOT NULL,
                   entity_type TEXT NOT NULL,
                   entity_id TEXT,
                   details_json TEXT NOT NULL DEFAULT '{}',
                   created_at TEXT NOT NULL
               )""",
            "CREATE INDEX IF NOT EXISTS staff_access_audit_created ON staff_access_audit(created_at, audit_id)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS staff_memberships (
                   telegram_id BIGINT PRIMARY KEY CHECK (telegram_id > 0),
                   role TEXT NOT NULL CHECK (role IN ('seller', 'admin', 'owner')),
                   active SMALLINT NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                   granted_by BIGINT,
                   created_at TIMESTAMPTZ NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL,
                   revoked_at TIMESTAMPTZ
               )""",
            "CREATE INDEX IF NOT EXISTS staff_memberships_active ON staff_memberships(active, role, telegram_id)",
            """CREATE TABLE IF NOT EXISTS staff_customer_assignments (
                   assignment_id TEXT PRIMARY KEY,
                   seller_telegram_id BIGINT NOT NULL REFERENCES staff_memberships(telegram_id),
                   customer_telegram_id BIGINT NOT NULL CHECK (customer_telegram_id > 0),
                   assigned_by BIGINT NOT NULL,
                   assigned_at TIMESTAMPTZ NOT NULL,
                   active SMALLINT NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                   revoked_at TIMESTAMPTZ
               )""",
            "CREATE UNIQUE INDEX IF NOT EXISTS staff_customer_one_active_seller ON staff_customer_assignments(customer_telegram_id) WHERE active = 1",
            "CREATE INDEX IF NOT EXISTS staff_customer_seller_active ON staff_customer_assignments(seller_telegram_id, active, customer_telegram_id)",
            """CREATE TABLE IF NOT EXISTS staff_capability_overrides (
                   telegram_id BIGINT NOT NULL REFERENCES staff_memberships(telegram_id),
                   capability TEXT NOT NULL,
                   effect TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
                   changed_by BIGINT NOT NULL,
                   changed_at TIMESTAMPTZ NOT NULL,
                   PRIMARY KEY (telegram_id, capability)
               )""",
            """CREATE TABLE IF NOT EXISTS staff_access_audit (
                   audit_id TEXT PRIMARY KEY,
                   actor_id BIGINT NOT NULL,
                   target_id BIGINT,
                   action TEXT NOT NULL,
                   entity_type TEXT NOT NULL,
                   entity_id TEXT,
                   details_json TEXT NOT NULL DEFAULT '{}',
                   created_at TIMESTAMPTZ NOT NULL
               )""",
            "CREATE INDEX IF NOT EXISTS staff_access_audit_created ON staff_access_audit(created_at, audit_id)",
        ),
    ),
)


def _parse_id_set(value: str | None, name: str) -> set[int]:
    result: set[int] = set()
    try:
        for raw in (value or "").split(","):
            raw = raw.strip()
            if not raw:
                continue
            telegram_id = int(raw)
            if telegram_id <= 0 or telegram_id > MAX_TELEGRAM_ID:
                raise ValueError
            result.add(telegram_id)
    except ValueError as exc:
        raise StaffAccessConfigurationError(
            f"{name} must be comma-separated positive Telegram numeric IDs"
        ) from exc
    return result


def _now_text() -> str:
    return datetime.now(UTC).isoformat()


class StaffAccessService:
    """Database-backed staff policy checked at each privileged action."""

    ROLES = ROLES
    CAPABILITIES = CAPABILITIES
    ADMIN_CAPABILITIES = ADMIN_CAPABILITIES

    def __init__(
        self,
        database: Any | None,
        *,
        admin_ids: Iterable[int] = (),
        owner_ids: Iterable[int] = (),
        seller_ids: Iterable[int] = (),
        mode: str = "legacy",
    ):
        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in {"legacy", "roles"}:
            raise StaffAccessConfigurationError(
                "AURIX_STAFF_AUTHORIZATION_MODE must be legacy or roles"
            )
        self.database = database
        self.admin_ids = frozenset(
            int(value) for value in admin_ids if 0 < int(value) <= MAX_TELEGRAM_ID
        )
        self.owner_ids = frozenset(
            int(value) for value in owner_ids if 0 < int(value) <= MAX_TELEGRAM_ID
        )
        self.seller_ids = frozenset(
            int(value) for value in seller_ids if 0 < int(value) <= MAX_TELEGRAM_ID
        )
        self.mode = normalized_mode
        self.enabled = normalized_mode == "roles"
        if self.enabled and database is None:
            raise StaffAccessConfigurationError(
                "Role authorization requires the durable commerce database"
            )
        if self.seller_ids & self.admin_ids:
            raise StaffAccessConfigurationError(
                "A Telegram ID cannot be configured as both Seller and Admin"
            )
        if self.seller_ids & self.owner_ids:
            raise StaffAccessConfigurationError(
                "A Telegram ID cannot be configured as both Seller and Owner"
            )
        self._initialized = False

    @classmethod
    def from_environment(
        cls, database: Any | None, admin_ids: Iterable[int]
    ) -> "StaffAccessService | None":
        owner_ids = _parse_id_set(
            os.environ.get("OWNER_TELEGRAM_IDS"), "OWNER_TELEGRAM_IDS"
        )
        seller_ids = _parse_id_set(
            os.environ.get("SELLER_TELEGRAM_IDS"), "SELLER_TELEGRAM_IDS"
        )
        explicit_mode = (
            os.environ.get("AURIX_STAFF_AUTHORIZATION_MODE", "").strip().lower()
        )
        if explicit_mode:
            mode = explicit_mode
        else:
            mode = "roles" if owner_ids else "legacy"
        if seller_ids and mode != "roles":
            raise StaffAccessConfigurationError(
                "SELLER_TELEGRAM_IDS requires role authorization and an Owner"
            )
        if owner_ids and mode != "roles":
            raise StaffAccessConfigurationError(
                "OWNER_TELEGRAM_IDS requires AURIX_STAFF_AUTHORIZATION_MODE=roles"
            )
        if mode == "legacy" and not owner_ids and not seller_ids:
            return None
        access = cls(
            database,
            admin_ids=admin_ids,
            owner_ids=owner_ids,
            seller_ids=seller_ids,
            mode=mode,
        )
        if access.enabled:
            access.initialize()
        return access

    @property
    def dialect(self) -> str:
        return (
            "postgres"
            if isinstance(self.database, PostgresCommerceDatabase)
            else "sqlite"
        )

    def initialize(self) -> None:
        if not self.enabled or self._initialized:
            return
        with self.database.connect() as connection:
            apply_migrations(
                connection,
                component="staff_access",
                dialect=self.dialect,
                migrations=STAFF_ACCESS_MIGRATIONS,
            )
        self._bootstrap_environment_memberships()
        with self.database.connect() as connection:
            owners = connection.execute(
                "SELECT COUNT(*) AS n FROM staff_memberships WHERE role = 'owner' AND active = 1"
            ).fetchone()
            if int(owners["n"] if hasattr(owners, "keys") else owners[0]) < 1:
                raise StaffAccessConfigurationError(
                    "Role authorization requires at least one configured or persisted Owner"
                )
        self._initialized = True

    def _lock_policy(self, connection: Any) -> None:
        if self.dialect == "postgres":
            connection.execute("SELECT pg_advisory_xact_lock(731980220041)")

    @staticmethod
    def _row_dict(row: Any) -> dict[str, Any]:
        return dict(row) if row is not None else {}

    def _bootstrap_environment_memberships(self) -> None:
        now = _now_text()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_policy(connection)
            for telegram_id in sorted(self.owner_ids):
                previous = connection.execute(
                    "SELECT role, active FROM staff_memberships WHERE telegram_id = ?",
                    (telegram_id,),
                ).fetchone()
                previous_role = (
                    str(previous["role"] if hasattr(previous, "keys") else previous[0])
                    if previous
                    else None
                )
                previous_active = (
                    int(
                        previous["active"] if hasattr(previous, "keys") else previous[1]
                    )
                    if previous
                    else 0
                )
                connection.execute(
                    """INSERT INTO staff_memberships
                       (telegram_id, role, active, granted_by, created_at, updated_at, revoked_at)
                       VALUES (?, 'owner', 1, NULL, ?, ?, NULL)
                       ON CONFLICT(telegram_id) DO UPDATE SET
                         role = 'owner', active = 1, updated_at = excluded.updated_at,
                         revoked_at = NULL""",
                    (telegram_id, now, now),
                )
                if previous_role != "owner" or previous_active != 1:
                    self._audit(
                        connection,
                        0,
                        telegram_id,
                        "environment_role_configured",
                        "staff_member",
                        str(telegram_id),
                        {"role": "owner"},
                        now,
                    )
            for telegram_id in sorted(self.admin_ids - self.owner_ids):
                previous = connection.execute(
                    "SELECT role, active FROM staff_memberships WHERE telegram_id = ?",
                    (telegram_id,),
                ).fetchone()
                previous_role = (
                    str(previous["role"] if hasattr(previous, "keys") else previous[0])
                    if previous
                    else None
                )
                previous_active = (
                    int(
                        previous["active"] if hasattr(previous, "keys") else previous[1]
                    )
                    if previous
                    else 0
                )
                connection.execute(
                    """INSERT INTO staff_memberships
                       (telegram_id, role, active, granted_by, created_at, updated_at, revoked_at)
                       VALUES (?, 'admin', 1, NULL, ?, ?, NULL)
                       ON CONFLICT(telegram_id) DO UPDATE SET
                         role = CASE WHEN staff_memberships.role = 'owner'
                                     THEN 'owner' ELSE 'admin' END,
                         active = 1, updated_at = excluded.updated_at, revoked_at = NULL""",
                    (telegram_id, now, now),
                )
                if previous_role != "admin" or previous_active != 1:
                    self._audit(
                        connection,
                        0,
                        telegram_id,
                        "environment_role_configured",
                        "staff_member",
                        str(telegram_id),
                        {"role": "admin"},
                        now,
                    )
            for telegram_id in sorted(self.seller_ids):
                previous = connection.execute(
                    "SELECT role, active FROM staff_memberships WHERE telegram_id = ?",
                    (telegram_id,),
                ).fetchone()
                previous_role = (
                    str(previous["role"] if hasattr(previous, "keys") else previous[0])
                    if previous
                    else None
                )
                connection.execute(
                    """INSERT INTO staff_memberships
                       (telegram_id, role, active, granted_by, created_at, updated_at, revoked_at)
                       VALUES (?, 'seller', 1, NULL, ?, ?, NULL)
                       ON CONFLICT(telegram_id) DO NOTHING""",
                    (telegram_id, now, now),
                )
                if previous is None:
                    self._audit(
                        connection,
                        0,
                        telegram_id,
                        "environment_role_configured",
                        "staff_member",
                        str(telegram_id),
                        {"role": "seller"},
                        now,
                    )

    def _role_row(
        self, telegram_id: int, connection: Any | None = None
    ) -> dict[str, Any]:
        if not self.enabled or self.database is None:
            if int(telegram_id) in self.admin_ids:
                return {"telegram_id": int(telegram_id), "role": "admin", "active": 1}
            return {}
        if connection is None:
            with self.database.connect() as own_connection:
                return self._role_row(telegram_id, own_connection)
        row = connection.execute(
            "SELECT telegram_id, role, active FROM staff_memberships WHERE telegram_id = ?",
            (int(telegram_id),),
        ).fetchone()
        result = self._row_dict(row)
        if result and int(result.get("active") or 0) != 1:
            return {}
        return result

    def role_for(self, telegram_id: int) -> str | None:
        row = self._role_row(int(telegram_id))
        role = str(row.get("role") or "")
        return role if role in ROLES else None

    def is_staff(self, telegram_id: int) -> bool:
        return self.role_for(telegram_id) in ROLES

    def is_admin(self, telegram_id: int) -> bool:
        return self.role_for(telegram_id) in {"admin", "owner"}

    def is_owner(self, telegram_id: int) -> bool:
        return self.role_for(telegram_id) == "owner"

    def has_capability(self, telegram_id: int, capability: str) -> bool:
        if capability not in CAPABILITIES:
            return False
        row = self._role_row(int(telegram_id))
        role = str(row.get("role") or "")
        if role not in ROLES:
            return False
        if role == "owner":
            return True
        allowed = capability in DEFAULT_CAPABILITIES[role]
        with self.database.connect() as connection:
            override = connection.execute(
                "SELECT effect FROM staff_capability_overrides WHERE telegram_id = ? AND capability = ?",
                (int(telegram_id), capability),
            ).fetchone()
        if override is not None:
            effect = str(
                override["effect"] if hasattr(override, "keys") else override[0]
            )
            allowed = effect == "allow"
        # Global commerce operations stay unavailable to Sellers even when an
        # override row was written by an older or manually edited database.
        if role == "seller" and capability != "sales.assigned.read":
            return False
        return allowed

    def require_admin(self, telegram_id: int) -> None:
        if not self.is_admin(telegram_id):
            raise StaffAccessError("administrator access required")

    def require_owner(self, telegram_id: int) -> None:
        if not self.is_owner(telegram_id):
            raise StaffAccessError("Owner access required")

    def require_capability(self, telegram_id: int, capability: str) -> None:
        if not self.has_capability(telegram_id, capability):
            raise StaffAccessError("This staff action is not available to your role")

    def can_view_customer_case(self, telegram_id: int, customer_id: int) -> bool:
        role = self.role_for(telegram_id)
        if role in {"admin", "owner"}:
            return True
        if role != "seller" or not self.has_capability(
            telegram_id, "sales.assigned.read"
        ):
            return False
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM staff_customer_assignments
                    WHERE seller_telegram_id = ? AND customer_telegram_id = ? AND active = 1""",
                (int(telegram_id), int(customer_id)),
            ).fetchone()
        return row is not None

    def require_customer_case(self, telegram_id: int, customer_id: int) -> None:
        if not self.can_view_customer_case(telegram_id, customer_id):
            raise StaffAccessError("This customer case is not assigned to you")

    def require_configurable_role(
        self, actor_id: int, target_id: int, role: str
    ) -> None:
        if role not in ROLES:
            raise ValueError("Role must be seller, admin, or owner")
        if target_id <= 0 or target_id > MAX_TELEGRAM_ID:
            raise ValueError("Telegram ID must be a positive number")
        if target_id in self.owner_ids and role != "owner":
            raise StaffAccessError(
                "This Owner is configured in the deployment environment"
            )
        if target_id in self.admin_ids and role == "seller":
            raise StaffAccessError(
                "Remove this ID from ADMIN_TELEGRAM_IDS before assigning Seller"
            )
        if target_id in self.seller_ids and role != "seller":
            raise StaffAccessError(
                "This Seller is configured in the deployment environment"
            )

    def _audit(
        self,
        connection: Any,
        actor_id: int,
        target_id: int | None,
        action: str,
        entity_type: str,
        entity_id: str | None,
        details: dict[str, Any],
        now: str,
    ) -> None:
        connection.execute(
            """INSERT INTO staff_access_audit
               (audit_id, actor_id, target_id, action, entity_type, entity_id,
                details_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                secrets.token_hex(12),
                int(actor_id),
                int(target_id) if target_id is not None else None,
                action,
                entity_type,
                entity_id,
                json.dumps(details, sort_keys=True, separators=(",", ":")),
                now,
            ),
        )

    def grant_role(self, actor_id: int, target_id: int, role: str) -> bool:
        self.require_owner(actor_id)
        target_id, actor_id = int(target_id), int(actor_id)
        self.require_configurable_role(actor_id, target_id, role)
        now = _now_text()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_policy(connection)
            self.require_owner(actor_id)
            existing = connection.execute(
                "SELECT role, active FROM staff_memberships WHERE telegram_id = ?",
                (target_id,),
            ).fetchone()
            before = (
                str(existing["role"] if hasattr(existing, "keys") else existing[0])
                if existing
                else None
            )
            active = (
                int(existing["active"] if hasattr(existing, "keys") else existing[1])
                if existing
                else 0
            )
            if before == role and active == 1:
                return False
            if before == "owner" and active == 1 and role != "owner":
                owners = connection.execute(
                    "SELECT COUNT(*) AS n FROM staff_memberships WHERE role = 'owner' AND active = 1"
                ).fetchone()
                owner_count = int(owners["n"] if hasattr(owners, "keys") else owners[0])
                if owner_count <= 1:
                    raise StaffAccessError("The last active Owner cannot be demoted")
            connection.execute(
                """INSERT INTO staff_memberships
                   (telegram_id, role, active, granted_by, created_at, updated_at, revoked_at)
                   VALUES (?, ?, 1, ?, ?, ?, NULL)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                     role = excluded.role, active = 1, granted_by = excluded.granted_by,
                     updated_at = excluded.updated_at, revoked_at = NULL""",
                (target_id, role, actor_id, now, now),
            )
            if role != "admin":
                connection.execute(
                    "DELETE FROM staff_capability_overrides WHERE telegram_id = ?",
                    (target_id,),
                )
            self._audit(
                connection,
                actor_id,
                target_id,
                "role_granted",
                "staff_member",
                str(target_id),
                {"from": before, "to": role},
                now,
            )
            return True

    def revoke_role(self, actor_id: int, target_id: int) -> bool:
        self.require_owner(actor_id)
        target_id, actor_id = int(target_id), int(actor_id)
        if target_id in self.owner_ids:
            raise StaffAccessError(
                "The deployment-configured Owner cannot be revoked here"
            )
        if target_id in self.admin_ids:
            raise StaffAccessError(
                "Remove this ID from ADMIN_TELEGRAM_IDS before revoking access"
            )
        if target_id in self.seller_ids:
            raise StaffAccessError(
                "Remove this ID from SELLER_TELEGRAM_IDS before revoking access"
            )
        now = _now_text()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_policy(connection)
            self.require_owner(actor_id)
            current = connection.execute(
                "SELECT role, active FROM staff_memberships WHERE telegram_id = ?",
                (target_id,),
            ).fetchone()
            if (
                current is None
                or int(current["active"] if hasattr(current, "keys") else current[1])
                != 1
            ):
                return False
            role = str(current["role"] if hasattr(current, "keys") else current[0])
            if role == "owner":
                owners = connection.execute(
                    "SELECT COUNT(*) AS n FROM staff_memberships WHERE role = 'owner' AND active = 1"
                ).fetchone()
                owner_count = int(owners["n"] if hasattr(owners, "keys") else owners[0])
                if owner_count <= 1:
                    raise StaffAccessError("The last active Owner cannot be revoked")
            connection.execute(
                "UPDATE staff_memberships SET active = 0, revoked_at = ?, updated_at = ? WHERE telegram_id = ? AND active = 1",
                (now, now, target_id),
            )
            connection.execute(
                "UPDATE staff_customer_assignments SET active = 0, revoked_at = ? WHERE seller_telegram_id = ? AND active = 1",
                (now, target_id),
            )
            connection.execute(
                "DELETE FROM staff_capability_overrides WHERE telegram_id = ?",
                (target_id,),
            )
            self._audit(
                connection,
                actor_id,
                target_id,
                "role_revoked",
                "staff_member",
                str(target_id),
                {"role": role},
                now,
            )
            return True

    def set_capability(
        self,
        actor_id: int,
        target_id: int,
        capability: str,
        effect: str | None,
    ) -> bool:
        self.require_owner(actor_id)
        target_id, actor_id = int(target_id), int(actor_id)
        if capability not in ADMIN_CAPABILITIES:
            raise ValueError("Capability is not configurable")
        if effect not in {"allow", "deny", None}:
            raise ValueError("Capability effect must be allow, deny, or inherit")
        now = _now_text()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_policy(connection)
            self.require_owner(actor_id)
            member = connection.execute(
                "SELECT role, active FROM staff_memberships WHERE telegram_id = ?",
                (target_id,),
            ).fetchone()
            if (
                member is None
                or int(member["active"] if hasattr(member, "keys") else member[1]) != 1
            ):
                raise ValueError("Active staff member not found")
            role = str(member["role"] if hasattr(member, "keys") else member[0])
            if role != "admin":
                raise ValueError(
                    "Operational capability overrides apply only to Admins"
                )
            if effect is None:
                connection.execute(
                    "DELETE FROM staff_capability_overrides WHERE telegram_id = ? AND capability = ?",
                    (target_id, capability),
                )
            else:
                connection.execute(
                    """INSERT INTO staff_capability_overrides
                       (telegram_id, capability, effect, changed_by, changed_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(telegram_id, capability) DO UPDATE SET
                         effect = excluded.effect, changed_by = excluded.changed_by,
                         changed_at = excluded.changed_at""",
                    (target_id, capability, effect, actor_id, now),
                )
            self._audit(
                connection,
                actor_id,
                target_id,
                "capability_changed",
                "staff_capability",
                capability,
                {"effect": effect, "role": role},
                now,
            )
            return True

    def capability_overrides(self, actor_id: int, target_id: int) -> dict[str, str]:
        self.require_owner(actor_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT capability, effect FROM staff_capability_overrides WHERE telegram_id = ? ORDER BY capability",
                (int(target_id),),
            ).fetchall()
        return {
            str(row["capability"] if hasattr(row, "keys") else row[0]): str(
                row["effect"] if hasattr(row, "keys") else row[1]
            )
            for row in rows
        }

    def assign_customer(self, actor_id: int, seller_id: int, customer_id: int) -> bool:
        self.require_owner(actor_id)
        actor_id, seller_id, customer_id = (
            int(actor_id),
            int(seller_id),
            int(customer_id),
        )
        if min(seller_id, customer_id) <= 0:
            raise ValueError("Seller and customer IDs must be positive numbers")
        now = _now_text()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_policy(connection)
            self.require_owner(actor_id)
            seller = connection.execute(
                "SELECT role, active FROM staff_memberships WHERE telegram_id = ?",
                (seller_id,),
            ).fetchone()
            if (
                seller is None
                or str(seller["role"] if hasattr(seller, "keys") else seller[0])
                != "seller"
                or int(seller["active"] if hasattr(seller, "keys") else seller[1]) != 1
            ):
                raise ValueError("An active Seller must be selected")
            current = connection.execute(
                """SELECT assignment_id, seller_telegram_id FROM staff_customer_assignments
                    WHERE customer_telegram_id = ? AND active = 1""",
                (customer_id,),
            ).fetchone()
            if (
                current is not None
                and int(
                    current["seller_telegram_id"]
                    if hasattr(current, "keys")
                    else current[1]
                )
                == seller_id
            ):
                return False
            if current is not None:
                old_seller = int(
                    current["seller_telegram_id"]
                    if hasattr(current, "keys")
                    else current[1]
                )
                connection.execute(
                    "UPDATE staff_customer_assignments SET active = 0, revoked_at = ? WHERE assignment_id = ?",
                    (
                        now,
                        str(
                            current["assignment_id"]
                            if hasattr(current, "keys")
                            else current[0]
                        ),
                    ),
                )
            else:
                old_seller = None
            connection.execute(
                """INSERT INTO staff_customer_assignments
                   (assignment_id, seller_telegram_id, customer_telegram_id, assigned_by,
                    assigned_at, active, revoked_at)
                   VALUES (?, ?, ?, ?, ?, 1, NULL)""",
                (secrets.token_hex(12), seller_id, customer_id, actor_id, now),
            )
            self._audit(
                connection,
                actor_id,
                customer_id,
                "customer_reassigned"
                if old_seller is not None
                else "customer_assigned",
                "customer_case",
                str(customer_id),
                {"from_seller": old_seller, "to_seller": seller_id},
                now,
            )
            return True

    def unassign_customer(
        self, actor_id: int, seller_id: int, customer_id: int
    ) -> bool:
        self.require_owner(actor_id)
        actor_id, seller_id, customer_id = (
            int(actor_id),
            int(seller_id),
            int(customer_id),
        )
        now = _now_text()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_policy(connection)
            self.require_owner(actor_id)
            row = connection.execute(
                """SELECT assignment_id FROM staff_customer_assignments
                    WHERE seller_telegram_id = ? AND customer_telegram_id = ? AND active = 1""",
                (seller_id, customer_id),
            ).fetchone()
            if row is None:
                return False
            assignment_id = str(
                row["assignment_id"] if hasattr(row, "keys") else row[0]
            )
            connection.execute(
                "UPDATE staff_customer_assignments SET active = 0, revoked_at = ? WHERE assignment_id = ?",
                (now, assignment_id),
            )
            self._audit(
                connection,
                actor_id,
                customer_id,
                "customer_unassigned",
                "customer_case",
                str(customer_id),
                {"seller_id": seller_id},
                now,
            )
            return True

    def assigned_customers(self, seller_id: int) -> list[dict[str, Any]]:
        if self.role_for(seller_id) != "seller":
            raise StaffAccessError("Seller access required")
        self.require_capability(int(seller_id), "sales.assigned.read")
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT customer_telegram_id, assigned_at FROM staff_customer_assignments
                    WHERE seller_telegram_id = ? AND active = 1
                    ORDER BY assigned_at DESC, customer_telegram_id""",
                (int(seller_id),),
            ).fetchall()
        return [
            {
                "customer_id": int(
                    row["customer_telegram_id"] if hasattr(row, "keys") else row[0]
                ),
                "assigned_at": str(
                    row["assigned_at"] if hasattr(row, "keys") else row[1]
                ),
            }
            for row in rows
        ]

    def assignments(self, actor_id: int) -> list[dict[str, Any]]:
        self.require_owner(actor_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT a.seller_telegram_id, a.customer_telegram_id, a.assigned_by,
                          a.assigned_at
                     FROM staff_customer_assignments a
                    WHERE a.active = 1
                    ORDER BY a.seller_telegram_id, a.assigned_at DESC"""
            ).fetchall()
        return [
            {
                key: row[key] if hasattr(row, "keys") else row[index]
                for index, key in enumerate(
                    (
                        "seller_telegram_id",
                        "customer_telegram_id",
                        "assigned_by",
                        "assigned_at",
                    )
                )
            }
            for row in rows
        ]

    def audit_events(
        self, actor_id: int, *, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        self.require_owner(actor_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT actor_id, target_id, action, entity_type, entity_id,
                          details_json, created_at
                     FROM staff_access_audit
                    ORDER BY created_at DESC, audit_id DESC LIMIT ? OFFSET ?""",
                (max(1, min(int(limit), 100)), max(0, int(offset))),
            ).fetchall()
        return [self._row_dict(row) for row in rows]

    def staff_members(self, actor_id: int) -> list[dict[str, Any]]:
        self.require_owner(actor_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT telegram_id, role, active, created_at, updated_at, revoked_at
                     FROM staff_memberships
                    ORDER BY active DESC, CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,
                             telegram_id"""
            ).fetchall()
        return [self._row_dict(row) for row in rows]

    def configured_role(self, telegram_id: int) -> str | None:
        value = int(telegram_id)
        if value in self.owner_ids:
            return "owner"
        if value in self.admin_ids:
            return "admin"
        if value in self.seller_ids:
            return "seller"
        return None
