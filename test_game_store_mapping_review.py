from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from aurix_games.pricing import PricingPolicy
from scripts.review_game_store_mapping import review_mapping


class GameStoreMappingReviewTests(unittest.TestCase):
    def _snapshot(self) -> dict:
        return {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "families": {
                "topups": {
                    "details_by_id": {
                        "mlbb": {
                            "category_id": "mlbb",
                            "offers": [{"offer_id": "d55", "name": "55 Diamonds", "price_usd": "1", "stock": 1}],
                        }
                    }
                },
                "giftcards": {},
                "gamekeys": {},
            },
        }

    def _config(self) -> dict:
        now = datetime.now(timezone.utc)
        policy = PricingPolicy().as_dict()
        policy.update({
            "fx_rate": "4500",
            "rounding_minor": 100,
            "promo_pct": "0.10",
            "minimum_profit_pct": "0.10",
        })
        return {
            "policy": policy,
            "pricing_policy_approval": {
                "status": "approved",
                "approved_by": "staff:test",
                "approved_at": now.isoformat(),
                "fx_source": "test fixture",
                "fx_observed_at": now.isoformat(),
                "valid_until": (now + timedelta(days=1)).isoformat(),
            },
            "mappings": [
                {
                    "family": "topups",
                    "provider_category_id": "mlbb",
                    "provider_offer_id": "d55",
                    "offer_id": "aurix-mlbb-d55",
                    "product_slug": "mobile-legends",
                    "product_name": "Mobile Legends",
                    "fulfillment_kind": "direct_topup",
                    "denomination_label": "55 Diamonds",
                    "review_state": "approved",
                    "customer_visible": True,
                    "supplier_enabled": True,
                    "resale_authorized": True,
                    "region_rule": {"verified": True, "allow_all": True},
                }
            ],
        }

    def test_missing_mapping_config_is_blocked_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps(self._snapshot()), encoding="utf-8")
            report = review_mapping(snapshot_path=snapshot_path, config_path=root / "missing.json")

        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["reason"], "mapping_config_missing")
        self.assertEqual(report["records"], 1)
        self.assertFalse(report["writes"])

    def test_approved_mapping_is_reported_ready_without_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / "snapshot.json"
            config_path = root / "config.json"
            snapshot_path.write_text(json.dumps(self._snapshot()), encoding="utf-8")
            config_path.write_text(json.dumps(self._config()), encoding="utf-8")
            config_path.chmod(0o600)
            report = review_mapping(snapshot_path=snapshot_path, config_path=config_path)

        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["mapped"], 1)
        self.assertEqual(report["projected"], 1)
        self.assertFalse(report["writes"])
        self.assertFalse(report["supplier_key_used"])

    def test_approved_mapping_stays_blocked_without_explicit_pricing_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / "snapshot.json"
            config_path = root / "config.json"
            snapshot_path.write_text(json.dumps(self._snapshot()), encoding="utf-8")
            config = self._config()
            config.pop("pricing_policy_approval")
            config_path.write_text(json.dumps(config), encoding="utf-8")
            config_path.chmod(0o600)
            report = review_mapping(snapshot_path=snapshot_path, config_path=config_path)

        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["reason"], "pricing_policy_unconfigured")
        self.assertEqual(report["projected"], 0)


if __name__ == "__main__":
    unittest.main()
