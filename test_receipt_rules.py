import unittest
from datetime import datetime, timezone

from receipt_rules import evaluate_receipt_candidate


UTC = timezone.utc
SUBMITTED = datetime(2026, 7, 31, 4, 30, tzinfo=UTC)
PROFILES = {
    "kbzpay": {"names": ("MIN THANT HTOO",), "accounts": ("2716",)},
    "wavepay": {"names": ("Min Thant Htoo",), "accounts": ("09750232716",)},
    "ayapay": {"names": ("MIN THANT HTOO",), "accounts": ("2716",)},
    "uabpay": {"names": ("Min Thant Htoo",), "accounts": ("09750232716",)},
    "cbpay": {"names": ("U MIN THANT HTOO",), "accounts": ()},
}


class ReceiptRulesTest(unittest.TestCase):
    def evaluate(self, extraction, provider="kbzpay", amount=116300):
        return evaluate_receipt_candidate(
            extraction,
            selected_provider=provider,
            expected_amount_minor=amount,
            expected_currency="MMK",
            submitted_at=SUBMITTED,
            recipient_profiles=PROFILES,
        )

    def test_actual_kbz_sample_is_recipient_mismatch(self):
        result = self.evaluate(
            {
                "provider": "KBZPay",
                "completion_status": "completed",
                "transaction_id": "01004229060588467786",
                "transaction_id_label": "Transaction Number",
                "amount_minor": 116300,
                "currency": "MMK",
                "timestamp": "2026-07-31T10:57:59+06:30",
                "recipient": "DAW WIN WIN MAW",
                "recipient_account": "5729",
                "confidence": 0.99,
                "flags": [],
            }
        )
        self.assertEqual(result["automation_decision"], "candidate_reject")
        self.assertIn("recipient_mismatch", result["flags"])

    def test_actual_wave_sample_is_recipient_mismatch(self):
        result = self.evaluate(
            {
                "provider": "WavePay",
                "completion_status": "completed",
                "transaction_id": "564201837",
                "transaction_id_label": "Transaction ID",
                "amount_minor": 170000,
                "currency": "MMK",
                "timestamp": "2026-07-31T10:55:00+06:30",
                "recipient": "Theingi Wint Aung",
                "recipient_account": "09976049067",
                "confidence": 0.98,
                "flags": [],
            },
            provider="wavepay",
            amount=170000,
        )
        self.assertEqual(result["automation_decision"], "candidate_reject")
        self.assertIn("recipient_mismatch", result["flags"])

    def test_aya_recipient_alias_cannot_be_transaction_id(self):
        result = self.evaluate(
            {
                "provider": "AYA Pay",
                "completion_status": "completed",
                "transaction_id": "YAMIN",
                "transaction_id_label": "Transaction Code",
                "amount_minor": 1000,
                "currency": "MMK",
                "recipient": "YAMIN",
                "confidence": 0.99,
                "flags": [],
            },
            provider="ayapay",
            amount=1000,
        )
        self.assertNotEqual(result["automation_decision"], "candidate_pass")
        self.assertIn("ambiguous_transaction_id", result["flags"])

    def test_complete_matching_receipt_is_only_a_candidate_pass(self):
        result = self.evaluate(
            {
                "provider": "KBZ Pay",
                "completion_status": "completed",
                "transaction_id": "001234567890",
                "transaction_id_label": "Transaction No",
                "amount_minor": 116300,
                "currency": "MMK",
                "timestamp": "2026-07-31T10:55:00+06:30",
                "recipient": "U Min Thant Htoo",
                "recipient_account": "******2716",
                "confidence": 0.97,
                "flags": [],
            }
        )
        self.assertEqual(result["automation_decision"], "candidate_pass")

    def test_missing_merchant_profile_fails_closed_to_manual_review(self):
        result = evaluate_receipt_candidate(
            {
                "provider": "CB Pay",
                "completion_status": "completed",
                "transaction_id": "ABC12345",
                "transaction_id_label": "Payment Reference Number",
                "amount_minor": 3000,
                "currency": "MMK",
                "timestamp": "2026-07-31T10:55:00+06:30",
                "recipient": "U Min Thant Htoo",
                "confidence": 0.99,
                "flags": [],
            },
            selected_provider="cbpay",
            expected_amount_minor=3000,
            expected_currency="MMK",
            submitted_at=SUBMITTED,
            recipient_profiles={},
        )
        self.assertEqual(result["automation_decision"], "manual_review")
        self.assertIn("merchant_profile_not_configured", result["flags"])

    def test_suspected_edit_never_becomes_candidate_pass(self):
        result = self.evaluate(
            {
                "provider": "KBZ Pay",
                "completion_status": "completed",
                "transaction_id": "001234567890",
                "transaction_id_label": "Transaction ID",
                "amount_minor": 116300,
                "currency": "MMK",
                "timestamp": "2026-07-31T10:55:00+06:30",
                "recipient": "U Min Thant Htoo",
                "recipient_account": "******2716",
                "confidence": 0.99,
                "flags": ["suspected_edits"],
            }
        )
        self.assertEqual(result["automation_decision"], "manual_review")


if __name__ == "__main__":
    unittest.main()
