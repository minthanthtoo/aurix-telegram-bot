"""Review evidence stays separate from credit, even across crashes and races."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import sqlite3
import unittest
from datetime import timedelta
from unittest.mock import patch

from aurix_games.payment_review import PaymentReviewService, PaymentReviewError, PaymentReviewerAuthError, MAX_RECEIPT_BYTES, MAX_RECEIPT_BODY_BYTES
from aurix_games.customer_ledger import LedgerConflictError
from aurix_games.order_repository import OrderNotFoundError
from aurix_games.order_state import OrderState
from aurix_games.payment_service import PaymentBoundaryError
from aurix_games.customer_checkout import CheckoutInputError
import test_game_supplier_spine as fixtures
NOW = fixtures.NOW


class PaymentReviewTests(unittest.TestCase):
    _payment = fixtures.SupplierSpineTests._payment
    _service = fixtures.SupplierSpineTests._service
    # Reuse the existing full checkout fixture without inheriting its test cases.
    def setUp(self):
        fixtures.SupplierSpineTests.setUp(self)
        self._payment()
        self.payments = self._service()
        self.token = 'reviewer-alice-token-for-testing-123456'
        self.review = PaymentReviewService(self.payments, {
            'staff:alice': hashlib.sha256(self.token.encode()).hexdigest()})
        self.claim = dict(customer_id='customer-1', transaction_reference='customer-claim', evidence_sha256='a'*64, now=NOW)
        self.decision = dict(token=self.token, approve=True, reason='matched merchant records', now=NOW,
            verified_transfer=dict(rail='kpay', receiver_id='receiver-1', transaction_id='bank-123',
                                   amount_minor=5000, currency='MMK', occurred_at=NOW))

    def _reset_checkout_lookup_throttle(self):
        # This fixture deliberately creates multiple orders at one frozen test
        # timestamp; keep those tests focused on payment transfer identity.
        with sqlite3.connect(self.checkout.repository.path) as database:
            database.execute('DELETE FROM game_recipient_lookup_events')

    def test_claim_cannot_credit_and_is_owner_bound_immutable(self):
        with self.assertRaises(OrderNotFoundError):
            self.review.submit(self.order_id, **dict(self.claim, customer_id='other'))
        with self.assertRaises(PaymentReviewError):
            self.review.submit(self.order_id, **dict(self.claim, customer_id=None))
        self.review.submit(self.order_id, **self.claim)
        self.review.submit(self.order_id, **self.claim)
        self.assertIsNone(self.payments.ledger.balance('customer-1'))
        with self.assertRaises(PaymentReviewError):
            self.review.submit(self.order_id, **dict(self.claim, transaction_reference='changed'))
        with self.assertRaises(PaymentReviewerAuthError):
            self.review.pending('x'*32)
        self.assertEqual(len(self.review.pending(self.token)), 1)

    def test_customer_order_and_review_share_snapshot_during_approval(self):
        from threading import Event
        from aurix_games.customer_api import CustomerOrderService, CustomerPrincipal
        self.review.submit(self.order_id, **self.claim)
        self.review._initialize()
        original_case = self.review._case
        advanced = False
        started = Event()
        pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(pool.shutdown)
        pending = []

        def approve():
            started.set()
            return self.review.decide(self.order_id, **self.decision)

        def approve_after_snapshot_read(db, order_id, revision=None):
            nonlocal advanced
            row = original_case(db, order_id, revision)
            if not advanced:
                advanced = True
                pending.append(pool.submit(approve))
                self.assertTrue(started.wait(timeout=2))
            return row

        principal = CustomerPrincipal('customer-1', 'fixture', NOW + timedelta(hours=1))
        service = CustomerOrderService(self.checkout.repository, self.payments.ledger)
        with patch.object(self.review, '_case', side_effect=approve_after_snapshot_read):
            before = service.get_order(principal, self.order_id, payment_review=self.review)
        self.assertEqual(before['state'], 'awaiting_payment')
        self.assertEqual(before['payment_review']['review_status'], 'in_review')
        self.assertTrue(pending[0].result(timeout=5)['applied'])
        after = service.get_order(principal, self.order_id, payment_review=self.review)
        self.assertEqual(after['state'], 'paid_reserved')
        self.assertEqual(after['payment_review']['review_status'], 'verified')
        self.assertFalse(after['payment_review']['can_correct'])
        self.assertNotIn('account', after['payment'])
        for private in ('transaction_reference', 'evidence_sha256', 'decision_json', 'receiver-1'):
            self.assertNotIn(private, str(after))

    def test_customer_projection_preserves_owner_isolation(self):
        from aurix_games.customer_api import CustomerOrderService, CustomerPrincipal, CustomerApiError
        service = CustomerOrderService(self.checkout.repository, self.payments.ledger)
        with self.assertRaises(CustomerApiError):
            service.get_order(CustomerPrincipal('other', 'fixture', NOW + timedelta(hours=1)),
                              self.order_id, payment_review=self.review)

    def test_crash_after_settlement_resumes_original_decision_once(self):
        self.review.submit(self.order_id, **self.claim)
        original = self.payments.settle_verified_transfer
        def lost_response(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('lost response')
        with patch.object(self.payments, 'settle_verified_transfer', side_effect=lost_response):
            with self.assertRaises(RuntimeError):
                self.review.decide(self.order_id, **self.decision)
        with self.assertRaises(PaymentReviewError):
            self.review.decide(self.order_id, token=self.token, approve=False, reason='changed', now=NOW)
        restarted = PaymentReviewService(self.payments, self.review.reviewers)
        self.assertTrue(restarted.resume(self.order_id, token=self.token, now=NOW)['applied'])
        self.assertTrue(self.review.decide(self.order_id, **self.decision)['replayed'])
        self.assertEqual(self.payments.ledger.balance('customer-1').reserved_minor, 5000)
        self.assertEqual(self.review.pending(self.token), [])

    def test_invalid_transfer_does_not_freeze_decision(self):
        self.review.submit(self.order_id, **self.claim)
        bad = dict(self.decision, verified_transfer=dict(self.decision['verified_transfer'], amount_minor=6000))
        with self.assertRaises(PaymentBoundaryError):
            self.review.decide(self.order_id, **bad)
        self.review.decide(self.order_id, **self.decision)

    def test_late_partial_transfer_is_cancelled_and_fully_refunded_outside_the_wallet(self):
        received_at = NOW + timedelta(minutes=11)
        authorized_at = NOW + timedelta(minutes=12)
        self.review.submit(self.order_id, **self.claim)
        exception = dict(rail='kpay', receiver_id='receiver-1', transaction_id='late-partial-1',
            amount_minor=3200, currency='MMK', occurred_at=received_at,
            refund_destination_confirmed=True)
        result = self.review.decide(self.order_id, token=self.token, approve=False,
            reason='late partial payment; return the full verified incoming amount',
            external_refund=exception, now=authorized_at, revision=1)
        self.assertEqual(result['resolution'], 'external_refund_authorized')
        self.assertEqual(self.payments.repository.get(self.order_id).state, OrderState.CANCELLED)
        self.assertEqual(self.review.customer_status(self.order_id, customer_id='customer-1')['review_status'], 'refund_pending')
        self.assertIsNone(self.payments.ledger.balance('customer-1'))
        from aurix_games.customer_api import CustomerOrderService
        customer_order = CustomerOrderService(self.payments.repository).get_order(self.principal, self.order_id)
        self.assertNotIn('account', customer_order['payment'])
        self.assertNotIn('qr_asset', customer_order['payment'])
        pending = CustomerOrderService(self.payments.repository).get_order(
            self.principal, self.order_id, payment_review=self.review)['refund']
        self.assertEqual((pending['route'], pending['status'], pending['amount_minor']), ('bank', 'pending', 3200))
        with self.assertRaises(CheckoutInputError):
            self.journey.payment(self.principal, {'order_id': self.order_id, 'rail': 'kpay'}, now=authorized_at)
        with self.assertRaises(PaymentBoundaryError):
            self.payments.settle_verified_transfer(self.order_id, rail='kpay', receiver_id='receiver-1',
                transaction_id='late-partial-1', amount_minor=5000, currency='MMK',
                actor='staff:alice', occurred_at=NOW)
        self.assertEqual(len(self.review.pending_refunds(self.token)), 1)
        refunded_at = authorized_at + timedelta(minutes=1)
        completion = dict(token=self.token, revision=1, refund_transaction_id='outbound-1',
            refunded_at=refunded_at, reason='outbound merchant transfer matched original sender',
            now=refunded_at + timedelta(seconds=2))
        self.assertTrue(self.review.complete_external_refund(self.order_id, **completion)['refunded'])
        completed = CustomerOrderService(self.payments.repository).get_order(
            self.principal, self.order_id, payment_review=self.review)['refund']
        self.assertEqual((completed['status'], completed['amount_minor']), ('completed', 3200))
        self.assertEqual(completed['completed_at'], refunded_at.isoformat())
        self.assertEqual(self.review.customer_status(self.order_id, customer_id='customer-1')['review_status'], 'refunded')
        self.assertEqual(self.review.pending_refunds(self.token), [])
        self.assertTrue(self.review.complete_external_refund(self.order_id, **completion)['replayed'])
        with self.assertRaises(PaymentReviewError):
            self.review.complete_external_refund(self.order_id, **dict(completion, refund_transaction_id='different-outbound'))
        with sqlite3.connect(self.payments.repository.path) as db:
            row = db.execute('SELECT refund_amount_minor, currency FROM game_payment_external_refund_completions').fetchone()
            self.assertEqual(row, (3200, 'MMK'))

    def test_excess_payment_refund_returns_the_entire_incoming_amount(self):
        self.review.submit(self.order_id, **self.claim)
        received = dict(rail='kpay', receiver_id='receiver-1', transaction_id='overpay-1',
            amount_minor=7500, currency='MMK', occurred_at=NOW + timedelta(minutes=1),
            refund_destination_confirmed=True)
        self.review.decide(self.order_id, token=self.token, approve=False,
            reason='excess payment; refund full amount to verified sender', external_refund=received,
            now=NOW + timedelta(minutes=2), revision=1)
        self.review.complete_external_refund(self.order_id, token=self.token, revision=1,
            refund_transaction_id='outbound-overpay-1', refunded_at=NOW + timedelta(minutes=3),
            reason='full excess transfer returned', now=NOW + timedelta(minutes=4))
        with sqlite3.connect(self.payments.repository.path) as db:
            row = db.execute('SELECT refund_amount_minor FROM game_payment_external_refund_completions WHERE order_id=?',
                (self.order_id,)).fetchone()
        self.assertEqual(row[0], 7500)

    def test_refund_attestation_and_inbound_transaction_reuse_are_blocked(self):
        self.review.submit(self.order_id, **self.claim)
        received = dict(rail='kpay', receiver_id='receiver-1', transaction_id='shared-inbound',
            amount_minor=5000, currency='MMK', occurred_at=NOW,
            refund_destination_confirmed=False)
        with self.assertRaises(PaymentReviewError):
            self.review.decide(self.order_id, token=self.token, approve=False, reason='refund',
                external_refund=received, now=NOW, revision=1)
        self.assertEqual(self.payments.repository.get(self.order_id).state, OrderState.AWAITING_PAYMENT)
        received['refund_destination_confirmed'] = True
        self.review.decide(self.order_id, token=self.token, approve=False, reason='refund',
            external_refund=received, now=NOW, revision=1)
        self._reset_checkout_lookup_throttle()
        self._payment()
        second_order = self.order_id
        self.review.submit(second_order, customer_id='customer-1', transaction_reference='second claim',
            evidence_sha256='b'*64, now=NOW)
        with self.assertRaises(PaymentBoundaryError):
            self.review.decide(second_order, token=self.token, approve=False, reason='same bank transfer',
                external_refund=dict(received, occurred_at=NOW), now=NOW, revision=1)
        self.assertEqual(self.payments.repository.get(second_order).state, OrderState.AWAITING_PAYMENT)

    def test_concurrent_refund_and_cross_order_settlement_claim_one_transfer_once(self):
        first_order = self.order_id
        self.review.submit(first_order, **self.claim)
        self._reset_checkout_lookup_throttle()
        self._payment()
        second_order = self.order_id
        received = dict(rail='kpay', receiver_id='receiver-1', transaction_id='race-transfer',
            amount_minor=5000, currency='MMK', occurred_at=NOW, refund_destination_confirmed=True)
        def refund():
            try:
                self.review.decide(first_order, token=self.token, approve=False, reason='manual full refund',
                    external_refund=received, now=NOW, revision=1)
                return 'refund'
            except (PaymentReviewError, PaymentBoundaryError, LedgerConflictError):
                return 'blocked'
        def settle():
            try:
                self.payments.settle_verified_transfer(second_order, rail='kpay', receiver_id='receiver-1',
                    transaction_id='race-transfer', amount_minor=5000, currency='MMK',
                    actor='staff:alice', occurred_at=NOW)
                return 'settlement'
            except (PaymentBoundaryError, LedgerConflictError):
                return 'blocked'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda task: task(), (refund, settle)))
        self.assertEqual(sum(result != 'blocked' for result in results), 1)
        if 'refund' in results:
            self.assertEqual(self.payments.repository.get(first_order).state, OrderState.CANCELLED)
            self.assertEqual(self.payments.repository.get(second_order).state, OrderState.AWAITING_PAYMENT)
            self.assertIsNone(self.payments.ledger.balance('customer-1'))
        else:
            self.assertEqual(self.payments.repository.get(second_order).state, OrderState.PAID_RESERVED)
            self.assertEqual(self.payments.repository.get(first_order).state, OrderState.AWAITING_PAYMENT)
            self.assertEqual(self.payments.ledger.balance('customer-1').reserved_minor, 5000)

    def test_concurrent_reviewers_cannot_change_committed_decision(self):
        self.review.submit(self.order_id, **self.claim)
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.review.decide(self.order_id, **self.decision), range(3)))
        self.assertTrue(all(row['applied'] for row in results))
        with sqlite3.connect(self.payments.ledger.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM game_ledger_entries').fetchone()[0], 2)
        with sqlite3.connect(self.review.repository.path) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE game_payment_review_cases SET claim_json='{}'")

    def test_rejection_never_settles(self):
        self.review.submit(self.order_id, **self.claim)
        with patch.object(self.payments, 'settle_verified_transfer') as settle:
            self.review.decide(self.order_id, token=self.token, approve=False, reason='not in merchant records', now=NOW)
            settle.assert_not_called()
        self.assertIsNone(self.payments.ledger.balance('customer-1'))

    def test_named_identity_cannot_be_spoofed_and_other_reviewer_cannot_resume(self):
        self.review.submit(self.order_id, **self.claim)
        with self.assertRaises(PaymentReviewerAuthError):
            self.review.decide(self.order_id, **dict(self.decision, token='invalid-'*8))
        with patch.object(self.payments, 'settle_verified_transfer', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                self.review.decide(self.order_id, **self.decision)
        bob = 'reviewer-bob-token-for-testing-123456789'
        reviewers = dict(self.review.reviewers, **{'staff:bob': hashlib.sha256(bob.encode()).hexdigest()})
        service = PaymentReviewService(self.payments, reviewers)
        with self.assertRaises(PaymentReviewerAuthError):
            service.resume(self.order_id, token=bob, now=NOW)
        self.assertEqual(service.pending(bob)[0]['decision_json'], self.review.pending(self.token)[0]['decision_json'])

    def test_receipt_encryption_idempotency_integrity_and_restore(self):
        from cryptography.fernet import Fernet
        from aurix_games.backup_restore import create_backup, restore_backup
        from aurix_games.customer_ledger import CustomerLedger
        from aurix_games.order_repository import GameOrderRepository
        from aurix_games.payment_service import CustomerPaymentService
        from pathlib import Path
        import tempfile
        key = Fernet.generate_key()
        service = PaymentReviewService(self.payments, self.review.reviewers, evidence_key=key)
        receipt = b'%PDF-1.7 private test receipt'
        values = dict(customer_id='customer-1', transaction_reference='tx', receipt=receipt, now=NOW)
        service.submit_receipt(self.order_id, **values)
        service.submit_receipt(self.order_id, **values)
        self.assertEqual(service.receipt(self.order_id, token=self.token), receipt)
        with sqlite3.connect(service.repository.path) as db:
            encrypted = db.execute('SELECT encrypted_content FROM game_payment_review_cases').fetchone()[0]
            self.assertNotIn(b'private test receipt', encrypted)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM game_payment_review_cases').fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE game_payment_review_cases SET encrypted_content=X'00'")
        wrong_key = PaymentReviewService(self.payments, self.review.reviewers, evidence_key=Fernet.generate_key())
        with self.assertRaises(PaymentReviewError):
            wrong_key.receipt(self.order_id, token=self.token)
        with tempfile.TemporaryDirectory() as destination:
            backup = Path(destination) / 'backup'
            restored = Path(destination) / 'restored'
            create_backup(self.temp.name, backup)
            restore_backup(backup, restored)
            payments = CustomerPaymentService(GameOrderRepository(restored / self.payments.repository.path.name),
                                               CustomerLedger(restored / 'ledger.db'))
            recovered = PaymentReviewService(payments, service.reviewers, evidence_key=key)
            self.assertEqual(recovered.receipt(self.order_id, token=self.token), receipt)
        with self.assertRaises(PaymentReviewError):
            service.submit_receipt(self.order_id, **dict(values, receipt=b'%PDF-different'))

    def test_receipt_limits_fail_without_creating_claim(self):
        from cryptography.fernet import Fernet
        service = PaymentReviewService(self.payments, self.review.reviewers, evidence_key=Fernet.generate_key())
        for receipt in (b'', b'<script>untrusted</script>', b'%PDF-' + b'x' * MAX_RECEIPT_BYTES):
            with self.assertRaises(PaymentReviewError):
                service.submit_receipt(self.order_id, customer_id='customer-1', transaction_reference='tx', receipt=receipt, now=NOW)
        self.assertEqual(service.customer_status(self.order_id, customer_id='customer-1')['review_status'], 'not_submitted')
        self.assertEqual(service.pending(self.token), [])

    def test_runtime_builder_uses_existing_customer_stores(self):
        from types import SimpleNamespace
        from cryptography.fernet import Fernet
        from aurix_games.payment_review import build_env_payment_review
        import json
        api = SimpleNamespace(service=SimpleNamespace(repository=self.payments.repository),
                              wallet_service=SimpleNamespace(ledger=self.payments.ledger))
        key = Fernet.generate_key().decode()
        with patch.dict('os.environ', {'AURIX_GAMES_PAYMENT_REVIEWERS': json.dumps(self.review.reviewers),
                                      'AURIX_GAMES_PAYMENT_EVIDENCE_KEY': key}):
            service = build_env_payment_review(api)
            self.assertIs(service.repository, self.payments.repository)
            self.assertIs(service.payments.ledger, self.payments.ledger)
            self.assertTrue(service.customer_status(self.order_id, customer_id='customer-1')['receipt_upload_enabled'])


    def test_correction_is_append_only_and_stale_approval_is_blocked(self):
        self.review.submit(self.order_id, **self.claim)
        changed = dict(self.claim, transaction_reference='corrected', expected_revision=1)
        self.assertEqual(self.review.submit(self.order_id, **changed)['revision'], 2)
        self.assertEqual(self.review.submit(self.order_id, **changed)['revision'], 2)
        with self.assertRaises(PaymentReviewError):
            self.review.decide(self.order_id, revision=1, **self.decision)
        with self.assertRaises(PaymentReviewError):
            self.review.submit(self.order_id, **dict(changed, transaction_reference='stale'))
        self.assertEqual(self.review.pending(self.token)[0]['revision'], 2)
        self.review.decide(self.order_id, revision=2, **self.decision)
        self.assertFalse(self.review.customer_status(self.order_id, customer_id='customer-1')['can_correct'])
        with self.assertRaises(PaymentReviewError):
            self.review.submit(self.order_id, **dict(self.claim, expected_revision=2, transaction_reference='too late'))

    def test_rejected_submission_can_be_corrected_without_changing_rejection(self):
        self.review.submit(self.order_id, **self.claim)
        rejection = dict(token=self.token, approve=False, reason='reference not found', now=NOW, revision=1)
        self.review.decide(self.order_id, **rejection)
        self.assertTrue(self.review.customer_status(self.order_id, customer_id='customer-1')['can_correct'])
        self.review.submit(self.order_id, **dict(self.claim, transaction_reference='fixed', expected_revision=1))
        self.assertTrue(self.review.decide(self.order_id, **rejection)['replayed'])
        self.assertEqual(self.review.customer_status(self.order_id, customer_id='customer-1')['review_status'], 'in_review')
        self.review.decide(self.order_id, revision=2, **self.decision)
        self.assertEqual(self.review.customer_status(self.order_id, customer_id='customer-1')['review_status'], 'verified')

    def test_correction_racing_approval_has_one_winner(self):
        self.review.submit(self.order_id, **self.claim)
        def attempt(action):
            try:
                if action == 'approve':
                    return self.review.decide(self.order_id, revision=1, **self.decision)
                return self.review.submit(self.order_id, **dict(self.claim, transaction_reference='fixed', expected_revision=1))
            except PaymentReviewError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ['approve', 'correct']))
        self.assertEqual(sum(result is not None for result in results), 1)
        status = self.review.customer_status(self.order_id, customer_id='customer-1')
        balance = self.payments.ledger.balance('customer-1')
        self.assertTrue((status['revision'] == 1 and balance.reserved_minor == 5000)
                        or (status['revision'] == 2 and balance is None))

    def test_legacy_encrypted_case_migrates_and_resumes_once(self):
        from cryptography.fernet import Fernet
        import json
        key = Fernet.generate_key()
        content = b'%PDF-legacy receipt'
        claim = json.dumps({'transaction_reference': 'legacy', 'evidence_sha256': hashlib.sha256(content).hexdigest()}, sort_keys=True)
        transfer = dict(self.decision['verified_transfer'], occurred_at=NOW.isoformat())
        decision = json.dumps(dict(actor='staff:alice', approve=True, reason=self.decision['reason'], transfer=transfer), sort_keys=True)
        with sqlite3.connect(self.review.repository.path) as db:
            db.executescript("CREATE TABLE game_payment_reviews(order_id TEXT PRIMARY KEY, claim_json TEXT, submitted_at TEXT, decision_json TEXT, decided_at TEXT, applied_at TEXT); CREATE TABLE game_payment_receipts(order_id TEXT PRIMARY KEY, encrypted_content BLOB, sha256 TEXT);")
            db.execute('INSERT INTO game_payment_reviews VALUES(?,?,?,?,?,NULL)', (self.order_id, claim, NOW.isoformat(), decision, NOW.isoformat()))
            db.execute('INSERT INTO game_payment_receipts VALUES(?,?,?)', (self.order_id, Fernet(key).encrypt(content), hashlib.sha256(content).hexdigest()))
        service = PaymentReviewService(self.payments, self.review.reviewers, evidence_key=key)
        self.assertEqual(service.receipt(self.order_id, token=self.token, revision=1), content)
        service.resume(self.order_id, token=self.token, revision=1, now=NOW)
        self.assertTrue(service.resume(self.order_id, token=self.token, revision=1, now=NOW)['replayed'])
        self.assertEqual(self.payments.ledger.balance('customer-1').reserved_minor, 5000)
        self.assertEqual(service.pending(self.token), [])



class PaymentEvidenceHttpTests(unittest.TestCase):
    _payment = fixtures.SupplierSpineTests._payment
    _service = fixtures.SupplierSpineTests._service

    def setUp(self):
        import threading
        from cryptography.fernet import Fernet
        from aurix_games.web_api import create_server
        from aurix_games.customer_api import CustomerOrderService, SignedCustomerTokenVerifier, issue_test_token
        from datetime import datetime, timezone, timedelta
        fixtures.SupplierSpineTests.setUp(self)
        self._payment()
        self.token = 'reviewer-token-long-enough-for-evidence-123'
        self.review = PaymentReviewService(self._service(), {'staff:alice': hashlib.sha256(self.token.encode()).hexdigest()},
                                           evidence_key=Fernet.generate_key())
        secret = 'customer-secret-for-receipt-http-12345678'
        self.auth = issue_test_token('customer-1', secret, expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
        self.other = issue_test_token('other', secret, expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
        self.server = create_server(payment_review=self.review, customer_auth=SignedCustomerTokenVerifier(secret),
            customer_order_service=CustomerOrderService(self.checkout.repository, self.review.payments.ledger))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.path = f'/api/v1/orders/{self.order_id}/payment-evidence'

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, *, payload=None, token=None, headers=None):
        import json
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        hdr = dict(headers or {})
        if token:
            hdr['Authorization'] = f'Bearer {token}'
        if payload is not None:
            hdr['Content-Type'] = 'application/json'
        req = Request(self.base+self.path, data=None if payload is None else json.dumps(payload).encode(), headers=hdr)
        try:
            response = urlopen(req)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, json.loads(response.read())

    def test_owner_upload_status_and_private_response(self):
        import base64
        payload = {'transaction_reference': 'customer-tx', 'receipt_base64': base64.b64encode(b'%PDF-test receipt').decode()}
        self.assertEqual(self.request(payload=payload, headers={'Origin': self.base})[0], 401)
        self.assertEqual(self.request(payload=payload, token=self.other)[0], 404)
        self.assertEqual(self.request(payload=dict(payload, customer_id='customer-1'), token=self.auth)[0], 400)
        status, headers, body = self.request(payload=payload, token=self.auth)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(body['review_status'], 'in_review')
        self.assertNotIn('transaction_reference', body)
        self.assertEqual(self.review.receipt(self.order_id, token=self.token), b'%PDF-test receipt')
        self.assertEqual(self.request(payload=payload, token=self.auth)[0], 200)
        self.assertEqual(self.request(token=self.auth)[2]['review_status'], 'in_review')
        self.assertEqual(self.request(token=self.other)[0], 404)
        self.assertEqual(self.request(payload=dict(payload, receipt_base64='A' * (360*1024)), token=self.auth)[0], 400)
        self.assertIsNone(self.review.payments.ledger.balance('customer-1'))

    def test_order_endpoint_returns_review_snapshot_and_rejects_other_owner(self):
        import json
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        self.review.submit_receipt(self.order_id, customer_id='customer-1',
            transaction_reference='private-reference', receipt=b'%PDF-private-receipt', now=NOW)
        url = self.base + '/api/v1/orders/' + self.order_id
        with urlopen(Request(url, headers={'Authorization': 'Bearer ' + self.auth})) as response:
            result = json.loads(response.read())
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(result['state'], 'awaiting_payment')
        self.assertEqual(result['payment_review']['review_status'], 'in_review')
        self.assertEqual(result['payment_review']['revision'], 1)
        self.assertNotIn('private-reference', str(result))
        self.assertNotIn('encrypted_content', str(result))
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(url, headers={'Authorization': 'Bearer ' + self.other}))
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_phone_receipt_maximum_roundtrips_original_and_rejects_oversize_revision(self):
        import base64
        receipt = b'%PDF-' + b'x' * (MAX_RECEIPT_BYTES - 5)
        payload = {'transaction_reference': 'phone-receipt', 'receipt_base64': base64.b64encode(receipt).decode()}
        code, _, status = self.request(payload=payload, token=self.auth)
        self.assertEqual(code, 200)
        self.assertEqual(status['receipt_max_bytes'], MAX_RECEIPT_BYTES)
        self.assertEqual(self.review.receipt(self.order_id, token=self.token), receipt)
        payload.update(receipt_base64=base64.b64encode(receipt+b'x').decode(), expected_revision=1)
        self.assertEqual(self.request(payload=payload, token=self.auth)[0], 400)
        self.assertEqual(self.review.customer_status(self.order_id, customer_id='customer-1')['revision'], 1)
        self.assertEqual(self.review.receipt(self.order_id, token=self.token), receipt)
        self.assertIsNone(self.review.payments.ledger.balance('customer-1'))

    def test_declared_oversize_body_is_rejected_before_read_or_claim(self):
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        request = Request(self.base+self.path, data=b'{}', headers={
            'Authorization': 'Bearer '+self.auth, 'Content-Type': 'application/json',
            'Content-Length': str(MAX_RECEIPT_BODY_BYTES+1)})
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(self.review.customer_status(self.order_id, customer_id='customer-1')['review_status'], 'not_submitted')

    def test_cookie_upload_needs_same_origin_and_strict_base64(self):
        from aurix_games.customer_api import SESSION_COOKIE_NAME
        payload = {'transaction_reference': 'tx', 'receipt_base64': '%%%'}
        cookie = { 'Cookie': f'{SESSION_COOKIE_NAME}={self.auth}' }
        self.assertEqual(self.request(payload=payload, headers=cookie)[0], 403)
        self.assertEqual(self.request(payload=payload, headers=dict(cookie, Origin=self.base))[0], 400)
        self.assertEqual(self.review.pending(self.token), [])

    def review_request(self, action='queue', *, payload=None, token=None, method=None):
        import json
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        path = '/api/v1/admin/payment-review/' + action
        headers = {} if token is None else {'Authorization': f'Bearer {token}'}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            response = urlopen(request)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            body = json.loads(raw) if response.headers['Content-Type'].startswith('application/json') else raw
            return response.status, response.headers, body

    def test_named_reviewer_routes_complete_payment_and_hide_evidence_from_customers(self):
        self.review.submit_receipt(self.order_id, customer_id='customer-1', transaction_reference='tx',
                                  receipt=b'%PDF-pilot receipt', now=NOW)
        self.assertEqual(self.review_request(token=self.auth)[0], 401)
        self.assertEqual(self.review_request()[0], 401)
        status, headers, queue = self.review_request(token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(queue['actor'], 'staff:alice')
        self.assertEqual(queue['cases'][0]['revision'], 1)
        self.assertNotIn('encrypted_content', queue['cases'][0])
        prefix = f'orders/{self.order_id}/revisions/1/'
        code, headers, content = self.review_request(prefix+'receipt', token=self.token)
        self.assertEqual(code, 200)
        self.assertEqual(content, b'%PDF-pilot receipt')
        self.assertEqual(headers['Cache-Control'], 'private, no-store')
        self.assertIn('attachment;', headers['Content-Disposition'])
        self.assertEqual(self.review_request(prefix+'decision', token=self.token)[0], 405)
        payload = dict(approve=True, reason='verified in merchant account', verified_transfer=dict(
            rail='kpay', receiver_id='receiver-1', transaction_id='bank-123', amount_minor=5000,
            currency='MMK', occurred_at=NOW.isoformat()))
        self.assertEqual(self.review_request(prefix+'decision', token=self.token, payload=dict(payload, actor='staff:spoof'))[0], 400)
        self.assertEqual(self.review_request(prefix+'decision', token=self.token, payload=payload)[0], 200)
        self.assertTrue(self.review_request(prefix+'resume', token=self.token, payload={})[2]['replayed'])
        self.assertEqual(self.request(token=self.auth)[2]['review_status'], 'verified')
        self.assertEqual(self.review_request(token=self.token)[2]['cases'], [])
        self.assertEqual(self.review.payments.ledger.balance('customer-1').reserved_minor, 5000)

    def test_customer_correction_invalidates_stale_operator_page(self):
        import base64
        first = dict(transaction_reference='first', receipt_base64=base64.b64encode(b'%PDF-first').decode())
        self.assertEqual(self.request(token=self.auth, payload=first)[2]['revision'], 1)
        corrected = dict(transaction_reference='fixed', receipt_base64=base64.b64encode(b'%PDF-fixed').decode(), expected_revision=1)
        self.assertEqual(self.request(token=self.auth, payload=corrected)[2]['revision'], 2)
        action = f'orders/{self.order_id}/revisions/1/decision'
        self.assertEqual(self.review_request(action, token=self.token, payload={'approve': False, 'reason': 'stale page'})[0], 409)
        queue = self.review_request(token=self.token)[2]
        self.assertEqual(queue['cases'][0]['revision'], 2)
        self.assertEqual(queue['cases'][0]['claim']['transaction_reference'], 'fixed')
        self.assertIsNone(self.review.payments.ledger.balance('customer-1'))

    def test_staff_session_delegation_and_payment_settlement(self):
        import json
        import threading
        from datetime import datetime, timezone
        from pathlib import Path
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        from aurix_games.customer_api import CustomerIdentityService, CustomerIdentityStore, SESSION_COOKIE_NAME
        from aurix_games.web_api import create_server

        identities = CustomerIdentityStore(Path(self.checkout.repository.path).parent / "staff-identities.sqlite3")
        now = datetime.now(timezone.utc)
        owner = identities.customer_for("google", "owner", now=now)
        identities.link(owner, "telegram", "234", now=now)
        delegate = identities.customer_for("telegram", "345", now=now)
        identity = CustomerIdentityService("test-staff-signing-secret-at-least-32-bytes", identities)
        with patch.dict("os.environ", {"AURIX_GAMES_SUPER_ADMIN_CUSTOMER_ID": owner}):
            server = create_server(customer_identity_service=identity, payment_review=self.review)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        origin = f"http://127.0.0.1:{server.server_port}"
        owner_session, _ = identity._issue(owner, now)
        delegate_session, _ = identity._issue(delegate, now)

        def call(path, session=None, payload=None, request_origin=origin):
            headers = {"Origin": request_origin}
            if session:
                headers["Cookie"] = f"{SESSION_COOKIE_NAME}={session}"
            if payload is not None:
                headers["Content-Type"] = "application/json"
            request = Request(origin + "/api/v1/store-staff/" + path, headers=headers,
                              data=None if payload is None else json.dumps(payload).encode())
            try:
                response = urlopen(request, timeout=5)
            except HTTPError as error:
                response = error
            with response:
                return response.status, json.loads(response.read())

        queue = "payment-review/queue"
        self.assertEqual(call(queue)[0], 401)
        self.assertEqual(call(queue, delegate_session)[0], 403)
        payload = {"customer_id": delegate, "grant": True}
        self.assertEqual(call("admins", owner_session, payload, "https://wrong.invalid")[0], 403)
        self.assertEqual(call("admins", owner_session, {"customer_id": "absent", "grant": True})[0], 400)
        self.assertEqual(call("admins", owner_session, payload), (200, {"changed": True}))
        self.assertEqual(call("admins", delegate_session)[0], 403)
        self.assertEqual(call("admins", delegate_session, payload)[0], 403)
        self.review.submit_receipt(self.order_id, customer_id="customer-1", transaction_reference="claim",
                                  receipt=b"%PDF-staff-test", now=NOW)
        self.assertEqual(call(queue, delegate_session)[1]["cases"][0]["revision"], 1)
        path = f"payment-review/orders/{self.order_id}"
        decision = dict(approve=True, reason="verified in merchant account", verified_transfer=dict(
            rail="kpay", receiver_id="receiver-1", transaction_id="bank-staff-test", amount_minor=5000,
            currency="MMK", occurred_at=NOW.isoformat()))
        self.assertEqual(call(path + "/revisions/1/decision", delegate_session, decision)[0], 200)
        self.assertEqual(call("payment-review/recent", owner_session)[1]["orders"][0]["order_id"], self.order_id)
        status, history = call(path + "/history", owner_session)
        self.assertEqual(status, 200)
        self.assertEqual(history["reviews"][0]["decision"]["actor"], f"account:{delegate}")
        self.assertEqual(self.review.payments.ledger.balance("customer-1").reserved_minor, 5000)
        self.assertEqual(call("admins", owner_session, {**payload, "grant": False}), (200, {"changed": True}))
        self.assertEqual(call(queue, delegate_session)[0], 403)

    def test_external_refund_http_routes_and_customer_status(self):
        from datetime import datetime, timezone, timedelta
        self.review.submit(self.order_id, customer_id='customer-1', transaction_reference='customer-claim',
            evidence_sha256='c'*64, now=NOW)
        prefix = f'orders/{self.order_id}/revisions/1/'
        incoming = dict(rail='kpay', receiver_id='receiver-1', transaction_id='late-http-in',
            amount_minor=4300, currency='MMK', occurred_at=NOW.isoformat(),
            refund_destination_confirmed=True)
        status, _, result = self.review_request(prefix+'decision', token=self.token,
            payload={'approve': False, 'reason': 'late payment; refund the verified incoming amount',
                     'external_refund': incoming})
        self.assertEqual(status, 200)
        self.assertEqual(result['resolution'], 'external_refund_authorized')
        self.assertEqual(self.review.payments.repository.get(self.order_id).state, OrderState.CANCELLED)
        self.assertEqual(self.request(token=self.auth)[2]['review_status'], 'refund_pending')
        queue = self.review_request(token=self.token)[2]
        self.assertEqual(queue['refunds'][0]['incoming_amount_minor'], 4300)
        self.assertEqual(queue['cases'], [])
        refunded_at = datetime.now(timezone.utc)
        status, _, result = self.review_request(prefix+'refund-complete', token=self.token,
            payload={'refund_transaction_id': 'late-http-out', 'refunded_at': refunded_at.isoformat(),
                     'reason': 'outbound transfer verified at the merchant'})
        self.assertEqual(status, 200)
        self.assertTrue(result['refunded'])
        customer_status = self.request(token=self.auth)[2]
        self.assertEqual(customer_status['review_status'], 'refunded')
        self.assertNotIn('refund_transaction_id', customer_status)
