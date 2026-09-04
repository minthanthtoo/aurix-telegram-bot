from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from commerce_repositories import CommerceDatabase
from deploy.migrate_sqlite_to_postgres import (
    FleetError,
    copy_database,
    dependency_order,
    ensure_source,
    normalize_value,
    quote_identifier,
)


class SQLiteToPostgresMigrationTests(unittest.TestCase):
    def make_database(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        path = Path(temporary.name) / "bot.db"
        CommerceDatabase(path).initialize()
        return temporary, path

    def test_identifier_is_strictly_quoted(self) -> None:
        self.assertEqual(quote_identifier("orders"), '"orders"')
        with self.assertRaises(FleetError):
            quote_identifier('orders"; DROP TABLE users;--')

    def test_dependency_order_places_parents_before_children(self) -> None:
        temporary, path = self.make_database()
        try:
            with ensure_source(path) as connection:
                order = dependency_order(connection, ["orders", "users", "plans"])
            self.assertLess(order.index("users"), order.index("orders"))
            self.assertLess(order.index("plans"), order.index("orders"))
        finally:
            temporary.cleanup()

    def test_dry_run_never_requires_postgres_connection(self) -> None:
        temporary, path = self.make_database()
        try:
            report = copy_database(
                path,
                {"COMMERCE_DATABASE_URL": "postgresql://user:pass@example.invalid/db"},
                confirm=False,
                dry_run=True,
            )
            self.assertEqual(report["status"], "dry_run")
            self.assertTrue(report["tables"])
        finally:
            temporary.cleanup()

    def test_writes_require_an_explicit_confirmation(self) -> None:
        temporary, path = self.make_database()
        try:
            with self.assertRaises(FleetError):
                copy_database(
                    path,
                    {"COMMERCE_DATABASE_URL": "postgresql://user:pass@example.invalid/db"},
                    confirm=False,
                    dry_run=False,
                )
        finally:
            temporary.cleanup()

    def test_binary_values_are_comparable_without_plaintext_coercion(self) -> None:
        normalized = normalize_value(memoryview(b"secret"))
        self.assertEqual(normalized, {"__bytes__": "c2VjcmV0"})


if __name__ == "__main__":
    unittest.main()
