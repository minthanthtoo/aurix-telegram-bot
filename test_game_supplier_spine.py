"""Fault and concurrency coverage for the supplier/payment handoff."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from aurix_games.customer_ledger import CustomerLedger, LedgerConflictError
from aurix_games.customer_checkout import CheckoutInputError
from aurix_games.customer_api import CustomerApiError, CustomerOrderService
from aurix_games.backup_restore import create_backup, restore_backup
from aurix_games.fulfillment import FulfillmentTaskStore, GameFulfillmentWorker, PaidOrderDispatcher
from aurix_games.fulfillment_audit import audit_fulfillment
from aurix_games.eligibility import RegionDimension, RegionRule
from aurix_games.order_reconciliation import SupplierWebhookReconciler, drain_provider_events
from aurix_games.order_repository import OrderRepositoryError
from aurix_games.order_state import OrderState
from aurix_games.payment_service import CustomerPaymentService, PaymentBoundaryError
from test_game_fulfillment import _Supplier
import test_game_checkout_journey as journey_fixtures

NOW = journey_fixtures.NOW


class SupplierSpineTests(unittest.TestCase):
    def setUp(self):
        journey_fixtures.CheckoutJourneyTests.setUp(self)

    def _payment(self):
        quote = self.journey.review(self.principal, self.payload, now=NOW)["quote"]
        self.order_id = self.journey.confirm(self.principal,
            {"quote_id": quote["quote_id"], "confirmed": True}, now=NOW)["order"]["order_id"]
        intent = self.journey.open_payment(self.principal, quote["quote_id"], rail="kpay",
            receiver_id="receiver-1", receiver_revision="v1", now=NOW, receipt_review_ready=True)
        return quote, intent

    def _service(self):
        return CustomerPaymentService(self.checkout.repository, CustomerLedger(Path(self.temp.name) / "ledger.db"))

    def _settle(self, service, **changes):
        args = dict(rail="kpay", receiver_id="receiver-1", transaction_id="tx-123",
                    amount_minor=5000, currency="MMK", actor="staff:alice", occurred_at=NOW)
        args.update(changes)
        return service.settle_verified_transfer(self.order_id, **args)

    def test_game_specific_routes_complete_customer_and_admin_payment_to_delivery(self):
        from aurix_games.payment_review import PaymentReviewService
        scenarios = (
            ("mobile-legends", "Mobile Legends", "mobile_legends_global", "mobile_legends",
             {"player_id": "123456789", "server_id": "4574"}, {"user_id": "player_id", "zone_id": "server_id"}),
            ("pubg-mobile", "PUBG Mobile", "pubg_mobile_auto", "pubg_mobile",
             {"player_id": "123456789"}, {}),
            ("freefire", "Free Fire", "free_fire_th", "free_fire",
             {"player_id": "123456789"}, {}),
        )
        for slug, name, category, validation_category, fields, field_map in scenarios:
            with self.subTest(game=slug):
                fixture = journey_fixtures.CheckoutJourneyTests()
                fixture.setUp()
                try:
                    config_path = fixture.checkout.supplier_route_store.path
                    config = json.loads(config_path.read_text())
                    row = config["mappings"][0]
                    row.update(product_slug=slug, provider_category_id=category,
                               validation_category_id=validation_category,
                               required_input_labels=list(fields), validation_field_map=field_map,
                               region_rule={"verified": True, "allow_all": False,
                                            "dimension": "game_server", "allowed_values": ["th"]},
                               region_label="Thailand", region_dimension="game_server",
                               validation_region_map={"Thailand": "th"})
                    config_path.write_text(json.dumps(config))
                    fixture.offers["offer-1"] = replace(fixture.offer, product_slug=slug, product_name=name,
                        required_input_labels=tuple(fields),
                        region_rule=RegionRule(verified=True, allow_all=False,
                            dimension=RegionDimension.GAME_SERVER, allowed_values=frozenset({"th"})),
                        region_label="Thailand", region_dimension=RegionDimension.GAME_SERVER)
                    response = {**fixture.response, "category_id": validation_category, "region": "Thailand"}
                    with patch.object(fixture.journey.supplier, "validate_topup_recipient", return_value=response) as lookup:
                        quote = fixture.journey.review(fixture.principal,
                            {"offer_id": "offer-1", "fields": fields}, now=NOW)["quote"]
                        lookup.assert_called_once_with(validation_category,
                            {target: fields[source] for target, source in field_map.items()} if field_map else fields)
                    self.assertEqual(quote["region"], "Thailand")
                    order = fixture.journey.confirm(fixture.principal,
                        {"quote_id": quote["quote_id"], "confirmed": True}, now=NOW)["order"]
                    fixture.journey.payment_receivers = {"kpay": {"id": "receiver-1", "revision": "v1",
                        "account": "test-account", "account_name": "Test merchant"}}
                    instructions = fixture.journey.payment(fixture.principal,
                        {"order_id": order["order_id"], "rail": "kpay"}, now=NOW,
                        receipt_review_ready=True)["payment"]
                    self.assertEqual(instructions["amount_minor"], 5000)
                    payments = CustomerPaymentService(fixture.checkout.repository,
                        CustomerLedger(Path(fixture.temp.name) / "ledger.db"))
                    token = "test-reviewer-credential-" + "x" * 32
                    review = PaymentReviewService(payments, {"staff:reviewer": hashlib.sha256(token.encode()).hexdigest()})
                    review.submit(order["order_id"], customer_id=fixture.principal.customer_id,
                        transaction_reference="test-claim", evidence_sha256="a"*64, now=NOW)
                    case = review.queue(token)["cases"][0]
                    self.assertEqual(case["product_slug"], slug)
                    self.assertEqual(case["offer_id"], "offer-1")
                    decision = dict(token=token, approve=True, reason="matched test merchant record",
                        verified_transfer=dict(rail="kpay", receiver_id="receiver-1", transaction_id="test-transfer",
                            amount_minor=5000, currency="MMK", occurred_at=NOW), now=NOW)
                    review.decide(order["order_id"], **decision)
                    tasks = FulfillmentTaskStore(Path(fixture.temp.name) / "tasks.db",
                        encryption_key=journey_fixtures.Fernet.generate_key())
                    dispatcher = PaidOrderDispatcher(orders=fixture.checkout.repository,
                        tasks=tasks, checkout=fixture.checkout)
                    self.assertEqual(dispatcher.run_once(now=NOW)[0].status, "queued")
                    supplier = _Supplier()
                    worker = GameFulfillmentWorker(orders=fixture.checkout.repository,
                        tasks=tasks, supplier=supplier, payments=payments)
                    self.assertEqual(worker.run_once(now=NOW)[0].status, "submitted")
                    self.assertEqual(supplier.calls[0][1]["category_id"], category)
                    self.assertEqual(supplier.calls[0][1]["fields"], fields)
                    reconciler = SupplierWebhookReconciler(fixture.checkout.repository, webhook_secret="secret")
                    body = json.dumps({"event": "order.status_changed", "event_id": "delivered",
                        "timestamp": NOW.isoformat(), "data": {"order_id": "ord-9001", "status": "completed"}}).encode()
                    reconciler.receive(body, "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest(), now=NOW)
                    drain_provider_events(fixture.checkout.repository, now=NOW, limit=10)
                    detail = CustomerOrderService(fixture.checkout.repository, payments.ledger).get_order(
                        fixture.principal, order["order_id"])
                    self.assertEqual(detail["state"], "delivered")
                    self.assertEqual(len(supplier.calls), 1)
                    refund_body = json.dumps({"event": "order.status_changed", "event_id": "supplier-refund",
                        "timestamp": NOW.isoformat(), "data": {"order_id": "ord-9001", "status": "refund"}}).encode()
                    reconciler.receive(refund_body,
                        "sha256=" + hmac.new(b"secret", refund_body, hashlib.sha256).hexdigest(), now=NOW)
                    drain_provider_events(fixture.checkout.repository, now=NOW, limit=10)
                    fixture.checkout.repository.transition(order["order_id"], OrderState.REVIEW_REFUND,
                        at=NOW, reason="reviewed supplier refund")
                    refund = dict(event_key="refund-1", actor="staff:reviewer",
                        reason="supplier refund verified", occurred_at=NOW)
                    payments.refund_captured_order(order["order_id"], **refund)
                    self.assertTrue(payments.refund_captured_order(order["order_id"], **refund).ledger.replayed)
                    refunded = CustomerOrderService(fixture.checkout.repository, payments.ledger).get_order(
                        fixture.principal, order["order_id"])
                    self.assertEqual(refunded["state"], "refunded")
                    self.assertEqual(payments.ledger.balance(fixture.principal.customer_id).available_minor, 5000)
                    self.assertEqual(review.order_history(order["order_id"], token=token)["product_slug"], slug)
                    self.assertEqual(audit_fulfillment(fixture.checkout.repository.path,
                        payments.ledger.path, tasks.path)["total_findings"], 0)
                finally:
                    fixture.doCleanups()

    def test_verified_transfer_to_delivery_refund_and_restore(self):
        self._payment()
        service = self._service()
        self._settle(service)
        tasks = FulfillmentTaskStore(Path(self.temp.name) / "tasks.db",
            encryption_key=journey_fixtures.Fernet.generate_key())
        dispatcher = PaidOrderDispatcher(orders=self.checkout.repository, tasks=tasks, checkout=self.checkout)
        self.assertEqual(dispatcher.run_once(now=NOW)[0].status, "queued")
        supplier = _Supplier()
        worker = GameFulfillmentWorker(orders=self.checkout.repository, tasks=tasks, supplier=supplier, payments=service)
        self.assertEqual(worker.run_once(now=NOW)[0].status, "submitted")
        reconciler = SupplierWebhookReconciler(self.checkout.repository, webhook_secret="secret")
        for event_id, status in (("delivered", "completed"), ("supplier-refund", "refund")):
            body = json.dumps({"event": "order.status_changed", "event_id": event_id, "timestamp": NOW.isoformat(),
                "data": {"order_id": "ord-9001", "status": status}}).encode()
            reconciler.receive(body, "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest(), now=NOW)
            self.assertEqual(len(drain_provider_events(self.checkout.repository, now=NOW, limit=10)), 1)
            self.assertEqual(service.ledger.balance("customer-1").available_minor, 0)
        self.checkout.repository.transition(self.order_id, OrderState.REVIEW_REFUND, at=NOW, reason="refund approved")
        refund = dict(event_key="refund-1", actor="staff:alice", reason="supplier refund verified", occurred_at=NOW)
        self.assertEqual(service.refund_captured_order(self.order_id, **refund).order.state, OrderState.REFUNDED)
        self.assertTrue(service.refund_captured_order(self.order_id, **refund).ledger.replayed)
        self.assertEqual(service.ledger.balance("customer-1").available_minor, 5000)
        self.assertEqual(len(supplier.calls), 1)
        self.assertEqual(audit_fulfillment(self.checkout.repository.path, service.ledger.path, tasks.path)["total_findings"], 0)
        with tempfile.TemporaryDirectory() as destination:
            backup = Path(destination) / "backup"
            restored = Path(destination) / "restored"
            create_backup(self.temp.name, backup)
            restore_backup(backup, restored)
            restored_orders = type(self.checkout.repository)(restored / self.checkout.repository.path.name)
            self.assertEqual(restored_orders.get(self.order_id).state, OrderState.REFUNDED)
            self.assertEqual(restored_orders.get_payment_intent(self.order_id)["receiver_revision"], "v1")

    def test_payment_rechecks_current_offer_and_freezes_receiver(self):
        quote, intent = self._payment()
        self.assertEqual(intent["amount_minor"], 5000)
        for offer in (replace(self.offer, retail_amount_minor=6000), replace(self.offer, available=False),
                      replace(self.offer, customer_visible=False), replace(self.offer, observed_at=NOW - timedelta(hours=1))):
            self.offers["offer-1"] = offer
            with self.assertRaises(CheckoutInputError):
                self.journey.open_payment(self.principal, quote["quote_id"], rail="kpay",
                    receiver_id="receiver-1", receiver_revision="v1", now=NOW, receipt_review_ready=True)
        self.offers["offer-1"] = self.offer
        with self.assertRaises(CheckoutInputError):
            self.journey.open_payment(self.principal, quote["quote_id"], rail="kpay",
                receiver_id="receiver-2", receiver_revision="v2", now=NOW, receipt_review_ready=True)
        saved = self.checkout.repository.get_payment_intent(self.order_id)
        self.assertEqual(saved["receiver_id"], "receiver-1")
        with self.assertRaises(CheckoutInputError):
            self.journey.open_payment(self.principal, quote["quote_id"], rail="kpay",
                receiver_id="receiver-1", receiver_revision="v1", now=NOW + timedelta(seconds=111),
                receipt_review_ready=True)

    def test_concurrent_settlement_allocates_and_reserves_once(self):
        self._payment()
        service = self._service()
        service.ledger.initialize()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self._settle(service), range(4)))
        self.assertTrue(all(result.order.state == OrderState.PAID_RESERVED for result in results))
        self.assertEqual(service.ledger.balance("customer-1").available_minor, 0)
        self.assertEqual(service.ledger.balance("customer-1").reserved_minor, 5000)
        with sqlite3.connect(service.ledger.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM game_ledger_entries").fetchone()[0], 2)
        with self.assertRaises(LedgerConflictError):
            self._settle(service, transaction_id="different-transfer")
        self.checkout.repository.transition(self.order_id, OrderState.FULFILLMENT_QUEUED, at=NOW, reason="queued")
        self.assertEqual(self._settle(service).order.state, OrderState.FULFILLMENT_QUEUED)
        with self.assertRaises(LedgerConflictError):
            self._settle(service, transaction_id="another-transfer")

    def test_payment_instructions_survive_receiver_rotation_and_quote_expiry(self):
        quote = self.journey.review(self.principal, self.payload, now=NOW)["quote"]
        self.journey.confirm(self.principal, {"quote_id": quote["quote_id"], "confirmed": True}, now=NOW)
        self.journey.payment_receivers = {"kpay": {"id": "receiver-1", "revision": "v1",
            "account": "091234", "account_name": "Approved shop"}}
        payload = {"quote_id": quote["quote_id"], "rail": "kpay"}
        first = self.journey.payment(self.principal, payload, now=NOW, receipt_review_ready=True)["payment"]
        self.journey.payment_receivers["kpay"]["account"] = "099999"
        replay = self.journey.payment(self.principal, payload, now=NOW + timedelta(minutes=2),
            receipt_review_ready=True)["payment"]
        self.assertEqual(first, replay)
        expired = self.journey.payment(self.principal, payload, now=NOW + timedelta(minutes=11),
            receipt_review_ready=True)["payment"]
        self.assertTrue(expired["expired"])
        self.assertEqual(expired["account"], "091234")
        self.journey.payment_receivers = {}
        service = CustomerOrderService(self.checkout.repository)
        detail = service.get_order(self.principal, first["order_id"])
        self.assertEqual(detail["payment"]["account"], "091234")
        with self.assertRaises(CustomerApiError):
            service.get_order(replace(self.principal, customer_id="other"), first["order_id"])

    def test_lost_order_transition_keeps_reserved_transfer_and_recovers(self):
        self._payment()
        service = self._service()
        with patch.object(self.checkout.repository, "transition", side_effect=RuntimeError("disk full")):
            with self.assertRaises(PaymentBoundaryError):
                self._settle(service)
        self.assertEqual(service.ledger.balance("customer-1").reserved_minor, 5000)
        self.assertEqual(self._settle(service).order.state, OrderState.PAID_RESERVED)

    def test_same_transfer_cannot_pay_another_order(self):
        self._payment()
        service = self._service()
        self._settle(service)
        original = self.checkout.repository.get(self.order_id)
        second = replace(original, order_id="second-order", customer_id="customer-2",
            customer_idempotency_key="second-key", provider_idempotency_key="second-supplier-key",
            state=OrderState.DRAFT, events=())
        self.checkout.repository.create(second, created_at=NOW)
        self.checkout.repository.transition(second.order_id, OrderState.AWAITING_PAYMENT, at=NOW, reason="test")
        self.checkout.repository.freeze_payment_intent(order_id=second.order_id, customer_id="customer-2",
            rail="kpay", receiver_id="receiver-1", receiver_revision="v1", opened_at=NOW,
            expires_at=NOW + timedelta(minutes=10))
        self.order_id = second.order_id
        with self.assertRaises(LedgerConflictError):
            self._settle(service)
        self.assertIsNone(service.ledger.balance("customer-2"))

    def test_partial_late_and_wrong_receiver_do_not_credit(self):
        self._payment()
        service = self._service()
        for changes in ({"amount_minor": 4000}, {"receiver_id": "other"}, {"currency": "THB"},
                        {"occurred_at": NOW + timedelta(minutes=10)}, {"occurred_at": NOW - timedelta(seconds=1)}):
            with self.assertRaises(PaymentBoundaryError):
                self._settle(service, **changes)
        self.assertIsNone(service.ledger.balance("customer-1"))

    def test_release_replay_and_capture_key_collision_are_safe(self):
        self._payment()
        service = self._service()
        self._settle(service)
        args = dict(event_key="release-1", actor="staff:alice", reason="approved", occurred_at=NOW)
        original_transition = self.checkout.repository.transition
        def interrupted(order_id, target, **kwargs):
            if target == OrderState.REFUNDED:
                raise RuntimeError("disk full")
            return original_transition(order_id, target, **kwargs)
        with patch.object(self.checkout.repository, "transition", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                service.release_order(self.order_id, **args)
        service.release_order(self.order_id, **args)
        self.assertTrue(service.release_order(self.order_id, **args).ledger.replayed)
        with self.assertRaises(LedgerConflictError):
            service.ledger.capture(order_id=self.order_id, **args)
        self.assertEqual(service.ledger.balance("customer-1").available_minor, 5000)

    def test_mlbb_requires_explicit_pack_regions_and_maps_provider_country(self):
        mapping = Path(self.temp.name) / "mapping.json"
        config = json.loads(mapping.read_text())
        row = config["mappings"][0]
        row["provider_category_id"] = "mobile_legends_global"
        mapping.write_text(json.dumps(config))
        with patch.object(self.journey.supplier, "validate_topup_recipient") as lookup:
            with self.assertRaises(CheckoutInputError):
                self.journey.review(self.principal, self.payload, now=NOW)
            lookup.assert_not_called()
        row["region_rule"] = {"verified": True, "allow_all": False, "dimension": "game_server", "allowed_values": ["mm", "th"]}
        row["region_label"] = "Myanmar / Thailand"
        row["validation_region_map"] = {"Myanmar": "mm", "Thailand": "th"}
        mapping.write_text(json.dumps(config))
        self.offers["offer-1"] = replace(self.offer,
            region_rule=RegionRule(verified=True, dimension=RegionDimension.GAME_SERVER, allowed_values=frozenset({"mm", "th"})),
            region_label="Myanmar / Thailand")
        self.payload["region"] = {"dimension": "game_server", "value": "mm"}
        self.response["region"] = "Myanmar"
        detected_payload = {key: value for key, value in self.payload.items() if key != "region"}
        detected_quote = self.journey.review(self.principal, detected_payload, now=NOW)["quote"]
        self.assertEqual(detected_quote["region"], "Myanmar")
        detected_order = self.journey.confirm(self.principal,
            {"quote_id": detected_quote["quote_id"], "confirmed": True}, now=NOW)
        self.assertEqual(detected_order["order"]["state"], "awaiting_payment")
        quote = self.journey.review(self.principal, self.payload, now=NOW + timedelta(seconds=11))["quote"]
        row["validation_region_map"] = {"Myanmar": "mm"}
        mapping.write_text(json.dumps(config))
        with self.assertRaises(CheckoutInputError):
            self.journey.confirm(self.principal, {"quote_id": quote["quote_id"], "confirmed": True}, now=NOW)

    def test_inbox_keeps_unmatched_event_and_replays_after_acceptance(self):
        self._payment()
        reconciler = SupplierWebhookReconciler(self.checkout.repository, webhook_secret="secret")
        body = json.dumps({"event": "order.status_changed", "event_id": "early", "timestamp": NOW.isoformat(),
            "data": {"order_id": "ord-12", "status": "completed", "type": "topup"}}).encode()
        signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        reconciler.receive(body, signature, now=NOW)
        self.assertEqual(drain_provider_events(self.checkout.repository, now=NOW, limit=10), ())
        for state in (OrderState.PAID_RESERVED, OrderState.FULFILLMENT_QUEUED):
            self.checkout.repository.transition(self.order_id, state, at=NOW, reason="test")
        self.checkout.repository.transition(self.order_id, OrderState.SUBMITTED, at=NOW, reason="accepted", provider_order_id="ord-12")
        self.assertEqual(drain_provider_events(self.checkout.repository, now=NOW, limit=10)[0].state, "delivered")
        self.assertEqual(drain_provider_events(self.checkout.repository, now=NOW, limit=10), ())
        body2 = body.replace(b'completed', b'failed')
        with self.assertRaises(OrderRepositoryError):
            reconciler.receive(body2, "sha256=" + hmac.new(b"secret", body2, hashlib.sha256).hexdigest(), now=NOW)

    def test_reconciliation_rotates_past_checked_orders(self):
        self._payment()
        first = self.checkout.repository.get(self.order_id)
        second = replace(first, order_id="second", customer_idempotency_key="second",
            provider_idempotency_key="second-provider", state=OrderState.DRAFT, events=())
        self.checkout.repository.create(second, created_at=NOW)
        for order_id, receipt in ((first.order_id, "ord-12"), (second.order_id, "ord-13")):
            for state in (OrderState.PAID_RESERVED, OrderState.FULFILLMENT_QUEUED):
                if order_id == second.order_id and state == OrderState.PAID_RESERVED:
                    self.checkout.repository.transition(order_id, OrderState.AWAITING_PAYMENT, at=NOW, reason="test")
                self.checkout.repository.transition(order_id, state, at=NOW, reason="test")
            self.checkout.repository.transition(order_id, OrderState.SUBMITTED, at=NOW, reason="test", provider_order_id=receipt)
        chosen = self.checkout.repository.reconciliation_batch(limit=1)[0]
        self.checkout.repository.record_poll_check(chosen.order_id, now=NOW)
        self.assertNotEqual(chosen.order_id, self.checkout.repository.reconciliation_batch(limit=1)[0].order_id)

    def test_webhook_never_delivers_codes_or_resolves_unknown_automatically(self):
        self._payment()
        original = self.checkout.repository.get(self.order_id)
        for order_id, kind, receipt in (("code-order", "gift_card", "ord-14"), ("unknown-order", "direct_topup", "ord-15")):
            order = replace(original, order_id=order_id, customer_idempotency_key=order_id,
                provider_idempotency_key=order_id, state=OrderState.DRAFT, events=(),
                snapshot=replace(original.snapshot, fulfillment_kind=kind))
            self.checkout.repository.create(order, created_at=NOW)
            for state in (OrderState.AWAITING_PAYMENT, OrderState.PAID_RESERVED, OrderState.FULFILLMENT_QUEUED):
                self.checkout.repository.transition(order_id, state, at=NOW, reason="test")
            target = OrderState.UNKNOWN if order_id == "unknown-order" else OrderState.SUBMITTED
            self.checkout.repository.transition(order_id, target, at=NOW, reason="test", provider_order_id=receipt)
            body = json.dumps({"event": "order.status_changed", "event_id": order_id, "timestamp": NOW.isoformat(),
                "data": {"order_id": receipt, "status": "completed"}}).encode()
            reconciler = SupplierWebhookReconciler(self.checkout.repository, webhook_secret="secret")
            outcome = reconciler.process(body, "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest(), now=NOW)
            self.assertEqual(outcome.state, "reconciliation")

    def test_signed_old_failure_does_not_reverse_newer_delivery(self):
        self._payment()
        for state in (OrderState.PAID_RESERVED, OrderState.FULFILLMENT_QUEUED):
            self.checkout.repository.transition(self.order_id, state, at=NOW, reason="test")
        self.checkout.repository.transition(self.order_id, OrderState.SUBMITTED, at=NOW, reason="accepted", provider_order_id="ord-12")
        reconciler = SupplierWebhookReconciler(self.checkout.repository, webhook_secret="secret")
        for event_id, status, timestamp in (("new", "completed", NOW), ("old", "failed", NOW - timedelta(seconds=1)),
                                             ("refund", "refund", NOW + timedelta(seconds=1))):
            body = json.dumps({"event": "order.status_changed", "event_id": event_id, "timestamp": timestamp.isoformat(),
                "data": {"order_id": "ord-12", "status": status}}).encode()
            signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
            outcome = reconciler.process(body, signature, now=NOW + timedelta(seconds=2))
            expected = "reconciliation" if status == "refund" else "delivered"
            self.assertEqual(outcome.state, expected)
