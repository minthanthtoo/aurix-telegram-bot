from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aurix_games.public_catalog import PublicCatalogStore, build_supplier_discovery
from aurix_games.pricing import PricingPolicy
from aurix_games.pricing_store import PricingPolicyStore, catalog_config_revision
from aurix_games.customer_checkout import PublicCatalogOfferResolver


def approve_catalog_fixture(path: Path, *, now: datetime | None = None) -> PricingPolicyStore:
    now = now or datetime.now(timezone.utc)
    policy = PricingPolicyStore(path.parent / 'public-catalog-config.json')
    policy.save(PricingPolicy(fx_rate='4500'), approved_by='staff:fixture',
                fx_source='test-only', fx_observed_at=now.isoformat(),
                valid_until=(now + timedelta(hours=1)).isoformat(), now=now)
    config, _, _ = policy.approved_snapshot(now=now)
    payload = json.loads(path.read_text())
    payload['config_revision'] = catalog_config_revision(config)
    path.write_text(json.dumps(payload))
    return policy


class PublicCatalogTests(unittest.TestCase):
    def test_supplier_titles_show_snapshot_package_names_without_enabling_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(timezone.utc)
            discovery_path = root / "supplier-discovery.json"
            discovery_path.write_text(json.dumps(build_supplier_discovery([
                {"category_id": "supplier-stumble", "name": "Stumble Guys"},
            ], generated_at=now)), encoding="utf-8")
            discovery_path.chmod(0o600)
            preview = {
                "stumble-guys": {
                    "regions": [{
                        "id": "group-1", "name": "Stumble Guys",
                        "observed_at": now.isoformat(),
                        "offers": [{
                            "id": "preview-1", "name": "250 Gems",
                            "stock_label": "Quantity unreported",
                        }],
                    }],
                },
            }
            store = PublicCatalogStore(
                root / "missing-public-catalog.json", now=now,
                discovery_path=discovery_path, topup_preview=preview,
            )

            listing = store.response()
            product = listing["products"][0]
            detail = store.product_detail("stumble-guys")

            self.assertEqual(product["preview_package_count"], 1)
            self.assertFalse(product["purchase_enabled"])
            self.assertEqual(detail["status"], "supplier_review")
            self.assertFalse(detail["purchase_enabled"])
            self.assertEqual(detail["offers"], [])
            self.assertEqual(detail["supplier_preview"]["regions"][0]["offers"][0]["name"], "250 Gems")
            self.assertNotIn("price", json.dumps(detail))
            self.assertNotIn("supplier", json.dumps(detail["supplier_preview"]["regions"][0]["offers"][0]))

    def test_supplier_category_index_is_browse_only_and_contains_no_supplier_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(timezone.utc)
            discovery_path = root / "supplier-discovery.json"
            artifact = build_supplier_discovery([
                {"category_id": "private-mlbb-category", "name": "Mobile Legends", "stock": 123},
                {"category_id": "private-pubg-category", "name": "PUBG Mobile"},
            ], generated_at=now)
            discovery_path.write_text(json.dumps(artifact), encoding="utf-8")
            discovery_path.chmod(0o600)

            store = PublicCatalogStore(root / "missing-public-catalog.json", now=now,
                                       discovery_path=discovery_path)
            payload = store.response()

            self.assertEqual(payload["status"], "supplier_listed")
            self.assertFalse(payload["checkout_enabled"])
            self.assertEqual(payload["total_count"], 2)
            self.assertEqual({row["name"] for row in payload["products"]}, {"Mobile Legends", "PUBG Mobile"})
            for product in payload["products"]:
                self.assertEqual(product["status"], "supplier_review")
                self.assertFalse(product["purchase_enabled"])
                self.assertEqual(product["offers"], [])
            self.assertNotIn("private-mlbb-category", json.dumps(payload))
            self.assertNotIn("stock", json.dumps(payload))
            detail = store.product_detail("mobile-legends")
            self.assertEqual(detail["status"], "supplier_review")
            self.assertEqual(detail["offers"], [])
            self.assertIn("Quantity is unreported", detail["notice"])

    def test_supplier_titles_join_current_listings_without_expanding_purchase_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(timezone.utc)
            catalog_path = root / "public.json"
            discovery_path = root / "supplier-discovery.json"
            catalog_path.write_text(json.dumps({
                "schema_version": 1,
                "status": "approved_projection",
                "generated_at": now.isoformat(),
                "source_observed_at": now.isoformat(),
                "valid_until": (now + timedelta(minutes=10)).isoformat(),
                "offers": [{
                    "offer_id": "mlbb-pack", "product_slug": "mobile-legends",
                    "product_name": "Mobile Legends", "fulfillment_kind": "direct_topup",
                    "denomination_label": "14 Diamonds", "retail_amount_minor": 1300,
                    "currency": "MMK", "region": {"type": "global", "label": "Global"},
                    "required_input_labels": ["Player ID", "Zone ID"],
                }],
            }), encoding="utf-8")
            discovery_path.write_text(json.dumps(build_supplier_discovery([
                {"category_id": "mlbb", "name": "Mobile Legends"},
                {"category_id": "free-fire", "name": "Free Fire"},
            ], generated_at=now)), encoding="utf-8")
            discovery_path.chmod(0o600)

            store = PublicCatalogStore(catalog_path, now=now, discovery_path=discovery_path)
            payload = store.response()
            by_slug = {product["slug"]: product for product in payload["products"]}
            self.assertEqual(payload["total_count"], 2)
            self.assertEqual(set(by_slug), {"mobile-legends", "free-fire"})
            self.assertEqual(len(by_slug["mobile-legends"]["offers"]), 1)
            self.assertTrue(by_slug["mobile-legends"]["live"])
            self.assertEqual(by_slug["free-fire"]["status"], "supplier_review")
            self.assertEqual(by_slug["free-fire"]["offers"], [])
            self.assertEqual(store.response(category="supplier-review")["total_count"], 1)
            self.assertEqual(store.response(category="live-offers")["total_count"], 1)
            self.assertNotIn("private-b", json.dumps(payload))

    def test_status_uses_metadata_and_offers_from_the_same_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'public.json'
            path.touch()
            now = datetime.now(timezone.utc)
            payload = {'schema_version': 1, 'status': 'approved_projection',
                       'generated_at': now.isoformat(), 'source_observed_at': now.isoformat(),
                       'valid_until': (now + timedelta(minutes=5)).isoformat(), 'offers': []}
            legacy = {key: value for key, value in payload.items() if key != 'valid_until'}
            with patch.object(Path, 'read_text', side_effect=[json.dumps(payload), json.dumps(legacy)]) as read:
                status = PublicCatalogStore(path, now=now).status()
            read.assert_called_once()
            self.assertEqual(status['status'], 'fresh')
            self.assertEqual(status['valid_until'], payload['valid_until'])

    def test_deadline_is_exclusive_and_required_on_every_read_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'public.json'
            now = datetime.now(timezone.utc)
            payload = {'schema_version': 1, 'status': 'approved_projection',
                       'generated_at': now.isoformat(), 'source_observed_at': now.isoformat(),
                       'valid_until': (now + timedelta(seconds=5)).isoformat(), 'offers': [{
                           'offer_id': 'pack', 'product_slug': 'game', 'product_name': 'Game',
                           'fulfillment_kind': 'direct_topup', 'denomination_label': '12 coins',
                           'retail_amount_minor': 1000, 'currency': 'MMK',
                           'region': {'type': 'global', 'label': 'Global'},
                           'required_input_labels': ['Player ID'],
                       }]}
            path.write_text(json.dumps(payload))
            fresh = PublicCatalogStore(path, now=now)
            self.assertIsNotNone(fresh.response())
            self.assertIsNotNone(fresh.product_detail('game'))
            self.assertIsNotNone(fresh.product_offers('game'))
            self.assertIsNotNone(PublicCatalogOfferResolver(fresh).resolve('pack'))
            expired = PublicCatalogStore(path, now=now + timedelta(seconds=5))
            self.assertIsNone(expired.response())
            self.assertIsNone(expired.offer('pack'))
            self.assertIsNone(expired.product_detail('game'))
            self.assertIsNone(expired.product_offers('game'))
            self.assertIsNone(PublicCatalogOfferResolver(expired).resolve('pack'))
            self.assertEqual(expired.status()['reason'], 'stale')
            for bad in (None, '', True, 'not-a-date', '2026-11-14T00:00:00', now.isoformat()):
                payload['valid_until'] = bad
                path.write_text(json.dumps(payload))
                self.assertIsNone(PublicCatalogStore(path, now=now).response())

    def test_changed_approval_invalidates_artifact_and_rebuild_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'public.json'
            now = datetime.now(timezone.utc)
            payload = {'schema_version': 1, 'status': 'approved_projection',
                       'generated_at': now.isoformat(), 'source_observed_at': now.isoformat(),
                       'valid_until': (now + timedelta(minutes=10)).isoformat(), 'offers': []}
            path.write_text(json.dumps(payload))
            policy = approve_catalog_fixture(path, now=now)
            store = PublicCatalogStore(path, now=now, pricing_policy_store=policy)
            self.assertIsNotNone(store.response())
            config = json.loads(policy.path.read_text())
            for field, replacement in (('policy', {**config['policy'], 'fx_rate': '5000'}),
                                       ('mappings', [{'review_state': 'paused'}])):
                changed = {**config, field: replacement}
                policy.path.write_text(json.dumps(changed))
                self.assertIsNone(store.response())
                self.assertIsNone(store.offer('any'))
                self.assertEqual(store.status()['status'], 'unavailable')
            payload['config_revision'] = catalog_config_revision(changed)
            path.write_text(json.dumps(payload))
            self.assertIsNotNone(store.response())
            self.assertNotIn('config_revision', store.response())
            policy.path.unlink()
            self.assertIsNone(store.response())

    def test_fresh_stock_uses_last_approved_rate_after_pricing_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "public.json"
            config = root / "public-catalog-config.json"
            now = datetime.now(timezone.utc)
            policy = PricingPolicyStore(config)
            policy.save(
                PricingPolicy(fx_rate="4800"),
                approved_by="staff:fixture",
                fx_source="test-only",
                fx_observed_at=now.isoformat(),
                valid_until=(now + timedelta(seconds=1)).isoformat(),
                now=now,
            )
            config_snapshot, approved_policy, _ = policy.approved_snapshot(now=now)
            path.write_text(json.dumps({
                "schema_version": 1,
                "status": "approved_projection",
                "generated_at": now.isoformat(),
                "source_observed_at": now.isoformat(),
                "valid_until": (now + timedelta(minutes=5)).isoformat(),
                "config_revision": catalog_config_revision(config_snapshot),
                "offers": [{
                    "offer_id": "pack-1",
                    "product_slug": "mobile-legends",
                    "product_name": "Mobile Legends",
                    "fulfillment_kind": "direct_topup",
                    "denomination_label": "14 Diamonds",
                    "retail_amount_minor": 1300,
                    "currency": "MMK",
                    "region": {"type": "global", "label": "Global"},
                    "required_input_labels": ["Player ID", "Zone ID"],
                }],
            }))
            after_expiry = now + timedelta(seconds=2)
            self.assertEqual(policy.status(now=after_expiry)["policy_status"], "expired")
            with self.assertRaisesRegex(ValueError, "current approved pricing policy"):
                policy.approved_snapshot(now=after_expiry)
            _, retained_policy, expiry = policy.approved_snapshot(
                now=after_expiry,
                allow_expired=True,
            )
            self.assertEqual(retained_policy.fx_rate, approved_policy.fx_rate)
            self.assertLess(expiry, after_expiry)

            store = PublicCatalogStore(path, now=after_expiry, pricing_policy_store=policy)
            response = store.response(category="live-offers")
            self.assertEqual(response["products"][0]["offers"][0]["price_minor"], 1300)
            self.assertNotIn("expired", json.dumps(response).casefold())
            self.assertEqual(store.status()["status"], "fresh")

    def test_store_groups_live_offers_and_filters_without_private_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public.json"
            now = datetime.now(timezone.utc)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "approved_projection",
                        "generated_at": now.isoformat(),
                        "source_observed_at": now.isoformat(),
                        "valid_until": (now + timedelta(minutes=15)).isoformat(),
                        "offers": [
                            {
                                "offer_id": "a-1",
                                "product_slug": "game-a",
                                "product_name": "Game A",
                                "fulfillment_kind": "direct_topup",
                                "denomination_label": "100 coins",
                                "retail_amount_minor": 5000,
                                "currency": "MMK",
                                "region": {"type": "global", "label": "Global"},
                                "required_input_labels": ["Player ID"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            response = PublicCatalogStore(path, now=now).response(query="coins", category="live-offers")
            self.assertEqual(response["status"], "live")
            self.assertEqual(response["products"][0]["offers"][0]["price_minor"], 5000)
            self.assertEqual(response["products"][0]["offers"][0]["fulfillment_kind"], "direct_topup")
            resolved = PublicCatalogStore(path, now=now).offer("a-1")
            self.assertEqual(resolved["fulfillment_kind"], "direct_topup")
            detail = PublicCatalogStore(path, now=now).product_detail("game-a")
            self.assertEqual(detail["status"], "live")
            self.assertEqual(detail["product"]["offers"][0]["price_minor"], 5000)
            offers = PublicCatalogStore(path, now=now).product_offers("game-a", region="global")
            self.assertEqual(offers["offers"][0]["offer_id"], "a-1")
            self.assertNotIn("supplier", json.dumps(response))
            self.assertEqual(PublicCatalogStore(path, now=now).response(category="mobile-games")["products"], [])

    def test_store_rejects_stale_or_missing_runtime_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public.json"
            generated = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
            payload = {
                "schema_version": 1,
                "status": "approved_projection",
                "generated_at": generated.isoformat(),
                "source_observed_at": generated.isoformat(),
                "valid_until": (generated + timedelta(minutes=15)).isoformat(),
                "offers": [],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(
                PublicCatalogStore(
                    path,
                    now=generated + timedelta(minutes=16),
                ).response()
            )
            payload.pop("generated_at")
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(PublicCatalogStore(path, now=generated).response())

    def test_missing_file_is_unavailable(self):
        self.assertIsNone(PublicCatalogStore("/tmp/does-not-exist-aurix-public.json").response())

    def test_store_rejects_malformed_recipient_schema_and_region(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public.json"
            now = datetime.now(timezone.utc)
            base = {
                "schema_version": 1,
                "status": "approved_projection",
                "generated_at": now.isoformat(),
                "source_observed_at": now.isoformat(),
                        "valid_until": (now + timedelta(minutes=15)).isoformat(),
                "offers": [{
                    "offer_id": "a-1",
                    "product_slug": "game-a",
                    "product_name": "Game A",
                    "fulfillment_kind": "direct_topup",
                    "denomination_label": "100 coins",
                    "retail_amount_minor": 5000,
                    "currency": "MMK",
                    "region": {"type": "global", "label": "Global"},
                    "required_input_labels": ["Player ID"],
                }],
            }
            for replacement in (
                {"required_input_labels": ["Player ID", {"bad": "value"}]},
                {"required_input_labels": ["Player ID", "Player ID"]},
                {"region": {"type": "global"}},
                {"fulfillment_kind": "unsupported"},
            ):
                payload = json.loads(json.dumps(base))
                payload["offers"][0].update(replacement)
                path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertIsNone(PublicCatalogStore(path, now=now).response())


if __name__ == "__main__":
    unittest.main()
