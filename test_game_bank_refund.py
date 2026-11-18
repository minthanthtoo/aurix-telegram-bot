import hashlib
import json
import sqlite3
import unittest
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_game_supplier_recovery as fixture
from aurix_games.bank_refund import SettledBankRefundService
from aurix_games.payment_review import PaymentReviewService,PaymentReviewError
from aurix_games.customer_ledger import LedgerConflictError
from aurix_games.order_state import OrderState
from persistence import open_sqlite_connection


class BankRefundTests(unittest.TestCase):
    recover=fixture.SupplierRecoveryTests.recover
    # Reuse the paid, unknown-dispatch fixture; supplier recovery tests remain independent.
    def setUp(self):
        fixture.SupplierRecoveryTests.setUp(self)
        self.remote['status']='failed';self.recover()
        self.orders.transition('one',OrderState.REVIEW_REFUND,at=self.now,reason='verified failed supplier obligation')
        with open_sqlite_connection(self.orders.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS game_payment_transfer_identity_claims(incoming_transfer_key TEXT,order_id TEXT,resolution TEXT)')
            db.execute('INSERT INTO game_payment_transfer_identity_claims VALUES (?,?,?)',('incoming-key','one','settlement'))
        self.token='r'*32
        self.review=PaymentReviewService(self.payments,{'staff:refund':hashlib.sha256(self.token.encode()).hexdigest()})
        self.service=SettledBankRefundService(self.review)
        self.authorization={'rail':'kpay','sender_fingerprint':'a'*64,'merchant_reference':'bank-evidence-one',
            'reason':'supplier failure','original_sender_confirmed':True}
        self.completion={'transaction_id':'outbound-123','paid_at':self.now.isoformat(),'amount_minor':1000,
            'currency':'MMK','sender_fingerprint':'a'*64,'merchant_reference':'payout-proof','verified_paid':True}
        self.intent={'rail':'kpay','receiver_id':'merchant-one'}
        self.intent_patch=patch.object(self.orders,'get_payment_intent',return_value=self.intent)
        self.intent_patch.start();self.addCleanup(self.intent_patch.stop)

    def authorize(self):
        return self.service.authorize('one',token=self.token,payload=self.authorization,now=self.now)

    def complete(self):
        return self.service.complete('one',token=self.token,payload=self.completion,now=self.now)

    def test_bank_route_blocks_wallet_credit_and_completes_without_wallet_money(self):
        self.authorize();self.assertTrue(self.authorize()['replayed'])
        with self.assertRaises(LedgerConflictError):
            self.payments.refund_captured_order('one',event_key='refund',actor='staff:test',reason='refund',occurred_at=self.now)
        self.assertEqual(self.complete()['state'],'refunded')
        self.assertTrue(self.complete()['replayed'])
        self.assertEqual(self.ledger.balance('private-customer').total_minor,0)

    def test_wallet_refund_first_blocks_bank_authorization(self):
        self.ledger.refund(order_id='one',event_key='refund',actor='staff:test',reason='refund',occurred_at=self.now)
        with self.assertRaises(LedgerConflictError):self.authorize()

    def test_wrong_amount_sender_and_future_time_do_not_complete(self):
        self.authorize()
        for change in [{'amount_minor':999},{'sender_fingerprint':'b'*64},{'paid_at':(self.now+timedelta(seconds=1)).isoformat()}]:
            with self.subTest(change=change),self.assertRaises(PaymentReviewError):
                self.service.complete('one',token=self.token,payload={**self.completion,**change},now=self.now)
        self.assertEqual(self.service.status('one',token=self.token)['state'],'bank_refund_pending')

    def test_completion_response_loss_resumes_order_transition_without_duplicate_payment(self):
        self.authorize()
        with patch.object(self.orders,'transition',side_effect=RuntimeError('lost order handoff')):
            with self.assertRaises(RuntimeError):self.complete()
        self.assertEqual(self.orders.get('one').state,OrderState.REVIEW_REFUND)
        self.assertTrue(self.complete()['replayed'])
        self.assertEqual(self.orders.get('one').state,OrderState.REFUNDED)

    def test_race_between_wallet_and_bank_has_one_winner(self):
        def bank():
            try:self.authorize();return 'bank'
            except LedgerConflictError:return 'blocked'
        def wallet():
            try:
                self.ledger.refund(order_id='one',event_key='race',actor='staff:test',reason='refund',occurred_at=self.now)
                return 'wallet'
            except LedgerConflictError:return 'blocked'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda fn:fn(),[bank,wallet]))
        self.assertEqual(results.count('blocked'),1)

    def test_invalid_token_cannot_authorize(self):
        from aurix_games.payment_review import PaymentReviewerAuthError
        with self.assertRaises(PaymentReviewerAuthError):
            self.service.authorize('one',token='wrong',payload=self.authorization,now=self.now)

    def test_same_outbound_transfer_cannot_be_used_by_exception_refund(self):
        self.authorize();self.complete()
        saved=self.ledger.bank_refund('one')
        with self.assertRaises(LedgerConflictError):
            self.ledger.claim_outbound_refund(saved['outbound_key'],'exception:other:1')

    def test_exception_claim_blocks_settled_completion(self):
        self.authorize()
        key='refund:'+hashlib.sha256(json.dumps(['kpay','MMK','outbound-123'],separators=(',',':')).encode()).hexdigest()
        self.ledger.claim_outbound_refund(key,'exception:other:1')
        with self.assertRaises(LedgerConflictError):self.complete()
        self.assertIsNone(self.ledger.bank_refund('one')['completed_at'])

    def test_monitor_reports_pending_and_incomplete_order_handoff(self):
        from aurix_games.fulfillment_audit import audit_money_integrity
        self.authorize()
        def issues():return {r['issue'] for r in audit_money_integrity(self.orders.path,self.ledger.path,self.tasks.path)['findings']}
        self.assertIn('settled_bank_refund_pending',issues())
        with patch.object(self.orders,'transition',side_effect=RuntimeError()):
            with self.assertRaises(RuntimeError):self.complete()
        self.assertIn('settled_bank_refund_order_handoff',issues())
        self.complete()
        self.assertNotIn('settled_bank_refund_order_handoff',issues())

    def test_http_auth_authorize_status_and_completion(self):
        from http.server import ThreadingHTTPServer
        from aurix_games.web_api import make_handler
        import threading
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(payment_review=self.review))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}/api/v1/admin/payment-review/orders/one/bank-refund'
        def request(suffix='',payload=None,token=None):
            headers={'Authorization':'Bearer '+(token or self.token),'Content-Type':'application/json'}
            with urlopen(Request(url+suffix,data=None if payload is None else json.dumps(payload).encode(),headers=headers)) as response:
                self.assertEqual(response.headers['Cache-Control'],'no-store')
                return json.load(response)
        try:
            with self.assertRaises(HTTPError) as error:request(token='wrong')
            self.assertEqual(error.exception.code,401);error.exception.close()
            self.assertEqual(request('/authorize',self.authorization)['state'],'bank_refund_pending')
            self.assertEqual(request()['state'],'bank_refund_pending')
            from datetime import datetime,timezone
            self.completion['paid_at']=datetime.now(timezone.utc).isoformat()
            self.assertEqual(request('/complete',self.completion)['state'],'refunded')
        finally:server.shutdown();server.server_close();thread.join(timeout=2)

    def test_customer_refund_status_has_no_merchant_or_sender_evidence(self):
        from aurix_games.customer_api import CustomerOrderService,CustomerPrincipal
        self.authorize()
        service=CustomerOrderService(self.orders,self.ledger)
        principal=CustomerPrincipal('private-customer','fixture',self.now+timedelta(hours=1))
        with patch.object(self.orders,'get_payment_intent',return_value=None):
            result=service.get_order(principal,'one')['refund']
        self.assertEqual(result['status'],'pending')
        for private in ('merchant_reference','sender_fingerprint','authorized_by','transaction_id'):
            self.assertNotIn(private,result)

    def test_customer_reads_completed_bank_refund_before_order_handoff(self):
        from aurix_games.customer_api import CustomerOrderService, CustomerPrincipal
        self.authorize()
        with patch.object(self.orders, 'transition', side_effect=RuntimeError('lost handoff')):
            with self.assertRaises(RuntimeError):
                self.complete()
        service = CustomerOrderService(self.orders, self.ledger)
        principal = CustomerPrincipal('private-customer', 'fixture', self.now + timedelta(hours=1))
        with patch.object(self.orders, 'get_payment_intent', return_value=None):
            detail = service.get_order(principal, 'one')
        self.assertEqual(detail['state'], 'review_refund')
        self.assertEqual(detail['refund']['route'], 'bank')
        self.assertEqual(detail['refund']['status'], 'completed')
        self.assertEqual(service.list_orders(principal)[0]['refund'], detail['refund'])
        self.assertIsNone(self.ledger.customer_refund('one', customer_id='other'))

    def test_customer_reads_wallet_reimbursement_from_ledger(self):
        from aurix_games.customer_api import CustomerOrderService, CustomerPrincipal
        self.ledger.refund(order_id='one', event_key='customer-refund-view', actor='staff:test',
                           reason='refund', occurred_at=self.now)
        service = CustomerOrderService(self.orders, self.ledger)
        principal = CustomerPrincipal('private-customer', 'fixture', self.now + timedelta(hours=1))
        refund = service.list_orders(principal)[0]['refund']
        self.assertEqual((refund['route'], refund['status'], refund['amount_minor']), ('wallet', 'completed', 1000))
