from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from aurix_games.customer_ledger import CustomerLedger
from aurix_games.fulfillment import FulfillmentTaskStore, GameFulfillmentWorker, FulfillmentInputError, FulfillmentTaskError
from aurix_games.fulfillment_audit import audit_fulfillment
from aurix_games.order_repository import GameOrderRepository
from aurix_games.order_state import create_order, OrderSnapshot, OrderState
from aurix_games.payment_service import CustomerPaymentService
from aurix_games.supplier_recovery import inspect_unknown_attempt, recover_original_topup
from aurix_games.spend_guard import PilotSpendGuard, schema_fingerprint


class SupplierRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.now = datetime.now(timezone.utc)
        self.orders = GameOrderRepository(root/'orders.db')
        self.tasks = FulfillmentTaskStore(root/'tasks.db',encryption_key=Fernet.generate_key())
        self.ledger = CustomerLedger(root/'ledger.db')
        self.payments = CustomerPaymentService(self.orders,self.ledger)
        self.payload = {'category_id':'mobile_legends_global','offer_id':'12_diamonds',
            'fields':{'player_id':'private-recipient','server_id':'private-zone'}}
        order = create_order(order_id='one', customer_id='private-customer',customer_idempotency_key='one',
            snapshot=OrderSnapshot('pack','mobile-legends','direct_topup',1000,'MMK','private-fingerprint'))
        self.orders.create(order,created_at=self.now)
        self.orders.transition('one',OrderState.AWAITING_PAYMENT,at=self.now,reason='fixture')
        self.payments.record_verified_credit(customer_id=order.customer_id,amount_minor=1000,currency='MMK',
            event_key='credit',actor='staff:test',reason='fixture',occurred_at=self.now)
        self.payments.reserve_order('one',event_key='reserve',actor='staff:test',reason='fixture',occurred_at=self.now)
        self.supplier = Mock();self.supplier.submit.side_effect=TimeoutError()
        self.worker=GameFulfillmentWorker(orders=self.orders,tasks=self.tasks,supplier=self.supplier,payments=self.payments)
        self.worker.enqueue_paid_order('one',payload=self.payload,now=self.now)
        self.worker.run_once(now=self.now)
        self.assertEqual(self.orders.get('one').state,OrderState.UNKNOWN)
        facts=inspect_unknown_attempt(self.orders,self.tasks,'one')
        self.request={k:facts[k] for k in ('expected_attempts','expected_payload_sha256','expected_provider_idempotency_key')}
        self.request.update(provider_order_id='ord-123',provider_reference='support-case-confirming-original-key',confirmed_original_attempt=True)
        self.remote={**deepcopy(self.payload),'id':'ord-123','kind':'topup','status':'completed','created_at':self.now.isoformat()}
        self.client=Mock();self.client.get_order.side_effect=lambda _: {'ok':True,'order':deepcopy(self.remote)}

    def test_timeout_without_declared_attempt_reference_is_held_for_reconciliation(self):
        order = self.orders.get('one')
        task = self.tasks.get('one')
        self.assertEqual(order.state, OrderState.UNKNOWN)
        self.assertIsNone(order.provider_order_id)
        self.assertEqual(task.status, 'failed')
        self.assertIsNone(task.provider_order_id)
        self.assertIsNotNone(task.dispatch_started_at)

    def recover(self,request=None):
        return recover_original_topup(self.orders,self.tasks,self.payments,self.client,order_id='one',
            request=request or self.request,reviewer='staff:treasury',now=self.now)

    def test_exact_support_confirmed_attempt_recovers_once_without_a_new_purchase(self):
        result=self.recover()
        self.assertEqual(result['state'],'delivered')
        self.assertTrue(self.recover()['replayed'])
        self.assertEqual(self.ledger.balance('private-customer').total_minor,0)
        self.assertEqual(self.tasks.get('one').status,'submitted')
        self.supplier.submit.assert_called_once()
        self.assertTrue(all(call[0]=='get_order' for call in self.client.method_calls))
        raw=self.tasks.path.read_bytes()
        for secret in (b'private-recipient',b'private-zone',b'support-case-confirming-original-key'):
            self.assertNotIn(secret,raw)
        with sqlite3.connect(self.tasks.path) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE game_supplier_recovery_links SET reviewer='staff:other'")

    def test_changed_recipient_schema_or_provider_identity_cannot_link(self):
        for changes in ({'fields':{'player_id':'other'}},{'category_id':'other'},{'offer_id':'other'},
                {'kind':'gift_card'},{'id':'ord-999'},{'created_at':'2000-01-01T00:00:00Z'}):
            with self.subTest(changes=changes):
                original=deepcopy(self.remote);self.remote.update(changes)
                with self.assertRaises(FulfillmentInputError):self.recover()
                self.assertIsNone(self.tasks.get('one').provider_order_id)
                self.remote=original

    def test_support_key_confirmation_and_exact_revision_are_required(self):
        for changes in ({'confirmed_original_attempt':False},{'provider_reference':''},
                {'expected_provider_idempotency_key':'wrong'},{'expected_payload_sha256':'wrong'},{'expected_attempts':2}):
            with self.subTest(changes=changes),self.assertRaises(FulfillmentInputError):
                self.recover({**self.request,**changes})
        self.client.get_order.assert_not_called()

    def test_crash_after_receipt_link_resumes_without_double_capture(self):
        capture=self.payments.capture_order
        def lose_capture_response(*args,**kwargs):
            capture(*args,**kwargs)
            raise RuntimeError('lost local response after capture committed')
        with patch.object(self.payments,'capture_order',side_effect=lose_capture_response):
            with self.assertRaises(RuntimeError):self.recover()
        self.assertEqual(self.tasks.get('one').provider_order_id,'ord-123')
        self.assertEqual(self.orders.get('one').state,OrderState.RECONCILIATION)
        self.assertEqual(self.ledger.balance('private-customer').reserved_minor,0)
        self.assertIn('accepted_task_handoff_pending', {row['issue'] for row in
            audit_fulfillment(self.orders.path,self.ledger.path,self.tasks.path)['findings']})
        self.assertEqual(self.recover()['state'],'delivered')
        self.assertEqual(self.recover()['state'],'delivered')
        self.assertEqual(self.ledger.balance('private-customer').total_minor,0)

    def test_refunded_supplier_order_never_becomes_customer_bank_refund(self):
        self.remote['status']='refund'
        result=self.recover()
        self.assertEqual(result['state'],'reconciliation')
        self.assertFalse(result['customer_bank_refund_created'])
        self.assertEqual(self.ledger.balance('private-customer').total_minor,0)
        self.assertEqual(self.orders.get('one').state,OrderState.RECONCILIATION)

    def test_changed_recovery_decision_is_rejected(self):
        self.recover()
        with self.assertRaises(FulfillmentInputError):
            self.recover({**self.request,'provider_reference':'different-case'})
        self.assertEqual(self.tasks.get('one').provider_order_id,'ord-123')

    def test_receipt_already_assigned_to_another_order_is_rejected(self):
        other=create_order(order_id='other',customer_id='customer-two',customer_idempotency_key='other',
            snapshot=self.orders.get('one').snapshot)
        self.orders.create(other,created_at=self.now)
        for state in (OrderState.AWAITING_PAYMENT,OrderState.PAID_RESERVED,OrderState.FULFILLMENT_QUEUED,OrderState.SUBMITTED):
            self.orders.transition('other',state,at=self.now,reason='fixture',
                provider_order_id='ord-123' if state==OrderState.SUBMITTED else None)
        with self.assertRaises(FulfillmentInputError):self.recover()
        self.assertIsNone(self.tasks.get('one').provider_order_id)
        self.client.get_order.assert_not_called()

    def test_lost_order_handoff_after_durable_link_is_recoverable(self):
        with patch.object(self.orders,'transition',side_effect=RuntimeError('order DB unavailable')):
            with self.assertRaises(RuntimeError):self.recover()
        self.assertEqual(self.orders.get('one').state,OrderState.UNKNOWN)
        self.assertEqual(self.tasks.get('one').provider_order_id,'ord-123')
        self.assertIn('receipt_handoff_pending', {row['issue'] for row in
            audit_fulfillment(self.orders.path,self.ledger.path,self.tasks.path)['findings']})
        self.assertEqual(self.recover()['state'],'delivered')

    def test_mismatched_customer_reservation_blocks_recovery_before_supplier_read(self):
        with sqlite3.connect(self.ledger.path) as db:
            db.execute('UPDATE game_order_reservations SET amount_minor=999')
        with self.assertRaises(FulfillmentInputError):self.recover()
        self.client.get_order.assert_not_called()
        self.assertIsNone(self.tasks.get('one').provider_order_id)

    def test_cli_requires_named_credential_before_opening_stores(self):
        from scripts.recover_game_supplier_order import main
        admin=Mock();admin.authenticate_treasury.side_effect=PermissionError('secret error should not be printed')
        with patch('scripts.recover_game_supplier_order.build_env_admin_service',return_value=admin), \
                patch('scripts.recover_game_supplier_order.getpass',return_value='wrong-token'), \
                patch('scripts.recover_game_supplier_order.GameOrderRepository') as repo,patch('builtins.print') as output:
            self.assertEqual(main(['--order','one']),1)
            repo.assert_not_called()
            self.assertNotIn('wrong-token',str(output.call_args))
            self.assertNotIn('secret error',str(output.call_args))

    def test_legacy_duplicate_task_receipts_block_migration_without_deleting_evidence(self):
        self.tasks.enqueue(order_id='other',fulfillment_kind='direct_topup',payload=self.payload,available_at=self.now)
        with sqlite3.connect(self.tasks.path) as db:
            db.execute('DROP INDEX game_fulfillment_provider_identity')
            db.execute("UPDATE game_fulfillment_tasks SET provider_order_id='ord-123'")
        with self.assertRaises(FulfillmentTaskError):self.tasks.initialize()
        with sqlite3.connect(self.tasks.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM game_fulfillment_tasks WHERE provider_order_id='ord-123'").fetchone()[0],2)

    def add_spend(self,state='held',payload_hash=None):
        PilotSpendGuard(tasks=self.tasks,client=Mock(),catalog_client=Mock(),checkout=Mock(),policy={
            'daily_limit_usd':'3','minimum_balance_usd':'0','offers':[{'category_id':self.payload['category_id'],
                'offer_id':self.payload['offer_id'],'max_cost_usd':'0.30','allow_unknown_stock':True,'fields_sha256':'a'*64}]})
        with sqlite3.connect(self.tasks.path) as db:
            db.execute('INSERT OR REPLACE INTO game_supplier_spend VALUES (?,?,?,?,?,?,?)',
                ('one',payload_hash or schema_fingerprint(self.payload),'2026-11-12','0.30',state,None,self.now.isoformat()))

    def test_recovery_retains_original_budget_day_and_ceiling(self):
        self.add_spend()
        self.recover()
        with sqlite3.connect(self.tasks.path) as db:
            self.assertEqual(db.execute('SELECT state,provider_order_id,budget_day,ceiling_usd FROM game_supplier_spend').fetchone(),
                ('accepted','ord-123','2026-11-12','0.30'))

    def test_conflicting_budget_rolls_back_receipt_and_recovery_decision_together(self):
        for state,payload_hash in [('disabled',None),('held','wrong-payload')]:
            self.add_spend(state,payload_hash)
            with self.assertRaises(FulfillmentInputError):self.recover()
            self.assertIsNone(self.tasks.get('one').provider_order_id)
            with sqlite3.connect(self.tasks.path) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM game_supplier_recovery_links').fetchone()[0],0)
