from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from aurix_games.order_repository import (
    GameOrderRepository,
    IdempotencyConflictError,
    OrderNotFoundError,
    RecipientLookupThrottledError,
)
from aurix_games.order_state import OrderSnapshot, OrderState, OrderTransitionError, create_order


UTC = timezone.utc
NOW = datetime(2026, 9, 22, 15, 0, tzinfo=UTC)


def _order(order_id="ord-game-001", key="client-key-001", amount=455000):
    return create_order(
        order_id=order_id,
        customer_id="customer-001",
        customer_idempotency_key=key,
        snapshot=OrderSnapshot(
            offer_id="aurix-mlbb-asia-55",
            product_slug="mobile-legends",
            fulfillment_kind="direct_topup",
            amount_minor=amount,
            currency="MMK",
            recipient_fingerprint="recipient-fp-123",
        ),
    )


class GameOrderRepositoryTests(unittest.TestCase):
    def test_recipient_lookup_claim_is_atomic_window_limited_and_pseudonymous(self):
        with tempfile.TemporaryDirectory() as root:
            repository = GameOrderRepository(Path(root) / "orders.sqlite3")
            repository.claim_recipient_lookup("customer-1", now=NOW, fingerprint_secret=b"x" * 32, global_limit=1)
            with self.assertRaises(RecipientLookupThrottledError):
                repository.claim_recipient_lookup(
                    "customer-2", now=NOW + timedelta(seconds=1), fingerprint_secret=b"x" * 32, global_limit=1)
            repository.claim_recipient_lookup(
                "customer-2", now=NOW + timedelta(seconds=11), fingerprint_secret=b"x" * 32, global_limit=1)

            with repository._connect() as connection:
                rows = connection.execute(
                    "SELECT customer_fingerprint FROM game_recipient_lookup_events"
                ).fetchall()
            self.assertEqual(len(rows), 2, "expired window claims remain pseudonymous until retention cleanup")
            self.assertTrue(all(len(row["customer_fingerprint"]) == 64 for row in rows))
            self.assertNotIn("customer-2", {row["customer_fingerprint"] for row in rows})

    def test_repository_connection_closes_after_commit_or_failure(self):
        with tempfile.TemporaryDirectory() as root:
            repository = GameOrderRepository(Path(root) / "orders.sqlite3")
            for fail in (False, True):
                connection = repository._connect()
                try:
                    with connection:
                        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                        if fail:
                            raise ValueError("rollback")
                except ValueError:
                    pass
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "private" / "orders.sqlite3"
        self.repository = GameOrderRepository(self.path)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_create_and_read_are_durable_and_owner_scoped(self):
        order = _order()
        saved = self.repository.create(order, created_at=NOW)
        loaded = self.repository.get(order.order_id, customer_id="customer-001")
        self.assertEqual(saved, loaded)
        with self.assertRaises(OrderNotFoundError):
            self.repository.get(order.order_id, customer_id="other-customer")
        self.assertEqual(len(self.repository.list_for_customer("customer-001")), 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_bounded_state_scan_is_stable_for_recovery_workers(self):
        first = self.repository.create(_order("ord-game-001", "client-1"), created_at=NOW)
        second = self.repository.create(_order("ord-game-002", "client-2"), created_at=NOW)
        self.repository.transition(first.order_id, OrderState.AWAITING_PAYMENT, at=NOW, reason="await payment")
        self.repository.transition(second.order_id, OrderState.AWAITING_PAYMENT, at=NOW, reason="await payment")
        self.repository.transition(first.order_id, OrderState.PAID_RESERVED, at=NOW, reason="reserved")
        self.repository.transition(second.order_id, OrderState.PAID_RESERVED, at=NOW, reason="reserved")

        self.assertEqual(
            tuple(order.order_id for order in self.repository.list_by_state(OrderState.PAID_RESERVED, limit=1)),
            ("ord-game-001",),
        )
        self.assertEqual(
            tuple(order.order_id for order in self.repository.list_by_state(OrderState.PAID_RESERVED)),
            ("ord-game-001", "ord-game-002"),
        )
        with self.assertRaisesRegex(Exception, "limit"):
            self.repository.list_by_state(OrderState.PAID_RESERVED, limit=True)

    def test_same_idempotency_key_returns_original_order(self):
        first = self.repository.create(_order(), created_at=NOW)
        retry = self.repository.create(_order(order_id="different-order-id"), created_at=NOW)
        self.assertEqual(retry.order_id, first.order_id)
        self.assertEqual(len(self.repository.list_for_customer("customer-001")), 1)

    def test_reusing_idempotency_key_with_changed_snapshot_is_rejected(self):
        self.repository.create(_order(), created_at=NOW)
        with self.assertRaises(IdempotencyConflictError):
            self.repository.create(_order(order_id="different-order-id", amount=999), created_at=NOW)

    def test_transition_persists_append_only_event_and_provider_metadata(self):
        self.repository.create(_order(), created_at=NOW)
        self.repository.transition(
            "ord-game-001", OrderState.AWAITING_PAYMENT, at=NOW, reason="await customer payment"
        )
        self.repository.transition(
            "ord-game-001", OrderState.PAID_RESERVED, at=NOW, reason="wallet reservation recorded"
        )
        submitted = self.repository.transition(
            "ord-game-001",
            OrderState.FULFILLMENT_QUEUED,
            at=NOW,
            reason="enqueue fulfillment",
        )
        self.assertEqual(submitted.state, OrderState.FULFILLMENT_QUEUED)
        loaded = self.repository.transition(
            "ord-game-001",
            OrderState.SUBMITTED,
            at=NOW,
            reason="supplier submission acknowledged",
            provider_order_id="supplier-order-1",
            provider_status="processing",
        )
        self.assertEqual(loaded.provider_order_id, "supplier-order-1")
        self.assertEqual(len(loaded.events), 4)
        self.assertEqual(self.repository.get("ord-game-001").events[-1].reason, "supplier submission acknowledged")

    def test_invalid_transition_rolls_back_without_event(self):
        self.repository.create(_order(), created_at=NOW)
        with self.assertRaises(OrderTransitionError):
            self.repository.transition("ord-game-001", OrderState.DELIVERED, at=NOW, reason="skip")
        loaded = self.repository.get("ord-game-001")
        self.assertEqual(loaded.state, OrderState.DRAFT)
        self.assertEqual(loaded.events, ())

    def test_sqlite_guards_protect_snapshot_and_append_only_events(self):
        self.repository.create(_order(), created_at=NOW)
        self.repository.transition(
            "ord-game-001", OrderState.AWAITING_PAYMENT, at=NOW, reason="await payment"
        )
        with sqlite3.connect(self.path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE game_orders SET snapshot_json = '{}' WHERE order_id = ?",
                    ("ord-game-001",),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM game_order_events WHERE order_id = ?", ("ord-game-001",))

    def test_missing_order_and_malformed_time_fail_safely(self):
        with self.assertRaises(OrderNotFoundError):
            self.repository.get("missing")
        with self.assertRaisesRegex(Exception, "timezone-aware"):
            self.repository.create(_order(), created_at=NOW.replace(tzinfo=None))


if __name__ == "__main__":
    unittest.main()
