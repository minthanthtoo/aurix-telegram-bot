from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from aurix_games.customer_ledger import (
    CustomerLedger,
    InsufficientFundsError,
    LedgerConflictError,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 22, 16, 0, tzinfo=UTC)


class CustomerLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "private" / "ledger.sqlite3"
        self.ledger = CustomerLedger(self.path)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_credit_reservation_capture_and_refund_are_idempotent(self):
        first = self.ledger.credit(
            customer_id="customer-1", amount_minor=10000, currency="MMK",
            event_key="payment-1", actor="staff:1", reason="verified transfer",
            occurred_at=NOW,
        )
        replay = self.ledger.credit(
            customer_id="customer-1", amount_minor=10000, currency="MMK",
            event_key="payment-1", actor="staff:1", reason="verified transfer",
            occurred_at=NOW,
        )
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        reserved = self.ledger.reserve(
            order_id="order-1", customer_id="customer-1", amount_minor=4500,
            currency="MMK", event_key="reserve-1", actor="system",
            reason="reserve order", occurred_at=NOW,
        )
        self.assertEqual(reserved.balance.available_minor, 5500)
        self.assertEqual(reserved.balance.reserved_minor, 4500)
        self.assertTrue(self.ledger.reserve(
            order_id="order-1", customer_id="customer-1", amount_minor=4500,
            currency="MMK", event_key="reserve-1", actor="system",
            reason="reserve order", occurred_at=NOW,
        ).replayed)
        captured = self.ledger.capture(
            order_id="order-1", event_key="capture-1", actor="worker",
            reason="supplier accepted", occurred_at=NOW,
        )
        self.assertEqual(captured.balance.available_minor, 5500)
        self.assertEqual(captured.balance.reserved_minor, 0)
        refunded = self.ledger.refund(
            order_id="order-1", event_key="refund-1", actor="staff:1",
            reason="provider failure", occurred_at=NOW,
        )
        self.assertEqual(refunded.balance.available_minor, 10000)
        self.assertEqual(refunded.reservation.status, "refunded")

    def test_release_returns_reserved_funds_and_insufficient_balance_fails(self):
        self.ledger.credit(
            customer_id="customer-1", amount_minor=1000, currency="MMK",
            event_key="payment-1", actor="staff:1", reason="verified transfer",
            occurred_at=NOW,
        )
        with self.assertRaises(InsufficientFundsError):
            self.ledger.reserve(
                order_id="order-1", customer_id="customer-1", amount_minor=1001,
                currency="MMK", event_key="reserve-1", actor="system",
                reason="reserve order", occurred_at=NOW,
            )
        self.ledger.reserve(
            order_id="order-1", customer_id="customer-1", amount_minor=1000,
            currency="MMK", event_key="reserve-1", actor="system",
            reason="reserve order", occurred_at=NOW,
        )
        released = self.ledger.release(
            order_id="order-1", event_key="release-1", actor="staff:1",
            reason="cancelled before supplier submit", occurred_at=NOW,
        )
        self.assertEqual(released.balance.available_minor, 1000)
        self.assertEqual(released.balance.reserved_minor, 0)
        with self.assertRaises(LedgerConflictError):
            self.ledger.reserve(
                order_id="order-1", customer_id="customer-1", amount_minor=1000,
                currency="MMK", event_key="reserve-2", actor="system",
                reason="duplicate order", occurred_at=NOW,
            )

    def test_database_is_private_and_entries_are_append_only(self):
        self.ledger.credit(
            customer_id="customer-1", amount_minor=100, currency="MMK",
            event_key="payment-1", actor="staff:1", reason="verified transfer",
            occurred_at=NOW,
        )
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        with sqlite3.connect(self.path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE game_ledger_entries SET reason = 'changed'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM game_ledger_entries")

    def test_customer_credit_history_is_sanitized_and_owner_scoped(self):
        self.ledger.credit(
            customer_id="customer-1", amount_minor=1000, currency="MMK",
            event_key="payment-1", actor="staff:private", reason="private reason",
            occurred_at=NOW, metadata={"receipt": "private"},
        )
        self.ledger.credit(
            customer_id="customer-2", amount_minor=2000, currency="MMK",
            event_key="payment-2", actor="staff:private", reason="private reason",
            occurred_at=NOW,
        )
        entries = self.ledger.list_credits("customer-1")
        self.assertEqual(len(entries), 1)
        payload = entries[0].as_dict()
        self.assertEqual(payload["amount_minor"], 1000)
        self.assertEqual(payload["status"], "posted")
        self.assertNotIn("event_key", payload)
        self.assertNotIn("actor", payload)
        self.assertNotIn("reason", payload)
        self.assertEqual(self.ledger.list_credits("customer-1")[0].currency, "MMK")
        self.assertEqual(self.ledger.list_credits("customer-2")[0].amount_minor, 2000)


if __name__ == "__main__":
    unittest.main()
