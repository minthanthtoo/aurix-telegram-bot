from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone

from aurix_games.catalog_inventory import load_catalog_inventory, load_shop2topup_catalog_inventory


class GameCatalogInventoryTests(unittest.TestCase):
    def _snapshot(self) -> dict:
        return {
            "snapshot_version": 1,
            "captured_at": "2026-09-23T00:00:00+00:00",
            "status": "complete",
            "coverage": {},
            "families": {
                "topups": {
                    "categories": [{"category_id": "mlbb", "name": "Mobile Legends", "note": ""}],
                    "details_by_id": {"mlbb": {"category_id": "mlbb", "offers": [{"offer_id": "d55", "name": "55 Diamonds", "price_usd": "1.2300"}], "fields": []}},
                },
                "giftcards": {"categories": [], "details_by_id": {}},
                "gamekeys": {"categories": [], "details_by_id": {}},
                "steam_gifts": {"games": [{"appid": 730, "name": "Counter-Strike 2"}]},
                "telegram_stars": {"kind": "telegram_stars", "price_per_star": "0.015", "min_amount": 50, "max_amount": 10000},
                "telegram_premium": {"kind": "telegram_premium", "plans": [{"months": 3, "price_usd": "12.1999"}]},
                "steam_topup": {"base": "USD", "rates": {"USD": "1"}, "updated_at": "2026-09-23T00:00:00Z"},
                "payment_methods": {"items": [{"code": "usdt", "label": "USDT", "minAmountUsd": 3, "maxAmountUsd": 5000}]},
                "manual_services": {"items": [], "offers_by_id": {}},
            },
        }

    def test_exact_supplier_sku_and_source_metadata_are_loaded_from_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(self._snapshot()), encoding="utf-8")
            path.chmod(0o600)
            payload = load_catalog_inventory(path)

        self.assertEqual(payload["counts"], {"topups": 1})
        self.assertEqual(payload["items"][0]["sku"], "d55")
        self.assertEqual(payload["items"][0]["price_usd"], "1.2300")
        self.assertEqual(payload["items"][0]["category_name"], "Mobile Legends")
        self.assertEqual(payload["auxiliary"]["steam_titles"][0]["app_id"], 730)
        self.assertEqual(payload["auxiliary"]["steam_title_count"], 1)
        self.assertFalse(payload["auxiliary"]["steam_titles_truncated"])
        self.assertEqual(payload["auxiliary"]["telegram_premium"]["plans"][0]["months"], 3)

    def test_large_steam_title_list_is_not_expanded_into_inventory_records_or_response(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            snapshot = self._snapshot()
            games = [{"appid": index + 1, "name": f"Game {index + 1}"} for index in range(250)]
            snapshot["families"]["steam_gifts"].update(
                games=games,
                full_list_complete=True,
                offers_expanded=False,
            )
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            path.chmod(0o600)
            payload = load_catalog_inventory(path)

        self.assertEqual(payload["auxiliary"]["steam_title_count"], 250)
        self.assertEqual(len(payload["auxiliary"]["steam_titles"]), 100)
        self.assertTrue(payload["auxiliary"]["steam_titles_truncated"])
        self.assertTrue(payload["auxiliary"]["steam_full_list_complete"])
        self.assertFalse(payload["auxiliary"]["steam_offers_expanded"])

    def test_manual_service_catalog_rows_keep_exact_ids_cost_and_recipient_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            snapshot = self._snapshot()
            snapshot["families"]["manual_services"] = {
                "items": [{"id": "manual-boost", "name": "Manual Boost", "kind": "game-service", "chat": True, "info": "Requires an account ID."}],
                "offers_by_id": {"manual-boost": {
                    "manual_service_id": "manual-boost",
                    "items": [{
                        "id": "boost-1", "name": "Boost service", "price_usd": "2.5000",
                        "fields": [{"key": "player_id", "label": "Player ID", "type": "text"}],
                    }],
                }},
            }
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            path.chmod(0o600)
            payload = load_catalog_inventory(path)

        self.assertEqual(payload["counts"]["manual_services"], 1)
        manual = next(item for item in payload["items"] if item["family"] == "manual_services")
        self.assertEqual((manual["category_id"], manual["sku"]), ("manual-boost", "boost-1"))
        self.assertEqual(manual["price_usd"], "2.5000")
        self.assertEqual(manual["input_schema"], [{"key": "player_id", "label": "Player ID", "type": "text"}])
        self.assertEqual(
            manual["manual_service"],
            {"kind": "game-service", "chat_enabled": True, "info": "Requires an account ID."},
        )

    def test_world_readable_snapshot_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(self._snapshot()), encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                load_catalog_inventory(path)

    def test_mapping_status_join_explains_missing_and_blocked_rows_without_changing_exact_sku(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / "snapshot.json"
            snapshot = self._snapshot()
            snapshot["captured_at"] = "2026-09-23T00:00:00+00:00"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            snapshot_path.chmod(0o600)
            config_path = root / "mapping.json"
            config_path.write_text(json.dumps({"mappings": [{
                "family": "topups", "provider_category_id": "mlbb", "provider_offer_id": "d55",
                "offer_id": "pending-d55", "review_state": "blocked", "customer_visible": False,
                "supplier_enabled": False, "resale_authorized": False,
                "region_rule": {"verified": False, "allow_all": False},
            }]}), encoding="utf-8")
            config_path.chmod(0o600)
            payload = load_catalog_inventory(
                snapshot_path,
                mapping_path=config_path,
                now=datetime(2026, 9, 23, 0, 5, tzinfo=timezone.utc),
            )

        item = payload["items"][0]
        self.assertEqual(item["sku"], "d55")
        self.assertEqual(item["mapping_status"], "blocked")
        self.assertEqual(item["mapping_gate"], "blocked")
        self.assertEqual(item["freshness_status"], "fresh")
        self.assertEqual(payload["mapping_status_counts"], {"blocked": 1})
        self.assertEqual(payload["mapping_gate_counts"], {"blocked": 1})
        self.assertEqual(payload["projection_status_counts"], {"not_projected": 1})

    def test_stale_mapping_is_visible_as_stale_and_keeps_gate_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps(self._snapshot()), encoding="utf-8")
            snapshot_path.chmod(0o600)
            config_path = root / "mapping.json"
            config_path.write_text(json.dumps({"mappings": [{
                "family": "topups", "provider_category_id": "mlbb", "provider_offer_id": "d55",
                "offer_id": "aurix-mlbb-d55", "review_state": "approved", "customer_visible": True,
                "supplier_enabled": True, "resale_authorized": True,
                "region_rule": {"verified": True, "allow_all": True},
            }]}), encoding="utf-8")
            config_path.chmod(0o600)
            payload = load_catalog_inventory(
                snapshot_path,
                mapping_path=config_path,
                now=datetime(2026, 9, 23, 1, 0, tzinfo=timezone.utc),
            )

        item = payload["items"][0]
        self.assertEqual(item["mapping_status"], "stale")
        self.assertEqual(item["mapping_gate"], "ready")
        self.assertEqual(item["projection_status"], "not_projected")
        self.assertEqual(item["mapping_reasons"], [])

    def test_shop2topup_audit_is_private_provider_scoped_and_route_ready_only_when_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_path = root / "shop2topup.json"
            report = {
                "provider": "shop2topup", "schema_version": 1,
                "captured_at": "2026-11-26T00:00:00+00:00", "complete": True,
                "category_complete_by_id": {"12": True},
                "account": {"enabled": True, "verified": True},
                "requirements_by_category": {"12": [{"field_name": "player_id", "data_type": "text"}]},
                "products": [{"category_id": 12, "category": "Mobile Legends", "item_id": 999,
                    "name": "86 Diamonds", "product_type": "topup", "in_stock": True, "listed_price": "0.95"}],
            }
            audit_path.write_text(json.dumps(report), encoding="utf-8")
            audit_path.chmod(0o600)
            mapping_path = root / "mapping.json"
            mapping_path.write_text(json.dumps({"mappings": [
                {"family": "topups", "provider_category_id": "12", "provider_offer_id": "999",
                 "offer_id": "fazer-offer", "review_state": "blocked", "customer_visible": False,
                 "supplier_enabled": False, "resale_authorized": False,
                 "region_rule": {"verified": False, "allow_all": False}},
                {"supplier_provider": "shop2topup", "family": "topups", "provider_category_id": "12",
                 "provider_offer_id": "999", "offer_id": "shop2topup-offer", "review_state": "approved",
                 "customer_visible": True, "supplier_enabled": True, "resale_authorized": True,
                 "region_rule": {"verified": True, "allow_all": True}},
            ]}), encoding="utf-8")
            mapping_path.chmod(0o600)
            inventory = load_shop2topup_catalog_inventory(
                audit_path, mapping_path=mapping_path,
                now=datetime(2026, 11, 26, 0, 5, tzinfo=timezone.utc),
            )

        item = inventory["items"][0]
        self.assertEqual(item["supplier_provider"], "shop2topup")
        self.assertEqual(item["price_source"], "supplier_catalog_listing")
        self.assertEqual(item["stock_label"], "available")
        self.assertEqual(item["mapping_gate"], "ready")
        self.assertTrue(item["source_category_complete"])
        self.assertEqual(item["mapping_offer_id"], "shop2topup-offer")

    def test_shop2topup_vouchers_are_visible_but_never_approvable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_path = root / "shop2topup.json"
            audit_path.write_text(json.dumps({
                "provider": "shop2topup", "schema_version": 1,
                "captured_at": "2026-11-26T00:00:00+00:00", "complete": True,
                "requirements_by_category": {}, "products": [{"category_id": 21, "category": "Steam",
                    "item_id": 102, "name": "Steam Voucher", "product_type": "voucher",
                    "in_stock": True, "listed_price": "10.00"}],
            }), encoding="utf-8")
            audit_path.chmod(0o600)
            inventory = load_shop2topup_catalog_inventory(
                audit_path, now=datetime(2026, 11, 26, 0, 5, tzinfo=timezone.utc),
            )
        self.assertEqual(inventory["items"][0]["family"], "vouchers")
        self.assertEqual(inventory["items"][0]["mapping_status"], "unsupported")
        self.assertEqual(inventory["items"][0]["mapping_gate"], "unsupported")

    def test_shop2topup_missing_audit_is_reported_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            result = load_shop2topup_catalog_inventory(Path(directory) / "missing.json")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["items"], [])

    def test_fresh_category_observation_overrides_stale_global_snapshot_only_for_that_category(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            snapshot = self._snapshot()
            snapshot["captured_at"] = "2026-09-22T00:00:00+00:00"
            snapshot["families"]["topups"].update(
                categories=[
                    {"category_id": "mlbb", "name": "Mobile Legends", "note": ""},
                    {"category_id": "other", "name": "Other Game", "note": ""},
                ],
                categories_observed_at="2026-09-23T00:00:00+00:00",
                details_observed_at_by_id={"mlbb": "2026-09-23T00:00:00+00:00"},
            )
            snapshot["families"]["topups"]["details_by_id"]["other"] = {
                "category_id": "other",
                "offers": [{"offer_id": "sku2", "name": "Other", "price_usd": "2"}],
            }
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            path.chmod(0o600)
            payload = load_catalog_inventory(
                path, now=datetime(2026, 9, 23, 0, 5, tzinfo=timezone.utc)
            )

        rows = {item["category_id"]: item for item in payload["items"]}
        self.assertEqual(payload["freshness_status"], "stale")
        self.assertEqual(rows["mlbb"]["freshness_status"], "fresh")
        self.assertEqual(rows["mlbb"]["source_observed_at"], "2026-09-23T00:00:00+00:00")
        self.assertEqual(rows["other"]["freshness_status"], "stale")


if __name__ == "__main__":
    unittest.main()
