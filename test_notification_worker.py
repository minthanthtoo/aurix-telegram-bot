"""Notification behavior through narrow ports, without a commerce service host."""

from contextlib import contextmanager
from datetime import datetime, timezone
from types import MappingProxyType
import unittest

from notification_contracts import NotificationRecord
from notification_worker import NotificationWorker


class RecordingTransactions:
    def __init__(self, rows):
        self.rows = rows
        self.events = []
        self.open = False

    @contextmanager
    def __call__(self, *, write=False):
        self.open = True
        self.events.append(("begin", write))
        try:
            yield self
        except Exception:
            self.events.append(("rollback",))
            raise
        else:
            self.events.append(("commit",))
        finally:
            self.open = False

    def pending(self, now, limit):
        return self.rows[:limit]

    def claim(self, now, limit, lease_until):
        self.events.append(("claim", now, limit, lease_until))
        return self.rows[:limit]

    def mark_sent(self, notification_id, now):
        self.events.append(("sent", notification_id, now))

    def mark_failed(self, notification_id, now, retry_at):
        self.events.append(("failed", notification_id, now, retry_at))


class NotificationWorkerTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 6, tzinfo=timezone.utc)

    def test_claim_commits_before_decrypting_and_preserves_source_record(self):
        row = MappingProxyType({"id": "n1", "text": "Ready", "access_url_ciphertext": "cipher"})
        transactions = RecordingTransactions([NotificationRecord(row)])

        def decrypt(ciphertext):
            self.assertFalse(transactions.open)
            self.assertEqual(transactions.events[-1], ("commit",))
            self.assertEqual(ciphertext, "cipher")
            return "ss://test-key"

        worker = NotificationWorker(transactions, decrypt)
        result = worker.claim_pending_notifications(self.now, limit=1000, lease_seconds=1)
        self.assertEqual(result[0]["text"], "Ready\n\nYour Outline key:\nss://test-key")
        self.assertEqual(row["text"], "Ready")
        self.assertEqual(transactions.events[1], (
            "claim", "2026-09-06T00:00:00+00:00", 100, "2026-09-06T00:00:30+00:00",
        ))

    def test_unavailable_secret_is_flagged_without_disclosing_ciphertext_in_text(self):
        transactions = RecordingTransactions([
            NotificationRecord({"id": "n1", "text": "Ready", "access_url_ciphertext": "cipher"}),
        ])
        worker = NotificationWorker(transactions, lambda _: None)
        result = worker.pending_notifications(self.now)
        self.assertTrue(result[0]["secret_unavailable"])
        self.assertNotIn("access_url", result[0])
        self.assertEqual(result[0]["text"], "Ready")

    def test_delivery_failure_records_retry_through_a_write_transaction(self):
        transactions = RecordingTransactions([])
        NotificationWorker(transactions, lambda _: None).mark_notification_failed("n1", self.now)
        self.assertEqual(transactions.events[0], ("begin", True))
        self.assertEqual(transactions.events[1][0:2], ("failed", "n1"))
        self.assertGreater(transactions.events[1][3], transactions.events[1][2])
        self.assertEqual(transactions.events[-1], ("commit",))
