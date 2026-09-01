import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import Database
from commerce import CommerceDatabase
from persistence import ClosingSQLiteConnection, open_sqlite_connection


class SQLiteConnectionLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bot.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_database_context_commits_and_closes(self):
        database = Database(self.path)
        database.initialize()

        with database.connect() as connection:
            self.assertIsInstance(connection, ClosingSQLiteConnection)
            connection.execute(
                "INSERT INTO users (telegram_id, first_name, created_at) VALUES (?, ?, ?)",
                (123, "Min", "2026-08-29T00:00:00+00:00"),
            )

        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with database.connect() as check:
            self.assertEqual(
                check.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                1,
            )

    def test_connect_retains_raw_connection_api_for_legacy_callers(self):
        database = Database(self.path)
        database.initialize()

        connection = database.connect()
        try:
            self.assertIsInstance(connection, sqlite3.Connection)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        finally:
            connection.close()

    def test_database_context_rolls_back_and_closes_after_exception(self):
        database = Database(self.path)
        database.initialize()

        with self.assertRaisesRegex(RuntimeError, "force rollback"):
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO users (telegram_id, first_name, created_at) VALUES (?, ?, ?)",
                    (123, "Min", "2026-08-29T00:00:00+00:00"),
                )
                raise RuntimeError("force rollback")

        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with database.connect() as check:
            self.assertEqual(
                check.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                0,
            )

    def test_commerce_context_keeps_busy_timeout_and_closes(self):
        database = CommerceDatabase(self.path)
        database.initialize()

        with database.connect() as connection:
            self.assertIsInstance(connection, ClosingSQLiteConnection)
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 30_000)

        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_open_connection_rejects_invalid_busy_timeout(self):
        with self.assertRaisesRegex(ValueError, "must not be negative"):
            open_sqlite_connection(self.path, busy_timeout_ms=-1)


if __name__ == "__main__":
    unittest.main()
