from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aurix_games.catalog import CatalogInputError, catalog_response, list_products, navigation_manifest, product_detail, product_offers
from aurix_games.topup_preview import load_topup_preview


class GameStoreCatalogTests(unittest.TestCase):
    def test_supplier_preview_uses_latest_details_without_enabling_sales(self):
        def capture(stamp, **details):
            return {"captured_at": stamp, "families": {"topups": {"details_by_id": details}}}

        def category(title, *offers, fields=()):
            return {"name": title, "fields": list(fields), "offers": [
                {"offer_id": str(i), "name": label, "price_usd": "999", "stock": stock}
                for i, (label, stock) in enumerate(offers)
            ]}

        with TemporaryDirectory() as directory:
            old, new = (Path(directory) / name for name in ("old.json", "new.json"))
            old.write_text(json.dumps(capture(
                "2026-11-13T00:00:00+00:00",
                pubg=category("PUBG Mobile (Auto)", ("Old pack", None)),
                login=category("Game (Via Login)", ("Private pack", None), fields=[{"key": "password"}]),
                new_title=category("New Game", ("First pack", 0)),
            )))
            new.write_text(json.dumps(capture(
                "2026-11-14T00:00:00+00:00",
                pubg=category("PUBG Mobile (Auto)", ("New pack", None)),
            )))
            preview = load_topup_preview([new, old])
            listing = catalog_response(query="PUBG Mobile", topup_preview=preview)
            self.assertEqual(listing["products"][0]["preview_package_count"], 1)
            self.assertFalse(listing["products"][0]["purchase_enabled"])
            self.assertNotIn("regions", listing["products"][0])
            detail = product_detail("pubg-mobile", topup_preview=preview)
            self.assertEqual(detail["offers"], [])
            self.assertIn("read-only supplier snapshot", detail["notice"].lower())
            self.assertNotIn("under review", detail["notice"].lower())
            self.assertEqual(detail["supplier_preview"]["regions"][0]["offers"], [
                {"id": "preview-1", "name": "New pack", "stock_label": "Quantity unreported"},
            ])
            self.assertNotIn("price_usd", json.dumps(detail))
            self.assertNotIn("game", preview)
            self.assertEqual(product_offers("new-game", topup_preview=preview)["offers"], [])
            self.assertEqual(preview["new-game"]["regions"][0]["offers"][0]["stock_label"], "No stock recorded")

    def test_catalog_contains_only_preview_items_and_no_offers(self) -> None:
        response = catalog_response()

        self.assertFalse(response["checkout_enabled"])
        self.assertEqual(response["status"], "preview")
        self.assertEqual(response["result_count"], 50)
        self.assertEqual(response["total_count"], 65)
        self.assertEqual(response["next_offset"], 50)
        self.assertEqual(catalog_response(category="mobile-games")["total_count"], 61)
        self.assertTrue(all(item["status"] == "supplier_review" for item in response["products"]))
        self.assertTrue(all(item["purchase_enabled"] is False for item in response["products"]))
        self.assertTrue(all("price" not in item and "supplier_sku" not in item for item in response["products"]))

    def test_filter_by_category_and_case_insensitive_query(self) -> None:
        products = list_products(category="MOBILE-GAMES", query="MOBILE LEGENDS")

        self.assertEqual([item["slug"] for item in products], ["mobile-legends"])

    def test_valid_empty_category_returns_no_products(self) -> None:
        self.assertEqual(list_products(query="not-a-real-game"), [])

    def test_sample_category_overlap_preorder_and_unconfirmed_group_are_retained(self) -> None:
        pc_names = {item["name"] for item in list_products(category="pc-games")}
        gift_card_names = {item["name"] for item in list_products(category="gift-cards")}
        app_names = {item["name"] for item in list_products(category="mobile-apps")}
        redeem_names = {item["name"] for item in list_products(category="redeem-codes")}
        all_products = catalog_response(limit=50, offset=50)
        ecommerce = next(item for item in all_products["products"] if item["name"] == "Ecommerce")
        legends = next(item for item in list_products(query="Mobile Legends") if item["name"] == "Mobile Legends")
        pubg = next(item for item in list_products(query="PUBG Mobile") if item["name"] == "PUBG Mobile")

        self.assertEqual(pc_names, {"Where Winds Meet", "Genshin Impact"})
        self.assertEqual(gift_card_names, {"Bigo Live", "Telegram Premium"})
        self.assertEqual(app_names, {"Commerce", "Bigo Live", "Telegram Premium"})
        self.assertEqual(redeem_names, {"PUBG Mobile"})
        self.assertEqual(legends["reference_badges"], [])
        self.assertFalse(legends["reference_detail_available"])
        self.assertFalse(pubg["reference_detail_available"])
        self.assertFalse(ecommerce["reference_detail_available"])
        self.assertEqual(ecommerce["category"], "Category unconfirmed")
        self.assertEqual(ecommerce["category_ids"], [])

    def test_second_page_and_late_detail_are_reachable(self) -> None:
        page = catalog_response(limit=50, offset=50)
        detail = product_detail("heaven-burns-red")

        self.assertEqual(page["result_count"], 15)
        self.assertEqual(page["total_count"], 65)
        self.assertIsNone(page["next_offset"])
        self.assertEqual(detail["product"]["name"], "Heaven Burns Red")

    def test_unknown_category_query_and_limit_are_rejected(self) -> None:
        with self.assertRaises(CatalogInputError):
            list_products(category="unknown")
        with self.assertRaises(CatalogInputError):
            list_products(query="x" * 81)
        with self.assertRaises(CatalogInputError):
            list_products(limit=51)
        with self.assertRaises(CatalogInputError):
            list_products(offset=-1)

    def test_detail_stays_unavailable_and_rejects_unknown_slug(self) -> None:
        detail = product_detail("mobile-legends")

        self.assertIsNotNone(detail)
        self.assertEqual(detail["offers"], [])
        self.assertEqual(detail["regions"], [])
        self.assertFalse(detail["checkout_enabled"])
        self.assertIsNone(product_detail("unlisted-product"))

    def test_unverified_historical_package_snapshots_are_not_published(self) -> None:
        for slug in ("mobile-legends", "pubg-mobile"):
            detail = product_detail(slug)
            listing = next(item for item in list_products() if item["slug"] == slug)
            self.assertNotIn("reference_snapshot", detail)
            self.assertFalse(listing["reference_detail_available"])
            self.assertEqual(detail["offers"], [])
            self.assertEqual(detail["regions"], [])
            self.assertFalse(detail["checkout_enabled"])
            self.assertNotIn("observed_price", json.dumps(detail))

    def test_attached_reference_surfaces_are_explicit_and_fail_closed(self) -> None:
        items = {item["id"]: item for item in navigation_manifest()["items"]}
        self.assertEqual(items["mbb-hero-guide"]["surface"], "hero_guide")
        self.assertEqual(items["mobile-legends-region-checker"]["surface"], "region_checker")
        self.assertEqual(items["discount-events"]["surface"], "empty_state")
        self.assertEqual(items["discount-events"]["empty_label"], "No Data")
        self.assertEqual(items["update-profile"]["surface"], "customer_profile")
        self.assertEqual(items["myanmar-game-guide"]["surface"], "strategy_guide")
        self.assertEqual(items["myanmar-game-guide"]["status"], "reference_only")
        self.assertEqual(items["myanmar-game-guide"]["guide_slug"], "pubg-mobile-myanmar")
        self.assertEqual(items["leaderboard"]["label"], "Most active buyers")
        self.assertEqual(items["leaderboard"]["status"], "available")
        self.assertIn("Opt-in · delivered top-ups · rolling 30 days", items["leaderboard"]["notice"])
        self.assertIn("no prize or giveaway is active", items["leaderboard"]["notice"])
        for key in ("mbb-hero-guide", "mobile-legends-region-checker", "update-profile"):
            self.assertFalse(items[key].get("purchase_enabled", False))


if __name__ == "__main__":
    unittest.main()
