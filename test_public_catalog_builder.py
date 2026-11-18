from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import stat
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import threading

from aurix_games.pricing import PricingPolicy
from scripts.build_fazercards_public_catalog import main, _records
from scripts import build_fazercards_public_catalog as builder
from aurix_games.pricing_store import catalog_config_revision
from aurix_games.public_catalog import PublicCatalogStore


def _approved_config(policy: dict, mappings: list[dict]) -> dict:
    now = datetime.now(timezone.utc)
    values = PricingPolicy().as_dict()
    values.update(policy)
    return {
        "policy": values,
        "pricing_policy_approval": {
            "status": "approved",
            "approved_by": "staff:test",
            "approved_at": now.isoformat(),
            "fx_source": "test fixture",
            "fx_observed_at": now.isoformat(),
            "valid_until": (now + timedelta(days=1)).isoformat(),
        },
        "mappings": mappings,
    }


class PublicCatalogBuilderTests(unittest.TestCase):
    def test_concurrent_publishers_cannot_regress_stock_and_failed_replace_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, output = root / 'config.json', root / 'public.json'
            config = _approved_config({}, [])
            config_path.write_text(json.dumps(config)); config_path.chmod(0o600)
            now = datetime.now(timezone.utc)
            def artifact(seconds):
                stamp = now - timedelta(seconds=seconds)
                return {'schema_version': 1, 'status': 'approved_projection',
                        'generated_at': stamp.isoformat(), 'source_observed_at': stamp.isoformat(),
                        'valid_until': (stamp + timedelta(minutes=15)).isoformat(),
                        'config_revision': catalog_config_revision(config), 'offers': []}
            older, newer = artifact(20), artifact(10)
            barrier = threading.Barrier(2)
            def publish(payload):
                barrier.wait(timeout=5)
                try:
                    builder._write_private_output(output, payload, config_path=config_path)
                    return 'published'
                except ValueError as exc:
                    self.assertIn('newer catalog', str(exc))
                    return 'superseded'
            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(workers.map(publish, (older, newer)))
            self.assertIn('published', results)
            self.assertEqual(json.loads(output.read_text()), newer)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            before = output.read_bytes()
            with patch.object(builder.os, 'replace', side_effect=OSError('interrupted')):
                with self.assertRaisesRegex(SystemExit, 'could not be published'):
                    builder._write_private_output(output, artifact(5), config_path=config_path)
            self.assertEqual(output.read_bytes(), before)
            self.assertEqual(list(root.glob('*.tmp')), [])
            builder._write_private_output(output, artifact(5), config_path=config_path)
            self.assertEqual(json.loads(output.read_text()), artifact(5))
            self.assertEqual(PublicCatalogStore(output).status()['status'], 'fresh')

    def test_builder_deadline_uses_oldest_source_and_keeps_last_approved_fx(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot, config_path, output = (root / name for name in ('snapshot.json', 'config.json', 'public.json'))
            now = datetime.now(timezone.utc)
            config = _approved_config({}, [])
            config_path.write_text(json.dumps(config)); config_path.chmod(0o600)
            observed = now - timedelta(minutes=10)
            snapshot.write_text(json.dumps({'captured_at': observed.isoformat(), 'families': {}}))
            args = ['--snapshot', str(snapshot), '--config', str(config_path), '--output', str(output)]
            main(args)
            payload = json.loads(output.read_text())
            self.assertEqual(payload['valid_until'], (observed + timedelta(minutes=15)).isoformat())
            approved_at = datetime.fromisoformat(config['pricing_policy_approval']['approved_at'])
            config['pricing_policy_approval']['valid_until'] = (approved_at + timedelta(seconds=1)).isoformat()
            config_path.write_text(json.dumps(config))
            main(args)
            retained = json.loads(output.read_text())
            self.assertEqual(retained['valid_until'], (observed + timedelta(minutes=15)).isoformat())
            self.assertEqual(PublicCatalogStore(output).status()['status'], 'fresh')

    def test_records_use_both_category_and_detail_observation_times(self):
        captured = datetime(2026, 9, 1, tzinfo=timezone.utc)
        category_time = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
        detail_time = datetime(2026, 9, 23, 0, 2, tzinfo=timezone.utc)
        snapshot = {
            "families": {
                "topups": {
                    "categories_observed_at": category_time.isoformat(),
                    "details_observed_at_by_id": {"mlbb": detail_time.isoformat()},
                    "details_by_id": {"mlbb": {"offers": [{
                        "offer_id": "d55", "price_usd": "1", "stock": None,
                    }] }},
                }
            }
        }

        record = next(_records(snapshot, captured))

        self.assertEqual(record.observed_at, category_time)
        self.assertFalse(record.available)  # Missing supplier stock stays unknown in the general publisher.

    def test_single_unit_code_sales_require_positive_stock_and_explicit_quantity_limits(self):
        for family, list_key, id_key in (("giftcards", "offers", "card_id"), ("gamekeys", "keys", "key_id")):
            base = {id_key: "sku", "name": "Code", "price_usd": "1", "stock": 2,
                "min_order_quantity": 1, "max_order_quantity": 2}
            for changes, expected in (({}, True), ({"stock": 0, "max_order_quantity": 0}, False),
                    ({"stock": None}, False), ({"min_order_quantity": 2}, False),
                    ({"max_order_quantity": None}, False), ({"min_order_quantity": True}, False)):
                with self.subTest(family=family, changes=changes):
                    snapshot = {"families": {family: {"details_by_id": {"cat": {list_key: [{**base, **changes}]}}}}}
                    record = next(_records(snapshot, datetime.now(timezone.utc)))
                    self.assertEqual(record.available, expected)

    def test_builder_writes_only_customer_safe_priced_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timestamp = datetime.now(timezone.utc).isoformat()
            snapshot = {
                "captured_at": timestamp,
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
            config = _approved_config({
                    "fx_rate": "4500",
                    "rounding_minor": 100,
                    "promo_pct": "0.10",
                    "minimum_profit_pct": "0.10",
                }, [
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
                ])
            snapshot_path = root / "snapshot.json"
            config_path = root / "config.json"
            output_path = root / "public.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            config_path.write_text(json.dumps(config), encoding="utf-8")
            config_path.chmod(0o600)
            self.assertEqual(
                main(["--snapshot", str(snapshot_path), "--config", str(config_path), "--output", str(output_path)]),
                0,
            )
            result = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(result["offer_count"], 1)
            offer = result["offers"][0]
            self.assertEqual(offer["offer_id"], "aurix-mlbb-d55")
            self.assertEqual(offer["retail_amount_minor"], 5000)
            self.assertEqual(offer["promo_discount_minor"], 200)
            self.assertNotIn("supplier_cost_usd", json.dumps(result))
            self.assertNotIn("provider_offer_id", json.dumps(result))
            self.assertEqual(stat.S_IMODE(output_path.stat().st_mode), 0o600)

            # A later provider snapshot drives a fresh retail price under the
            # same operator-owned policy; the builder does not freeze the old
            # price into the mapping.
            snapshot["families"]["topups"]["details_by_id"]["mlbb"]["offers"][0]["price_usd"] = "2"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            self.assertEqual(
                main(["--snapshot", str(snapshot_path), "--config", str(config_path), "--output", str(output_path)]),
                0,
            )
            repriced = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertGreater(
                repriced["offers"][0]["retail_amount_minor"],
                offer["retail_amount_minor"],
            )

    def test_manual_service_sku_can_use_explicit_manual_fulfillment_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timestamp = datetime.now(timezone.utc).isoformat()
            snapshot = {
                "captured_at": timestamp,
                "families": {
                    "topups": {},
                    "giftcards": {},
                    "gamekeys": {},
                    "manual_services": {
                        "items": [{"id": "manual-boost", "name": "Manual Boost", "kind": "game-service", "chat": False, "info": ""}],
                        "offers_by_id": {"manual-boost": {
                            "manual_service_id": "manual-boost",
                            "items": [{"id": "boost-1", "name": "Boost service", "price_usd": "2.50"}],
                        }},
                    },
                },
            }
            config = _approved_config({"fx_rate": "4500", "rounding_minor": 100}, [{
                    "family": "manual_services",
                    "provider_category_id": "manual-boost",
                    "provider_offer_id": "boost-1",
                    "offer_id": "aurix-manual-boost-1",
                    "product_slug": "mobile-legends",
                    "product_name": "Mobile Legends",
                    "fulfillment_kind": "manual_service",
                    "denomination_label": "Boost service",
                    "review_state": "approved",
                    "customer_visible": True,
                    "supplier_enabled": True,
                    "resale_authorized": True,
                    "region_rule": {"verified": True, "allow_all": True},
                    "required_input_labels": ["Player ID"],
                }])
            snapshot_path = root / "snapshot.json"
            config_path = root / "config.json"
            output_path = root / "public.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            config_path.write_text(json.dumps(config), encoding="utf-8")
            config_path.chmod(0o600)
            self.assertEqual(
                main(["--snapshot", str(snapshot_path), "--config", str(config_path), "--output", str(output_path)]),
                0,
            )
            result = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(result["offer_count"], 1)
            self.assertEqual(result["offers"][0]["fulfillment_kind"], "manual_service")
            self.assertEqual(result["offers"][0]["denomination_label"], "Boost service")

    def test_catalog_builder_refuses_missing_approved_policy_and_preserves_previous_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(timezone.utc).isoformat()
            snapshot = root / "snapshot.json"
            config = root / "config.json"
            output = root / "public.json"
            snapshot.write_text(json.dumps({"captured_at": now, "families": {}}), encoding="utf-8")
            config.write_text(json.dumps({"mappings": []}), encoding="utf-8")
            config.chmod(0o600)
            output.write_text("previous-approved", encoding="utf-8")
            with self.assertRaises(SystemExit):
                main(["--snapshot", str(snapshot), "--config", str(config), "--output", str(output)])
            self.assertEqual(output.read_text(encoding="utf-8"), "previous-approved")

    def test_builder_rejects_symlink_output_without_replacing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timestamp = datetime.now(timezone.utc).isoformat()
            snapshot_path = root / "snapshot.json"
            config_path = root / "config.json"
            target_path = root / "target.json"
            output_path = root / "public.json"
            snapshot_path.write_text(
                json.dumps({"captured_at": timestamp, "families": {"topups": {}, "giftcards": {}, "gamekeys": {}}}),
                encoding="utf-8",
            )
            config_path.write_text(json.dumps({"policy": {}, "mappings": []}), encoding="utf-8")
            target_path.write_text("keep", encoding="utf-8")
            output_path.symlink_to(target_path)
            with self.assertRaises(SystemExit):
                main(["--snapshot", str(snapshot_path), "--config", str(config_path), "--output", str(output_path)])
            self.assertEqual(target_path.read_text(encoding="utf-8"), "keep")

    def test_builder_rejects_truthy_string_flags_in_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timestamp = datetime.now(timezone.utc).isoformat()
            snapshot_path = root / "snapshot.json"
            config_path = root / "config.json"
            output_path = root / "public.json"
            snapshot_path.write_text(
                json.dumps({"captured_at": timestamp, "families": {"topups": {}, "giftcards": {}, "gamekeys": {}}}),
                encoding="utf-8",
            )
            config_path.write_text(
                json.dumps({
                    "policy": {},
                    "mappings": [{
                        "family": "topups",
                        "provider_category_id": "game",
                        "provider_offer_id": "offer",
                        "offer_id": "aurix-offer",
                        "product_slug": "game",
                        "product_name": "Game",
                        "fulfillment_kind": "direct_topup",
                        "denomination_label": "Pack",
                        "review_state": "approved",
                        "customer_visible": "false",
                        "supplier_enabled": True,
                        "resale_authorized": True,
                        "region_rule": {"verified": True, "allow_all": True},
                    }],
                }),
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit):
                main(["--snapshot", str(snapshot_path), "--config", str(config_path), "--output", str(output_path)])
            self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
