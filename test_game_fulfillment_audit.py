import sqlite3
import tempfile
import unittest
from pathlib import Path

from aurix_games.fulfillment_audit import audit_fulfillment, audit_money_integrity
from aurix_games.order_repository import GameOrderRepository
from aurix_games.customer_ledger import CustomerLedger
from aurix_games.fulfillment import FulfillmentTaskStore
from aurix_games.payment_service import CustomerPaymentService
from aurix_games.payment_review import PaymentReviewService
from cryptography.fernet import Fernet
from datetime import datetime, timezone
import hashlib
from scripts.audit_games_fulfillment import main
from unittest.mock import patch


class FulfillmentAuditTests(unittest.TestCase):
    def test_finds_cross_store_gaps_without_exposing_receipts_or_mutating_files(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("orders.db", "ledger.db", "tasks.db")]
            schemas = [
                "CREATE TABLE game_orders(order_id TEXT, state TEXT, provider_order_id TEXT)",
                "CREATE TABLE game_order_reservations(order_id TEXT, status TEXT)",
                "CREATE TABLE game_fulfillment_tasks(order_id TEXT, status TEXT, provider_order_id TEXT)",
            ]
            for path, schema in zip(paths, schemas):
                with sqlite3.connect(path) as db:
                    db.execute(schema)
            with sqlite3.connect(paths[0]) as db:
                db.executemany("INSERT INTO game_orders VALUES (?,?,?)", [
                    ("a", "submitted", "private-receipt"),
                    ("b", "fulfillment_queued", None),
                    ("c", "submitted", "receipt-1"),
                    ("d", "delivered", "receipt-2"),
                    ("e", "fulfillment_queued", None),
                    ("f", "delivered", "receipt-3"),
                    ("g", "submitted", "receipt-4"),
                    ("h", "review_refund", None),
                ])
            with sqlite3.connect(paths[1]) as db:
                db.executemany("INSERT INTO game_order_reservations VALUES (?,?)", [
                    ("a", "reserved"), ("b", "reserved"), ("c", "captured"), ("d", "captured"), ("e", "reserved"), ("g", "released"), ("h", "reserved"),
                ])
            with sqlite3.connect(paths[2]) as db:
                db.executemany("INSERT INTO game_fulfillment_tasks VALUES (?,?,?)", [
                    ("a", "submitted", "private-receipt"),
                    ("c", "submitted", "different-receipt"),
                    ("d", "submitted", "receipt-2"),
                    ("e", "running", "accepted-receipt"),
                    ("orphan", "queued", None),
                    ("h", "failed", None),
                ])
            before = [path.read_bytes() for path in paths]
            result = audit_fulfillment(*paths, limit=2)
            self.assertEqual(result["total_findings"], 8)
            self.assertTrue(result["truncated"])
            self.assertEqual([item["issue"] for item in result["findings"]], [
                "accepted_funds_not_captured", "queue_handoff_missing",
            ])
            all_results = audit_fulfillment(*paths)
            self.assertEqual({row["issue"] for row in all_results["findings"]}, {
                "accepted_funds_not_captured", "queue_handoff_missing", "receipt_conflict",
                "receipt_handoff_pending", "orphan_workflow_record", "accepted_reservation_missing",
                "accepted_funds_released", "failed_task_funds_reserved",
            })
            self.assertNotIn("private-receipt", str(all_results))
            self.assertNotIn("d", [item["order_id"] for item in all_results["findings"]])
            self.assertEqual([path.read_bytes() for path in paths], before)
            with patch("builtins.print"):
                self.assertEqual(main(["--orders-db", str(paths[0]), "--ledger-db", str(paths[1]), "--tasks-db", str(paths[2])]), 1)

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.db"
            with self.assertRaises(FileNotFoundError):
                audit_fulfillment(missing, missing, missing)
            self.assertFalse(missing.exists())
            with patch("builtins.print"):
                self.assertEqual(main(["--orders-db", str(missing), "--ledger-db", str(missing), "--tasks-db", str(missing)]), 2)


class MoneyIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = tuple(self.root / name for name in ('orders.db', 'ledger.db', 'tasks.db'))
        self.orders = GameOrderRepository(self.paths[0]); self.orders.initialize()
        self.ledger = CustomerLedger(self.paths[1]); self.ledger.initialize()
        FulfillmentTaskStore(self.paths[2], encryption_key=Fernet.generate_key()).initialize()
        payments = CustomerPaymentService(self.orders, self.ledger)
        payments.initialize_transfer_identity_claims()
        PaymentReviewService(payments, {'staff:test': hashlib.sha256(b'token').hexdigest()})._initialize()
        self.now = datetime.now(timezone.utc)

    def credit(self):
        self.ledger.credit(customer_id='private-customer', amount_minor=1000, currency='MMK',
            event_key='verified-credit', actor='staff:test', reason='fixture', occurred_at=self.now)

    def test_empty_and_clean_are_distinct_and_files_are_unchanged(self):
        self.assertEqual(audit_money_integrity(*self.paths)['status'], 'empty')
        self.credit()
        before = [p.read_bytes() for p in self.paths]
        result = audit_money_integrity(*self.paths)
        self.assertEqual(result['status'], 'clear')
        self.assertEqual(result['total_findings'], 0)
        self.assertFalse(result['merchant_funds_verified'])
        self.assertFalse(result['supplier_funds_verified'])
        self.assertEqual([p.read_bytes() for p in self.paths], before)

    def test_detects_drift_with_masked_accounts_and_bounded_output(self):
        self.credit()
        with sqlite3.connect(self.paths[1]) as db:
            db.execute("UPDATE game_customer_accounts SET available_minor=900,reserved_minor=100")
        before = [p.read_bytes() for p in self.paths]
        result = audit_money_integrity(*self.paths, limit=1)
        self.assertEqual(result['total_findings'], 2)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['findings']), 1)
        self.assertNotIn('private-customer', str(result))
        self.assertEqual([p.read_bytes() for p in self.paths], before)

    def test_unfinished_settlement_and_unmatched_event_are_reported(self):
        from aurix_games.order_state import create_order, OrderSnapshot
        order = create_order(order_id='order-one', customer_id='private-customer', customer_idempotency_key='one',
            snapshot=OrderSnapshot('pack', 'mobile-legends', 'direct_topup', 1000, 'MMK', 'private-fingerprint'))
        self.orders.create(order, created_at=self.now)
        with sqlite3.connect(self.paths[0]) as db:
            db.execute("INSERT INTO game_payment_transfer_identity_claims VALUES (?,?,?,?,?,?)",
                ('private-transfer', order.order_id, 'settlement', None, 'staff:test', self.now.isoformat()))
            db.execute("INSERT INTO game_order_webhook_inbox VALUES (?,?,?,?,?,?)",
                ('event-one', 'private-provider-order', '{}', self.now.isoformat(), self.now.isoformat(), None))
        result = audit_money_integrity(*self.paths)
        self.assertEqual({f['issue'] for f in result['findings']},
            {'settlement_handoff_pending', 'unmatched_supplier_event'})
        for secret in ['private-transfer', 'private-customer', 'private-provider-order', 'private-fingerprint']:
            self.assertNotIn(secret, str(result))

    def test_missing_optional_schema_is_incomplete_not_clear(self):
        with sqlite3.connect(self.paths[0]) as db:
            db.execute('DROP TABLE game_payment_review_cases')
        result = audit_money_integrity(*self.paths)
        self.assertEqual(result['status'], 'incomplete')
        self.assertIn('game_payment_review_cases', result['unavailable_checks'])
        with patch('builtins.print'):
            self.assertEqual(main(['--orders-db', str(self.paths[0]), '--ledger-db', str(self.paths[1]),
                '--tasks-db', str(self.paths[2]), '--money-integrity']), 2)

    def test_corrupt_running_entry_and_unreceipted_dispatch_are_visible(self):
        from aurix_games.order_state import create_order, OrderSnapshot
        self.credit()
        with sqlite3.connect(self.paths[1]) as db:
            db.execute('DROP TRIGGER game_ledger_entries_append_only_update')
            db.execute('UPDATE game_ledger_entries SET available_after_minor=999')
        order = create_order(order_id='order-one', customer_id='private-customer', customer_idempotency_key='one',
            snapshot=OrderSnapshot('pack', 'mobile-legends', 'direct_topup', 1000, 'MMK', 'private-fingerprint'))
        self.orders.create(order, created_at=self.now)
        tasks = FulfillmentTaskStore(self.paths[2], encryption_key=Fernet.generate_key())
        tasks.enqueue(order_id=order.order_id, fulfillment_kind='direct_topup', payload={'private':'recipient'}, available_at=self.now)
        task, = tasks.claim_due(now=self.now)
        tasks.begin_dispatch(task, now=self.now)
        result = audit_money_integrity(*self.paths)
        self.assertEqual({f['issue'] for f in result['findings']},
            {'entry_running_balance_drift','unresolved_supplier_dispatch'})

    def test_reservation_must_match_frozen_amount(self):
        from aurix_games.order_state import create_order, OrderSnapshot
        self.credit()
        order = create_order(order_id='order-one', customer_id='private-customer', customer_idempotency_key='one',
            snapshot=OrderSnapshot('pack', 'mobile-legends', 'direct_topup', 1000, 'MMK', 'private-fingerprint'))
        self.orders.create(order, created_at=self.now)
        self.ledger.reserve(order_id=order.order_id, customer_id=order.customer_id, amount_minor=900,
            currency='MMK', event_key='reserve', actor='staff:test', reason='fixture', occurred_at=self.now)
        result = audit_money_integrity(*self.paths)
        self.assertIn('reservation_identity_mismatch', {f['issue'] for f in result['findings']})

    def test_summary_reports_failures_without_logging_customer_references(self):
        import io
        import json
        from contextlib import redirect_stdout
        self.credit()
        with sqlite3.connect(self.paths[1]) as db:
            db.execute('UPDATE game_customer_accounts SET available_minor=1')
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--orders-db', str(self.paths[0]), '--ledger-db', str(self.paths[1]),
                '--tasks-db', str(self.paths[2]), '--money-integrity', '--summary']), 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result['total_findings'], 1)
        self.assertEqual(result['status'], 'attention')
        self.assertNotIn('private-customer', output.getvalue())
        self.assertNotIn('account-', output.getvalue())

    def test_bank_refund_cannot_be_reported_clean_when_wallet_was_also_reimbursed(self):
        from aurix_games.order_state import create_order, OrderSnapshot
        self.credit()
        order = create_order(order_id='refund-one',customer_id='private-customer',customer_idempotency_key='refund',
            snapshot=OrderSnapshot('pack','mobile-legends','direct_topup',1000,'MMK','private-fingerprint'))
        self.orders.create(order,created_at=self.now)
        self.ledger.reserve(order_id=order.order_id,customer_id=order.customer_id,amount_minor=1000,currency='MMK',
            event_key='reserve',actor='staff:test',reason='fixture',occurred_at=self.now)
        self.ledger.release(order_id=order.order_id,event_key='release',actor='staff:test',reason='fixture',occurred_at=self.now)
        # Deliberately inject inconsistent imported evidence; normal review rejects it.
        with sqlite3.connect(self.paths[0]) as db:
            db.execute('DROP TABLE game_payment_external_refund_completions')
            db.execute('DROP TABLE game_payment_external_refund_authorizations')
            db.execute('CREATE TABLE game_payment_external_refund_authorizations(order_id,revision,incoming_transfer_key)')
            db.execute('CREATE TABLE game_payment_external_refund_completions(order_id,revision)')
            db.execute('INSERT INTO game_payment_external_refund_authorizations VALUES (?,?,?)',
                (order.order_id,1,'verified-credit'))
            db.execute('INSERT INTO game_payment_external_refund_completions VALUES (?,?)',(order.order_id,1))
        with sqlite3.connect(self.paths[1]) as db:
            db.execute('DROP TRIGGER game_ledger_entries_append_only_update')
            db.execute("UPDATE game_ledger_entries SET kind='verified_transfer' WHERE event_key='verified-credit'")
        before=[path.read_bytes() for path in self.paths]
        result=audit_money_integrity(*self.paths)
        issues={row['issue'] for row in result['findings']}
        self.assertIn('bank_and_wallet_refund_conflict',issues)
        self.assertIn('bank_refund_with_allocated_transfer',issues)
        self.assertEqual(result['status'],'attention')
        self.assertEqual(before,[path.read_bytes() for path in self.paths])
