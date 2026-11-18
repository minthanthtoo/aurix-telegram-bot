from __future__ import annotations

import unittest

from aurix_games.providers.digital_catalogs import FazerCardsDigitalCatalogAdapter
from aurix_games.providers.fazercards import CatalogReadError, FazerCardsReadOnlyClient


class _FakeReadClient:
    def __init__(self, responses: dict[tuple[str, tuple[tuple[str, object], ...]], dict]):
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, path: str, **query: object) -> dict:
        self.calls.append((path, dict(query)))
        key = (path, tuple(sorted(query.items())))
        return self.responses[key]


class FazerCardsDigitalCatalogAdapterTests(unittest.TestCase):
    def test_opt_in_ui_metadata_is_normalized_and_kept_out_of_repr(self):
        cover = "https://api.fzr.cards/api/v2/media/game-key-covers/cat-1.webp"
        client = _FakeReadClient(
            {
                ("/giftcards", (("cursor", None), ("include_ui", True), ("limit", 4))): {
                    "items": [
                        {"category_id": "gift-cat", "name": "Gift", "imageurl": cover}
                    ],
                    "meta": {"total": 1, "next_cursor": None, "has_more": False},
                },
                ("/giftcards/cards", (("category_id", "gift-cat"), ("include_ui", True))): {
                    "category_id": "gift-cat",
                    "name": "Gift",
                    "imageurl": cover,
                    "offers": [],
                },
                ("/gamekeys", (("cursor", None), ("include_ui", True), ("limit", 4))): {
                    "items": [
                        {
                            "game_id": "key-game",
                            "name": "Game",
                            "region": "GLOBAL",
                            "platform": "steam",
                            "region_restriction": False,
                            "appid": 730,
                            "imageurl": cover,
                        }
                    ],
                    "meta": {"total": 1, "next_cursor": None, "has_more": False},
                },
                ("/gamekeys/keys", (("game_id", "key-game"), ("include_ui", True))): {
                    "game_id": "key-game",
                    "GameName": "Game",
                    "region": "GLOBAL",
                    "platform": "steam",
                    "region_restriction": False,
                    "appid": 730,
                    "imageurl": cover,
                    "keys": [],
                },
                ("/manual-services", (("include_ui", True),)): {
                    "items": [
                        {
                            "id": "service",
                            "name": "Manual",
                            "kind": "manual_transfer",
                            "chat": False,
                            "info": "",
                            "imageurl": cover,
                        }
                    ]
                },
                ("/manual-services/service/offers", (("include_ui", True),)): {
                    "manual_service_id": "service",
                    "category": {
                        "id": "service",
                        "name": "Manual",
                        "kind": "manual_transfer",
                        "chat": False,
                    },
                    "items": [],
                    "info": "",
                    "imageurl": cover,
                },
            }
        )
        adapter = FazerCardsDigitalCatalogAdapter(client)
        gift_categories = adapter.list_giftcard_categories(limit=4, include_ui=True)
        gift_detail = adapter.get_giftcard_offers("gift-cat", include_ui=True)
        key_categories = adapter.list_gamekey_categories(limit=4, include_ui=True)
        key_detail = adapter.get_gamekey_offers("key-game", include_ui=True)
        manual_categories = adapter.list_manual_service_categories(include_ui=True)
        manual_detail = adapter.get_manual_service_offers("service", include_ui=True)

        self.assertEqual(gift_categories.items[0].image_url, cover)
        self.assertEqual(gift_detail.image_url, cover)
        self.assertEqual(key_categories.items[0].app_id, 730)
        self.assertEqual(key_categories.items[0].image_url, cover)
        self.assertEqual(key_detail.app_id, 730)
        self.assertEqual(key_detail.image_url, cover)
        self.assertEqual(manual_categories[0].image_url, cover)
        self.assertEqual(manual_detail.image_url, cover)
        for value in (gift_categories, gift_detail, key_categories, key_detail, manual_detail):
            self.assertNotIn(cover, repr(value))
        self.assertEqual(len(client.calls), 6)

    def test_ui_image_metadata_rejects_external_or_markup_values(self):
        client = _FakeReadClient(
            {
                ("/gamekeys", (("cursor", None), ("include_ui", True), ("limit", 50))): {
                    "items": [
                        {
                            "game_id": "key-game",
                            "name": "Game",
                            "region": "GLOBAL",
                            "platform": "steam",
                            "region_restriction": False,
                            "imageurl": "https://untrusted.example/image.webp",
                        }
                    ],
                    "meta": {"total": 1, "next_cursor": None, "has_more": False},
                }
            }
        )
        with self.assertRaisesRegex(CatalogReadError, "host"):
            FazerCardsDigitalCatalogAdapter(client).list_gamekey_categories(include_ui=True)

    def test_include_ui_requires_a_boolean(self):
        adapter = FazerCardsDigitalCatalogAdapter(_FakeReadClient({}))
        with self.assertRaisesRegex(ValueError, "include_ui must be boolean"):
            adapter.list_gamekey_categories(include_ui=1)  # type: ignore[arg-type]

    def test_giftcard_categories_and_cards_are_normalized_and_redacted(self):
        client = _FakeReadClient(
            {
                ("/giftcards", (("cursor", None), ("limit", 10))): {
                    "items": [{"category_id": "private-category", "name": "Steam USD"}],
                    "meta": {"total": 1, "next_cursor": None, "has_more": False},
                },
                ("/giftcards/cards", (("category_id", "private-category"),)): {
                    "category_id": "private-category",
                    "name": "Steam USD",
                    "offers": [
                        {
                            "card_id": "private-sku",
                            "name": "Steam 10",
                            "price_usd": "10.5000",
                            "stock": 7,
                            "min_order_quantity": 1,
                            "max_order_quantity": 4,
                        }
                    ],
                },
            }
        )
        adapter = FazerCardsDigitalCatalogAdapter(client)
        page = adapter.list_giftcard_categories(limit=10)
        catalog = adapter.get_giftcard_offers("private-category")
        self.assertEqual(page.items[0].name, "Steam USD")
        self.assertEqual(page.total, 1)
        self.assertEqual(catalog.offers[0].supplier_cost_usd.as_tuple().exponent, -4)
        self.assertEqual(catalog.offers[0].stock, 7)
        self.assertEqual(catalog.offers[0].maximum_quantity, 4)
        self.assertNotIn("private-category", repr(catalog))
        self.assertNotIn("private-sku", repr(catalog))
        self.assertNotIn("10.5000", repr(catalog))

    def test_gamekey_region_platform_stock_and_restrictions_are_preserved(self):
        client = _FakeReadClient(
            {
                ("/gamekeys", (("cursor", None), ("limit", 50))): {
                    "items": [
                        {
                            "game_id": "private-game-id",
                            "name": "Example Game",
                            "region": "GLOBAL",
                            "platform": "steam",
                            "region_restriction": True,
                        }
                    ],
                    "meta": {"total": 1, "next_cursor": None, "has_more": False},
                },
                ("/gamekeys/keys", (("game_id", "private-game-id"),)): {
                    "game_id": "private-game-id",
                    "GameName": "Example Game",
                    "region": "GLOBAL",
                    "platform": "steam",
                    "region_restriction": True,
                    "keys": [
                        {
                            "key_id": "private-key",
                            "name": "Standard edition",
                            "price_usd": "19.9900",
                            "stock": 5,
                            "min_order_quantity": 1,
                            "max_order_quantity": 5,
                        }
                    ],
                },
                ("/gamekeys/region-restriction", (("game_id", "private-game-id"),)): {
                    "game_id": "private-game-id",
                    "countries": ["US", "GB"],
                },
            }
        )
        adapter = FazerCardsDigitalCatalogAdapter(client)
        page = adapter.list_gamekey_categories()
        catalog = adapter.get_gamekey_offers("private-game-id")
        restrictions = adapter.get_gamekey_region_restrictions("private-game-id")
        self.assertTrue(page.items[0].region_restriction)
        self.assertEqual(page.items[0].platform, "steam")
        self.assertTrue(catalog.region_restriction)
        self.assertEqual(catalog.offers[0].stock, 5)
        self.assertEqual(restrictions.details["countries"], ["US", "GB"])
        self.assertNotIn("private-game-id", repr(restrictions))
        self.assertNotIn("private-key", repr(catalog))
        self.assertNotIn("19.9900", repr(catalog))

    def test_steam_catalog_preserves_truncation_and_per_region_prices(self):
        client = _FakeReadClient(
            {
                ("/steam-gifts/games", (("limit", 100),)): {
                    "games": [{"name": "Example Game", "appid": 730}],
                    "meta": {"total": 177269, "returned": 1, "truncated": True},
                },
                ("/steam-gifts/games", (("limit", None),)): {
                    "games": [
                        {"name": "Example Game", "appid": 730},
                        {"name": "Second Game", "appid": 731},
                    ],
                    "meta": {"total": 2, "returned": 2, "truncated": False},
                },
                ("/steam-gifts/games/730", ()): {
                    "appid": 730,
                    "offers": [
                        {
                            "sub_id": 54029,
                            "name": "Example Edition",
                            "regions": [
                                {"region": "CIS", "price": "4.2500"},
                                {"region": "ROW", "price": "5.0000"},
                            ],
                        }
                    ],
                },
                ("/steam-topup/rates", ()): {
                    "base": "USD",
                    "rates": {"USD": 1, "RUB": 92.5},
                    "updated_at": "2026-09-21T00:00:00Z",
                },
            }
        )
        adapter = FazerCardsDigitalCatalogAdapter(client)
        page = adapter.list_steam_gift_games()
        full_page = adapter.list_steam_gift_games(limit=None)
        catalog = adapter.get_steam_gift_editions("730")
        rates = adapter.get_steam_topup_rates()
        self.assertEqual(page.total, 177269)
        self.assertTrue(page.truncated)
        self.assertEqual(page.returned, 1)
        self.assertEqual(full_page.total, 2)
        self.assertEqual(full_page.returned, 2)
        self.assertFalse(full_page.truncated)
        self.assertEqual(catalog.offers[0].regions[0].region, "CIS")
        self.assertEqual(str(catalog.offers[0].regions[0].supplier_cost_usd), "4.2500")
        self.assertEqual(dict(rates.rates)["RUB"], 92.5)
        self.assertNotIn("4.2500", repr(catalog))
        self.assertNotIn("54029", repr(catalog))
        self.assertNotIn("92.5", repr(rates))

    def test_manual_services_preserve_operator_and_customer_input_metadata(self):
        client = _FakeReadClient(
            {
                ("/manual-services", ()): {
                    "items": [
                        {
                            "id": "private-service",
                            "name": "Manual game transfer",
                            "kind": "manual_transfer",
                            "chat": True,
                            "info": "Handled by an operator",
                        }
                    ]
                },
                ("/manual-services/private-service/offers", ()): {
                    "manual_service_id": "private-service",
                    "category": {
                        "id": "private-service",
                        "name": "Manual game transfer",
                        "kind": "manual_transfer",
                        "chat": True,
                    },
                    "items": [
                        {
                            "id": "private-offer",
                            "name": "Standard",
                            "price_usd": "8.7500",
                            "delivery_minutes": 120,
                            "fields": [{"key": "player_id", "type": "text"}],
                        }
                    ],
                },
            }
        )
        adapter = FazerCardsDigitalCatalogAdapter(client)
        categories = adapter.list_manual_service_categories()
        catalog = adapter.get_manual_service_offers("private-service")
        self.assertTrue(categories[0].chat_enabled)
        self.assertEqual(catalog.offers[0].delivery_minutes, 120)
        self.assertIn('"player_id"', catalog.offers[0].input_schema_json)
        self.assertNotIn("private-offer", repr(catalog))
        self.assertNotIn("8.7500", repr(catalog))

    def test_manual_service_catalog_mismatch_or_invalid_schema_fails_closed(self):
        client = _FakeReadClient(
            {
                ("/manual-services/wanted/offers", ()): {
                    "manual_service_id": "wanted",
                    "category": {
                        "id": "wanted",
                        "name": "Service",
                        "kind": "manual_transfer",
                        "chat": False,
                    },
                    "items": [
                        {
                            "id": "offer",
                            "name": "Option",
                            "price_usd": "1.00",
                            "replenishment_fields": "not-a-schema",
                        }
                    ],
                }
            }
        )
        with self.assertRaisesRegex(CatalogReadError, "input schema"):
            FazerCardsDigitalCatalogAdapter(client).get_manual_service_offers("wanted")

    def test_malformed_and_mismatched_upstream_data_fails_closed(self):
        bad_quantity = _FakeReadClient(
            {
                ("/giftcards/cards", (("category_id", "cat"),)): {
                    "category_id": "cat",
                    "name": "Cards",
                    "offers": [
                        {
                            "card_id": "sku",
                            "name": "Offer",
                            "price_usd": "1.00",
                            "min_order_quantity": 5,
                            "max_order_quantity": 2,
                        }
                    ],
                }
            }
        )
        with self.assertRaisesRegex(CatalogReadError, "quantity limits"):
            FazerCardsDigitalCatalogAdapter(bad_quantity).get_giftcard_offers("cat")

        mismatch = _FakeReadClient(
            {
                ("/gamekeys/keys", (("game_id", "requested"),)): {
                    "game_id": "different",
                    "GameName": "Example",
                    "keys": [],
                }
            }
        )
        with self.assertRaisesRegex(CatalogReadError, "did not match"):
            FazerCardsDigitalCatalogAdapter(mismatch).get_gamekey_offers("requested")

    def test_adapter_has_no_write_methods_and_transport_bounds_routes(self):
        class _Opener:
            request = None

            def open(self, request, timeout):
                self.request = request
                raise AssertionError("unexpected request")

        opener = _Opener()
        client = FazerCardsReadOnlyClient("secret", opener=opener, min_interval_seconds=0)
        adapter = FazerCardsDigitalCatalogAdapter(client)
        self.assertFalse(hasattr(adapter, "submit_order"))
        self.assertFalse(hasattr(adapter, "create_invoice"))
        with self.assertRaises(ValueError):
            client.get("/steam-gifts/games/not-an-app-id")
        with self.assertRaises(ValueError):
            client.get("/steam-gifts/games", limit=MAX_LIMIT + 1)
        with self.assertRaises(ValueError):
            client.get("/gamekeys/keys", game_id=" padded-id ")

        with self.assertRaises(ValueError):
            FazerCardsReadOnlyClient("secret", timeout=float("inf"))
        with self.assertRaises(ValueError):
            FazerCardsReadOnlyClient("secret", timeout=True)
        with self.assertRaises(ValueError):
            FazerCardsReadOnlyClient("secret", min_interval_seconds=float("nan"))


MAX_LIMIT = 25_000


if __name__ == "__main__":
    unittest.main()
