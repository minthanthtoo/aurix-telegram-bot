from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from aurix_games.backup_restore import (
    GamesBackupError,
    create_backup,
    restore_backup,
    verify_backup,
)


class GamesBackupRestoreTests(unittest.TestCase):
    def test_backup_verify_and_fresh_restore_round_trip_sqlite_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "live-data"
            source.mkdir(mode=0o700)
            (source / "nested").mkdir(mode=0o700)
            (source / "public-catalog.json").write_text('{"schema_version":1}\n', encoding="utf-8")
            database = source / "nested" / "game-orders.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE orders (id TEXT PRIMARY KEY, state TEXT NOT NULL)")
                connection.execute("INSERT INTO orders VALUES ('order-1', 'AWAITING_PAYMENT')")

            backup = root / "backup-001"
            summary = create_backup(source, backup)
            self.assertEqual(summary["status"], "verified")
            self.assertEqual(summary["file_count"], 2)
            self.assertEqual(summary["sqlite_count"], 1)
            self.assertFalse(summary["secret_values_included"])
            self.assertEqual(verify_backup(backup)["total_bytes"], summary["total_bytes"])
            manifest = json.loads((backup / "aurix-games-backup.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["secret_values_included"], False)
            self.assertEqual((backup / "nested" / "game-orders.sqlite3").stat().st_mode & 0o777, 0o600)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o700)

            restored = root / "restored-data"
            result = restore_backup(backup, restored)
            self.assertEqual(result["status"], "verified")
            self.assertFalse(result["secret_values_restored"])
            self.assertEqual(
                (restored / "public-catalog.json").read_text(encoding="utf-8"),
                '{"schema_version":1}\n',
            )
            with sqlite3.connect(restored / "nested" / "game-orders.sqlite3") as connection:
                row = connection.execute("SELECT id, state FROM orders").fetchone()
            self.assertEqual(row, ("order-1", "AWAITING_PAYMENT"))

    def test_backup_uses_sqlite_snapshot_api_for_open_wal_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir(mode=0o700)
            database = source / "ledger.db"
            connection = sqlite3.connect(database)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE ledger (balance INTEGER)")
            connection.execute("INSERT INTO ledger VALUES (6500)")
            connection.commit()
            backup = root / "backup"
            create_backup(source, backup)
            connection.close()

            restored = root / "restored"
            restore_backup(backup, restored)
            with sqlite3.connect(restored / "ledger.db") as copy:
                self.assertEqual(copy.execute("SELECT balance FROM ledger").fetchone(), (6500,))

    def test_corrupt_backup_is_rejected_before_restore_target_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "config.json").write_text("{}", encoding="utf-8")
            backup = root / "backup"
            create_backup(source, backup)
            (backup / "config.json").write_text("changed", encoding="utf-8")
            target = root / "restore-target"
            with self.assertRaisesRegex(GamesBackupError, "hash or size"):
                restore_backup(backup, target)
            self.assertFalse(target.exists())

    def test_restore_never_overwrites_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            backup = root / "backup"
            create_backup(source, backup)
            target = root / "target"
            target.mkdir()
            sentinel = target / "keep.txt"
            sentinel.write_text("preserve", encoding="utf-8")
            with self.assertRaisesRegex(GamesBackupError, "already exists"):
                restore_backup(backup, target)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def test_backup_rejects_source_symlink_and_nested_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "data.txt").write_text("private", encoding="utf-8")
            (source / "linked-dir").symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(GamesBackupError, "symlink"):
                create_backup(source, root / "backup")
            (source / "linked-dir").unlink()
            with self.assertRaisesRegex(GamesBackupError, "outside"):
                create_backup(source, source / "backup")

    def test_manifest_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "data.txt").write_text("private", encoding="utf-8")
            backup = root / "backup"
            create_backup(source, backup)
            manifest_path = backup / "aurix-games-backup.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"][0]["path"] = "../escape.txt"
            manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
            manifest_path.write_bytes(manifest_bytes)
            (backup / "aurix-games-backup.sha256").write_text(
                hashlib.sha256(manifest_bytes).hexdigest() + "\n",
                encoding="ascii",
            )
            with self.assertRaisesRegex(GamesBackupError, "unsafe relative path"):
                verify_backup(backup)

    def test_backup_symlink_is_rejected_before_following_manifest_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "data.txt").write_text("private", encoding="utf-8")
            outside = root / "outside"
            outside.mkdir()
            (outside / "data.txt").write_text("outside", encoding="utf-8")
            backup = root / "backup"
            create_backup(source, backup)
            (backup / "nested" / "data.txt").unlink()
            (backup / "nested").rmdir()
            (backup / "nested").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(GamesBackupError, "symlink"):
                verify_backup(backup)


if __name__ == "__main__":
    unittest.main()
