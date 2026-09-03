from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_object_store_replaces_offsite_directory(self) -> None:
        self.env.pop("AURIX_DATABASE_BACKUP_OFFSITE_DIR")
        self.env["AURIX_BACKUP_OBJECT_STORE_URL"] = "s3://bucket/aurix"
        objects: dict[str, bytes] = {}

        def put(_env: dict[str, str], key: str, content: bytes) -> str:
            objects[key] = content
            return f"s3://bucket/aurix/{key}"

        def get(_env: dict[str, str], key: str) -> bytes:
            return objects[key]

        with patch.object(database_backup.offsite_storage, "put", put), \
                patch.object(database_backup.offsite_storage, "get", get), \
                patch.object(database_backup.offsite_storage, "prune", return_value=0), \
                patch.object(database_backup.offsite_storage, "latest_key",
                             lambda _env, prefix, suffix: sorted(
                                 key for key in objects if key.startswith(prefix) and key.endswith(suffix)
                             )[-1]):
            database_backup.backup(self.env)
            report = database_backup.verify(self.env)

        self.assertIn("offsite", report)
        self.assertTrue(any(key.startswith("database/") for key in objects))

    def test_restore_recreates_missing_database_from_latest_archive(self) -> None:
        database_backup.backup(self.env)
        restored = self.root / "recovered" / "bot.db"
        self.env["DATABASE_PATH"] = str(restored)

        result = database_backup.restore(self.env, confirm_path=str(restored))

        self.assertEqual(result["status"], "restored")
        with sqlite3.connect(restored) as connection:
            self.assertEqual(connection.execute("SELECT id FROM ready").fetchone()[0], 1)
        self.assertEqual(restored.stat().st_mode & 0o777, 0o600)

    def test_restore_refuses_existing_database_without_explicit_override(self) -> None:
        archive = database_backup.backup(self.env)

        with self.assertRaises(FleetError):
            database_backup.restore(self.env, archive, confirm_path=str(self.database))

        result = database_backup.restore(
            self.env,
            archive,
            confirm_path=str(self.database),
            allow_existing=True,
        )

        self.assertEqual(result["status"], "restored")
        self.assertTrue(any(self.root.glob("bot.db.rollback-*")))


if __name__ == "__main__":
    unittest.main()
