from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from aurix_games.catalog_repository import (
    CatalogImportError,
    GameCatalogSnapshotDatabase,
    _IMPORT_MIGRATIONS,
)


def _snapshot() -> dict:
    return {
        "snapshot_version": 1,
        "captured_at": "2026-09-21T12:00:00Z",
        "status": "partial",
        "coverage": {"complete": False, "notes": ["steam list truncated"]},
        "families": {
            "topups": {
                "categories": [{"category_id": "private-topup-cat", "name": "Example Game", "note": ""}],
                "details_by_id": {
                    "private-topup-cat": {
                        "category_id": "private-topup-cat",
                        "name": "Example Game",
                        "fields": [{"key": "player_id", "label": "Player ID", "type": "text"}],
                        "offers": [{"offer_id": "private-topup-sku", "name": "100 Coins", "price_usd": "1.2300"}],
                    }
                },
            },
            "giftcards": {
                "categories": [{"category_id": "private-card-cat", "name": "Game Card"}],
                "details_by_id": {
                    "private-card-cat": {
                        "category_id": "private-card-cat",
                        "name": "Game Card",
                        "offers": [{"card_id": "private-card-sku", "name": "$10", "price_usd": "9.5000", "stock": 2, "min_order_quantity": 1, "max_order_quantity": 2}],
                    }
                },
            },
            "gamekeys": {
                "categories": [{"game_id": "private-key-game", "name": "Key Game", "region": "GLOBAL", "platform": "steam", "region_restriction": True}],
                "details_by_id": {
                    "private-key-game": {
                        "game_id": "private-key-game",
                        "GameName": "Key Game",
                        "region": "GLOBAL",
                        "platform": "steam",
                        "region_restriction": True,
                        "region_restrictions": {"countries": ["US"]},
                        "keys": [{"key_id": "private-key-sku", "name": "Standard", "price_usd": "5.0000", "stock": 1}],
                    }
                },
            },
            "manual_services": {
                "items": [{"id": "private-service", "name": "Manual top-up", "kind": "manual_transfer", "chat": True, "info": "Operator handled"}],
                "offers_by_id": {
                    "private-service": {
                        "manual_service_id": "private-service",
                        "category": {"id": "private-service", "name": "Manual top-up", "kind": "manual_transfer", "chat": True},
                        "items": [{"id": "private-manual-sku", "name": "Standard", "price_usd": "3.0000", "delivery_minutes": 60, "fields": [{"key": "player_id", "type": "text"}]}],
                    }
                },
            },
            "steam_gifts": {
                "games": [{"appid": 730, "name": "Steam Game"}],
                "catalog_meta": {"total": 177269, "returned": 1, "truncated": True},
                "offers_expanded": False,
            },
            "steam_topup": {"base": "USD", "rates": {"USD": 1, "RUB": 92.5}},
            "topup_validation": {
                "items": [{"category_id": "validation-game", "name": "Validate Game", "fields": [{"key": "player_id", "type": "text"}]}]
            },
            "full_catalog": {
                "items": [{"sku_id": "private-global-sku", "name": "Example Game 100 Coins", "price_usd": "1.2500"}],
                "catalog_meta": {"total": 1},
                "page_count": 1,
                "complete": True,
            },
        },
    }


