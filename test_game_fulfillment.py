from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import os
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from aurix_games.customer_ledger import CustomerLedger
from aurix_games.fulfillment import (
    FazerCardsOrderAdapter,
    FulfillmentClaimLost,
    FulfillmentTaskStore,
    GameFulfillmentWorker,
    PaidOrderDispatcher,
)
from aurix_games.order_repository import GameOrderRepository
from aurix_games.order_state import OrderSnapshot, OrderState, create_order
from aurix_games.payment_service import CustomerPaymentService, PaymentBoundaryError
from aurix_games.providers.fazercards_commands import SupplierWriteDisabledError


UTC = timezone.utc
NOW = datetime(2026, 9, 22, 17, 0, tzinfo=UTC)


class FulfillmentRuntimePathTests(unittest.TestCase):
    def test_worker_uses_same_default_ledger_as_customer_api(self):
        from scripts.run_games_fulfillment import main
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'AURIX_GAMES_BASE_DIR': directory, 'FAZERCARDS_API_KEY':'fixture',
                'AURIX_GAMES_FULFILLMENT_KEY':Fernet.generate_key().decode()}, clear=True):
            with patch('scripts.run_games_fulfillment.GameFulfillmentWorker') as worker, patch('builtins.print'):
                worker.return_value.run_once.return_value = ()
                self.assertEqual(main([]), 0)
                payments = worker.call_args.kwargs['payments']
                self.assertEqual(payments.ledger.path, Path(directory) / 'game-ledger.sqlite3')
                self.assertEqual(payments.repository.path, Path(directory) / 'game-orders.sqlite3')


def _order(repository: GameOrderRepository) -> None:
    order = create_order(
        order_id="order-1", customer_id="customer-1", customer_idempotency_key="client-1",
        snapshot=OrderSnapshot(
            offer_id="offer-1", product_slug="mobile-legends", fulfillment_kind="direct_topup",
            amount_minor=4500, currency="MMK", recipient_fingerprint="recipient-fp",
        ),
    )
    repository.create(order, created_at=NOW)
    repository.transition(order.order_id, OrderState.AWAITING_PAYMENT, at=NOW, reason="await payment")


class _Supplier:
    def __init__(self, *, disabled: bool = False, failed: bool = False) -> None:
        self.disabled = disabled
        self.failed = failed
        self.calls = []

    def submit(self, *, fulfillment_kind, payload, idempotency_key):
        self.calls.append((fulfillment_kind, dict(payload), idempotency_key))
        if self.disabled:
            raise SupplierWriteDisabledError("supplier writes are disabled")
        if self.failed:
            raise RuntimeError("upstream timeout")
        return {"ok": True, "order": {"id": "ord-9001", "status": "processing"}}


class _CommandClient:
    def __init__(self):
        self.calls = []

    def order_topup(self, **kwargs):
        self.calls.append(("topup", kwargs))
        return {"ok": True, "order": {"id": "ord-9010", "status": "processing"}}

    def order_gift_card(self, **kwargs):
        self.calls.append(("gift", kwargs))
        return {"ok": True, "order": {"id": "ord-9011", "status": "processing"}}

    def order_game_key(self, **kwargs):
        self.calls.append(("key", kwargs))
        return {"ok": True, "order": {"id": "ord-9012", "status": "processing"}}

    def order_manual_service(self, **kwargs):
        self.calls.append(("manual", kwargs))
        return {"ok": True, "order": {"id": "ord-9015", "status": "processing"}}

    def order_steam_topup(self, **kwargs):
        self.calls.append(("steam", kwargs))
        return {"ok": True, "order": {"id": "ord-9013", "status": "processing"}}

    def order_steam_gift(self, **kwargs):
        self.calls.append(("gift-steam", kwargs))
        return {"ok": True, "order": {"id": "ord-9014", "status": "processing"}}


