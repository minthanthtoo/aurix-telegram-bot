import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.receipt_pipeline_smoke import latest_receipt


class ReceiptPipelineSmokeDatabaseTest(unittest.TestCase):
    def test_latest_receipt_reads_sqlite_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.db"
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE orders (id TEXT PRIMARY KEY, amount_minor INTEGER, currency TEXT);
                    CREATE TABLE payment_evidence (
                        id TEXT PRIMARY KEY, order_id TEXT, telegram_file_id TEXT,
                        mime_type TEXT, submitted_at TEXT, extraction_status TEXT,
                        review_status TEXT, provider TEXT
                    );
                    INSERT INTO orders VALUES ('o1', 3000, 'MMK');
                    INSERT INTO payment_evidence VALUES
                      ('e1', 'o1', 'file-1', 'image/jpeg', '2026-09-04T01:00:00+00:00',
                       'needs_review', 'pending', 'kbzpay');
                    """
                )
            receipt = latest_receipt(path)
        self.assertEqual(receipt["id"], "e1")
        self.assertEqual(receipt["amount_minor"], 3000)

    def test_latest_receipt_reads_postgres_authority(self):
        class FakeCursor:
            def fetchone(self):
                return {"id": "e2", "amount_minor": 6000, "currency": "MMK"}

        class FakeConnection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, _query):
                return FakeCursor()

        fake_psycopg = types.ModuleType("psycopg")
        fake_psycopg.connect = lambda *_args, **_kwargs: FakeConnection()
        fake_rows = types.ModuleType("psycopg.rows")
        fake_rows.dict_row = object()
        with patch.dict(
            sys.modules,
            {"psycopg": fake_psycopg, "psycopg.rows": fake_rows},
        ):
            receipt = latest_receipt(database_url="postgresql://redacted.invalid/aurix")
        self.assertEqual(receipt["id"], "e2")
        self.assertEqual(receipt["amount_minor"], 6000)


if __name__ == "__main__":
    unittest.main()
