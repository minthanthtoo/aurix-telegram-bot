from __future__ import annotations

import json
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aurix_games.strategy_guides import (
    StrategyGuideInputError,
    _mlbb_guide,
    _rankings_freshness,
    _free_fire_patch_status,
    fetch_mlbbdex,
    mlbbdex_url,
    strategy_guide,
    strategy_guides,
    write_mlbb_snapshot,
)
from scripts.refresh_mlbb_strategy import _project_heroes, _project_items


class StrategyGuideTests(unittest.TestCase):
    def test_catalog_contains_mlbb_and_myanmar_localized_guides(self) -> None:
        guides = {guide["slug"]: guide for guide in strategy_guides()}
        self.assertEqual(set(guides), {"mlbb", "pubg-mobile-myanmar", "free-fire-myanmar"})
        self.assertEqual(guides["mlbb"]["source"]["license"], "CC BY-SA")
        self.assertTrue(guides["mlbb"]["source"]["attribution_required"])
        self.assertEqual(
            guides["mlbb"]["source"]["rankings_provider"],
            "Rone Arena community API, as disclosed by MLBBDex",
        )
        self.assertIn("no separate ranking-data license", guides["mlbb"]["source"]["rankings_license_status"])
        self.assertIn("not verified", guides["mlbb"]["source"]["rankings_commercial_use_status"])
        self.assertIn("educational, analytical, and community", guides["mlbb"]["source"]["rankings_commercial_use_status"])
        self.assertEqual(guides["mlbb"]["rankings_freshness"]["status"], "unknown")
        mlbb_sections = {section["id"]: section for section in guides["mlbb"]["sections"]}
        self.assertEqual(mlbb_sections["draft"]["status"], "reference_shell")
        self.assertEqual(mlbb_sections["counters"]["status"], "reference_shell")
        self.assertEqual(mlbb_sections["emblems"]["status"], "reference_shell")
        item_section = next(section for section in guides["mlbb"]["sections"] if section["id"] == "items")
        self.assertEqual(item_section["categories"][0], "All")
        self.assertEqual(guides["pubg-mobile-myanmar"]["locale"], "my-MM")
        self.assertFalse(guides["pubg-mobile-myanmar"]["source"]["official_api_mobile_data"])

    def test_free_fire_guide_includes_dated_official_patch_watch_not_live_meta(self) -> None:
        guide = strategy_guide("free-fire-myanmar")
        assert guide is not None
        self.assertEqual(guide["game"], "Garena Free Fire")
        self.assertEqual(guide["locale"], "my-MM")
        self.assertEqual(guide["status"], "offline_reference")
        self.assertEqual(guide["freshness"], "official_patch_snapshot")
        self.assertTrue(guide["season_specific"])
        self.assertEqual(guide["patch_version"], "OB55")
        self.assertEqual(guide["patch_changes_effective_on"], "2026-10-01")
        self.assertFalse(guide["player_lookup"])
        self.assertFalse(guide["source"]["official_api_mobile_data"])
        self.assertEqual(guide["source"]["patch_notes_published_on"], "2026-09-10")
        self.assertEqual(guide["source"]["patch_notes_url"], "https://ff.garena.com/en/article/1712/")
        self.assertIn("schedule only", guide["source"]["notice"])
        self.assertIn(guide["patch_status"], {"scheduled_unverified", "verification_required"})
        self.assertEqual(guide["patch_verification_required"], guide["patch_status"] != "scheduled_unverified")
        self.assertIn("Craftland creation/editor scripting", guide["source"]["craftland_api_scope"])
        self.assertEqual(
            [section["id"] for section in guide["sections"]],
            ["team-play", "drop-and-rotate", "fight-and-endgame", "myanmar-play-checklist", "review-loop", "patch-watch"],
        )
        self.assertTrue(all(section["cards"] for section in guide["sections"]))
        self.assertIsNone(strategy_guide("free-fire"))

    def test_mlbb_snapshot_exposes_weekly_sync_freshness_and_provenance_split(self) -> None:
        guide = _mlbb_guide(
            {
                "schema_version": 1,
                "captured_at": "2026-09-24T03:38:04Z",
                "rankings_measured_at": "2026-09-21T10:00:42Z",
                "patch": "2.1.90",
                "heroes": [{"slug": "test", "name": "Test"}],
                "items": [],
            },
            today=date(2026, 9, 24),
        )

        self.assertEqual(guide["rankings_freshness"]["status"], "fresh")
        self.assertEqual(guide["rankings_freshness"]["age_days"], 3)
        self.assertEqual(guide["rankings_freshness"]["last_measured_at"], "2026-09-21T10:00:42Z")
        self.assertEqual(guide["rankings_freshness"]["source_reported_measured_date"], "2026-09-21")
        self.assertEqual(
            guide["rankings_freshness"]["last_successful_snapshot_capture_at"],
            "2026-09-24T03:38:04Z",
        )
        self.assertEqual(guide["rankings_freshness"]["freshness_window_days"], 7)
        self.assertEqual(guide["rankings_freshness"]["time_basis"], "UTC calendar date")
        self.assertIn("provider sync-health check", guide["rankings_freshness"]["notice"])

    def test_mlbb_rankings_freshness_is_stale_after_week_and_safe_for_bad_dates(self) -> None:
        stale = _rankings_freshness("2026-09-17T23:59:00Z", today=date(2026, 9, 24))
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["age_days"], 7)

        unknown = _rankings_freshness("not-a-date", today=date(2026, 9, 24))
        self.assertEqual(unknown["status"], "unknown")
        self.assertIsNone(unknown["source_reported_measured_date"])
        self.assertIsNone(_rankings_freshness(None)["source_reported_measured_date"])
        self.assertEqual(
            _rankings_freshness("2026-09-21", today=date(2026, 9, 24))["status"],
            "fresh",
        )
        self.assertEqual(
            _rankings_freshness("2026-09-21T10:00:00", today=date(2026, 9, 24))["status"],
            "unknown",
        )
        self.assertEqual(
            _rankings_freshness("2026-09-25T00:00:00Z", today=date(2026, 9, 24))["status"],
            "future_dated",
        )
        self.assertIsNone(
            _rankings_freshness("2026-09-25T00:00:00Z", today=date(2026, 9, 24))["source_reported_measured_date"]
        )

    def test_free_fire_patch_status_moves_to_verification_after_myanmar_date(self) -> None:
        self.assertEqual(
            _free_fire_patch_status("2026-10-01", today=date(2026, 9, 30)),
            "scheduled_unverified",
        )
        self.assertEqual(
            _free_fire_patch_status("2026-10-01", today=date(2026, 10, 1)),
            "verification_required",
        )
        self.assertEqual(
            _free_fire_patch_status("2026-10-01", today=date(2026, 10, 2)),
            "verification_required",
        )
        self.assertEqual(_free_fire_patch_status("not-a-date", today=date(2026, 9, 30)), "unknown")

    def test_mlbbdex_url_is_allow_listed(self) -> None:
        self.assertEqual(mlbbdex_url("heroes"), "https://mlbbdex.com/api/v1/heroes")
        with self.assertRaises(StrategyGuideInputError):
            mlbbdex_url("../private")

    def test_operator_snapshot_is_loaded_without_player_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mlbb.json"
            write_mlbb_snapshot(
                path,
                heroes=[{
                    "slug": "test", "name": "Test", "roles": ["Mage"],
                    "lanes": ["Mid Lane"], "tier": "S+", "win_rate": 51.25,
                }],
                items=[],
                patch="2.1.90",
                rankings_measured_at="2026-09-21T10:00:00Z",
            )
            guide = strategy_guide("mlbb", mlbb_snapshot_path=path)
        assert guide is not None
        self.assertEqual(guide["freshness"], "operator_snapshot")
        self.assertEqual(guide["sections"][0]["heroes"][0]["slug"], "test")
        self.assertEqual(guide["sections"][0]["heroes"][0], {
            "slug": "test", "name": "Test", "roles": ["Mage"], "lanes": ["Mid Lane"],
        })
        self.assertEqual(guide["rankings_display_status"], "withheld_unverified_terms_and_freshness")
        self.assertEqual(guide["sections"][0]["title"], "Hero Reference")
        self.assertEqual(guide["snapshot"]["patch"], "2.1.90")
        self.assertEqual(guide["snapshot"]["rankings_measured_at"], "2026-09-21T10:00:00Z")
        self.assertFalse(guide["player_lookup"])

    def test_api_projection_merges_rankings_and_preserves_item_details(self) -> None:
        heroes = _project_heroes(
            [{"slug": "test-hero", "name": "Test Hero", "roles": ["Mage"], "lanes": ["Mid"]}],
            [{"slug": "test-hero", "tier": "S+", "winRate": 51.25, "banRate": 12.5, "pickRate": 7.1, "score": 72.4, "lowSample": False}],
        )
        self.assertEqual(heroes[0]["lanes"], ["Mid Lane"])
        self.assertEqual(heroes[0]["tier"], "S+")
        self.assertEqual(heroes[0]["win_rate"], 51.25)
        self.assertFalse(heroes[0]["low_sample"])

        items = _project_items(
            [{"slug": "test-item", "name": "Test Item", "category": "Defense", "price": 123, "bonus": "+10 HP", "passive": "Deter", "recipe": ["Part A"]}]
        )
        self.assertEqual(items[0]["bonus"], "+10 HP")
        self.assertEqual(items[0]["passive"], "Deter")
        self.assertEqual(items[0]["recipe"], ["Part A"])
        self.assertNotIn("artwork", items[0])

    def test_invalid_snapshot_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mlbb.json"
            path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
            with self.assertRaises(StrategyGuideInputError):
                strategy_guide("mlbb", mlbb_snapshot_path=path)

    def test_fetcher_rejects_invalid_timeout_before_network(self) -> None:
        with self.assertRaises(StrategyGuideInputError):
            fetch_mlbbdex("heroes", timeout=0)

    def test_rankings_endpoint_accepts_documented_object_envelope(self) -> None:
        response = unittest.mock.MagicMock()
        response.status = 200
        response.read.return_value = json.dumps(
            {"data": {"heroes": [{"slug": "test"}], "measuredAt": "2026-09-21T10:00:00Z"}}
        ).encode("utf-8")
        response.__enter__.return_value = response
        with patch("aurix_games.strategy_guides.urlopen", return_value=response):
            payload = fetch_mlbbdex("rankings")
        self.assertEqual(payload["data"]["heroes"][0]["slug"], "test")

    def test_patches_endpoint_accepts_documented_list_envelope(self) -> None:
        response = unittest.mock.MagicMock()
        response.status = 200
        response.read.return_value = json.dumps(
            {"data": [{"version": "2.1.90", "title": "Patch Notes 2.1.90"}], "source": {"license": "CC BY-SA"}}
        ).encode("utf-8")
        response.__enter__.return_value = response
        with patch("aurix_games.strategy_guides.urlopen", return_value=response):
            payload = fetch_mlbbdex("patches")
        self.assertEqual(payload["data"][0]["version"], "2.1.90")