class GameCatalogSnapshotDatabaseTests(unittest.TestCase):
    def test_upgrade_preserves_history_and_accepts_zero_limit_only_when_sold_out(self):
        database = GameCatalogSnapshotDatabase(self.database_path)
        with patch("aurix_games.catalog_repository._IMPORT_MIGRATIONS", _IMPORT_MIGRATIONS[:2]):
            original = database.import_snapshot(self.snapshot_path)
        database.initialize()
        database.initialize()
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM game_catalog_offers WHERE import_id=?", (original.import_id,)).fetchone()[0], 4)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        snapshot = _snapshot()
        row = snapshot["families"]["giftcards"]["details_by_id"]["private-card-cat"]["offers"][0]
        row.update(stock=0, max_order_quantity=0)
        self._write_snapshot(snapshot)
        imported = database.import_snapshot(self.snapshot_path)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(connection.execute("SELECT stock,minimum_quantity,maximum_quantity,review_state FROM game_catalog_offers WHERE import_id=? AND family='giftcards'", (imported.import_id,)).fetchone(), (0, 1, 0, "blocked"))
            for stock in (None, 1):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE game_catalog_offers SET stock=? WHERE import_id=? AND family='giftcards'", (stock, imported.import_id))
        row.update(stock=1)
        self._write_snapshot(snapshot)
        with self.assertRaises(CatalogImportError):
            database.import_snapshot(self.snapshot_path)

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.snapshot_path = self.root / "catalog.json"
        self.database_path = self.root / "aurix-game-catalog.sqlite3"
        self._write_snapshot(_snapshot())

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write_snapshot(self, payload: dict) -> None:
        self.snapshot_path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(self.snapshot_path, 0o600)

    def test_imports_private_families_with_blocked_offers_and_safe_summary(self):
        database = GameCatalogSnapshotDatabase(self.database_path)
        summary = database.import_snapshot(self.snapshot_path)
        self.assertEqual(summary.status, "partial")
        self.assertEqual(
            summary.category_counts,
            {"gamekeys": 1, "giftcards": 1, "manual_services": 1, "topups": 1},
        )
        self.assertEqual(
            summary.offer_counts,
            {"gamekeys": 1, "giftcards": 1, "manual_services": 1, "topups": 1},
        )
        self.assertEqual(summary.steam_title_count, 1)
        self.assertEqual(summary.validation_target_count, 1)
        self.assertEqual(summary.full_catalog_item_count, 1)
        public_summary = json.dumps(summary.as_dict())
        for secret in ("private-topup-sku", "private-service", "private-global-sku", "1.2300", "1.2500", "Example Game", "Steam Game"):
            self.assertNotIn(secret, public_summary)

        self.assertEqual(os.stat(self.database_path).st_mode & 0o777, 0o600)
        self.assertEqual(database.current_summary()["review_state"], "blocked")
        with sqlite3.connect(self.database_path) as connection:
            states = connection.execute(
                "SELECT DISTINCT review_state FROM game_catalog_offers"
            ).fetchall()
            self.assertEqual(states, [("blocked",)])
            topup = connection.execute(
                "SELECT supplier_cost_usd, input_schema_json FROM game_catalog_offers WHERE family = 'topups'"
            ).fetchone()
            self.assertEqual(topup[0], "1.2300")
            self.assertIn('"player_id"', topup[1])
            metadata = connection.execute(
                "SELECT payload_json FROM game_catalog_metadata WHERE family = 'steam_gifts'"
            ).fetchone()
            self.assertIn('"truncated":true', metadata[0])
            full_catalog = connection.execute(
                "SELECT payload_json FROM game_catalog_metadata WHERE family = 'full_catalog'"
            ).fetchone()
            self.assertIn('"private-global-sku"', full_catalog[0])
            restriction = connection.execute(
                "SELECT source_metadata_json FROM game_catalog_categories WHERE family = 'gamekeys'"
            ).fetchone()
            self.assertIn('"countries":["US"]', restriction[0])

    def test_import_is_idempotent_and_new_snapshot_becomes_current(self):
        database = GameCatalogSnapshotDatabase(self.database_path)
        first = database.import_snapshot(self.snapshot_path)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("DELETE FROM game_catalog_metadata WHERE family = 'full_catalog'")
        again = database.import_snapshot(self.snapshot_path)
        self.assertEqual(first.import_id, again.import_id)
        self.assertTrue(again.already_imported)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM game_catalog_imports").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM game_catalog_offers").fetchone()[0],
                4,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM game_catalog_metadata WHERE family = 'full_catalog'"
                ).fetchone()[0],
                1,
            )

        changed = _snapshot()
        changed["captured_at"] = "2026-09-22T00:00:00Z"
        self._write_snapshot(changed)
        second = database.import_snapshot(self.snapshot_path)
        self.assertNotEqual(first.import_id, second.import_id)
        self.assertEqual(database.current_summary()["import_id"], second.import_id)

    def test_title_matches_are_persisted_as_blocked_private_candidates(self):
        payload = _snapshot()
        payload["families"]["topups"]["categories"][0]["name"] = "Mobile Legends Global"
        payload["families"]["topups"]["details_by_id"]["private-topup-cat"]["name"] = "Mobile Legends Global"
        self._write_snapshot(payload)
        database = GameCatalogSnapshotDatabase(self.database_path)
        summary = database.import_snapshot(self.snapshot_path)
        self.assertEqual(summary.candidate_counts.get("topups"), 1)
        self.assertEqual(summary.matched_reference_counts.get("topups"), 1)
        self.assertNotIn("Mobile Legends", json.dumps(summary.as_dict()))
        with sqlite3.connect(self.database_path) as connection:
            candidate = connection.execute(
                "SELECT reference_product_slug, source_id, source_name, match_method, review_state "
                "FROM game_catalog_reference_candidates WHERE family = 'topups'"
            ).fetchone()
        self.assertEqual(candidate[0], "mobile-legends")
        self.assertEqual(candidate[1], "private-topup-cat")
        self.assertEqual(candidate[2], "Mobile Legends Global")
        self.assertEqual(candidate[3], "reference_title_substring")
        self.assertEqual(candidate[4], "blocked")

    def test_refuses_permissive_private_snapshot(self):
        os.chmod(self.snapshot_path, 0o644)
        with self.assertRaisesRegex(CatalogImportError, "group/world accessible"):
            GameCatalogSnapshotDatabase(self.database_path).import_snapshot(self.snapshot_path)
        self.assertIsNone(GameCatalogSnapshotDatabase(self.database_path).current_summary())

    def test_invalid_snapshot_rolls_back_without_current_catalog(self):
        payload = _snapshot()
        payload["families"]["topups"]["details_by_id"]["private-topup-cat"]["offers"][0]["price_usd"] = "NaN"
        self._write_snapshot(payload)
        database = GameCatalogSnapshotDatabase(self.database_path)
        with self.assertRaisesRegex(CatalogImportError, "price was malformed"):
            database.import_snapshot(self.snapshot_path)
        self.assertIsNone(database.current_summary())


if __name__ == "__main__":
    unittest.main()
