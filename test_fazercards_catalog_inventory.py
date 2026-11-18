from __future__ import annotations

import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from urllib.request import Request

from scripts.fazercards_catalog_inventory import (
    CatalogReadError,
    FazerCardsReadOnlyClient,
    MAX_CURSOR_PAGES,
    _list_cursor_items,
    _summary,
    _validate_provider_image_fields,
    _write_snapshot,
    collect_inventory,
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


class _Opener:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.request: Request | None = None

    def open(self, request: Request, timeout: float) -> _Response:
        assert timeout > 0
        self.request = request
        return _Response(self.payload)


class SelectedCatalogInventoryTests(unittest.TestCase):
    class Client:
        def __init__(self, fail=False, wrong_identity=False):
            self.calls = []
            self.fail = fail
            self.wrong_identity = wrong_identity

        def get(self, path, **query):
            self.calls.append((path, query))
            if path == '/topups':
                return {'items': [{'category_id': 'chosen', 'name': 'Chosen'},
                                  {'category_id': 'other', 'name': 'Other'}],
                        'meta': {'next_cursor': None, 'has_more': False, 'total': 2}}
            if path == '/topups/offers' and query['category_id'] == 'chosen':
                if self.fail:
                    raise CatalogReadError('simulated read failure')
                return {'category_id': 'wrong' if self.wrong_identity else 'chosen',
                        'offers': [{'offer_id': 'pack', 'name': 'Pack', 'price_usd': '1'}]}
            raise AssertionError(f'unexpected request: {path}')

    def test_selected_capture_skips_unrelated_details_steam_and_payment_routes(self):
        client = self.Client()
        result = collect_inventory(client, selection={'topups': ['chosen']}, include_ui=False)
        self.assertEqual([p for p, _ in client.calls], ['/topups', '/topups/offers'])
        self.assertEqual(result['status'], 'complete')
        data = result['families']['topups']
        self.assertFalse(data['details_complete'])
        self.assertTrue(data['selected_details_complete'])
        self.assertEqual(set(data['details_by_id']), {'chosen'})
        self.assertIn('chosen', data['details_observed_at_by_id'])
        self.assertIn('topups', _summary(result)['selection'])
        from aurix_games.catalog_inventory import load_catalog_inventory
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'selected.json'
            _write_snapshot(path, result)
            inventory = load_catalog_inventory(path)
        self.assertEqual(inventory['source_selection'], {'topups': ['chosen']})
        self.assertEqual(len(inventory['items']), 1)
        self.assertEqual(inventory['items'][0]['freshness_status'], 'fresh')
        self.assertTrue(inventory['items'][0]['source_category_complete'])

    def test_selected_giftcards_and_gamekeys_keep_numeric_stock_and_restrictions(self):
        for family in ('giftcards', 'gamekeys'):
            calls = []
            class Client:
                def get(self, path, **query):
                    calls.append(path)
                    id_key = 'game_id' if family == 'gamekeys' else 'category_id'
                    if path == f'/{family}':
                        return {'items': [{id_key: 'chosen', 'name': 'Chosen'}],
                                'meta': {'next_cursor': None, 'has_more': False, 'total': 1}}
                    if path == '/gamekeys/region-restriction':
                        return {'region_type': 'whitelist', 'available_countries': ['TH']}
                    return {id_key: 'chosen', 'region_restriction': family == 'gamekeys',
                            'keys' if family == 'gamekeys' else 'offers': [
                                {'key_id' if family == 'gamekeys' else 'card_id': 'sku',
                                 'name': 'Pack', 'price_usd': '1', 'stock': 3,
                                 'min_order_quantity': 1, 'max_order_quantity': 3}]}
            with self.subTest(family=family):
                result = collect_inventory(Client(), selection={family: ['chosen']}, include_ui=False)
                self.assertEqual(result['status'], 'complete')
                detail = result['families'][family]['details_by_id']['chosen']
                self.assertEqual(detail['keys' if family == 'gamekeys' else 'offers'][0]['stock'], 3)
                self.assertEqual('/gamekeys/region-restriction' in calls, family == 'gamekeys')

    def test_partial_capture_resumes_only_missing_selected_detail(self):
        partial = collect_inventory(self.Client(fail=True), selection={'topups': ['chosen']}, include_ui=False)
        self.assertEqual(partial['status'], 'partial')
        client = self.Client()
        result = collect_inventory(client, initial=partial, selection={'topups': ['chosen']}, include_ui=False)
        self.assertEqual([p for p, _ in client.calls], ['/topups/offers'])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['families']['topups']['read_errors'], [])

    def test_mismatched_or_full_resume_scope_is_rejected_before_requests(self):
        initial = collect_inventory(self.Client(), selection={'topups': ['chosen']}, include_ui=False)
        for selection in (None, {'topups': ['other']}):
            client = self.Client()
            with self.assertRaisesRegex(ValueError, 'resume selection'):
                collect_inventory(client, initial=initial, selection=selection)
            self.assertEqual(client.calls, [])

    def test_unknown_selection_and_wrong_detail_identity_fail_closed(self):
        with self.assertRaisesRegex(CatalogReadError, 'absent'):
            collect_inventory(self.Client(), selection={'topups': ['absent']}, include_ui=False)
        result = collect_inventory(self.Client(wrong_identity=True), selection={'topups': ['chosen']}, include_ui=False)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['families']['topups']['details_by_id'], {})

    def test_invalid_selection_never_issues_requests(self):
        for selection in ({'topups': []}, {'topups': ['chosen', 'chosen']}, {'topups': ['../bad']},
                          {'unknown': ['chosen']}, {'topups': ['chosen'], 'giftcards': ['chosen']}):
            client = self.Client()
            with self.assertRaises(ValueError):
                collect_inventory(client, selection=selection)
            self.assertEqual(client.calls, [])


