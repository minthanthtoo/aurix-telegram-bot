from __future__ import annotations

from datetime import datetime, timedelta, timezone

import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from aurix_games.pricing import PricingPolicy
from aurix_games.public_catalog import PublicCatalogStore, build_supplier_discovery
from scripts import refresh_fazercards_public_catalog as refresh_module
from scripts import build_fazercards_public_catalog as builder_module
from scripts import refresh_game_store_catalog as scheduled
from aurix_games.pricing_store import PricingPolicyStore
import fcntl
import os


class FazerCardsCatalogRefreshTests(unittest.TestCase):
    def test_scheduled_refresh_fences_workers_and_keeps_unique_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(timezone.utc)
            config = root / "public-catalog-config.json"
            PricingPolicyStore(config).save(
                PricingPolicy(), approved_by="staff:test", fx_source="fixture",
                fx_observed_at=now.isoformat(), valid_until=(now + timedelta(hours=1)).isoformat(),
            )
            shortlist = root / "pilot-shortlist.json"
            shortlist.write_text("[]"); shortlist.chmod(0o600)
            output = root / "public-catalog.json"
            supplier_discovery = root / "supplier-index.json"
            output.write_text("previous")
            with patch.dict(os.environ, {
                "AURIX_GAMES_BASE_DIR": str(root), "FAZERCARDS_PUBLIC_CATALOG_CONFIG": str(config),
                "AURIX_GAMES_PUBLIC_CATALOG": str(output), "AURIX_GAMES_CATALOG_SHORTLIST": str(shortlist),
                "AURIX_GAMES_SUPPLIER_DISCOVERY": str(supplier_discovery), "FAZERCARDS_API_KEY": "fixture",
            }), patch.object(scheduled, "_refresh_supplier_discovery", return_value={
                "status": "current", "count": 2, "updated_at": now.isoformat(),
            }) as discovery_refresh, patch.object(
                scheduled, "refresh_shortlist", return_value={"offer_count": 2, "selected_count": 3}
            ) as refresh:
                with (root / "catalog-refresh.lock").open("w") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    self.assertEqual(scheduled.refresh_from_environment(), {"status": "already_running"})
                    refresh.assert_not_called()
                self.assertEqual(scheduled.refresh_from_environment()["status"], "published")
                discovery_refresh.assert_called_once()
                self.assertEqual(discovery_refresh.call_args.args[0], supplier_discovery)
                first = refresh.call_args.kwargs["snapshot_path"]
                scheduled.refresh_from_environment()
                self.assertNotEqual(first, refresh.call_args.kwargs["snapshot_path"])
                refresh.side_effect = ValueError("fixture failure")
                self.assertEqual(scheduled.main(), 1)
                self.assertEqual(output.read_text(), "previous")
                refresh.reset_mock()
                config.unlink()
                self.assertEqual(scheduled.main(), 1)
                refresh.assert_not_called()

    def test_supplier_discovery_refresh_is_cached_private_and_names_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            path = root / "supplier-discovery.json"
            now = datetime.now(timezone.utc)
            categories = [
                {"category_id": "private-a", "name": "Mobile Legends", "stock": 5},
                {"category_id": "private-b", "name": "PUBG Mobile"},
            ]
            with patch.object(scheduled, "FazerCardsReadOnlyClient") as client, patch.object(
                scheduled, "_list_cursor_items", return_value=(categories, {"total": 2})
            ) as list_items:
                result = scheduled._refresh_supplier_discovery(path, api_key="fixture-secret", now=now)
                self.assertEqual(result["status"], "published")
                self.assertEqual(result["count"], 2)
                client.assert_called_once_with("fixture-secret", min_interval_seconds=1.0)
                list_items.assert_called_once_with(
                    client.return_value, "/topups", item_id_key="category_id"
                )
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                saved = path.read_text(encoding="utf-8")
                self.assertNotIn("private-a", saved)
                self.assertNotIn("fixture-secret", saved)
                self.assertNotIn("stock", saved)
                cached = scheduled._refresh_supplier_discovery(
                    path, api_key="fixture-secret", now=now + timedelta(hours=12)
                )
                self.assertEqual(cached["status"], "current")
                client.assert_called_once()

    def test_failed_supplier_discovery_read_preserves_previous_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            path = root / "supplier-discovery.json"
            now = datetime.now(timezone.utc)
            artifact = build_supplier_discovery(
                [{"category_id": "private-a", "name": "Mobile Legends"}],
                generated_at=now - timedelta(hours=25),
            )
            path.write_text(json.dumps(artifact), encoding="utf-8")
            path.chmod(0o600)
            original = path.read_bytes()
            with patch.object(scheduled, "_list_cursor_items", side_effect=RuntimeError("upstream down")):
                with self.assertRaisesRegex(RuntimeError, "upstream down"):
                    scheduled._refresh_supplier_discovery(path, api_key="fixture-secret", now=now)
            self.assertEqual(path.read_bytes(), original)
            stale = scheduled._supplier_discovery_status(path, now=now)
            self.assertEqual(stale["status"], "stale")

    def test_invalid_full_refresh_paths_or_freshness_stop_before_supplier_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, output = root / "config.json", root / "public.json"
            config.write_text("{}")
            output.write_text("previous-approved")
            alias = root / "alias.json"
            alias.symlink_to(output)
            for snapshot, minutes, error in (
                (config, 15, "separate files"),
                (alias, 15, "separate files"),
                (root / "snapshot.json", 16, "freshness"),
            ):
                with self.subTest(snapshot=snapshot.name, minutes=minutes):
                    with patch.object(refresh_module, "FazerCardsReadOnlyClient") as client:
                        with self.assertRaisesRegex(ValueError, error):
                            refresh_module.refresh(
                                snapshot_path=snapshot, config_path=config, output_path=output,
                                api_key="fixture", resume=True, min_interval=1, max_age_minutes=minutes,
                            )
                    client.assert_not_called()
            self.assertEqual(config.read_text(), "{}")
            self.assertEqual(output.read_text(), "previous-approved")

    def test_refresh_checkpoints_inventory_and_builds_without_persisting_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "catalog.json"
            config = root / "config.json"
            output = root / "public.json"
            config.write_text("{}", encoding="utf-8")
            captured: dict[str, object] = {}

            def fake_client(api_key: str, **kwargs: object) -> object:
                captured["api_key"] = api_key
                captured["client_kwargs"] = kwargs
                return object()

            payload = {
                "snapshot_version": 1,
                "captured_at": "2026-09-22T03:00:00+00:00",
                "status": "partial",
                "coverage": {"complete": False},
                "families": {
                    "topups": {"details_complete": True},
                    "giftcards": {"details_complete": True},
                    "gamekeys": {"details_complete": True},
                },
            }

            def fake_collect(client: object, *, initial: object, checkpoint: object) -> dict[str, object]:
                self.assertIsNone(initial)
                self.assertIsNotNone(client)
                checkpoint(payload)
                return payload

            def fake_build(arguments: list[str]) -> int:
                captured["build_args"] = arguments
                output.write_text("approved", encoding="utf-8")
                return 0

            with patch.object(refresh_module, "FazerCardsReadOnlyClient", side_effect=fake_client), patch.object(
                refresh_module, "collect_inventory", side_effect=fake_collect
            ), patch.object(refresh_module, "build_public_catalog", side_effect=fake_build):
                result = refresh_module.refresh(
                    snapshot_path=snapshot,
                    config_path=config,
                    output_path=output,
                    api_key="rotated-secret",
                    resume=False,
                    min_interval=0.7,
                    max_age_minutes=15,
                )

            self.assertEqual(captured["api_key"], "rotated-secret")
            self.assertEqual(captured["client_kwargs"], {"min_interval_seconds": 0.7})
            self.assertEqual(result["inventory_status"], "partial")
            self.assertFalse(result["inventory_coverage_complete"])
            saved = json.loads(snapshot.read_text(encoding="utf-8"))
            self.assertNotIn("rotated-secret", snapshot.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "partial")
            self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
            self.assertEqual(output.read_text(encoding="utf-8"), "approved")

    def test_key_stdin_is_read_once_and_missing_key_fails_without_network(self) -> None:
        with patch.object(refresh_module.sys, "stdin", io.StringIO("one-time-key\n")):
            self.assertEqual(refresh_module._read_api_key(key_stdin=True), "one-time-key")
        with patch.dict(refresh_module.os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "provide FAZERCARDS_API_KEY"):
                refresh_module._read_api_key(key_stdin=False)

    def test_existing_snapshot_requires_explicit_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "pass --resume"):
                refresh_module._load_resume(path, resume=False)

    def test_incomplete_core_refresh_preserves_previous_public_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "catalog.json"
            output = root / "public.json"
            output.write_text("previous-approved", encoding="utf-8")
            payload = {
                "snapshot_version": 1,
                "captured_at": "2026-09-22T03:00:00+00:00",
                "status": "partial",
                "coverage": {"complete": False},
                "families": {"topups": {"details_complete": False}},
            }

            with patch.object(refresh_module, "FazerCardsReadOnlyClient", return_value=object()), patch.object(
                refresh_module, "collect_inventory", return_value=payload
            ), patch.object(refresh_module, "build_public_catalog") as build:
                with self.assertRaisesRegex(RuntimeError, "core catalog refresh is incomplete"):
                    refresh_module.refresh(
                        snapshot_path=snapshot,
                        config_path=root / "config.json",
                        output_path=output,
                        api_key="rotated-secret",
                        resume=False,
                        min_interval=1.0,
                        max_age_minutes=15,
                    )
            build.assert_not_called()
            self.assertEqual(output.read_text(encoding="utf-8"), "previous-approved")


class ShortlistRefreshTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.shortlist, self.config = root / "shortlist.json", root / "config.json"
        self.evidence, self.output = root / "evidence.json", root / "pilot.json"
        self.rows = [
            {"category_id": "mlbb", "offer_id": sku, "max_cost_usd": "2.00", "allow_unknown_stock": allow}
            for sku, allow in (("confirmed", False), ("unknown", False), ("pilot", True),
                               ("zero", True), ("expensive", True), ("missing", True))
        ]
        self.mappings = [
            {"family": "topups", "provider_category_id": "mlbb", "provider_offer_id": row["offer_id"],
             "offer_id": f"store-{row['offer_id']}", "product_slug": "mobile-legends",
             "product_name": "Mobile Legends", "fulfillment_kind": "direct_topup",
             "denomination_label": "55 Diamonds", "review_state": "approved",
             "customer_visible": True, "supplier_enabled": True, "resale_authorized": True,
             "region_label": "Myanmar",
             "region_rule": {"verified": True, "dimension": "store_country", "allowed_values": ["MM"]}}
            for row in self.rows
        ]

    def run_refresh(self, response, *, valid_for=timedelta(days=1)):
        self.shortlist.write_text(json.dumps(self.rows))
        now = datetime.now(timezone.utc)
        policy = PricingPolicy().as_dict()
        policy["fx_rate"] = "4500"
        self.config.write_text(json.dumps({
            "policy": policy,
            "pricing_policy_approval": {
                "status": "approved", "approved_by": "staff:test",
                "approved_at": now.isoformat(), "fx_source": "test fixture",
                "fx_observed_at": now.isoformat(),
                "valid_until": (now + valid_for).isoformat(),
            },
            "mappings": self.mappings,
        }))
        self.config.chmod(0o600)
        with patch.object(refresh_module, "FazerCardsReadOnlyClient") as client:
            if isinstance(response, Exception) or callable(response):
                client.return_value.get.side_effect = response
            else:
                client.return_value.get.return_value = response
            result = refresh_module.refresh_shortlist(
                shortlist_path=self.shortlist, snapshot_path=self.evidence, config_path=self.config,
                output_path=self.output, api_key="secret-value", min_interval=0.7, max_age_minutes=15,
            )
            client.return_value.get.assert_called_once_with("/topups/offers", category_id="mlbb")
            return result

    def test_only_selected_approved_and_eligible_offers_publish(self):
        result = self.run_refresh({"category_id": "mlbb", "offers": [
            {"offer_id": "confirmed", "stock": 3, "price_usd": "1.00"},
            {"offer_id": "unknown", "price_usd": "1.00"},
            {"offer_id": "pilot", "price_usd": "1.00"},
            {"offer_id": "zero", "stock": 0, "price_usd": "1.00"},
            {"offer_id": "expensive", "stock": 3, "price_usd": "2.01"},
            {"offer_id": "extra", "stock": 3, "price_usd": "0.10"},
        ]})
        self.assertEqual(result["offer_count"], 2)
        evidence = json.loads(self.evidence.read_text())
        statuses = {row["offer_id"]: row["status"] for row in evidence["observations"]}
        self.assertEqual(statuses, {"confirmed": "eligible", "unknown": "unknown_stock",
                                    "pilot": "eligible_unknown_stock", "zero": "out_of_stock",
                                    "expensive": "above_cost_ceiling", "missing": "missing_offer"})
        self.assertEqual(stat.S_IMODE(self.evidence.stat().st_mode), 0o600)
        self.assertNotIn("secret-value", self.evidence.read_text())
        public = json.loads(self.output.read_text())
        self.assertEqual({row["offer_id"] for row in public["offers"]}, {"store-confirmed", "store-pilot"})
        self.assertNotIn("price_usd", self.output.read_text())
        self.assertNotIn("stock", self.output.read_text())
        store = PublicCatalogStore(self.output)
        self.assertIsNotNone(store.offer("store-confirmed"))
        self.assertIsNotNone(store.offer("store-pilot"))
        self.assertIsNone(store.offer("store-unknown"))

    def test_malformed_provider_response_preserves_previous_projection(self):
        self.output.write_text("previous projection")
        with self.assertRaisesRegex(ValueError, "duplicate offer"):
            self.run_refresh({"offers": [{"offer_id": "confirmed"}, {"offer_id": "confirmed"}]})
        self.assertEqual(self.output.read_text(), "previous projection")
        self.assertFalse(self.evidence.exists())

    def test_unapproved_mapping_cannot_refresh(self):
        self.mappings[0]["resale_authorized"] = False
        with self.assertRaisesRegex(ValueError, "approved direct top-up"):
            self.run_refresh({"offers": []})
        self.assertFalse(self.output.exists())

    def test_successful_refresh_removes_previously_available_pack(self):
        self.output.write_text("previous projection")
        result = self.run_refresh({"offers": []})
        self.assertEqual(result["offer_count"], 0)
        self.assertEqual(json.loads(self.output.read_text())["offers"], [])

    def test_slow_read_cannot_reset_source_freshness(self):
        self.output.write_text("previous projection")
        start = datetime(2026, 11, 13, tzinfo=timezone.utc)
        with patch.object(refresh_module, "datetime") as clock:
            clock.now.side_effect = [start, start, start + timedelta(minutes=16)]
            with self.assertRaisesRegex(RuntimeError, "freshness window"):
                self.run_refresh({"offers": []})
        self.assertEqual(self.output.read_text(), "previous projection")
        self.assertFalse(self.evidence.exists())

    def test_supplier_read_error_preserves_previous_projection(self):
        self.output.write_text("previous projection")
        with self.assertRaises(refresh_module.CatalogReadError):
            self.run_refresh(refresh_module.CatalogReadError("simulated upstream failure"))
        self.assertEqual(self.output.read_text(), "previous projection")
        self.assertFalse(self.evidence.exists())

    def test_fx_expiring_during_supplier_fetch_keeps_last_approved_rate(self):
        self.output.write_text('previous projection')
        after_expiry = datetime.now(timezone.utc) + timedelta(minutes=2)
        with patch.object(builder_module, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = after_expiry
            self.run_refresh({'offers': []}, valid_for=timedelta(minutes=1))
        self.assertEqual(json.loads(self.output.read_text())['offer_count'], 0)
        self.assertEqual(PublicCatalogStore(self.output).status()['status'], 'fresh')

    def test_mapping_changed_during_fetch_cannot_publish(self):
        self.output.write_text('previous projection')
        def supplier_response(*args, **kwargs):
            config = json.loads(self.config.read_text())
            config['mappings'][0]['review_state'] = 'paused'
            self.config.write_text(json.dumps(config))
            return {'offers': []}
        with self.assertRaisesRegex(ValueError, 'approval changed'):
            self.run_refresh(supplier_response)
        self.assertEqual(self.output.read_text(), 'previous projection')

    def test_publication_deadline_tracks_stock_not_pricing_reminder_expiry(self):
        self.run_refresh({'offers': []}, valid_for=timedelta(seconds=30))
        payload = json.loads(self.output.read_text())
        self.assertGreater(
            datetime.fromisoformat(payload['valid_until']),
            datetime.fromisoformat(json.loads(self.config.read_text())['pricing_policy_approval']['valid_until']),
        )
        self.assertEqual(PublicCatalogStore(self.output).status()['status'], 'fresh')
