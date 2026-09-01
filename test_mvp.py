import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from PIL import Image

from app import ClaimService, Database, PUBLIC_LIMIT_BYTES, TRIAL_LIMIT_BYTES
from commerce import CommerceDatabase, CommerceError, CommerceService
from receipt_llm import (
    FallbackReceiptExtractor,
    OpenAICompatibleReceiptExtractor,
    ReceiptExtraction,
    ReceiptLLMUnavailable,
    normalize_receipt_image,
    validate_extraction,
)

UTC = timezone.utc


class ReceiptStorage:
    configured = True
    bucket = "payment-receipts"

    def __init__(self):
        self.uploads = []
        self.deleted = []
        self.fail = False

    def upload(self, path, data, mime_type):
        if self.fail:
            raise RuntimeError("storage unavailable")
        self.uploads.append((path, bytes(data), mime_type))
        return path

    def signed_url(self, path, expires_in=300):
        return f"https://storage.example/{path}?ttl={expires_in}"

    def delete(self, path):
        self.deleted.append(path)


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
            CommerceDatabase(self.path),
            self.outline,
            Fernet.generate_key(),
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
        self.assertEqual(plans["basic_50gb"].quota_bytes, 50_000_000_000)
        self.assertEqual(plans["standard_100gb"].price_minor, 6000)
        self.assertEqual(plans["standard_100gb"].quota_bytes, 100_000_000_000)

    def test_receipt_submission_is_idempotent_and_extracts_transaction(self):
        order = self.commerce.create_order(101, "A", "basic_50gb", self.now)
        extraction = {"provider": "kbz", "transaction_id": "TX-1", "confidence": 0.98}
        first = self.commerce.submit_receipt(
            101,
            order.order_id,
            "manual",
            "file-1",
            "uniq-1",
            b"receipt",
            "image/jpeg",
            extraction,
            self.now,
        )
        second = self.commerce.submit_receipt(
            101,
            order.order_id,
            "manual",
            "file-1",
            "uniq-1",
            b"receipt",
            "image/jpeg",
            extraction,
            self.now,
        )
        self.assertEqual(first["transaction_id"], "TX-1")
        self.assertEqual(second["transaction_id"], "TX-1")
        self.assertEqual(len(self.commerce.list_pending_receipts()), 1)
        with self.commerce.database.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM payments WHERE order_id = ?", (order.order_id,)
                ).fetchone()[0],
                0,
            )

    def test_receipt_storage_keeps_bytes_out_of_database_and_is_idempotent(self):
        storage = ReceiptStorage()
        service = CommerceService(
            CommerceDatabase(self.path),
            self.outline,
            Fernet.generate_key(),
            receipt_storage=storage,
            receipt_storage_required=True,
        )
        service.initialize()
        order = service.create_order(101, "A", "basic_50gb", self.now)
        first = service.submit_receipt(
            101,
            order.order_id,
            "manual",
            "file-storage",
            "unique-storage",
            b"receipt-bytes",
            "image/jpeg",
            None,
            self.now,
        )
        # Simulate a legacy/inconsistent row so an idempotent retry repairs the
        # customer-facing order state along with the stored evidence.
        with service.database.connect() as connection:
            connection.execute(
                "UPDATE orders SET status = 'awaiting_payment' WHERE id = ?",
                (order.order_id,),
            )
        second = service.submit_receipt(
            101,
            order.order_id,
            "manual",
            "file-storage",
            "unique-storage",
            b"receipt-bytes",
            "image/jpeg",
            None,
            self.now,
        )
        self.assertEqual(first["storage_status"], "stored")
        self.assertEqual(second["storage_status"], "stored")
        self.assertEqual(len(storage.uploads), 1)
        receipt = service.get_receipt(first["evidence_id"])
        self.assertEqual(receipt["storage_bucket"], "payment-receipts")
        self.assertTrue(receipt["storage_path"].startswith(f"orders/{order.order_id}/"))
        self.assertEqual(receipt["storage_status"], "stored")
        self.assertEqual(service.order_detail(order.order_id, 101)["status"], "payment_submitted")
        with service.database.connect() as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(payment_evidence)")}
            self.assertNotIn("image_bytes", columns)
            self.assertEqual(
                connection.execute("SELECT byte_size FROM payment_evidence").fetchone()[0],
                len(b"receipt-bytes"),
            )

    def test_receipt_storage_failure_is_retryable_without_review_state(self):
        storage = ReceiptStorage()
        storage.fail = True
        service = CommerceService(
            CommerceDatabase(self.path),
            self.outline,
            Fernet.generate_key(),
            receipt_storage=storage,
            receipt_storage_required=True,
        )
        service.initialize()
        order = service.create_order(102, "A", "basic_50gb", self.now)
        with self.assertRaises(CommerceError):
            service.submit_receipt(
                102,
                order.order_id,
                "manual",
                "file-fail",
                "unique-fail",
                b"receipt-failure",
                "image/png",
                None,
                self.now,
            )
        self.assertEqual(service.order_detail(order.order_id, 102)["status"], "awaiting_payment")
        with service.database.connect() as connection:
            row = connection.execute(
                "SELECT id, storage_status, storage_error FROM payment_evidence WHERE order_id = ?",
                (order.order_id,),
            ).fetchone()
        self.assertEqual(row["storage_status"], "failed")
        self.assertEqual(row["storage_error"], "RuntimeError")
        with self.assertRaises(CommerceError):
            service.verify_receipt(row["id"], 999, "TX-FAILED", 3000, "MMK", self.now)
        storage.fail = False
        retried = service.submit_receipt(
            102,
            order.order_id,
            "manual",
            "file-fail",
            "unique-fail",
            b"receipt-failure",
            "image/png",
            None,
            self.now,
        )
        self.assertEqual(retried["evidence_id"], row["id"])
        self.assertEqual(retried["storage_status"], "stored")
        self.assertEqual(service.order_detail(order.order_id, 102)["status"], "payment_submitted")

    def test_human_verified_transaction_id_is_normalized_across_orders(self):
        first = self.commerce.create_order(113, "First", "basic_50gb", self.now)
        first_evidence = self.commerce.submit_receipt(
            113,
            first.order_id,
            "manual",
            "file-tx-first",
            "uniq-tx-first",
            b"receipt-tx-first",
            "image/jpeg",
            None,
            self.now,
        )
        self.commerce.verify_receipt(
            first_evidence["evidence_id"], 999, "TX 42", 3000, "MMK", self.now
        )
        second = self.commerce.create_order(114, "Second", "basic_50gb", self.now)
        second_evidence = self.commerce.submit_receipt(
            114,
            second.order_id,
            "manual",
            "file-tx-second",
            "uniq-tx-second",
            b"receipt-tx-second",
            "image/jpeg",
            None,
            self.now,
        )
        with self.assertRaises(CommerceError):
            self.commerce.verify_receipt(
                second_evidence["evidence_id"], 999, " tx42 ", 3000, "MMK", self.now
            )

    def test_duplicate_transaction_candidate_is_preserved_for_review(self):
        first = self.commerce.create_order(111, "First", "basic_50gb", self.now)
        self.commerce.submit_receipt(
            111,
            first.order_id,
            "manual",
            "file-first",
            "uniq-first",
            b"receipt-first",
            "image/jpeg",
            {"provider": "kbz", "transaction_id": "DUP-TX"},
            self.now,
        )
        second = self.commerce.create_order(112, "Second", "basic_50gb", self.now)
        result = self.commerce.submit_receipt(
            112,
            second.order_id,
            "manual",
            "file-second",
            "uniq-second",
            b"receipt-second",
            "image/jpeg",
            {"provider": "kbz", "transaction_id": "DUP-TX"},
            self.now,
        )
        self.assertEqual(result["extraction_status"], "needs_review")
        self.assertIn("duplicate_transaction_candidate", result["flags"])
        self.assertEqual(len(self.commerce.list_pending_receipts()), 2)

    def test_verified_overpayment_leaves_wallet_credit_after_capture(self):
        order = self.commerce.create_order(101, "A", "basic_50gb", self.now)
        extraction = {
            "provider": "kbz",
            "transaction_id": "TX-WALLET",
            "amount_minor": 4000,
            "currency": "MMK",
            "confidence": 0.95,
        }
        evidence = self.commerce.submit_receipt(
            101,
            order.order_id,
            "manual",
            "file-wallet",
            "uniq-wallet",
            b"wallet-receipt",
            "image/jpeg",
            extraction,
            self.now,
        )
        with self.assertRaises(CommerceError):
            self.commerce.approve_order(order.order_id, 999, self.now)
        self.commerce.verify_receipt(
            evidence["evidence_id"], 999, "TX-WALLET", 4000, "MMK", self.now
        )
        self.commerce.approve_order(order.order_id, 999, self.now)
        self.assertEqual(self.commerce.wallet_balance(101), 1000)

    def test_wallet_topup_requires_exact_verified_receipt_and_credits_once(self):
        order = self.commerce.create_wallet_topup(101, "A", 7500, self.now)
        self.commerce.choose_payment_method(101, order.order_id, "wavepay")
        evidence = self.commerce.submit_receipt(
            101,
            order.order_id,
            "wavepay",
            "topup-file",
            "topup-unique",
            b"topup-receipt",
            "image/jpeg",
            {
                "provider": "WavePay",
                "transaction_id": "TOPUP-7500",
                "amount_minor": 7500,
                "currency": "MMK",
                "timestamp": self.now.isoformat(),
                "confidence": 0.97,
            },
            self.now,
        )
        with self.assertRaisesRegex(CommerceError, "match exactly"):
            self.commerce.verify_receipt(
                evidence["evidence_id"], 999, "TOPUP-7500", 8000, "MMK", self.now
            )

        self.commerce.verify_receipt(
            evidence["evidence_id"], 999, "TOPUP-7500", 7500, "MMK", self.now
        )
        first = self.commerce.approve_order(order.order_id, 999, self.now)
        second = self.commerce.approve_order(order.order_id, 999, self.now + timedelta(minutes=1))

        self.assertEqual(first.status, "wallet_credited")
        self.assertEqual(second.status, "already_credited")
        self.assertEqual(self.commerce.wallet_balance(101), 7500)
        with self.assertRaisesRegex(CommerceError, "manual off-platform"):
            self.commerce.refund_order(order.order_id, 999, now=self.now)
        with self.commerce.database.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM subscriptions WHERE telegram_id = 101"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM provisioning_jobs").fetchone()[0],
                0,
            )

    def test_same_receipt_image_cannot_fund_two_wallet_topups(self):
        first = self.commerce.create_wallet_topup(201, "First", 3000, self.now)
        self.commerce.submit_receipt(
            201,
            first.order_id,
            "kbzpay",
            "first-file",
            "same-telegram-file",
            b"same-receipt-image",
            "image/jpeg",
            now=self.now,
        )
        second = self.commerce.create_wallet_topup(202, "Second", 3000, self.now)

        self.assertEqual(
            self.commerce.receipt_duplicate_status(
                202,
                second.order_id,
                b"same-receipt-image",
                "different-telegram-file",
            ),
            "different_order",
        )
        with self.assertRaisesRegex(CommerceError, "already submitted"):
            self.commerce.submit_receipt(
                202,
                second.order_id,
                "kbzpay",
                "second-file",
                "different-telegram-file",
                b"same-receipt-image",
                "image/jpeg",
                now=self.now,
            )

    def test_assisted_topup_extraction_flags_stale_receipt_for_review(self):
        order = self.commerce.create_wallet_topup(203, "Stale", 6000, self.now)
        self.commerce.choose_payment_method(203, order.order_id, "kbzpay")
        submitted = self.commerce.submit_receipt(
            203,
            order.order_id,
            "kbzpay",
            "stale-file",
            "stale-unique",
            b"stale-receipt",
            "image/jpeg",
            queue_extraction=True,
            now=self.now,
        )
        job = self.commerce.claim_receipt_extraction_job(self.now)
        self.commerce.finish_receipt_extraction(
            job["job_id"],
            submitted["evidence_id"],
            {
                "provider": "KBZPay",
                "transaction_id": "STALE-6000",
                "amount_minor": 6000,
                "currency": "MMK",
                "timestamp": (self.now - timedelta(hours=2)).isoformat(),
                "confidence": 0.99,
                "flags": [],
            },
            now=self.now,
        )

        receipt = self.commerce.get_receipt(submitted["evidence_id"])
        self.assertEqual(receipt["extraction_status"], "needs_review")
        self.assertIn("receipt_older_than_1_hour", receipt["extraction"]["flags"])

    def test_async_receipt_age_uses_upload_time_not_late_model_completion(self):
        order = self.commerce.create_wallet_topup(204, "Delayed", 6000, self.now)
        self.commerce.choose_payment_method(204, order.order_id, "kbzpay")
        submitted = self.commerce.submit_receipt(
            204,
            order.order_id,
            "kbzpay",
            "delayed-file",
            "delayed-unique",
            b"delayed-receipt",
            "image/jpeg",
            queue_extraction=True,
            now=self.now,
        )
        job = self.commerce.claim_receipt_extraction_job(self.now)
        self.commerce.finish_receipt_extraction(
            job["job_id"],
            submitted["evidence_id"],
            {
                "provider": "KBZPay",
                "completion_status": "completed",
                "transaction_id": "DELAY-6000",
                "transaction_id_label": "Transaction ID",
                "amount_minor": 6000,
                "currency": "MMK",
                "timestamp": (self.now - timedelta(minutes=30)).isoformat(),
                "confidence": 0.99,
                "flags": [],
            },
            now=self.now + timedelta(hours=3),
        )

        receipt = self.commerce.get_receipt(submitted["evidence_id"])
        self.assertNotIn("receipt_older_than_1_hour", receipt["extraction"]["flags"])

    def test_selected_payment_method_cannot_be_overwritten_by_model_output(self):
        order = self.commerce.create_wallet_topup(205, "Provider", 6000, self.now)
        self.commerce.choose_payment_method(205, order.order_id, "kbzpay")
        result = self.commerce.submit_receipt(
            205,
            order.order_id,
            "kbzpay",
            "provider-file",
            "provider-unique",
            b"provider-receipt",
            "image/jpeg",
            extraction={"provider": "wavepay", "transaction_id": "WRONG-PROVIDER"},
            now=self.now,
        )

        receipt = self.commerce.get_receipt(result["evidence_id"])
        self.assertEqual(receipt["provider"], "kbzpay")

    def test_unparsed_receipt_can_be_human_verified(self):
        order = self.commerce.create_order(101, "A", "basic_50gb", self.now)
        evidence = self.commerce.submit_receipt(
            101,
            order.order_id,
            "manual",
            "file-manual",
            "uniq-manual",
            b"unreadable-receipt",
            "image/jpeg",
            None,
            self.now,
        )
        self.commerce.verify_receipt(
            evidence["evidence_id"], 999, "MANUAL-TX-1", 3000, "MMK", self.now
        )
        approval = self.commerce.approve_order(order.order_id, 999, self.now)
        self.assertEqual(approval.status, "approved")

    def test_llm_amount_never_credits_wallet_without_human_verification(self):
        order = self.commerce.create_order(101, "A", "basic_50gb", self.now)
        extraction = {
            "provider": "kbz",
            "transaction_id": "FAKE-HIGH",
            "amount_minor": 1_000_000,
            "currency": "MMK",
            "confidence": 1,
        }
        self.commerce.submit_receipt(
            101,
            order.order_id,
            "manual",
            "file-high",
            "uniq-high",
            b"receipt-high",
            "image/jpeg",
            extraction,
            self.now,
        )
        with self.assertRaises(CommerceError):
            self.commerce.approve_order(order.order_id, 999, self.now)
        self.assertEqual(self.commerce.wallet_balance(101), 0)

    def test_wallet_payment_reserves_and_rejection_releases_funds(self):
        self.commerce.credit_wallet(101, 3000, "deposit-1", 999)
        order = self.commerce.create_order(101, "A", "basic_50gb", self.now)
        self.assertEqual(
            self.commerce.pay_order_with_wallet(101, order.order_id, self.now), "reserved"
        )
        self.assertEqual(self.commerce.wallet_balance(101), 0)
        self.commerce.reject_order(order.order_id, 999, self.now)
        self.assertEqual(self.commerce.wallet_balance(101), 3000)

    def test_unverified_text_payment_cannot_be_approved_in_public_mode(self):
        strict = CommerceService(CommerceDatabase(self.path), self.outline, Fernet.generate_key())
        strict.initialize()
        order = strict.create_order(202, "Strict", "basic_50gb", self.now)
        strict.submit_payment(202, order.order_id, "manual", "TEXT-ONLY", self.now)
        with self.assertRaises(CommerceError):
            strict.approve_order(order.order_id, 999, self.now)

    def test_receipt_without_transaction_moves_order_to_review_pending(self):
        order = self.commerce.create_order(303, "Review", "basic_50gb", self.now)
        evidence = self.commerce.submit_receipt(
            303,
            order.order_id,
            "manual",
            "file-review",
            "uniq-review",
            b"review-receipt",
            "image/jpeg",
            None,
            self.now,
        )
        detail = self.commerce.order_detail(order.order_id, 303)
        self.assertEqual(detail["status"], "payment_submitted")
        self.assertEqual(detail["stage"], "review_pending")
        self.assertEqual(
            self.commerce.reject_receipt(evidence["evidence_id"], 999, now=self.now), order.order_id
        )
        self.assertEqual(
            self.commerce.order_detail(order.order_id, 303)["stage"], "awaiting_payment"
        )

    def test_wallet_capture_does_not_double_deduct_reserved_balance(self):
        self.commerce.credit_wallet(404, 3000, "deposit-capture", 999)
        order = self.commerce.create_order(404, "Wallet", "basic_50gb", self.now)
        self.assertEqual(
            self.commerce.pay_order_with_wallet(404, order.order_id, self.now), "reserved"
        )
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
            self.commerce.release_expired_wallet_reservations(self.now + timedelta(hours=24)),
            1,
        )
        self.assertEqual(self.commerce.wallet_balance(405), 3000)
        self.assertEqual(self.commerce.order_detail(order.order_id, 405)["stage"], "cancelled")

    def test_untouched_orders_expire_and_can_be_replaced(self):
        order = self.commerce.create_order(505, "Expired", "basic_50gb", self.now)
        closed = self.commerce.expire_open_orders(self.now + timedelta(hours=24))
        self.assertEqual(closed, 1)
        self.assertEqual(self.commerce.order_detail(order.order_id, 505)["stage"], "cancelled")
        replacement = self.commerce.create_order(
            505, "Expired", "standard_100gb", self.now + timedelta(hours=24, minutes=1)
        )
        self.assertNotEqual(replacement.order_id, order.order_id)

    def test_quota_hit_revokes_paid_key_once(self):
        order = self.commerce.create_order(101, "A", "basic_50gb", self.now)
        self.commerce.submit_payment(101, order.order_id, "manual", "TX-2", self.now)
        approval = self.commerce.approve_order(order.order_id, 999, self.now)
        self.commerce.process_jobs(self.now)
        self.outline.transfer["1"] = 50_000_000_000
        self.assertEqual(self.commerce.enforce_quotas(self.now), 1)
        self.assertEqual(self.commerce.enforce_quotas(self.now), 0)
        self.commerce.process_jobs(self.now)
        self.assertEqual(self.outline.deleted, ["1"])
        self.assertEqual(self.commerce.user_vpn(101)["access_url"], None)
        self.assertEqual(approval.order_id, order.order_id)

    def test_paid_quota_warning_is_queued_before_hard_stop(self):
        order = self.commerce.create_order(101, "A", "basic_50gb", self.now)
        self.commerce.submit_payment(101, order.order_id, "manual", "TX-WARN", self.now)
        self.commerce.approve_order(order.order_id, 999, self.now)
        self.commerce.process_jobs(self.now)
        self.outline.transfer["1"] = int(50_000_000_000 * 0.8)

        self.assertEqual(self.commerce.enforce_quotas(self.now), 0)
        pending = self.commerce.pending_notifications(self.now)
        warnings = [item for item in pending if item["kind"] == "quota_warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn(":v1:", warnings[0]["dedupe_key"])
        self.assertIn("trailing-30-day", warnings[0]["text"])

        self.commerce.enforce_quotas(self.now)
        self.assertEqual(
            len(
                [
                    item
                    for item in self.commerce.pending_notifications(self.now)
                    if item["kind"] == "quota_warning"
                ]
            ),
            1,
        )

    def test_llm_requires_explicit_configuration_and_validates_shape(self):
        with self.assertRaises(ReceiptLLMUnavailable):
            OpenAICompatibleReceiptExtractor().extract(b"x")
        parsed = validate_extraction(
            {"transaction_id": "TX", "confidence": 1, "flags": [], "notes": []}
        )
        self.assertEqual(parsed.transaction_id, "TX")

    def test_llm_explicitly_disables_streaming_for_openai_compatible_gateways(self):
        response = io.BytesIO(
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "provider": None,
                                        "transaction_id": None,
                                        "amount_minor": None,
                                        "currency": None,
                                        "timestamp": None,
                                        "recipient": None,
                                        "confidence": 0,
                                        "flags": ["unreadable"],
                                        "notes": ["synthetic test"],
                                    }
                                )
                            }
                        }
                    ]
                }
            ).encode("utf-8")
        )
        response.status = 200
        response.headers = {}
        extractor = OpenAICompatibleReceiptExtractor(
            base_url="https://gateway.example/v1",
            model="vision-model",
            api_key="test-only",
        )

        with patch("receipt_llm.urllib.request.urlopen", return_value=response) as urlopen:
            extractor.extract(b"synthetic-image", "image/png")

        request = urlopen.call_args.args[0]
        self.assertIs(json.loads(request.data)["stream"], False)

    def test_receipt_image_normalization_bounds_the_llm_copy(self):
        source = io.BytesIO()
        Image.new("RGBA", (1800, 1200), (255, 255, 255, 128)).save(source, "PNG")

        normalized, mime_type = normalize_receipt_image(source.getvalue(), "image/png")

        self.assertEqual(mime_type, "image/jpeg")
        with Image.open(io.BytesIO(normalized)) as image:
            self.assertLessEqual(max(image.size), 1100)
            self.assertEqual(image.mode, "RGB")

    def test_receipt_fallback_uses_second_route_only_for_incomplete_primary(self):
        class StubExtractor:
            base_url = "https://gateway.example/v1"
            api_key = "test-only"

            def __init__(self, model, extraction):
                self.model = model
                self.extraction = extraction
                self.calls = 0

            def extract_with_diagnostics(self, _image, _mime):
                self.calls += 1
                if isinstance(self.extraction, Exception):
                    raise self.extraction
                return self.extraction, {"model": self.model, "duration_ms": 10}

        incomplete = ReceiptExtraction("WavePay", None, 150000, "MMK", None, None, 0.5, (), ())
        complete = ReceiptExtraction(
            "WavePay", "564201837", 150000, "MMK", None, "Theingi Wint Aung", 0.93, (), ()
        )
        primary = StubExtractor("primary", incomplete)
        fallback = StubExtractor("fallback", complete)
        chain = FallbackReceiptExtractor([primary, fallback])
        image = io.BytesIO()
        Image.new("RGB", (20, 20), "white").save(image, "PNG")

        result, diagnostics = chain.extract_with_diagnostics(image.getvalue(), "image/png")

        self.assertEqual(result.transaction_id, "564201837")
        self.assertEqual(diagnostics["selected_model"], "fallback")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)

    def test_receipt_fallback_accepts_qr_negative_without_second_call(self):
        class StubExtractor:
            base_url = "https://gateway.example/v1"
            api_key = "test-only"

            def __init__(self, model, extraction):
                self.model = model
                self.extraction = extraction
                self.calls = 0

            def extract_with_diagnostics(self, _image, _mime):
                self.calls += 1
                return self.extraction, {"model": self.model, "duration_ms": 10}

        negative = ReceiptExtraction(
            "KBZPay", None, None, None, None, "Merchant", 0.0, ("not_a_receipt",), ()
        )
        primary = StubExtractor("primary", negative)
        fallback = StubExtractor(
            "fallback", ReceiptExtraction(None, None, None, None, None, None, 0, (), ())
        )
        chain = FallbackReceiptExtractor([primary, fallback])
        image = io.BytesIO()
        Image.new("RGB", (20, 20), "white").save(image, "PNG")

        result = chain.extract(image.getvalue(), "image/png")

        self.assertIsNone(result.transaction_id)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 0)

    def test_assisted_receipt_extraction_job_is_durable_and_never_approves(self):
        order = self.commerce.create_order(777, "Queued", "basic_50gb", self.now)
        result = self.commerce.submit_receipt(
            777,
            order.order_id,
            "kbzpay",
            "telegram-file",
            "unique-file",
            b"receipt-bytes",
            "image/jpeg",
            queue_extraction=True,
            now=self.now,
        )
        job = self.commerce.claim_receipt_extraction_job(self.now)
        self.assertEqual(job["evidence_id"], result["evidence_id"])

        self.commerce.finish_receipt_extraction(
            job["job_id"],
            job["evidence_id"],
            {
                "provider": "KBZPay",
                "transaction_id": "TX-QUEUED",
                "amount_minor": 3000,
                "currency": "MMK",
                "confidence": 0.95,
                "flags": [],
                "notes": [],
            },
            {"selected_model": "vision-primary"},
            self.now,
        )

        receipt = self.commerce.get_receipt(result["evidence_id"])
        self.assertEqual(receipt["extraction"]["transaction_id"], "TX-QUEUED")
        self.assertEqual(receipt["review_status"], "pending")
        self.assertEqual(
            self.commerce.order_detail(order.order_id, 777)["status"], "payment_submitted"
        )

    def test_receipt_policy_defaults_manual_and_assisted_never_approves(self):
        self.assertEqual(self.commerce.receipt_policy()["mode"], "manual")
        changed = self.commerce.set_receipt_mode("assisted", 999)
        self.assertEqual(changed["mode"], "assisted")
        with self.assertRaisesRegex(CommerceError, "authoritative payment verifier"):
            self.commerce.set_receipt_mode("automatic", 999)

    def test_receipt_diagnostic_is_isolated_from_financial_state(self):
        run_id = self.commerce.start_receipt_diagnostic(999)
        result = self.commerce.finish_receipt_diagnostic(
            run_id,
            999,
            "passed",
            {"summary": "synthetic diagnostic", "transaction_id": "TEST-ONLY"},
        )
        self.assertEqual(result["status"], "passed")
        with self.commerce.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 0)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0], 0
            )

    def test_outline_quota_api_helpers_are_available(self):
        from app import OutlineClient

        client = OutlineClient.__new__(OutlineClient)
        calls = []
        client._request = lambda method, path, body=None, accepted_statuses=(
            200,
            201,
            204,
        ): calls.append((method, path, body)) or {"id": "a", "accessUrl": "ss://x"}
        self.assertEqual(client.create_key_with_id("a/b", "n", 10)["id"], "a")
        client.delete_data_limit("a/b")
        client.rename_key("a/b", "renamed")
        self.assertEqual(calls[0][1], "/access-keys/a%2Fb")
        self.assertEqual(calls[1][1], "/access-keys/a%2Fb/data-limit")
        self.assertEqual(calls[2][1], "/access-keys/a%2Fb/name")


if __name__ == "__main__":
    unittest.main()