class FazerCardsCatalogInventoryTests(unittest.TestCase):
    def test_client_is_get_only_and_rejects_order_paths(self) -> None:
        opener = _Opener({"ok": True, "items": []})
        client = FazerCardsReadOnlyClient("test-secret", opener=opener, min_interval_seconds=0)

        self.assertEqual(client.get("/topups", limit=50), {"ok": True, "items": []})
        with self.assertRaisesRegex(ValueError, "allowlist"):
            client.get("/catalog", limit=100)
        self.assertIsNotNone(opener.request)
        assert opener.request is not None
        self.assertEqual(opener.request.get_method(), "GET")
        self.assertEqual(opener.request.get_header("X-api-key"), "test-secret")
        with self.assertRaisesRegex(ValueError, "allowlist"):
            client.get("/topups/order")
        with self.assertRaisesRegex(ValueError, "allowlist"):
            client.get("/manual-services/orders/ord-1/chat/offers")

    def test_client_enforces_steam_three_minute_cooldown(self) -> None:
        class CountingOpener:
            calls = 0
            request: Request | None = None

            def open(self, request: Request, timeout: float) -> _Response:
                self.calls += 1
                self.request = request
                return _Response(
                    {"ok": True, "games": [], "meta": {"total": 0, "returned": 0, "truncated": False}}
                )

        opener = CountingOpener()
        client = FazerCardsReadOnlyClient("test-secret", opener=opener, min_interval_seconds=0)
        client.get("/steam-gifts/games")
        assert opener.request is not None
        self.assertTrue(opener.request.full_url.endswith("/steam-gifts/games"))
        with self.assertRaisesRegex(CatalogReadError, "cooling down"):
            client.get("/steam-gifts/games", limit=None)
        self.assertEqual(opener.calls, 1)

    def test_steam_gift_limit_matches_documented_string_cap_without_cursor(self) -> None:
        opener = _Opener({"ok": True, "games": [], "meta": {"total": 0, "returned": 0, "truncated": False}})
        client = FazerCardsReadOnlyClient("test-secret", opener=opener, min_interval_seconds=0)
        client.get("/steam-gifts/games", limit="100")
        assert opener.request is not None
        self.assertTrue(opener.request.full_url.endswith("/steam-gifts/games?limit=100"))

        with self.assertRaisesRegex(ValueError, "query parameters"):
            client.get("/steam-gifts/games", cursor="page-2")
        with self.assertRaisesRegex(ValueError, "decimal string"):
            client.get("/steam-gifts/games", limit=100)
        with self.assertRaisesRegex(ValueError, "decimal string"):
            client.get("/steam-gifts/games", limit="0")

    def test_cursor_pagination_preserves_all_items_and_rejects_loop(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def get(self, path: str, **query: object) -> dict[str, object]:
                self.calls.append(dict(query))
                if query.get("cursor") is None:
                    return {"items": [{"category_id": "a"}], "meta": {"has_more": True, "next_cursor": "next"}}
                return {"items": [{"category_id": "b"}], "meta": {"has_more": False, "next_cursor": None}}

        client = Client()
        rows, meta = _list_cursor_items(client, "/topups")  # type: ignore[arg-type]
        self.assertEqual([row["category_id"] for row in rows], ["a", "b"])
        self.assertTrue(meta["has_more"])
        self.assertEqual(len(client.calls), 2)

        class LoopClient:
            def get(self, path: str, **query: object) -> dict[str, object]:
                return {"items": [], "meta": {"has_more": True, "next_cursor": "repeat"}}

        with self.assertRaisesRegex(CatalogReadError, "repeated cursor"):
            _list_cursor_items(LoopClient(), "/topups", max_pages=3)  # type: ignore[arg-type]

    def test_cursor_pagination_rejects_inconsistent_or_untyped_metadata(self) -> None:
        class MalformedClient:
            def __init__(self, meta: dict[str, object]) -> None:
                self.meta = meta
                self.calls = 0

            def get(self, path: str, **query: object) -> dict[str, object]:
                self.calls += 1
                return {"items": [], "meta": self.meta}

        cases = (
            ({"has_more": "false", "next_cursor": None}, "pagination state"),
            ({"has_more": 0, "next_cursor": None}, "pagination state"),
            ({"has_more": False, "next_cursor": "unexpected"}, "without another page"),
            ({"has_more": True, "next_cursor": None}, "omitted its next cursor"),
            ({"has_more": False, "next_cursor": None, "total": True}, "invalid total"),
        )
        for meta, expected in cases:
            with self.subTest(meta=meta):
                client = MalformedClient(meta)
                with self.assertRaisesRegex(CatalogReadError, expected):
                    _list_cursor_items(client, "/topups")  # type: ignore[arg-type]
                self.assertEqual(client.calls, 1)

    def test_cursor_pagination_bounds_page_size_and_resume_count(self) -> None:
        class TooManyItems:
            def get(self, path: str, **query: object) -> dict[str, object]:
                return {
                    "items": [{"id": "1"}, {"id": "2"}],
                    "meta": {"has_more": False, "next_cursor": None},
                }

        with self.assertRaisesRegex(CatalogReadError, "requested page size"):
            _list_cursor_items(TooManyItems(), "/topups", limit=1)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "max_pages"):
            _list_cursor_items(TooManyItems(), "/topups", max_pages=MAX_CURSOR_PAGES + 1)  # type: ignore[arg-type]

    def test_cursor_resume_reuses_checkpointed_pages_after_mid_list_failure(self) -> None:
        checkpoint: dict[str, object] = {}

        class InterruptedClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def get(self, path: str, **query: object) -> dict[str, object]:
                self.calls.append(dict(query))
                if query.get("cursor") is None:
                    return {
                        "items": [{"category_id": "first"}],
                        "meta": {"has_more": True, "next_cursor": "page-2", "total": 2},
                    }
                raise CatalogReadError("fixture interruption")

        first_client = InterruptedClient()
        with self.assertRaisesRegex(CatalogReadError, "fixture interruption"):
            _list_cursor_items(
                first_client,  # type: ignore[arg-type]
                "/topups",
                item_id_key="category_id",
                checkpoint=lambda state: checkpoint.update(state),
            )
        self.assertEqual(len(first_client.calls), 2)
        self.assertEqual(checkpoint["items"], [{"category_id": "first"}])
        self.assertEqual(checkpoint["next_cursor"], "page-2")
        self.assertEqual(checkpoint["page_count"], 1)
        self.assertFalse(checkpoint["complete"])

        class ResumedClient:
            calls: list[dict[str, object]] = []

            def get(self, path: str, **query: object) -> dict[str, object]:
                self.calls.append(dict(query))
                if query.get("cursor") != "page-2":
                    raise AssertionError("resume must continue at the saved cursor")
                return {
                    "items": [{"category_id": "second"}],
                    "meta": {"has_more": False, "next_cursor": None, "total": 2},
                }

        resumed_client = ResumedClient()
        rows, meta = _list_cursor_items(
            resumed_client,  # type: ignore[arg-type]
            "/topups",
            item_id_key="category_id",
            initial=checkpoint,  # type: ignore[arg-type]
            checkpoint=lambda state: checkpoint.update(state),
        )
        self.assertEqual([row["category_id"] for row in rows], ["first", "second"])
        self.assertEqual(meta["total"], 2)
        self.assertEqual(len(resumed_client.calls), 1)
        self.assertTrue(checkpoint["complete"])
        self.assertIsNone(checkpoint["next_cursor"])

    def test_cursor_pagination_rejects_duplicate_category_ids(self) -> None:
        class OverlappingPages:
            def get(self, path: str, **query: object) -> dict[str, object]:
                if query.get("cursor") is None:
                    return {
                        "items": [{"category_id": "same"}],
                        "meta": {"has_more": True, "next_cursor": "next"},
                    }
                return {
                    "items": [{"category_id": "same"}],
                    "meta": {"has_more": False, "next_cursor": None},
                }

        with self.assertRaisesRegex(CatalogReadError, "duplicate category_id"):
            _list_cursor_items(
                OverlappingPages(),  # type: ignore[arg-type]
                "/topups",
                item_id_key="category_id",
            )

    def test_inventory_only_calls_documented_get_routes_and_expands_item_details(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def get(self, path: str, **query: object) -> dict[str, object]:
                self.calls.append((path, dict(query)))
                if path in {"/topups", "/giftcards", "/gamekeys"}:
                    key = {"/topups": "category_id", "/giftcards": "category_id", "/gamekeys": "game_id"}[path]
                    return {"items": [{key: f"{path}-1", "imageurl": "/api/v2/media/covers/category.webp"}], "meta": {"has_more": False, "next_cursor": None, "total": 1}}
                if path == "/topups/offers":
                    return {"imageurl": "/api/v2/media/covers/topup.webp", "offers": [{"offer_id": "offer-1", "price_usd": "1.25"}], "fields": []}
                if path == "/topups/validate-id":
                    return {
                        "items": [
                            {
                                "category_id": "topups-1",
                                "name": "Top-up validation",
                                "fields": [{"key": "player_id", "type": "text"}],
                            }
                        ]
                    }
                if path == "/giftcards/cards":
                    return {"imageurl": "/api/v2/media/covers/gift.webp", "offers": [{"card_id": "card-1", "stock": 3}]}
                if path == "/gamekeys/keys":
                    return {"region_restriction": True, "imageurl": "/api/v2/media/covers/key.webp", "keys": [{"key_id": "key-1"}]}
                if path == "/gamekeys/region-restriction":
                    return {"available": [{"code": "MM"}], "unavailable": []}
                if path == "/steam-gifts/games":
                    if query:
                        raise AssertionError("full Steam list must omit the limit to avoid a second request")
                    return {
                        "games": [{"appid": 123, "name": "Sample"}],
                        "meta": {"total": 1, "returned": 1, "truncated": False},
                    }
                if path == "/manual-services":
                    return {"items": [{"id": "service-1", "name": "Manual", "imageurl": "/api/v2/media/covers/manual.webp"}]}
                if path.endswith("/offers") and path.startswith("/manual-services/"):
                    return {"imageurl": "/api/v2/media/covers/manual-detail.webp", "items": [{"id": "manual-offer-1"}]}
                if path in {"/steam-topup/rates", "/telegram/stars", "/telegram/premium", "/payments/methods"}:
                    return {"ok": True, "items": []}
                raise AssertionError(f"unexpected path: {path}")

        client = Client()
        inventory = collect_inventory(client)  # type: ignore[arg-type]
        paths = [path for path, _ in client.calls]
        self.assertTrue(inventory["families"]["topups"]["details_by_id"]["/topups-1"]["offers"])
        self.assertTrue(inventory["families"]["topups"]["categories_observed_at"])
        self.assertTrue(inventory["families"]["topups"]["details_observed_at_by_id"]["/topups-1"])
        self.assertTrue(inventory["families"]["giftcards"]["details_by_id"]["/giftcards-1"]["offers"])
        self.assertTrue(inventory["families"]["gamekeys"]["details_by_id"]["/gamekeys-1"]["region_restrictions"])
        self.assertEqual(
            inventory["families"]["topup_validation"]["items"][0]["category_id"],
            "topups-1",
        )
        self.assertEqual(inventory["families"]["steam_gifts"]["games"][0]["appid"], 123)
        self.assertEqual(
            inventory["families"]["topups"]["details_by_id"]["/topups-1"]["imageurl"],
            "/api/v2/media/covers/topup.webp",
        )
        self.assertEqual(
            inventory["families"]["gamekeys"]["categories"][0]["imageurl"],
            "/api/v2/media/covers/category.webp",
        )
        self.assertTrue(
            all(query.get("include_ui") is True for path, query in client.calls if path in {"/topups", "/giftcards", "/gamekeys", "/topups/offers", "/giftcards/cards", "/gamekeys/keys", "/manual-services"} or path.endswith("/offers") and path.startswith("/manual-services/"))
        )
        self.assertTrue(inventory["families"]["steam_gifts"]["full_list_complete"])
        self.assertEqual(inventory["families"]["full_catalog"]["items"], [])
        self.assertFalse(inventory["families"]["full_catalog"]["supported"])
        self.assertEqual(inventory["families"]["full_catalog"]["verification_status"], "undocumented")
        self.assertFalse(inventory["families"]["full_catalog"]["complete"])
        self.assertNotIn("/catalog", paths)
        self.assertIn("/payments/methods", paths)
        self.assertFalse(any(path.endswith("/order") or "/order/" in path for path in paths))
        self.assertFalse(inventory["coverage"]["complete"])  # Steam gift details remain unexpanded.
        self.assertNotIn("private-global-sku", json.dumps(_summary(inventory)))
        metadata_client = Client()
        metadata = collect_inventory(metadata_client, include_ui=False)
        self.assertFalse(metadata["artwork_requested_on_last_run"])
        self.assertTrue(metadata["families"]["topups"]["details_complete"])
        self.assertTrue(metadata["families"]["gamekeys"]["details_complete"])
        self.assertTrue(all(query.get("include_ui") is not True for _, query in metadata_client.calls))

    def test_summary_keeps_denied_category_reads_distinct_from_empty_catalog(self):
        summary = _summary({"captured_at": "2026-11-13T00:00:00Z", "coverage": {"complete": False},
            "families": {"topups": {"categories": [], "details_by_id": {},
                "categories_complete": False, "read_error": "GET /topups returned HTTP 403"}}})
        topups = summary["families"]["topups"]
        self.assertFalse(topups["categories_complete"])
        self.assertIn("HTTP 403", topups["error"])

    def test_resuming_failed_detail_keeps_one_current_error_per_category(self):
        class Client:
            def get(self, path, **query):
                raise CatalogReadError(f"GET {path} returned HTTP 404")
        finished = {"categories": [], "details_by_id": {}, "details_complete": True, "categories_complete": True}
        initial = {"snapshot_version": 1, "captured_at": "2026-11-13T00:00:00Z",
            "coverage": {"complete": False, "notes": []}, "families": {
                "topups": {"categories": [{"category_id": "missing"}], "categories_complete": True,
                    "details_by_id": {}, "details_complete": False, "category_scan": {},
                    "read_errors": [{"id": "missing", "error": "old"}]},
                "giftcards": finished, "gamekeys": finished,
                "topup_validation": {"items": []},
                "steam_gifts": {"games": [], "catalog_meta": {"total": 0, "returned": 0, "truncated": False}},
                "steam_topup": {}, "telegram_stars": {}, "telegram_premium": {},
                "manual_services": {"items": []}, "payment_methods": {}}}
        result = collect_inventory(Client(), initial=initial, include_ui=False)
        self.assertEqual(result["families"]["topups"]["read_errors"],
            [{"id": "missing", "error": "GET /topups/offers returned HTTP 404"}])

    def test_provider_image_metadata_fails_closed_before_checkpoint(self) -> None:
        for imageurl in (
            "https://untrusted.example/cover.webp",
            "https://api.fzr.cards/api/v2/media/cover.html",
            "<html>not an image</html>",
            "/api/v2/media/" + ("x" * 2050) + ".png",
        ):
            with self.subTest(imageurl=imageurl[:40]):
                with self.assertRaises(CatalogReadError):
                    _validate_provider_image_fields({"imageurl": imageurl})

    def test_legacy_full_catalog_snapshot_is_retained_but_marked_unverified(self) -> None:
        class NoCallClient:
            def get(self, path: str, **query: object) -> dict[str, object]:
                raise AssertionError(f"no supplier call expected: {path}")

        legacy = {"items": [{"sku_id": "legacy-private"}], "complete": True}
        initial = {
            "snapshot_version": 1,
            "captured_at": "2026-09-21T00:00:00Z",
            "families": {
                "full_catalog": legacy,
                "topups": {"categories": [], "details_by_id": {}, "details_complete": True},
                "giftcards": {"categories": [], "details_by_id": {}, "details_complete": True},
                "gamekeys": {"categories": [], "details_by_id": {}, "details_complete": True},
                "topup_validation": {"items": []},
                "steam_gifts": {"games": [], "catalog_meta": {"total": 0, "truncated": False}},
                "steam_topup": {"ok": True},
                "telegram_stars": {"ok": True},
                "telegram_premium": {"ok": True},
                "manual_services": {"items": [], "offers_by_id": {}},
                "payment_methods": {"items": []},
            },
            "coverage": {"complete": True, "notes": []},
        }
        result = collect_inventory(NoCallClient(), initial=initial)  # type: ignore[arg-type]
        full_catalog = result["families"]["full_catalog"]
        self.assertEqual(full_catalog["items"], [{"sku_id": "legacy-private"}])
        self.assertFalse(full_catalog["complete"])
        self.assertFalse(full_catalog["supported"])
        self.assertEqual(full_catalog["verification_status"], "undocumented")
        self.assertFalse(result["coverage"]["complete"])

    def test_legacy_truncated_steam_snapshot_starts_cooldown_before_full_refresh(self) -> None:
        class NoCallClient:
            calls = 0

            def get(self, path: str, **query: object) -> dict[str, object]:
                self.calls += 1
                raise AssertionError(f"unexpected request before cooldown: {path}")

        families = {
            "full_catalog": {
                "items": [], "catalog_meta": {"total": 0}, "page_count": 0,
                "seen_cursors": [], "next_cursor": None, "complete": True,
            },
            "topups": {"categories": [], "details_by_id": {}, "details_complete": True},
            "giftcards": {"categories": [], "details_by_id": {}, "details_complete": True},
            "gamekeys": {"categories": [], "details_by_id": {}, "details_complete": True},
            "topup_validation": {"items": []},
            "steam_gifts": {
                "games": [{"appid": 123, "name": "Sample"}],
                "catalog_meta": {"total": 2, "returned": 1, "truncated": True},
            },
            "steam_topup": {"ok": True},
            "telegram_stars": {"ok": True},
            "telegram_premium": {"ok": True},
            "manual_services": {"items": [], "offers_by_id": {}},
            "payment_methods": {"items": []},
        }
        client = NoCallClient()
        result = collect_inventory(
            client,  # type: ignore[arg-type]
            initial={
                "snapshot_version": 1,
                "captured_at": "2026-09-21T00:00:00Z",
                "families": families,
                "coverage": {"complete": True, "notes": []},
            },
        )
        steam = result["families"]["steam_gifts"]
        self.assertEqual(client.calls, 0)
        self.assertIsInstance(steam["full_list_request_started_at"], float)
        self.assertIn("cooldown initialized", steam["read_error"])
        self.assertIsNot(steam.get("full_list_complete"), True)

    def test_private_snapshot_uses_owner_only_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "snapshot.json"
            _write_snapshot(path, {"families": {"topups": {"categories": []}}})
            self.assertEqual(json.loads(path.read_text()), {"families": {"topups": {"categories": []}}})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_resume_skips_successfully_captured_catalog_families(self) -> None:
        class NoCallClient:
            def get(self, path: str, **query: object) -> dict[str, object]:
                raise AssertionError(f"resume should not refetch {path}")

        initial = {
            "snapshot_version": 1,
            "captured_at": "2026-09-21T00:00:00+00:00",
            "families": {
                "topups": {"categories": [], "details_by_id": {}, "details_complete": True},
                "giftcards": {"categories": [], "details_by_id": {}, "details_complete": True},
                "gamekeys": {"categories": [], "details_by_id": {}, "details_complete": True},
                "topup_validation": {"items": []},
                "full_catalog": {"items": [], "catalog_meta": {"total": 0}, "page_count": 0, "complete": True},
                "steam_gifts": {"games": [], "catalog_meta": {"total": 0, "truncated": False}},
                "steam_topup": {"ok": True},
                "telegram_stars": {"ok": True},
                "telegram_premium": {"ok": True},
                "manual_services": {"items": [], "offers_by_id": {}},
                "payment_methods": {"items": []},
            },
            "coverage": {"complete": False, "notes": ["Steam details deferred"]},
        }
        result = collect_inventory(NoCallClient(), initial=initial)  # type: ignore[arg-type]
        self.assertEqual(result["families"]["topups"]["details_by_id"], {})
        self.assertEqual(result["status"], "partial")


if __name__ == "__main__":
    unittest.main()
