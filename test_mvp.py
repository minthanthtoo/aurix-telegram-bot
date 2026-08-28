import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from app import ClaimService, Database, PUBLIC_LIMIT_BYTES, TRIAL_LIMIT_BYTES
from commerce import CommerceDatabase, CommerceError, CommerceService
from receipt_llm import ReceiptLLMUnavailable, OpenAICompatibleReceiptExtractor, validate_extraction

UTC = timezone.utc


class Outline:
    def __init__(self):
        self.created = []
        self.deleted = []
        self.transfer = {}

    def create_key(self, name, limit_bytes):
        key = {"id": str(len(self.created) + 1), "accessUrl": f"ss://{len(self.created) + 1}"}
        self.created.append((name, limit_bytes, key))
        return key

    def delete_key(self, key_id):
        self.deleted.append(str(key_id))

    def transfer_metrics(self):
        return {"bytesTransferredByUserId": self.transfer}

    def list_keys(self):
        return {"accessKeys": []}

    def set_data_limit(self, *_args):
        return None


class MvpFeatureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "aurix.db"
        self.free_db = Database(self.path)
        self.free_db.initialize()
        self.outline = Outline()
        self.claims = ClaimService(self.free_db, self.outline, limit_bytes=PUBLIC_LIMIT_BYTES)
        self.commerce = CommerceService(
            CommerceDatabase(self.path), self.outline, Fernet.generate_key(),
            allow_legacy_text_approval=True,
        )
        self.commerce.initialize()
        self.now = datetime(2026, 8, 28, tzinfo=UTC)

    def tearDown(self):
        self.tmp.cleanup()

    def test_public_300_mib_daily_and_3_gib_monthly(self):
        free = self.claims.claim(101, "A", self.now)
        self.assertEqual(self.outline.created[0][1], PUBLIC_LIMIT_BYTES)
        trial = self.claims.claim_trial(101, "A", self.now)
        self.assertEqual(self.outline.created[1][1], TRIAL_LIMIT_BYTES)
        again = self.claims.claim_trial(101, "A", self.now + timedelta(days=2))
        self.assertIsNone(again.access_url)
        self.assertEqual(len(self.outline.created), 2)
        monthly = self.claims.claim_trial(101, "A", self.now + timedelta(days=30))
        self.assertTrue(monthly.access_url)
        self.assertEqual(len(self.outline.created), 3)
        self.assertTrue(free.access_url)

    def test_paid_catalog_contains_50gb_and_100gb_monthly(self):
        plans = {plan.code: plan for plan in self.commerce.plans()}
        self.assertEqual(plans["basic_50gb"].price_minor, 3000)
        self.assertEqual(plans["basic_50gb"].quota_bytes, 50 * 1024**3)
        self.assertEqual(plans["standard_100gb"].price_minor, 6000)
        self.assertEqual(plans["standard_100gb"].quota_bytes, 100 * 1024**3)

    def test_receipt_submission_is_idempotent_and_extracts_transaction(self):
        order = self.commerce.create_order(101, "A", "basic_50gb", self.now)
        extraction = {"provider": "kbz", "transaction_id": "TX-1", "confidence": 0.98}
        first = self.commerce.submit_receipt(101, order.order_id, "manual", "file-1", "uniq-1", b"receipt", "image/jpeg", extraction, self.now)
        second = self.commerce.submit_receipt(101, order.order_id, "manual", "file-1", "uniq-1", b"receipt", "image/jpeg", extraction, self.now)
        self.assertEqual(first["transaction_id"], "TX-1")
        self.assertEqual(second["transaction_id"], "TX-1")
        self.assertEqual(len(self.commerce.list_pending_receipts()), 1)
        with self.commerce.database.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payments WHERE order_id = ?", (order.order_id,)).fetchone()[0],
                0,
            )

    def test_human_verified_transaction_id_is_normalized_across_orders(self):
        first = self.commerce.create_order(113, "First", "basic_50gb", self.now)
        first_evidence = self.commerce.submit_receipt(
            113, first.order_id, "manual", "file-tx-first", "uniq-tx-first",
            b"receipt-tx-first", "image/jpeg", None, self.now,
        )
        self.commerce.verify_receipt(first_evidence["evidence_id"], 999, "TX 42", 3000, "MMK", self.now)
        second = self.commerce.create_order(114, "Second", "basic_50gb", self.now)
        second_evidence = self.commerce.submit_receipt(
            114, second.order_id, "manual", "file-tx-second", "uniq-tx-second",
            b"receipt-tx-second", "image/jpeg", None, self.now,
        )
        with self.assertRaises(CommerceError):
            self.commerce.verify_receipt(second_evidence["evidence_id"], 999, " tx42 ", 3000, "MMK", self.now)

    def test_duplicate_transaction_candidate_is_preserved_for_review(self):
        first = self.commerce.create_order(111, "First", "basic_50gb", self.now)
        self.commerce.submit_receipt(
            111, first.order_id, "manual", "file-first", "uniq-first",
            b"receipt-first", "image/jpeg",
            {"provider": "kbz", "transaction_id": "DUP-TX"}, self.now,
        )
        second = self.commerce.create_order(112, "Second", "basic_50gb", self.now)
        result = self.commerce.submit_receipt(
            112, second.order_id, "manual", "file-second", "uniq-second",
            b"receipt-second", "image/jpeg",
            {"provider": "kbz", "transaction_id": "DUP-TX"}, self.now,
        )
        self.assertEqual(result["extraction_status"], "needs_review")
        self.assertIn("duplicate_transaction_candidate", result["flags"])
        self.assertEqual(len(self.commerce.list_pending_receipts()), 2)

    def test_verified_overpayment_leaves_wallet_credit_after_capture(self):
        order = self.commerce.create_order(101, "A", "basic_50gb", self.now)
        extraction = {"provider": "kbz", "transaction_id": "TX-WALLET", "amount_minor": 4000, "currency": "MMK", "confidence": 0.95}
        evidence = self.commerce.submit_receipt(101, order.order_id, "manual", "file-wallet", "uniq-wallet", b"wallet-receipt", "image/jpeg", extraction, self.now)
        with self.assertRaises(CommerceError):
            self.commerce.approve_order(order.order_id, 999, self.now)
        self.commerce.verify_receipt(evidence["evidence_id"], 999, "TX-WALLET", 4000, "MMK", self.now)
        self.commerce.approve_order(order.order_id, 999, self.now)
        self.assertEqual(self.commerce.wallet_balance(101), 1000)

    def test_unparsed_receipt_can_be_human_verified(self):
        order = self.commerce.create_order(101, "A", "basic_50gb", self.now)
        evidence = self.commerce.submit_receipt(
            101, order.order_id, "manual", "file-manual", "uniq-manual",
            b"unreadable-receipt", "image/jpeg", None, self.now,
        )
        self.commerce.verify_receipt(
            evidence["evidence_id"], 999, "MANUAL-TX-1", 3000, "MMK", self.now
        )
        approval = self.commerce.approve_order(order.order_id, 999, self.now)
        self.assertEqual(approval.status, "approved")

    def test_llm_amount_never_credits_wallet_without_human_verification(self):
        order = self.commerce.create_order(101, "A", "basic_50gb", self.now)
        extraction = {
            "provider": "kbz", "transaction_id": "FAKE-HIGH",
            "amount_minor": 1_000_000, "currency": "MMK", "confidence": 1,
        }
        self.commerce.submit_receipt(
            101, order.order_id, "manual", "file-high", "uniq-high",
            b"receipt-high", "image/jpeg", extraction, self.now,
        )
        with self.assertRaises(CommerceError):
            self.commerce.approve_order(order.order_id, 999, self.now)
        self.assertEqual(self.commerce.wallet_balance(101), 0)

    def test_wallet_payment_reserves_and_rejection_releases_funds(self):
        self.commerce.credit_wallet(101, 3000, "deposit-1", 999)
        order = self.commerce.create_order(101, "A", "basic_50gb", self.now)
        self.assertEqual(self.commerce.pay_order_with_wallet(101, order.order_id, self.now), "reserved")
        self.assertEqual(self.commerce.wallet_balance(101), 0)
        self.commerce.reject_order(order.order_id, 999, self.now)
        self.assertEqual(self.commerce.wallet_balance(101), 3000)

    def test_unverified_text_payment_cannot_be_approved_in_public_mode(self):
        strict = CommerceService(
            CommerceDatabase(self.path), self.outline, Fernet.generate_key()
        )
        strict.initialize()
        order = strict.create_order(202, "Strict", "basic_50gb", self.now)
        strict.submit_payment(202, order.order_id, "manual", "TEXT-ONLY", self.now)
        with self.assertRaises(CommerceError):
            strict.approve_order(order.order_id, 999, self.now)

    def test_receipt_without_transaction_moves_order_to_review_pending(self):
        order = self.commerce.create_order(303, "Review", "basic_50gb", self.now)
        evidence = self.commerce.submit_receipt(
            303, order.order_id, "manual", "file-review", "uniq-review",
            b"review-receipt", "image/jpeg", None, self.now,
        )
        detail = self.commerce.order_detail(order.order_id, 303)
        self.assertEqual(detail["status"], "payment_submitted")
        self.assertEqual(detail["stage"], "review_pending")
        self.assertEqual(self.commerce.reject_receipt(evidence["evidence_id"], 999, now=self.now), order.order_id)
        self.assertEqual(self.commerce.order_detail(order.order_id, 303)["stage"], "awaiting_payment")

    def test_wallet_capture_does_not_double_deduct_reserved_balance(self):
        self.commerce.credit_wallet(404, 3000, "deposit-capture", 999)
        order = self.commerce.create_order(404, "Wallet", "basic_50gb", self.now)
        self.assertEqual(self.commerce.pay_order_with_wallet(404, order.order_id, self.now), "reserved")
        self.commerce.approve_order(order.order_id, 999, self.now)
        self.assertEqual(self.commerce.wallet_balance(404), 0)
        with self.commerce.database.connect() as connection:
            ledger = connection.execute(
                "SELECT kind, amount_minor FROM wallet_ledger WHERE telegram_id = 404 ORDER BY created_at, id"
            ).fetchall()
        self.assertEqual(sorted(row["kind"] for row in ledger), ["capture", "credit", "reserve"])

    def test_stale_wallet_reservation_is_released_and_order_closed(self):
        self.commerce.credit_wallet(405, 3000, "deposit-stale", 999)
        order = self.commerce.create_order(405, "Wallet", "basic_50gb", self.now)
        self.commerce.pay_order_with_wallet(405, order.order_id, self.now)
        self.assertEqual(
            self.commerce.release_expired_wallet_reservations(
                self.now + timedelta(hours=24)
            ),
            1,
        )
        self.assertEqual(self.commerce.wallet_balance(405), 3000)
        self.assertEqual(self.commerce.order_detail(order.order_id, 405)["stage"], "cancelled")

    def test_untouched_orders_expire_and_can_be_replaced(self):
        order = self.commerce.create_order(505, "Expired", "basic_50gb", self.now)
        closed = self.commerce.expire_open_orders(self.now + timedelta(hours=24))
        self.assertEqual(closed, 1)
        self.assertEqual(self.commerce.order_detail(order.order_id, 505)["stage"], "cancelled")
        replacement = self.commerce.create_order(505, "Expired", "standard_100gb", self.now + timedelta(hours=24, minutes=1))
        self.assertNotEqual(replacement.order_id, order.order_id)

    def test_quota_hit_revokes_paid_key_once(self):
        order = self.commerce.create_order(101, "A", "basic_50gb", self.now)
        self.commerce.submit_payment(101, order.order_id, "manual", "TX-2", self.now)
        approval = self.commerce.approve_order(order.order_id, 999, self.now)
        self.commerce.process_jobs(self.now)
        self.outline.transfer["1"] = 50 * 1024**3
        self.assertEqual(self.commerce.enforce_quotas(self.now), 1)
        self.assertEqual(self.commerce.enforce_quotas(self.now), 0)
        self.commerce.process_jobs(self.now)
        self.assertEqual(self.outline.deleted, ["1"])
        self.assertEqual(self.commerce.user_vpn(101)["access_url"], None)
        self.assertEqual(approval.order_id, order.order_id)

    def test_llm_requires_explicit_configuration_and_validates_shape(self):
        with self.assertRaises(ReceiptLLMUnavailable):
            OpenAICompatibleReceiptExtractor().extract(b"x")
        parsed = validate_extraction({"transaction_id": "TX", "confidence": 1, "flags": [], "notes": []})
        self.assertEqual(parsed.transaction_id, "TX")

    def test_outline_quota_api_helpers_are_available(self):
        from app import OutlineClient

        client = OutlineClient.__new__(OutlineClient)
        calls = []
        client._request = lambda method, path, body=None, accepted_statuses=(200, 201, 204): calls.append((method, path, body)) or {"id": "a", "accessUrl": "ss://x"}
        self.assertEqual(client.create_key_with_id("a/b", "n", 10)["id"], "a")
        client.delete_data_limit("a/b")
        client.rename_key("a/b", "renamed")
        self.assertEqual(calls[0][1], "/access-keys/a%2Fb")
        self.assertEqual(calls[1][1], "/access-keys/a%2Fb/data-limit")
        self.assertEqual(calls[2][1], "/access-keys/a%2Fb/name")


if __name__ == "__main__":
    unittest.main()
