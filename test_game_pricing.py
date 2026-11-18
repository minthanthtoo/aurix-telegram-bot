from __future__ import annotations

from decimal import Decimal
import unittest

from aurix_games.pricing import PricingInputError, PricingPolicy, calculate_price


class GamePricingTests(unittest.TestCase):
    def test_price_includes_fx_fees_marketing_payback_and_profit_floor(self):
        policy = PricingPolicy(
            fx_rate=Decimal("4500"),
            target_profit_pct=Decimal("0.20"),
            minimum_profit_pct=Decimal("0.10"),
            operating_fee_minor=100,
            payment_fee_pct=Decimal("0.02"),
            marketing_pct=Decimal("0.05"),
            payback_pct=Decimal("0.03"),
            promo_pct=Decimal("0.10"),
            rounding_minor=100,
        )
        result = calculate_price("1.00", policy)
        self.assertEqual(result.supplier_cost_minor, 4500)
        self.assertEqual(result.base_price_minor, 6200)
        self.assertEqual(result.final_price_minor, 5700)
        self.assertEqual(result.applied_promo_discount_minor, 500)
        self.assertGreaterEqual(result.profit_minor, 450)
        self.assertEqual(result.as_public_dict()["currency"], "MMK")
        self.assertNotIn("supplier_cost_minor", result.as_public_dict())

    def test_promotion_is_capped_at_minimum_profit(self):
        policy = PricingPolicy(
            fx_rate=Decimal("4000"),
            target_profit_pct=Decimal("0.30"),
            minimum_profit_pct=Decimal("0.10"),
            promo_pct=Decimal("0.50"),
            rounding_minor=100,
        )
        result = calculate_price("1", policy)
        self.assertEqual(result.base_price_minor, 5200)
        self.assertEqual(result.final_price_minor, 4400)
        self.assertEqual(result.applied_promo_discount_minor, 800)
        self.assertGreaterEqual(result.profit_minor, 400)

    def test_invalid_leak_percentages_and_price_cap_fail_closed(self):
        with self.assertRaises(PricingInputError):
            PricingPolicy(marketing_pct=Decimal("0.6"), payback_pct=Decimal("0.4"))
        with self.assertRaises(PricingInputError):
            calculate_price("1", PricingPolicy(maximum_price_minor=100))

    def test_admin_policy_round_trip_uses_decimal_strings(self):
        policy = PricingPolicy(
            fx_rate=Decimal("4532.75"),
            minimum_profit_pct=Decimal("0.05"),
            marketing_pct=Decimal("0.03"),
        )
        serialized = policy.as_dict()
        self.assertIsInstance(serialized["fx_rate"], str)
        loaded = PricingPolicy.from_mapping(serialized)
        self.assertEqual(loaded.fx_rate, Decimal("4532.75"))
        self.assertEqual(loaded.minimum_profit_pct, Decimal("0.05"))
        explicit = dict(serialized)
        explicit["target_profit_minor"] = "125"
        self.assertEqual(PricingPolicy.from_complete_mapping(explicit).target_profit_minor, 125)
        with self.assertRaises(PricingInputError):
            PricingPolicy.from_complete_mapping({"fx_rate": "4600"})
        with self.assertRaises(PricingInputError):
            PricingPolicy.from_mapping({"untrusted": True})


if __name__ == "__main__":
    unittest.main()
