from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import Mock

import test_game_fulfillment as fixture
from aurix_games.fulfillment import FulfillmentTaskStore, GameFulfillmentWorker
from aurix_games.order_state import OrderSnapshot, OrderState, create_order
from aurix_games.spend_guard import PilotSpendGuard, SpendPreflightError, schema_fingerprint


class PilotSpendGuardTests(unittest.TestCase):
    def setUp(self):
        fixture.GameFulfillmentTests.setUp(self)
        self.tasks = FulfillmentTaskStore(Path(self.tempdir.name) / 'tasks.sqlite3', encryption_key=self.fulfillment_key)
        self.client = Mock()
        self.subscription = {'ok': True, 'subscriptionActive': True, 'planExpiresAt': (fixture.NOW + timedelta(days=1)).isoformat()}
        self.client.get_subscription.return_value = self.subscription
        self.client.get_balance.return_value = {'ok': True, 'balance': '20.00', 'currency': 'USD'}
        self.catalog = Mock()
        self.detail = {'category_id': 'mlbb', 'fields': [{'key': 'player_id', 'type': 'text'}],
            'offers': [{'offer_id': 'd55', 'price_usd': '1.50', 'stock': 10}]}
        self.catalog.get.return_value = self.detail
        self.payload = {'category_id': 'mlbb', 'offer_id': 'd55', 'fields': {'player_id': 'private'}}
        self.checkout = Mock()
        self.checkout.fulfillment_payload.return_value = self.payload
        self.policy = {'daily_limit_usd': '3', 'minimum_balance_usd': '1', 'offers': [
            {'category_id': 'mlbb', 'offer_id': 'd55', 'max_cost_usd': '2', 'allow_unknown_stock': False,
             'fields_sha256': schema_fingerprint(self.detail['fields'])}]}
        self.guard = PilotSpendGuard(tasks=self.tasks, client=self.client, catalog_client=self.catalog,
            checkout=self.checkout, policy=self.policy)
        self.supplier = fixture._Supplier()
        self.worker = GameFulfillmentWorker(orders=self.orders, tasks=self.tasks, supplier=self.supplier,
            payments=self.payments, spend_guard=self.guard)
        self.worker.enqueue_paid_order('order-1', payload=self.payload, now=fixture.NOW)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_guards_subscription_schema_price_stock_balance_and_binding_before_reserving(self):
        task = self.tasks.claim_due(now=fixture.NOW, limit=1)[0]
        order = self.orders.get('order-1')
        cases = [
            ('subscription', {**self.subscription, 'subscriptionActive': False}),
            ('subscription', {**self.subscription, 'planExpiresAt': fixture.NOW.isoformat()}),
            ('schema', {**self.detail, 'fields': []}),
            ('cost', {**self.detail, 'offers': [{'offer_id': 'd55', 'price_usd': '2.01', 'stock': 10}]}),
            ('stock', {**self.detail, 'offers': [{'offer_id': 'd55', 'price_usd': '1.50'}]}),
        ]
        for kind, response in cases:
            with self.subTest(kind=kind):
                self.client.get_subscription.return_value = response if kind == 'subscription' else self.subscription
                self.catalog.get.return_value = self.detail if kind == 'subscription' else response
                with self.assertRaises(SpendPreflightError):
                    self.guard.prepare(task, order, now=fixture.NOW)
        self.client.get_subscription.return_value = self.subscription
        self.catalog.get.return_value = self.detail
        self.client.get_balance.return_value = {'ok': True, 'balance': '2.99', 'currency': 'USD'}
        with self.assertRaises(SpendPreflightError):
            self.guard.prepare(task, order, now=fixture.NOW)
        with sqlite3.connect(self.tasks.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM game_supplier_spend').fetchone()[0], 0)
        self.assertEqual(self.supplier.calls, [])

    def test_success_charges_ceiling_once_and_replays_without_post(self):
        result = self.worker.run_once(now=fixture.NOW)
        self.assertEqual(result[0].status, 'submitted')
        self.assertEqual(len(self.supplier.calls), 1)
        self.guard.accepted('order-1', 'ord-9001')
        self.assertEqual(self.worker.run_once(now=fixture.NOW), ())
        with sqlite3.connect(self.tasks.path) as db:
            self.assertEqual(db.execute('SELECT ceiling_usd,state FROM game_supplier_spend').fetchone(), ('2', 'accepted'))

    def test_timeout_preserves_budget_and_disables_repurchase(self):
        self.supplier.failed = True
        self.worker.run_once(now=fixture.NOW)
        self.assertEqual(self.orders.get('order-1').state, OrderState.UNKNOWN)
        self.assertEqual(self.worker.run_once(now=fixture.NOW + timedelta(days=8)), ())
        self.assertEqual(len(self.supplier.calls), 1)
        with sqlite3.connect(self.tasks.path) as db:
            self.assertEqual(db.execute('SELECT state FROM game_supplier_spend').fetchone()[0], 'held')

    def test_final_balance_change_releases_only_unsent_supplier_budget(self):
        self.client.get_balance.side_effect = [
            {'ok': True, 'balance': '20', 'currency': 'USD'},
            {'ok': True, 'balance': '0', 'currency': 'USD'},
        ]
        result = self.worker.run_once(now=fixture.NOW)
        self.assertEqual(result[0].status, 'preflight_review')
        self.assertEqual(self.supplier.calls, [])
        with sqlite3.connect(self.tasks.path) as db:
            self.assertEqual(db.execute('SELECT state FROM game_supplier_spend').fetchone()[0], 'disabled')

    def test_rejected_preflight_never_starts_dispatch_or_releases_customer_money(self):
        self.client.get_subscription.return_value = {**self.subscription, 'subscriptionActive': False}
        result = self.worker.run_once(now=fixture.NOW)
        self.assertEqual(result[0].status, 'preflight_review')
        self.assertEqual(self.orders.get('order-1').state, OrderState.REVIEW_REFUND)
        self.assertIsNone(self.tasks.get('order-1').dispatch_started_at)
        self.assertEqual(self.ledger.balance('customer-1').reserved_minor, 4500)
        self.assertEqual(self.supplier.calls, [])

    def test_accepted_budget_without_receipt_is_unknown_and_never_repurchased(self):
        task = self.tasks.claim_due(now=fixture.NOW, limit=1)[0]
        self.guard.prepare(task, self.orders.get('order-1'), now=fixture.NOW)
        self.guard.accepted('order-1', 'ord-9001')
        self.tasks.mark_retry(task, error='lost receipt fixture', next_at=fixture.NOW, now=fixture.NOW)
        result = self.worker.run_once(now=fixture.NOW)
        self.assertEqual(self.orders.get('order-1').state, OrderState.UNKNOWN)
        self.assertEqual(self.supplier.calls, [])
        self.assertNotEqual(result[0].status, 'submitted')

    def test_stale_preflight_claim_does_not_change_current_order(self):
        from aurix_games.spend_guard import SpendClaimLost
        task = self.tasks.claim_due(now=fixture.NOW, limit=1)[0]
        with sqlite3.connect(self.tasks.path) as db:
            db.execute('UPDATE game_fulfillment_tasks SET attempts=attempts+1 WHERE order_id=?', ('order-1',))
        with self.assertRaises(SpendClaimLost):
            self.guard.prepare(task, self.orders.get('order-1'), now=fixture.NOW)
        self.assertEqual(self.orders.get('order-1').state, OrderState.FULFILLMENT_QUEUED)
        self.assertEqual(self.supplier.calls, [])

    def enqueue_second_order(self):
        order = create_order(order_id='order-2', customer_id='customer-1', customer_idempotency_key='client-2',
            snapshot=OrderSnapshot('offer-1', 'mobile-legends', 'direct_topup', 4500, 'MMK', 'recipient-2'))
        self.orders.create(order, created_at=fixture.NOW)
        self.orders.transition('order-2', OrderState.AWAITING_PAYMENT, at=fixture.NOW, reason='test')
        self.payments.record_verified_credit(customer_id='customer-1', amount_minor=4500, currency='MMK',
            event_key='credit-2', actor='staff:1', reason='test', occurred_at=fixture.NOW)
        self.payments.reserve_order('order-2', event_key='reserve-2', actor='system', reason='test', occurred_at=fixture.NOW)
        self.worker.enqueue_paid_order('order-2', payload=self.payload, now=fixture.NOW)

    def test_total_acceptance_cap_does_not_reset_at_midnight(self):
        self.guard.lifetime_limit = Decimal('3')
        self.assertEqual(self.worker.run_once(now=fixture.NOW)[0].status, 'submitted')
        self.enqueue_second_order()
        self.client.get_subscription.return_value = {**self.subscription,
            'planExpiresAt': (fixture.NOW + timedelta(days=30)).isoformat()}
        outcome = self.worker.run_once(now=fixture.NOW + timedelta(days=1))
        self.assertEqual(outcome[0].status, 'preflight_review')
        self.assertIn('total supplier', outcome[0].error)
        self.assertEqual(len(self.supplier.calls), 1)

    def test_concurrent_workers_cannot_exceed_daily_budget(self):
        self.enqueue_second_order()
        barrier = threading.Barrier(2)
        def balance():
            if threading.current_thread().ident not in seen:
                seen.add(threading.current_thread().ident)
                barrier.wait(timeout=3)
            return {'ok': True, 'balance': '20', 'currency': 'USD'}
        seen = set()
        self.client.get_balance.side_effect = balance
        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(lambda _: self.worker.run_once(now=fixture.NOW, limit=1), range(2)))
        self.assertEqual(len(self.supplier.calls), 1)
        self.assertIn('preflight_review', [row[0].status for row in outcomes])
        with sqlite3.connect(self.tasks.path) as db:
            self.assertEqual(sum(Decimal(row[0]) for row in db.execute('SELECT ceiling_usd FROM game_supplier_spend')), Decimal('2'))
