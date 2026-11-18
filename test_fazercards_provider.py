from __future__ import annotations

import io
import json
import unittest
from email.message import Message
from urllib.error import HTTPError
from urllib.request import Request

from aurix_games.providers.fazercards import (
    CatalogReadError,
    FazerCardsCatalogAdapter,
    FazerCardsReadOnlyClient,
    _optional_provider_image,
)


class _Response:
    status = 200
    headers = Message()

    def __init__(self, payload: dict[str, object]) -> None:
        self._body = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


class _FakeCatalogClient:
    def __init__(self, responses: dict[str, dict[str, object]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, path: str, **query: object) -> dict[str, object]:
        self.calls.append((path, dict(query)))
        return self.responses[path]


class FazerCardsProviderTests(unittest.TestCase):
    def test_category_pages_keep_cursor_private_and_validate_metadata(self):
        client = _FakeCatalogClient(
            {
                "/topups": {
                    "items": [{"category_id": "secret-category", "name": "Sample", "note": ""}],
                    "meta": {"total": 3, "next_cursor": "secret-cursor", "has_more": True},
                }
            }
        )
        page = FazerCardsCatalogAdapter(client).list_topup_categories(limit=25)
        self.assertEqual(page.items[0].name, "Sample")
        self.assertEqual(page.total, 3)
        self.assertTrue(page.has_more)
        self.assertNotIn("secret-category", repr(page))
        self.assertNotIn("secret-cursor", repr(page))
        self.assertEqual(client.calls, [("/topups", {"limit": 25, "cursor": None})])

    def test_topup_ui_metadata_is_opt_in_typed_and_redacted(self):
        cover = "/api/v2/media/topup-covers/topup-1.webp"
        client = _FakeCatalogClient(
            {
                "/topups": {
                    "items": [
                        {
                            "category_id": "category",
                            "name": "Game",
                            "imageurl": cover,
                        }
                    ],
                    "meta": {"total": 1, "next_cursor": None, "has_more": False},
                },
                "/topups/offers": {
                    "category_id": "category",
                    "name": "Game",
                    "imageurl": cover,
                    "offers": [],
                    "fields": [],
                },
            }
        )
        adapter = FazerCardsCatalogAdapter(client)
        categories = adapter.list_topup_categories(include_ui=True)
        details = adapter.get_topup_offers("category", include_ui=True)
        self.assertEqual(categories.items[0].image_url, cover)
        self.assertEqual(details.image_url, cover)
        self.assertNotIn(cover, repr(categories))
        self.assertNotIn(cover, repr(details))
        self.assertEqual(client.calls[0][1]["include_ui"], True)
        self.assertEqual(client.calls[1][1], {"category_id": "category", "include_ui": True})

    def test_topup_offers_preserve_decimal_and_unknown_requiredness(self):
        client = _FakeCatalogClient(
            {
                "/topups/offers": {
                    "category_id": "secret-category",
                    "name": "Sample Game",
                    "offers": [
                        {"offer_id": "secret-offer", "name": "Pack", "price_usd": "1.2300"}
                    ],
                    "fields": [
                        {
                            "key": "server",
                            "label": "Server",
                            "type": "select",
                            "options": [{"label": "North", "value": "n"}],
                        }
                    ],
                }
            }
        )
        catalog = FazerCardsCatalogAdapter(client).get_topup_offers("secret-category")
        self.assertEqual(str(catalog.offers[0].supplier_cost_usd), "1.2300")
        self.assertIsNone(catalog.fields[0].required)
        self.assertEqual(catalog.fields[0].options[0].label, "North")
        self.assertNotIn("secret-category", repr(catalog))
        self.assertNotIn("secret-offer", repr(catalog))
        self.assertNotIn("1.2300", repr(catalog))

    def test_validation_capabilities_are_read_only_and_include_field_schemas(self):
        client = _FakeCatalogClient(
            {
                "/topups/validate-id": {
                    "items": [
                        {
                            "category_id": "secret-category",
                            "name": "Sample Game",
                            "fields": [
                                {"key": "player_id", "label": "Player ID", "type": "text"}
                            ],
                        }
                    ]
                }
            }
        )
        targets = FazerCardsCatalogAdapter(client).list_topup_validation_targets()
        self.assertEqual(targets[0].fields[0].key, "player_id")
        self.assertIsNone(targets[0].fields[0].required)
        self.assertEqual(client.calls, [("/topups/validate-id", {})])

    def test_malformed_price_or_required_flag_fails_closed(self):
        adapter = FazerCardsCatalogAdapter(
            _FakeCatalogClient(
                {
                    "/topups/offers": {
                        "category_id": "cat",
                        "name": "Sample",
                        "offers": [{"offer_id": "offer", "name": "Pack", "price_usd": "NaN"}],
                        "fields": [],
                    }
                }
            )
        )
        with self.assertRaisesRegex(CatalogReadError, "price was malformed"):
            adapter.get_topup_offers("cat")

        adapter = FazerCardsCatalogAdapter(
            _FakeCatalogClient(
                {
                    "/topups/offers": {
                        "category_id": "cat",
                        "name": "Sample",
                        "offers": [],
                        "fields": [{"key": "id", "label": "ID", "type": "text", "required": "yes"}],
                    }
                }
            )
        )
        with self.assertRaisesRegex(CatalogReadError, "required-field metadata"):
            adapter.get_topup_offers("cat")

    def test_transport_is_get_only_allowlisted_and_redacts_provider_errors(self):
        class _Opener:
            request: Request | None = None

            def open(self, request: Request, timeout: float) -> _Response:
                self.request = request
                return _Response({"ok": True, "items": []})

        opener = _Opener()
        client = FazerCardsReadOnlyClient(
            "test-secret", opener=opener, min_interval_seconds=0
        )
        self.assertEqual(client.get("/topups/validate-id"), {"ok": True, "items": []})
        adapter = FazerCardsCatalogAdapter(_FakeCatalogClient({}))
        self.assertFalse(hasattr(client, "post"))
        self.assertFalse(hasattr(adapter, "submit_order"))
        self.assertFalse(hasattr(adapter, "create_invoice"))
        assert opener.request is not None
        self.assertEqual(opener.request.get_method(), "GET")
        self.assertNotIn("test-secret", opener.request.full_url)
        self.assertEqual(opener.request.get_header("X-api-key"), "test-secret")
        with self.assertRaisesRegex(ValueError, "allowlist"):
            client.get("/topups/order")

        class _ErrorOpener:
            def open(self, request: Request, timeout: float) -> _Response:
                raise HTTPError(
                    request.full_url,
                    403,
                    "denied",
                    Message(),
                    io.BytesIO(b"private upstream detail and credentials"),
                )

        failing = FazerCardsReadOnlyClient(
            "test-secret", opener=_ErrorOpener(), min_interval_seconds=0
        )
        with self.assertRaises(CatalogReadError) as error:
            failing.get("/topups")
        self.assertNotIn("private upstream detail", str(error.exception))
        self.assertNotIn("test-secret", str(error.exception))

    def test_steam_429_retry_honors_three_minute_minimum(self):
        attempts = 0
        delays: list[float] = []

        class _RateLimitedOpener:
            def open(self, request: Request, timeout: float) -> _Response:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    headers = Message()
                    headers["Retry-After"] = "1"
                    raise HTTPError(
                        request.full_url,
                        429,
                        "rate limited",
                        headers,
                        io.BytesIO(b"retry later"),
                    )
                return _Response({"ok": True, "games": [], "meta": {}})

        client = FazerCardsReadOnlyClient(
            "test-secret",
            opener=_RateLimitedOpener(),
            min_interval_seconds=0,
            sleep=delays.append,
        )
        self.assertEqual(client.get("/steam-gifts/games"), {"ok": True, "games": [], "meta": {}})
        self.assertEqual(attempts, 2)
        self.assertTrue(any(delay >= 180 for delay in delays), delays)

    def test_retry_after_is_finite_and_excessive_wait_does_not_retry_early(self):
        for value in ("nan", "inf", "-1", "999999"):
            attempts = []
            delays = []
            class Opener:
                def open(self, request, timeout):
                    attempts.append(request)
                    if len(attempts) == 1:
                        headers = Message()
                        headers["Retry-After"] = value
                        raise HTTPError(request.full_url, 429, "limited", headers, io.BytesIO(b"limited"))
                    return _Response({"ok": True, "items": []})
            client = FazerCardsReadOnlyClient("secret", opener=Opener(), sleep=delays.append)
            if value == "999999":
                with self.assertRaisesRegex(CatalogReadError, "retry budget"):
                    client.get("/topups")
                self.assertEqual(len(attempts), 1)
                self.assertEqual(delays, [])
            else:
                self.assertTrue(client.get("/topups")["ok"])
                self.assertTrue(all(0 <= delay <= 300 for delay in delays))

    def test_documented_ui_metadata_queries_are_explicit_and_host_bounded(self):
        class _Opener:
            requests: list[Request]

            def __init__(self) -> None:
                self.requests = []

            def open(self, request: Request, timeout: float) -> _Response:
                self.requests.append(request)
                return _Response({"ok": True, "items": []})

        opener = _Opener()
        client = FazerCardsReadOnlyClient(
            "test-secret", opener=opener, min_interval_seconds=0
        )
        client.get("/topups", limit=500, include_ui=True)
        client.get("/manual-services/example/offers", include_ui=True)
        client.get("/giftcards/cards", card_id="canonical-category")
        self.assertIn("include_ui=1", opener.requests[0].full_url)
        self.assertIn("include_ui=1", opener.requests[1].full_url)
        self.assertTrue(opener.requests[2].full_url.endswith("category_id=canonical-category"))
        with self.assertRaisesRegex(ValueError, "deprecated card_id alias"):
            client.get("/giftcards/cards", category_id="one", card_id="two")
        with self.assertRaisesRegex(ValueError, "include_ui"):
            client.get("/topups", include_ui="false")
        with self.assertRaisesRegex(ValueError, "between 1 and 500"):
            client.get("/topups", limit=501)

    def test_ui_image_metadata_rejects_external_html_and_oversized_values(self):
        valid = "https://api.fzr.cards/api/v2/media/game-key-covers/cover-1.webp"
        official_reseller = "https://reseller.fazercards.com/api/v2/media/game-key-covers/cover-1.png"
        self.assertEqual(_optional_provider_image(official_reseller), official_reseller)
        self.assertEqual(_optional_provider_image(valid), valid)
        self.assertEqual(
            _optional_provider_image("/media/game-key-covers/cover-1"),
            "/media/game-key-covers/cover-1",
        )
        self.assertIsNone(_optional_provider_image(None))
        for invalid in (
            "https://evil.example/api/v2/media/cover.webp",
            "https://reseller.fazercards.com.evil.example/api/v2/media/covers/cover.webp",
            "https://reseller.fazercards.com:444/api/v2/media/covers/cover.webp",
            "https://user:password@reseller.fazercards.com/api/v2/media/covers/cover.webp",
            "http://api.fzr.cards/api/v2/media/cover.png",
            "https://api.fzr.cards/api/v2/media/cover.html",
            "https://api.fzr.cards/api/v2/media/../secret.png",
            "javascript:alert(1)",
            "<svg onload=alert(1)>",
            "https://api.fzr.cards/api/v2/media/" + ("a" * 2049) + ".png",
        ):
            with self.subTest(invalid=invalid[:64]):
                with self.assertRaises(CatalogReadError):
                    _optional_provider_image(invalid)


if __name__ == "__main__":
    unittest.main()
