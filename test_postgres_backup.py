from __future__ import annotations

import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from deploy import postgres_backup
from deploy.fleet_reconcile import FleetError


class PostgresBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = Fernet.generate_key().decode()
        self.url = "postgresql://postgres.project:pa%3Ass@example.invalid:5432/postgres?sslmode=require"

    def test_database_url_requires_a_complete_postgres_uri(self) -> None:
        self.assertEqual(
            postgres_backup.database_url({"COMMERCE_DATABASE_URL": self.url}), self.url
        )
        with self.assertRaises(FleetError):
            postgres_backup.database_url({"COMMERCE_DATABASE_URL": "postgresql://host"})
        with self.assertRaises(FleetError):
            postgres_backup.database_url({"COMMERCE_DATABASE_URL": "https://host/db"})

    def test_password_is_not_in_client_arguments(self) -> None:
        with postgres_backup._client_environment(self.url) as (environment, arguments):
            self.assertNotIn("pa:ss", arguments)
            self.assertNotIn("pa%3Ass", arguments)
            pass_file = Path(environment["PGPASSFILE"])
            self.assertTrue(pass_file.is_file())
            self.assertEqual(stat.S_IMODE(pass_file.stat().st_mode), 0o600)
            self.assertIn("pa\\:ss", pass_file.read_text(encoding="utf-8"))
        self.assertFalse(pass_file.exists())

    def test_verified_archive_rejects_wrong_format(self) -> None:
        ciphertext = Fernet(self.key.encode()).encrypt(b"dump")
        metadata = b'{"format":"sqlite","ciphertext_sha256":"bad"}'
        with patch.object(postgres_backup, "_verify_dump"):
            with self.assertRaises(FleetError):
                postgres_backup._verified_archive_bytes(
                    {"AURIX_DATABASE_BACKUP_KEY": self.key},
                    ciphertext,
                    metadata,
                    "test",
                )

    def test_restore_command_places_archive_as_input(self) -> None:
        captured: list[list[str]] = []

        def fake_run(command, **kwargs):
            del kwargs
            captured.append(list(command))
            return type("Result", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

        with patch.object(postgres_backup.subprocess, "run", side_effect=fake_run):
            postgres_backup._restore_bytes(self.url, b"dump", allow_existing=False)
        self.assertEqual(len(captured), 1)
        command = captured[0]
        self.assertNotIn("--file", command)
        self.assertTrue(command[-1].endswith("/commerce.dump"))


if __name__ == "__main__":
    unittest.main()
