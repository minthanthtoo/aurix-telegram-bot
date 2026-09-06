"""Dialect-specific commerce schema bootstrap and upgrade entry points."""

from __future__ import annotations

from migrations import COMMERCE_MIGRATIONS, FREE_ACCESS_MIGRATIONS
from commerce_schema_base_migrations import COMMERCE_BASE_MIGRATIONS
from commerce_schema_compatibility_migrations import (
    COMMERCE_SCHEMA_COMPATIBILITY_MIGRATIONS,
)
from schema_migrations import apply_migrations


def initialize_sqlite(self) -> None:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    with self.connect() as connection:
        apply_migrations(
            connection,
            component="commerce_base",
            dialect="sqlite",
            migrations=COMMERCE_BASE_MIGRATIONS,
        )
        apply_migrations(
            connection,
            component="commerce_compatibility",
            dialect="sqlite",
            migrations=COMMERCE_SCHEMA_COMPATIBILITY_MIGRATIONS,
        )
        self._seed_plans(connection)
        apply_migrations(
            connection,
            component="commerce",
            dialect="sqlite",
            migrations=COMMERCE_MIGRATIONS,
        )


def initialize_postgres(self) -> None:
    with self.connect() as connection:
        apply_migrations(
            connection,
            component="commerce_base",
            dialect="postgres",
            migrations=COMMERCE_BASE_MIGRATIONS,
        )
        apply_migrations(
            connection,
            component="commerce_compatibility",
            dialect="postgres",
            migrations=COMMERCE_SCHEMA_COMPATIBILITY_MIGRATIONS,
        )
        self._seed_plans(connection)
        apply_migrations(
            connection,
            component="free_access",
            dialect="postgres",
            migrations=FREE_ACCESS_MIGRATIONS,
        )
        apply_migrations(
            connection,
            component="commerce",
            dialect="postgres",
            migrations=COMMERCE_MIGRATIONS,
        )
