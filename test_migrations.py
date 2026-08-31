import tempfile
import unittest
from pathlib import Path

from app import Database
from commerce import CommerceDatabase, PostgresCommerceDatabase
from migrations import Migration, MigrationError, apply_migrations
from persistence import open_sqlite_connection
from repositories import HostedRepositoryDatabase, RepositoryDatabase


class MigrationRegistryTest(unittest.TestCase):
    def test_component_history_is_idempotent_and_applies_statements_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migrations.db"
            migration = Migration(
                1,
                "create_sample",
                sqlite_statements=(
                    "CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY)",
                ),
            )
            with open_sqlite_connection(path) as connection:
                apply_migrations(
                    connection,
                    component="test",
                    dialect="sqlite",
                    migrations=(migration,),
                    applied_at="2026-01-01T00:00:00+00:00",
                )
                apply_migrations(
                    connection,
                    component="test",
                    dialect="sqlite",
                    migrations=(migration,),
                    applied_at="2026-01-02T00:00:00+00:00",
                )
                rows = connection.execute(
                    "SELECT component, version, name, applied_at FROM schema_migrations"
                ).fetchall()
                table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'sample'"
                ).fetchone()

            self.assertEqual(
                [tuple(row) for row in rows],
                [("test", 1, "create_sample", "2026-01-01T00:00:00+00:00")],
            )
            self.assertEqual(table[0], "sample")

    def test_history_rejects_renamed_unknown_duplicate_and_nonpositive_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.db"
            with open_sqlite_connection(path) as connection:
                apply_migrations(
                    connection,
                    component="test",
                    dialect="sqlite",
                    migrations=(Migration(1, "original"),),
                )
                with self.assertRaisesRegex(MigrationError, "was renamed"):
                    apply_migrations(
                        connection,
                        component="test",
                        dialect="sqlite",
                        migrations=(Migration(1, "renamed"),),
                    )
                with self.assertRaisesRegex(MigrationError, "unknown"):
                    apply_migrations(
                        connection,
                        component="test",
                        dialect="sqlite",
                        migrations=(),
                    )

            for migrations, message in (
                ((Migration(1, "one"), Migration(1, "again")), "Duplicate"),
                ((Migration(0, "zero"),), "positive"),
            ):
                with open_sqlite_connection(Path(tmp) / f"{message}.db") as connection:
                    with self.assertRaisesRegex(MigrationError, message):
                        apply_migrations(
                            connection,
                            component="test",
                            dialect="sqlite",
                            migrations=migrations,
                        )

    def test_unsupported_dialect_is_rejected_before_recording_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dialect.db"
            with open_sqlite_connection(path) as connection:
                with self.assertRaisesRegex(MigrationError, "Unsupported"):
                    apply_migrations(
                        connection,
                        component="test",
                        dialect="oracle",
                        migrations=(Migration(1, "first"),),
                    )
                count = connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations"
                ).fetchone()[0]
            self.assertEqual(count, 0)

    def test_existing_initializers_adopt_component_scoped_version_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "application.db"
            Database(path).initialize()
            CommerceDatabase(path).initialize()
            with open_sqlite_connection(path) as connection:
                rows = connection.execute(
                    "SELECT component, version, name FROM schema_migrations "
                    "ORDER BY component"
                ).fetchall()

            self.assertEqual(
                [tuple(row) for row in rows],
                [
                    ("commerce", 1, "legacy_commerce_schema"),
                    ("commerce", 2, "receipt_control_and_diagnostics"),
                    ("free_access", 1, "legacy_free_access_schema"),
                    ("free_access", 2, "giveaway_campaigns"),
                    ("free_access", 3, "configurable_promo_campaigns"),
                    ("free_access", 4, "staff_access_control"),
                    ("free_access", 5, "staff_control_group_binding"),
                ],
            )


class RepositoryContractTest(unittest.TestCase):
    def test_database_adapters_satisfy_structural_repository_contracts(self):
        self.assertIsInstance(Database(":memory:"), RepositoryDatabase)
        self.assertIsInstance(CommerceDatabase(":memory:"), RepositoryDatabase)
        self.assertIsInstance(
            PostgresCommerceDatabase("postgresql://example.invalid/aurix"),
            HostedRepositoryDatabase,
        )


if __name__ == "__main__":
    unittest.main()
