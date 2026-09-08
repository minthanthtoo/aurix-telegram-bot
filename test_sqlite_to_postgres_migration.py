import sqlite3
import tempfile
import unittest
from pathlib import Path

from deploy.migrate_sqlite_to_postgres import dependency_order, sqlite_manifest


class SqliteToPostgresMigrationTest(unittest.TestCase):
    def test_manifest_checks_integrity_counts_rows_and_orders_parents_first(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE orders (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id)
                );
                CREATE TABLE schema_migrations (component TEXT PRIMARY KEY);
                INSERT INTO users VALUES (1, 'A');
                INSERT INTO orders VALUES ('order-1', 1);
                """
            )
            connection.close()

            counts, order, digest = sqlite_manifest(path)

            self.assertEqual(counts, {"users": 1, "orders": 1})
            self.assertLess(order.index("users"), order.index("orders"))
            self.assertEqual(len(digest), 64)

    def test_dependency_cycle_fails_closed(self):
        connection = sqlite3.connect(":memory:")
        connection.executescript(
            """
            CREATE TABLE left_side (
                id INTEGER PRIMARY KEY,
                right_id INTEGER REFERENCES right_side(id)
            );
            CREATE TABLE right_side (
                id INTEGER PRIMARY KEY,
                left_id INTEGER REFERENCES left_side(id)
            );
            """
        )
        with self.assertRaisesRegex(RuntimeError, "dependency cycle"):
            dependency_order(connection, ["left_side", "right_side"])
        connection.close()


if __name__ == "__main__":
    unittest.main()
