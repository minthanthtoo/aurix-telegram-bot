import json
import unittest

from scripts.fazercards_reference_match import _title_match, summarize_snapshot
from aurix_games.catalog_matching import candidate_match_method


class FazerCardsReferenceMatchTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "captured_at": "2026-09-21T00:00:00Z",
            "status": "partial",
            "families": {
                "topups": {
                    "categories": [
                        {"category_id": "private-1", "name": "Mobile Legends Global"},
                        {"category_id": "private-2", "name": "Mobile Legends Account"},
                    ],
                    "details_by_id": {
                        "private-1": {
                            "fields": [{"name": "player_id", "required": True}],
                            "offers": [{"offer_id": "private-offer", "price_usd": 1.5}],
                        },
                        "private-2": {
                            "fields": [{"name": "password", "required": True}],
                            "offers": [{"offer_id": "private-offer-2", "price_usd": 2.0}],
                        },
                    },
                },
                "giftcards": {"categories": [], "details_by_id": {}},
                "gamekeys": {"categories": [], "details_by_id": {}},
                "steam_gifts": {
                    "games": [{"appid": 123, "name": "Different Title"}],
                    "catalog_meta": {"total": 900, "truncated": True},
                    "offers_expanded": False,
                },
            },
        }

    def test_reports_candidate_coverage_without_supplier_values(self):
        result = summarize_snapshot(self.snapshot)
        topups = result["candidate_families"]["topups"]
        self.assertEqual(result["reference_product_labels"], 65)
        self.assertGreater(topups["name_matched_product_labels"], 0)
        self.assertGreater(topups["labels_with_nonpassword_marker_schema_items"], 0)
        self.assertGreater(topups["password_schema_categories_with_items"], 0)
        self.assertGreater(topups["field_descriptors_explicitly_required"], 0)
        self.assertEqual(
            result["candidate_families"]["giftcards"][
                "labels_with_nonpassword_marker_schema_items"
            ],
            0,
        )
        rendered = json.dumps(result)
        for private_value in (
            "private-1",
            "private-offer",
            "price_usd",
            "Mobile Legends Global",
        ):
            self.assertNotIn(private_value, rendered)

    def test_retains_truncation_warning_for_steam_gifts(self):
        result = summarize_snapshot(self.snapshot)
        steam = result["steam_gifts"]
        self.assertEqual(steam["titles_examined"], 1)
        self.assertEqual(steam["reported_total"], 900)
        self.assertTrue(steam["truncated"])
        self.assertFalse(steam["per_title_offers_expanded"])

    def test_reports_validation_capability_coverage_only_when_captured(self):
        missing = summarize_snapshot(self.snapshot)["topup_validation_capabilities"]
        self.assertFalse(missing["captured"])
        self.assertIsNone(missing["name_matched_reference_labels"])

        self.snapshot["families"]["topup_validation"] = {
            "items": [
                {
                    "category_id": "private-validation-id",
                    "name": "Mobile Legends validation",
                    "fields": [{"key": "player_id", "type": "text"}],
                }
            ]
        }
        captured = summarize_snapshot(self.snapshot)["topup_validation_capabilities"]
        self.assertTrue(captured["captured"])
        self.assertGreater(captured["name_matched_reference_labels"], 0)
        self.assertNotIn(
            "Mobile Legends validation", json.dumps(captured)
        )

    def test_requires_a_long_nonexact_substring_for_candidate_match(self):
        self.assertFalse(_title_match("RPG", "RPG Arena"))
        self.assertTrue(_title_match("Mobile Legends", "Mobile Legends Global"))
        self.assertEqual(
            candidate_match_method("PUBG Mobile", "PUBG-Mobile"), "exact_title"
        )
        self.assertIsNone(candidate_match_method("", "PUBG Mobile"))

    def test_tolerates_incomplete_snapshot_families(self):
        result = summarize_snapshot({"families": None})
        self.assertEqual(result["candidate_families"]["topups"]["name_matched_product_labels"], 0)
        self.assertEqual(result["steam_gifts"]["titles_examined"], 0)


if __name__ == "__main__":
    unittest.main()
