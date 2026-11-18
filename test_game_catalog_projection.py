from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from aurix_games.catalog_projection import (
    ApprovedOfferInput,
    ProjectionInputError,
    project_public_offers,
)
from aurix_games.eligibility import RegionDimension, RegionRule


UTC = timezone.utc
NOW = datetime(2026, 9, 22, 13, 0, tzinfo=UTC)


def _offer(**changes) -> ApprovedOfferInput:
    values = {
        "offer_id": "aurix-mlbb-asia-55",
        "product_slug": "mobile-legends",
        "product_name": "Mobile Legends",
        "fulfillment_kind": "direct_topup",
        "denomination_label": "55 Diamonds",
        "retail_amount_minor": 455000,
        "currency": "MMK",
        "observed_at": NOW - timedelta(minutes=1),
        "region_rule": RegionRule(
            verified=True,
            dimension=RegionDimension.GAME_SERVER,
            allowed_values=frozenset({"sea"}),
        ),
        "region_label": "SEA server",
        "region_dimension": RegionDimension.GAME_SERVER,
        "required_input_labels": ("Player ID", "Zone ID"),
    }
    values.update(changes)
    return ApprovedOfferInput(**values)


class GameCatalogProjectionTests(unittest.TestCase):
    def test_only_approved_fresh_customer_visible_offer_is_projected(self):
        rows = project_public_offers(
            [
                _offer(),
                _offer(offer_id="blocked", review_state="blocked"),
                _offer(offer_id="hidden", customer_visible=False),
                _offer(offer_id="unverified", resale_authorized=False),
            ],
            now=NOW,
        )
        self.assertEqual(len(rows), 1)
        public = rows[0].as_dict()
        self.assertEqual(public["offer_id"], "aurix-mlbb-asia-55")
        self.assertEqual(public["region"], {"type": "game_server", "label": "SEA server"})
        self.assertNotIn("supplier", repr(public).lower())
        self.assertNotIn("cost", repr(public).lower())

    def test_global_requires_explicit_verified_allow_all(self):
        row = _offer(
            region_rule=RegionRule(verified=True, allow_all=True),
            region_label=None,
            region_dimension=None,
        )
        projected = project_public_offers([row], now=NOW)
        self.assertEqual(projected[0].region, {"type": "global", "label": "Global"})

        with self.assertRaisesRegex(ProjectionInputError, "region dimension"):
            project_public_offers(
                [
                    _offer(
                        region_rule=RegionRule(verified=True, allow_all=True),
                        region_label=None,
                        region_dimension=RegionDimension.GAME_SERVER,
                    )
                ],
                now=NOW,
            )

    def test_stale_record_is_omitted_before_projection(self):
        self.assertEqual(
            project_public_offers(
                [_offer(observed_at=NOW - timedelta(minutes=16))], now=NOW
            ),
            (),
        )

    def test_approved_regional_record_requires_auriX_region_mapping(self):
        with self.assertRaisesRegex(ProjectionInputError, "missing its AuriX region"):
            project_public_offers(
                [_offer(region_label=None, region_dimension=None)], now=NOW
            )

    def test_supplier_cost_and_provider_id_are_not_accepted_as_public_fields(self):
        row = _offer()
        public = project_public_offers([row], now=NOW)[0].as_dict()
        self.assertEqual(
            set(public),
            {
                "offer_id",
                "product_slug",
                "product_name",
                "fulfillment_kind",
                "denomination_label",
                "retail_amount_minor",
                "currency",
                "region",
                "required_input_labels",
            },
        )

    def test_duplicate_public_ids_are_rejected(self):
        with self.assertRaisesRegex(ProjectionInputError, "duplicate"):
            project_public_offers([_offer(), _offer()], now=NOW)

    def test_unsupported_or_malformed_input_is_rejected(self):
        with self.assertRaisesRegex(ProjectionInputError, "fulfillment kind"):
            project_public_offers([_offer(fulfillment_kind="unknown")], now=NOW)
        with self.assertRaisesRegex(ProjectionInputError, "retail amount"):
            project_public_offers([_offer(retail_amount_minor=True)], now=NOW)
        with self.assertRaisesRegex(ProjectionInputError, "currency"):
            project_public_offers([_offer(currency="mmk")], now=NOW)


if __name__ == "__main__":
    unittest.main()
