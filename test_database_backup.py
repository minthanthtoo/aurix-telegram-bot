from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from deploy import database_backup
from deploy.fleet_reconcile import FleetError


class DatabaseBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "bot.db"
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE ready (id INTEGER)")
            connection.execute("INSERT INTO ready (id) VALUES (1)")
        self.env = {
            "DATABASE_PATH": str(self.database),
            "AURIX_DATABASE_BACKUP_KEY": Fernet.generate_key().decode(),
            "AURIX_DATABASE_BACKUP_DIR": str(self.root / "local"),
            "AURIX_DATABASE_BACKUP_OFFSITE_DIR": str(self.root / "offsite"),
            "AURIX_DATABASE_BACKUP_REQUIRE_OFFSITE": "1",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_backup_and_verify_local_and_offsite(self) -> None:
        archive = database_backup.backup(self.env)

        report = database_backup.verify(self.env)

        self.assertEqual(archive.parent, self.root / "local")
        self.assertIn("local", report)
        self.assertIn("offsite", report)

    def test_rejects_tampered_database_archive(self) -> None:
        archive = database_backup.backup(self.env)
        archive.write_bytes(archive.read_bytes() + b"x")

        with self.assertRaises(FleetError):
            database_backup.verify_archive(self.env, archive)

    def test_requires_offsite_when_enabled(self) -> None:
        self.env.pop("AURIX_DATABASE_BACKUP_OFFSITE_DIR")

        with self.assertRaises(FleetError):
            database_backup.backup(self.env)


if __name__ == "__main__":
    unittest.main()
