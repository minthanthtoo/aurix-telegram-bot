"""Numbered, component-scoped database migration registry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


UTC = timezone.utc


class MigrationError(RuntimeError):
    """Raised when recorded migration history disagrees with the code registry."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sqlite_statements: tuple[str, ...] = ()
    postgres_statements: tuple[str, ...] = ()

    def statements_for(self, dialect: str) -> tuple[str, ...]:
        if dialect == "sqlite":
            return self.sqlite_statements
        if dialect == "postgres":
            return self.postgres_statements
        raise MigrationError(f"Unsupported migration dialect: {dialect}")


FREE_ACCESS_MIGRATIONS = (
    Migration(1, "legacy_free_access_schema"),
)

COMMERCE_MIGRATIONS = (
    Migration(1, "legacy_commerce_schema"),
)


def apply_migrations(
    connection: Any,
    *,
    component: str,
    dialect: str,
    migrations: Iterable[Migration],
    applied_at: str | None = None,
) -> None:
    """Apply missing migrations and validate immutable version/name history.

    Phase 2 adopts the existing schema as version 1 for each component. Future
    schema changes belong in this registry and must use idempotent statements.
    """
    connection.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               component TEXT NOT NULL,
               version INTEGER NOT NULL,
               name TEXT NOT NULL,
               applied_at TEXT NOT NULL,
               PRIMARY KEY (component, version)
           )"""
    )
    rows = connection.execute(
        "SELECT version, name FROM schema_migrations WHERE component = ?",
        (component,),
    ).fetchall()
    recorded = {
        int(row["version"] if hasattr(row, "keys") else row[0]): str(
            row["name"] if hasattr(row, "keys") else row[1]
        )
        for row in rows
    }
    ordered = sorted(tuple(migrations), key=lambda migration: migration.version)
    if len({migration.version for migration in ordered}) != len(ordered):
        raise MigrationError(f"Duplicate migration version for {component}")
    if any(migration.version <= 0 for migration in ordered):
        raise MigrationError(f"Migration versions for {component} must be positive")
    known_versions = {migration.version for migration in ordered}
    unknown_versions = sorted(set(recorded) - known_versions)
    if unknown_versions:
        versions = ", ".join(str(version) for version in unknown_versions)
        raise MigrationError(
            f"Database has unknown {component} migration version(s): {versions}"
        )
    timestamp = applied_at or datetime.now(UTC).isoformat()
    for migration in ordered:
        existing_name = recorded.get(migration.version)
        if existing_name is not None:
            if existing_name != migration.name:
                raise MigrationError(
                    f"Migration {component}:{migration.version} was renamed "
                    f"from {existing_name!r} to {migration.name!r}"
                )
            continue
        for statement in migration.statements_for(dialect):
            connection.execute(statement)
        connection.execute(
            """INSERT INTO schema_migrations
               (component, version, name, applied_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(component, version) DO NOTHING""",
            (component, migration.version, migration.name, timestamp),
        )
