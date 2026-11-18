from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from aurix_games.eligibility import RegionRule
from aurix_games.live_catalog import (
    OfferMapping,
    SupplierOfferRecord,
    build_priced_offers,
)
from aurix_games.pricing import PricingPolicy


UTC = timezone.utc
NOW = datetime(2026, 9, 22, 2, 0, tzinfo=UTC)


class LiveCatalogTests(unittest.TestCase):
    def test_available_mapped_offer_is_repriced_without_supplier_identifiers(self):
        record = SupplierOfferRecord(
            family="topups",
            provider_category_id="supplier-cat",
            provider_offer_id="supplier-offer",
            display_name="55 Diamonds",
            supplier_cost_usd=Decimal("1"),
            observed_at=NOW,
            available=True,
        )
        mapping = OfferMapping(
            offer_id="aurix-mobile-legends-55",
            product_slug="mobile-legends",
            product_name="Mobile Legends",
            fulfillment_kind="direct_topup",
            denomination_label="55 Diamonds",
            region_rule=RegionRule(allow_all=True, verified=True),
        )
        priced = build_priced_offers(
            [record],
            {("topups", "supplier-cat", "supplier-offer"): mapping},
            policy=PricingPolicy(fx_rate=Decimal("4500")),
            now=NOW,
            max_age=timedelta(minutes=15),
        )
        self.assertEqual(len(priced), 1)
        self.assertEqual(priced[0].public_offer.offer_id, "aurix-mobile-legends-55")
        self.assertEqual(priced[0].public_offer.retail_amount_minor, 5200)
        self.assertNotIn("supplier", priced[0].public_offer.as_dict())

    def test_unmapped_unavailable_and_stale_rows_do_not_publish(self):
        record = SupplierOfferRecord(
            family="topups",
            provider_category_id="supplier-cat",
            provider_offer_id="supplier-offer",
            display_name="55 Diamonds",
            supplier_cost_usd=Decimal("1"),
            observed_at=NOW - timedelta(hours=1),
            available=True,
        )
        mapping = OfferMapping(
            offer_id="aurix-mobile-legends-55",
            product_slug="mobile-legends",
            product_name="Mobile Legends",
            fulfillment_kind="direct_topup",
            denomination_label="55 Diamonds",
            region_rule=RegionRule(allow_all=True, verified=True),
        )
        result = build_priced_offers(
            [record],
            {("topups", "supplier-cat", "supplier-offer"): mapping},
            policy=PricingPolicy(),
            now=NOW,
            max_age=timedelta(minutes=15),
        )
        self.assertEqual(result, ())
        unavailable = record.__class__(**{**record.__dict__, "available": False, "observed_at": NOW})
        self.assertEqual(
            build_priced_offers(
                [unavailable],
                {("topups", "supplier-cat", "supplier-offer"): mapping},
                policy=PricingPolicy(),
                now=NOW,
                max_age=timedelta(minutes=15),
            ),
            (),
        )


if __name__ == "__main__":
    unittest.main()
