from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from aurix_games.backup_encryption import (
    create_encrypted_backup,
    restore_encrypted_backup,
    verify_encrypted_backup,
)
from aurix_games.backup_restore import GamesBackupError


PASSPHRASE = "correct horse battery staple, AuriX"
SENSITIVE_MARKER = b"synthetic-private-catalog-cost-7f5cc9"


class GamesBackupEncryptionTests(unittest.TestCase):
    def _source(self, parent: Path) -> Path:
        source = parent / "source-data"
        source.mkdir(mode=0o700)
        (source / "catalog.json").write_bytes(SENSITIVE_MARKER)
        with sqlite3.connect(source / "orders.sqlite3") as connection:
            connection.execute("CREATE TABLE orders (id TEXT, state TEXT)")
            connection.execute("INSERT INTO orders VALUES (?, ?)", ("order-1", "PAID_RESERVED"))
        return source

    def test_authenticated_encrypted_backup_verify_restore_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            encrypted = root / "offsite" / "games-001.agb"
            summary = create_encrypted_backup(source, encrypted, PASSPHRASE)
            self.assertTrue(summary["encrypted"])
            self.assertEqual(summary["cipher"], "AES-256-GCM")
            self.assertTrue(summary["plaintext_staging_removed"])
            self.assertEqual(encrypted.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(SENSITIVE_MARKER, encrypted.read_bytes())
            self.assertEqual(sorted(path.name for path in encrypted.parent.iterdir()), [encrypted.name])

            verified = verify_encrypted_backup(encrypted, PASSPHRASE)
            self.assertTrue(verified["encrypted"])
            self.assertEqual(verified["file_count"], 2)

            restored = root / "restore-drill"
            result = restore_encrypted_backup(encrypted, restored, PASSPHRASE)
            self.assertEqual(result["status"], "verified")
            self.assertFalse(result["secret_values_restored"])
            self.assertEqual((restored / "catalog.json").read_bytes(), SENSITIVE_MARKER)
            with sqlite3.connect(restored / "orders.sqlite3") as connection:
                self.assertEqual(
                    connection.execute("SELECT id, state FROM orders").fetchone(),
                    ("order-1", "PAID_RESERVED"),
                )

    def test_wrong_passphrase_fails_before_restore_target_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            encrypted = root / "backup.agb"
            create_encrypted_backup(self._source(root), encrypted, PASSPHRASE)
            target = root / "restore-target"
            with self.assertRaisesRegex(GamesBackupError, "authentication failed"):
                restore_encrypted_backup(encrypted, target, "wrong passphrase, not correct")
            self.assertFalse(target.exists())

    def test_tampered_encrypted_archive_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            encrypted = root / "backup.agb"
            create_encrypted_backup(self._source(root), encrypted, PASSPHRASE)
            contents = bytearray(encrypted.read_bytes())
            contents[-1] ^= 0x01
            encrypted.write_bytes(contents)
            with self.assertRaisesRegex(GamesBackupError, "authentication failed"):
                verify_encrypted_backup(encrypted, PASSPHRASE)

    def test_short_passphrase_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(GamesBackupError, "at least 16"):
                create_encrypted_backup(self._source(root), root / "backup.agb", "too short")


if __name__ == "__main__":
    unittest.main()
