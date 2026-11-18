from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aurix_games.sandbox_acceptance import run_sandbox_acceptance


class GameStoreSandboxAcceptanceTests(unittest.TestCase):
    def test_provider_free_acceptance_report_is_complete_and_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            report = run_sandbox_acceptance(root=Path(directory))

        self.assertEqual(report["status"], "passed")
        self.assertFalse(report["external_writes"])
        self.assertFalse(report["supplier_key_used"])
        self.assertFalse(report["real_payment"])
        self.assertFalse(report["checkout_enabled"])
        self.assertEqual(
            [step["name"] for step in report["steps"]],
            [
                "wallet_low_balance",
                "idempotent_supplier_funding_intent",
                "supplier_offer_availability_gate",
                "synthetic_pricing_projection",
                "customer_order_awaiting_payment",
                "synthetic_customer_payment_and_admin_review",
                "supplier_fulfillment_submission",
                "signed_webhook_completion_and_deduplication",
                "delivered_order_leaderboard_opt_in",
            ],
        )


if __name__ == "__main__":
    unittest.main()