class _CheckoutPayloads:
    def __init__(self, payload=None, *, fail=False):
        self.payload = payload or {
            "category_id": "mlbb",
            "offer_id": "d55",
            "fields": {"player_id": "private-recipient"},
        }
        self.fail = fail
        self.calls = []

    def fulfillment_payload(self, *, customer_id, order_id):
        self.calls.append((customer_id, order_id))
        if self.fail:
            raise ValueError("private payload unavailable")
        return self.payload


class GameFulfillmentTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.orders = GameOrderRepository(root / "orders.sqlite3")
        self.ledger = CustomerLedger(root / "ledger.sqlite3")
        self.payments = CustomerPaymentService(self.orders, self.ledger)
        self.fulfillment_key = Fernet.generate_key()
        _order(self.orders)
        self.payments.record_verified_credit(
            customer_id="customer-1", amount_minor=4500, currency="MMK",
            event_key="payment-1", actor="staff:1", reason="verified transfer", occurred_at=NOW,
        )
        self.payments.reserve_order(
            "order-1", event_key="reserve-1", actor="system", reason="reserve order", occurred_at=NOW,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_worker_submission_is_durable_idempotent_and_captures_reserved_funds(self):
        supplier = _Supplier()
        tasks = FulfillmentTaskStore(Path(self.tempdir.name) / "tasks.sqlite3", encryption_key=self.fulfillment_key)
        worker = GameFulfillmentWorker(
            orders=self.orders, tasks=tasks, supplier=supplier, payments=self.payments,
        )
        worker.enqueue_paid_order(
            "order-1",
            payload={"category_id": "mlbb", "offer_id": "d55", "fields": {"player_id": "opaque"}},
            now=NOW,
        )
        self.assertEqual(self.orders.get("order-1").state, OrderState.FULFILLMENT_QUEUED)
        first = worker.run_once(now=NOW)
        self.assertEqual(first[0].status, "submitted")
        self.assertEqual(self.orders.get("order-1").state, OrderState.SUBMITTED)
        self.assertEqual(self.orders.get("order-1").provider_order_id, "ord-9001")
        balance = self.ledger.balance("customer-1")
        self.assertEqual(balance.available_minor, 0)
        self.assertEqual(balance.reserved_minor, 0)
        self.assertEqual(tasks.get("order-1").status, "submitted")
        self.assertEqual(len(supplier.calls), 1)
        self.assertTrue(supplier.calls[0][2].startswith("aurix-game:"))
        with sqlite3.connect(Path(self.tempdir.name) / "tasks.sqlite3") as connection:
            raw = connection.execute("SELECT payload_json FROM game_fulfillment_tasks").fetchone()[0]
        self.assertNotIn("opaque", raw)
        restarted_tasks = FulfillmentTaskStore(
            Path(self.tempdir.name) / "tasks.sqlite3", encryption_key=self.fulfillment_key
        )
        self.assertEqual(restarted_tasks.get("order-1").payload["fields"], {"player_id": "opaque"})
        self.assertEqual(worker.run_once(now=NOW), ())
        self.assertEqual(len(supplier.calls), 1)

    def test_dispatcher_prepares_paid_order_without_supplier_call_and_is_idempotent(self):
        supplier = _Supplier()
        tasks_path = Path(self.tempdir.name) / "tasks.sqlite3"
        tasks = FulfillmentTaskStore(tasks_path, encryption_key=self.fulfillment_key)
        checkout = _CheckoutPayloads()
        dispatcher = PaidOrderDispatcher(orders=self.orders, tasks=tasks, checkout=checkout)

        first = dispatcher.run_once(now=NOW)
        second = dispatcher.run_once(now=NOW)

        self.assertEqual([(item.order_id, item.status) for item in first], [("order-1", "queued")])
        self.assertEqual(second, ())
        self.assertEqual(self.orders.get("order-1").state, OrderState.FULFILLMENT_QUEUED)
        self.assertEqual(tasks.get("order-1").payload["fields"]["player_id"], "private-recipient")
        with sqlite3.connect(tasks_path) as connection:
            encrypted = connection.execute(
                "SELECT payload_json FROM game_fulfillment_tasks WHERE order_id = ?", ("order-1",)
            ).fetchone()[0]
        self.assertNotIn("private-recipient", encrypted)
        self.assertEqual(supplier.calls, [])

    def test_dispatcher_recovers_task_written_before_order_transition(self):
        tasks = FulfillmentTaskStore(
            Path(self.tempdir.name) / "tasks.sqlite3", encryption_key=self.fulfillment_key
        )
        checkout = _CheckoutPayloads()
        dispatcher = PaidOrderDispatcher(orders=self.orders, tasks=tasks, checkout=checkout)
        original_transition = self.orders.transition

        def interrupted_transition(order_id, target, **kwargs):
            if target == OrderState.FULFILLMENT_QUEUED:
                raise RuntimeError("simulated process interruption")
            return original_transition(order_id, target, **kwargs)

        self.orders.transition = interrupted_transition
        interrupted = dispatcher.run_once(now=NOW)
        self.assertEqual(interrupted[0].status, "retry")
        self.assertEqual(self.orders.get("order-1").state, OrderState.PAID_RESERVED)
        self.assertIsNotNone(tasks.get("order-1"))

        self.orders.transition = original_transition
        recovered = dispatcher.run_once(now=NOW + timedelta(seconds=1))
        self.assertEqual(recovered[0].status, "queued")
        self.assertEqual(self.orders.get("order-1").state, OrderState.FULFILLMENT_QUEUED)

    def test_worker_defers_queue_task_while_paid_order_handoff_is_pending(self):
        supplier = _Supplier()
        tasks = FulfillmentTaskStore(
            Path(self.tempdir.name) / "tasks.sqlite3", encryption_key=self.fulfillment_key
        )
        tasks.enqueue(
            order_id="order-1",
            fulfillment_kind="direct_topup",
            payload={"category_id": "mlbb", "offer_id": "d55", "fields": {"player_id": "opaque"}},
            available_at=NOW,
        )
        worker = GameFulfillmentWorker(
            orders=self.orders, tasks=tasks, supplier=supplier, payments=self.payments,
        )

        result = worker.run_once(now=NOW)

        self.assertEqual(result[0].status, "waiting_for_order_transition")
        self.assertEqual(tasks.get("order-1").status, "retry")
        self.assertEqual(self.orders.get("order-1").state, OrderState.PAID_RESERVED)
        self.assertEqual(self.ledger.balance("customer-1").reserved_minor, 4500)
        self.assertEqual(supplier.calls, [])

    def test_disabled_supplier_write_is_retryable_without_charging_or_state_rewind(self):
        supplier = _Supplier(disabled=True)
        tasks = FulfillmentTaskStore(Path(self.tempdir.name) / "tasks.sqlite3", encryption_key=self.fulfillment_key)
        worker = GameFulfillmentWorker(
            orders=self.orders, tasks=tasks, supplier=supplier, payments=self.payments,
        )
        worker.enqueue_paid_order(
            "order-1",
            payload={"category_id": "mlbb", "offer_id": "d55", "fields": {"player_id": "opaque"}},
            now=NOW,
        )
        result = worker.run_once(now=NOW)
        self.assertEqual(result[0].status, "retry")
        self.assertEqual(self.orders.get("order-1").state, OrderState.FULFILLMENT_QUEUED)
        self.assertEqual(self.ledger.balance("customer-1").reserved_minor, 4500)
        self.assertEqual(tasks.get("order-1").status, "retry")

    def test_uncertain_supplier_write_is_quarantined_without_retry_or_refund(self):
        for response in (RuntimeError("timeout after acceptance: private details"), {"ok": True, "order": {}}):
            with self.subTest(response=type(response).__name__):
                # Use separate stores because each uncertain purchase must remain held.
                with tempfile.TemporaryDirectory() as directory:
                    orders = GameOrderRepository(Path(directory) / "orders.sqlite3")
                    ledger = CustomerLedger(Path(directory) / "ledger.sqlite3")
                    payments = CustomerPaymentService(orders, ledger)
                    _order(orders)
                    payments.record_verified_credit(customer_id="customer-1", amount_minor=4500,
                        currency="MMK", event_key="credit",
                        actor="staff:1", reason="verified", occurred_at=NOW)
                    payments.reserve_order("order-1", event_key="reserve", actor="system",
                        reason="reserve", occurred_at=NOW)
                    tasks = FulfillmentTaskStore(Path(directory) / "tasks.sqlite3", encryption_key=self.fulfillment_key)
                    supplier = _Supplier()
                    worker = GameFulfillmentWorker(orders=orders, tasks=tasks, supplier=supplier, payments=payments)
                    worker.enqueue_paid_order("order-1", payload={"offer_id": "d55"}, now=NOW)
                    with patch.object(supplier, "submit") as submit:
                        if isinstance(response, Exception):
                            submit.side_effect = response
                        else:
                            submit.return_value = response
                        outcome = worker.run_once(now=NOW)
                        self.assertIn("uncertain", outcome[0].error)
                        self.assertNotIn("private", outcome[0].error)
                        self.assertEqual(worker.run_once(now=NOW + timedelta(hours=1)), ())
                        self.assertEqual(submit.call_count, 1)
                    self.assertEqual(orders.get("order-1").state, OrderState.UNKNOWN)
                    self.assertEqual(ledger.balance("customer-1").reserved_minor, 4500)

    def test_repeated_supplier_failure_stops_in_review_without_auto_refund(self):
        supplier = _Supplier(failed=True)
        tasks = FulfillmentTaskStore(Path(self.tempdir.name) / "tasks.sqlite3", encryption_key=self.fulfillment_key)
        worker = GameFulfillmentWorker(
            orders=self.orders, tasks=tasks, supplier=supplier, payments=self.payments,
            max_attempts=1,
        )
        worker.enqueue_paid_order(
            "order-1",
            payload={"category_id": "mlbb", "offer_id": "d55", "fields": {"player_id": "opaque"}},
            now=NOW,
        )
        result = worker.run_once(now=NOW)
        self.assertEqual(result[0].status, "failed")
        self.assertEqual(self.orders.get("order-1").state, OrderState.UNKNOWN)
        self.assertEqual(self.ledger.balance("customer-1").reserved_minor, 4500)
        with self.assertRaises(PaymentBoundaryError):
            self.payments.release_order("order-1", event_key="early-release", actor="staff:1", reason="unresolved", occurred_at=NOW)
        self.orders.transition("order-1", OrderState.RECONCILIATION, at=NOW, reason="reviewed supplier records")
        self.orders.transition("order-1", OrderState.REVIEW_REFUND, at=NOW, reason="supplier debit ruled out")
        released = self.payments.release_order(
            "order-1", event_key="release-after-review", actor="staff:1",
            reason="operator approved refund after retry exhaustion", occurred_at=NOW,
        )
        self.assertEqual(released.order.state, OrderState.REFUNDED)
        self.assertEqual(self.ledger.balance("customer-1").available_minor, 4500)

    def test_payment_service_release_returns_reserved_funds_and_closes_order(self):
        result = self.payments.release_order(
            "order-1", event_key="release-1", actor="staff:1",
            reason="customer cancellation", occurred_at=NOW,
        )
        self.assertEqual(result.order.state, OrderState.REFUNDED)
        self.assertEqual(self.ledger.balance("customer-1").available_minor, 4500)
        self.assertEqual(self.ledger.balance("customer-1").reserved_minor, 0)

    def test_fazercards_adapter_forwards_private_topup_payload_and_idempotency(self):
        client = _CommandClient()
        adapter = FazerCardsOrderAdapter(client)
        response = adapter.submit(
            fulfillment_kind="direct_topup",
            payload={"category_id": "mlbb", "offer_id": "d55", "fields": {"player_id": "opaque"}},
            idempotency_key="provider-key-1",
        )
        self.assertEqual(response["order"]["id"], "ord-9010")
        self.assertEqual(client.calls[0][1]["idempotency_key"], "provider-key-1")
        self.assertEqual(client.calls[0][1]["fields"], {"player_id": "opaque"})

    def test_fazercards_adapter_forwards_manual_service_ids_fields_and_idempotency(self):
        client = _CommandClient()
        adapter = FazerCardsOrderAdapter(client)
        response = adapter.submit(
            fulfillment_kind="manual_service",
            payload={
                "manual_service_id": "manual-service-42",
                "product_id": "product-7",
                "fields": {"player_id": "opaque", "server": "global"},
            },
            idempotency_key="provider-key-manual-1",
        )

        self.assertEqual(response["order"]["id"], "ord-9015")
        self.assertEqual(client.calls[0][0], "manual")
        self.assertEqual(
            client.calls[0][1],
            {
                "manual_service_id": "manual-service-42",
                "product_id": "product-7",
                "fields": {"player_id": "opaque", "server": "global"},
                "idempotency_key": "provider-key-manual-1",
            },
        )

    def test_manual_service_adapter_accepts_services_without_recipient_fields(self):
        client = _CommandClient()
        adapter = FazerCardsOrderAdapter(client)

        adapter.submit(
            fulfillment_kind="manual_service",
            payload={"manual_service_id": "manual-service-42", "product_id": "product-7"},
            idempotency_key="provider-key-manual-2",
        )

        self.assertIsNone(client.calls[0][1]["fields"])

    def test_manual_service_adapter_rejects_malformed_recipient_fields(self):
        client = _CommandClient()
        adapter = FazerCardsOrderAdapter(client)

        with self.assertRaisesRegex(ValueError, "fields is invalid"):
            adapter.submit(
                fulfillment_kind="manual_service",
                payload={
                    "manual_service_id": "manual-service-42",
                    "product_id": "product-7",
                    "fields": ["player_id"],
                },
                idempotency_key="provider-key-manual-3",
            )

        self.assertEqual(client.calls, [])

    def test_recovery_after_acceptance_and_after_capture_never_resubmits(self):
        supplier = _Supplier()
        tasks = FulfillmentTaskStore(Path(self.tempdir.name) / "tasks.sqlite3", encryption_key=self.fulfillment_key)
        worker = GameFulfillmentWorker(orders=self.orders, tasks=tasks, supplier=supplier, payments=self.payments)
        worker.enqueue_paid_order("order-1", payload={"offer_id": "d55"}, now=NOW)
        with patch.object(self.orders, "transition", side_effect=RuntimeError("order store offline")):
            self.assertEqual(worker.run_once(now=NOW)[0].status, "recovery_pending")
        self.assertEqual(tasks.get("order-1").provider_order_id, "ord-9001")
        self.assertEqual(self.orders.get("order-1").state, OrderState.FULFILLMENT_QUEUED)
        with patch.object(tasks, "mark_submitted", side_effect=RuntimeError("queue unavailable")):
            self.assertEqual(worker.run_once(now=NOW + timedelta(minutes=11))[0].status, "recovery_pending")
        self.assertEqual(self.ledger.balance("customer-1").reserved_minor, 0)
        self.assertEqual(worker.run_once(now=NOW + timedelta(minutes=22))[0].status, "submitted")
        self.assertEqual(tasks.get("order-1").status, "submitted")
        self.assertEqual(len(supplier.calls), 1)
        self.assertEqual(self.ledger.balance("customer-1").available_minor, 0)

    def test_crash_before_receipt_persistence_never_submits_again(self):
        supplier = _Supplier()
        tasks = FulfillmentTaskStore(Path(self.tempdir.name) / "tasks.sqlite3", encryption_key=self.fulfillment_key)
        worker = GameFulfillmentWorker(orders=self.orders, tasks=tasks, supplier=supplier, payments=self.payments)
        worker.enqueue_paid_order("order-1", payload={"offer_id": "d55"}, now=NOW)
        with patch.object(tasks, "record_acceptance", side_effect=RuntimeError("disk unavailable")):
            self.assertEqual(worker.run_once(now=NOW)[0].status, "recovery_pending")
        self.assertIsNotNone(tasks.get("order-1").dispatch_started_at)
        self.assertEqual(worker.run_once(now=NOW + timedelta(days=8))[0].status, "failed")
        self.assertEqual(self.orders.get("order-1").state, OrderState.UNKNOWN)
        self.assertEqual(len(supplier.calls), 1)
        self.assertEqual(self.ledger.balance("customer-1").reserved_minor, 4500)

    def test_legacy_running_task_migrates_as_uncertain_without_purchase(self):
        supplier = _Supplier()
        path = Path(self.tempdir.name) / "tasks.sqlite3"
        tasks = FulfillmentTaskStore(path, encryption_key=self.fulfillment_key)
        worker = GameFulfillmentWorker(orders=self.orders, tasks=tasks, supplier=supplier, payments=self.payments)
        worker.enqueue_paid_order("order-1", payload={"offer_id": "d55"}, now=NOW)
        tasks.claim_due(now=NOW)
        with sqlite3.connect(path) as connection:
            connection.execute("ALTER TABLE game_fulfillment_tasks DROP COLUMN dispatch_started_at")
        self.assertEqual(worker.run_once(now=NOW + timedelta(minutes=11))[0].status, "failed")
        self.assertEqual(self.orders.get("order-1").state, OrderState.UNKNOWN)
        self.assertEqual(supplier.calls, [])

    def test_reclaimed_attempt_wins_over_late_supplier_response(self):
        supplier = _Supplier()
        tasks = FulfillmentTaskStore(Path(self.tempdir.name) / "tasks.sqlite3", encryption_key=self.fulfillment_key)
        worker = GameFulfillmentWorker(orders=self.orders, tasks=tasks, supplier=supplier, payments=self.payments)
        worker.enqueue_paid_order("order-1", payload={"offer_id": "d55"}, now=NOW)
        submit = supplier.submit

        def delayed(**kwargs):
            with patch.object(supplier, "submit", wraps=submit):
                self.assertEqual(worker.run_once(now=NOW + timedelta(minutes=11))[0].status, "failed")
            return submit(**kwargs)

        with patch.object(supplier, "submit", side_effect=delayed):
            self.assertEqual(worker.run_once(now=NOW)[0].status, "claim_lost")
        self.assertEqual(tasks.get("order-1").status, "failed")
        self.assertEqual(self.orders.get("order-1").state, OrderState.UNKNOWN)
        self.assertEqual(len(supplier.calls), 1)

    def test_stale_running_task_is_reclaimed_after_restart_window(self):
        tasks = FulfillmentTaskStore(
            Path(self.tempdir.name) / "tasks.sqlite3", encryption_key=self.fulfillment_key
        )
        tasks.enqueue(
            order_id="order-1",
            fulfillment_kind="direct_topup",
            payload={"category_id": "mlbb", "offer_id": "d55", "fields": {"player_id": "opaque"}},
            available_at=NOW,
        )
        first = tasks.claim_due(now=NOW)
        self.assertEqual(first[0].attempts, 1)
        reclaimed = tasks.claim_due(now=NOW + timedelta(minutes=11))
        self.assertEqual(reclaimed[0].attempts, 2)
        self.assertEqual(reclaimed[0].payload["offer_id"], "d55")
        for write in (
            lambda: tasks.record_acceptance(first[0], "ord-123"),
            lambda: tasks.mark_failed(first[0], error="late failure", now=NOW),
            lambda: tasks.mark_retry(first[0], error="late retry", next_at=NOW, now=NOW),
            lambda: tasks.mark_submitted(first[0], provider_order_id="ord-123", now=NOW),
        ):
            with self.assertRaises(FulfillmentClaimLost):
                write()
        self.assertEqual(tasks.get("order-1").status, "running")
        self.assertIsNone(tasks.get("order-1").provider_order_id)



if __name__ == "__main__":
    unittest.main()
