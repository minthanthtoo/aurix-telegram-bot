import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from commerce import CommerceDatabase
from app import Database
from scripts.aurix_backup import backup_sqlite, restore_sqlite, verify_sqlite
from persistence import open_sqlite_connection


UTC = timezone.utc


class BackupStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "bot.db"
        Database(self.source).initialize()
        CommerceDatabase(self.source).initialize()
        with open_sqlite_connection(self.source) as connection:
            connection.execute(
                """INSERT INTO users (telegram_id, first_name, created_at)
                   VALUES (123, 'Min', ?)""",
                (datetime(2026, 9, 10, tzinfo=UTC).isoformat(),),
            )
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, status, created_at)
                   VALUES ('order-1', 123, 'basic_50gb', 3000, 'MMK', 'awaiting_payment', ?)""",
                (datetime(2026, 9, 10, tzinfo=UTC).isoformat(),),
            )
            connection.execute(
                """INSERT INTO payment_evidence
                   (id, order_id, telegram_id, provider, telegram_file_id,
                    image_sha256, mime_type, byte_size, extraction_status, submitted_at,
                    storage_bucket, storage_path)
                   VALUES ('evidence-1', 'order-1', 123, 'wavepay', 'file-1',
                           'a', 'image/jpeg', 12, 'needs_review', ?,
                           'payment-receipts', 'receipts/order-1/a.jpg')""",
                (datetime(2026, 9, 10, tzinfo=UTC).isoformat(),),
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_sqlite_backup_manifest_and_restore_reconcile_receipt_paths(self):
        output_dir = self.root / "backups"
        receipt_inventory = self.root / "receipts.txt"
        receipt_inventory.write_text("receipts/order-1/a.jpg\n", encoding="utf-8")

        backup = backup_sqlite(self.source, output_dir, "checkpoint.db")
        self.assertEqual(backup["status"], "ok")
        self.assertEqual(backup["receipt_path_count"], 1)
        verified = verify_sqlite(
            Path(backup["artifact"]), receipt_inventory=receipt_inventory
        )
        self.assertEqual(verified["status"], "ok")
        self.assertEqual(verified["table_counts"]["orders"], 1)

        restored = restore_sqlite(
            Path(backup["artifact"]),
            self.root / "restored" / "bot.db",
            receipt_inventory=receipt_inventory,
        )
        self.assertEqual(restored["status"], "ok")
        self.assertEqual(restored["table_counts_match_manifest"], True)

    def test_missing_receipt_object_fails_verification_without_mutating_backup(self):
        backup = backup_sqlite(self.source, self.root / "backups", "checkpoint.db")
        inventory = self.root / "empty-receipts.txt"
        inventory.write_text("", encoding="utf-8")

        report = verify_sqlite(Path(backup["artifact"]), receipt_inventory=inventory)

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["receipt_reconciliation"]["missing"], ["receipts/order-1/a.jpg"])
        self.assertTrue(Path(backup["artifact"]).exists())


if __name__ == "__main__":
    unittest.main()
