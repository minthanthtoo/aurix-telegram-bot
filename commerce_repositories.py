"""Compatibility facade for SQLite and PostgreSQL commerce databases."""

from commerce_postgres_database import (
    PostgresCommerceDatabase,
    _PostgresConnection,
)
from commerce_sqlite_database import CommerceDatabase

__all__ = ["CommerceDatabase", "PostgresCommerceDatabase", "_PostgresConnection"]
