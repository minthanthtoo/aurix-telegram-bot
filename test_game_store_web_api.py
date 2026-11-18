from __future__ import annotations

import json
import hashlib
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

from aurix_games.game_admin import GameAdminAuthError, GameStoreAdminService
from aurix_games.fazercards_account import WalletFundingIntentStore
from aurix_games.customer_api import CustomerOrderService
from aurix_games.customer_api import SignedCustomerTokenVerifier, issue_test_token
from aurix_games.checkout_journey import CheckoutJourney, MlbbAccountLookup
from aurix_games.order_repository import GameOrderRepository
from aurix_games.order_state import OrderSnapshot, OrderState, create_order
from aurix_games.web_api import make_handler
from aurix_games.wallet_monitor import SupplierWalletAlertStore


class _WebhookAdminStub:
    def __init__(self) -> None:
        self._admin_token = "admin-secret"
        self.configured = None
        self.deleted = False
        self.catalog_policy = None
        self.approved_mapping = None

    def authenticate(self, authorization):
        if authorization != "Bearer admin-secret":
            raise GameAdminAuthError("administrator authorization required")

    def webhook_status(self):
        return {"webhook": {"url": "https://store.example.test/api/v1/webhooks/fazercards", "enabled": True, "status": "active"}}

    def mapping_review_status(self):
        return {"mapping_review": {
            "status": "blocked",
            "reason": "mapping_config_missing",
            "mapped": 0,
            "projected": 0,
            "writes": False,
            "supplier_key_used": False,
        }}

    def mapping_history_status(self):
        return {"mapping_history": {
            "status": "empty",
            "count": 0,
            "events": [],
            "writes": False,
        }}

    def catalog_inventory(self, **kwargs):
        return {
            "schema_version": 1,
            "status": "private_snapshot",
            "source_sha256": "abc123",
            "captured_at": "2026-09-23T00:00:00Z",
            "source_status": "complete",
            "counts": {"topups": 1},
            "category_counts": {"topups": 1},
            "total_count": 1,
            "offset": 0,
            "limit": kwargs.get("limit", 50),
            "next_offset": None,
            "items": [{"family": "topups", "category_id": "mlbb", "sku": "d55", "name": "55 Diamonds", "price_usd": "1.2300"}],
            "auxiliary": {},
            "customer_visible": False,
            "checkout_enabled": False,
        }

    def configure_webhook(self, payload):
        self.configured = payload
        return {"webhook": {"url": payload["url"], "enabled": payload.get("enabled", True)}, "write_applied": True}

    def delete_webhook(self):
        self.deleted = True
        return {"webhook": {}, "write_applied": True}

    def update_catalog_policy(self, payload):
        self.catalog_policy = payload
        return {"configured": True, "policy": payload["policy"], "revision": 1}

    def approve_catalog_mapping(self, payload):
        self.approved_mapping = payload
        return {"mapping_approval": {
            "status": "approved_row",
            "provider_key": [payload.get("family"), payload.get("category_id"), payload.get("sku")],
            "offer_id": payload.get("offer_id"),
            "local_config_updated": True,
            "supplier_write": False,
            "customer_publication": False,
        }}


class GameStoreWebAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler())
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_version_endpoint_reports_uncached_deployed_release_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "release.json"
            marker.write_text(json.dumps({"release_id": "a" * 40, "commit_sha": "a" * 40}), encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(release_marker_path=marker))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urlopen(f"http://127.0.0.1:{server.server_port}/api/version") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get("Cache-Control"), "no-store")
                    self.assertEqual(json.load(response), {"release_id": "a" * 40, "commit_sha": "a" * 40})
                marker.write_text(json.dumps({"release_id": "legacy-" + "b" * 64, "commit_sha": None}), encoding="utf-8")
                with urlopen(f"http://127.0.0.1:{server.server_port}/api/version") as response:
                    self.assertEqual(json.load(response)["release_id"], "legacy-" + "b" * 64)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_local_mlbb_lookup_only_returns_confirmed_identity_and_throttles(self):
        with tempfile.TemporaryDirectory() as directory:
            supplier = Mock()
            supplier.validate_topup_recipient.return_value = {
                "ok": True, "valid": True, "category_id": "mobile_legends",
                "player_id": "12345", "player_name": "Fixture", "region": "Myanmar",
                "private": "supplier metadata",
            }
            zones = [{"id": "12", "name": "Fixture Zone"}, {"id": "13", "name": "Shared Zone"},
                     {"id": "14", "name": "Shared Zone"}]
            lookup = MlbbAccountLookup(supplier, GameOrderRepository(Path(directory) / "orders.sqlite3"),
                                       fingerprint_secret=b"x" * 32, local_guest=True, zones=zones)
            self.assertEqual(lookup.resolve_zone("  FIXTURE ZONE  "), "12")
            self.assertEqual(lookup.resolve_zone("Shared Zone (14)"), "14")
            self.assertEqual(lookup.resolve_zone("99999"), "99999")
            with ThreadingHTTPServer(("127.0.0.1", 0), make_handler(mlbb_lookup=lookup)) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    origin = f"http://127.0.0.1:{server.server_port}"
                    with urlopen(origin + "/api/v1/games/mlbb/zones", timeout=5) as response:
                        self.assertEqual(json.load(response), {"zones": zones})
                    url = origin + "/api/v1/games/mlbb/account-check"
                    def request(body, request_origin):
                        return urlopen(Request(url, data=json.dumps(body).encode(), headers={
                            "Origin": request_origin, "Content-Type": "application/json",
                        }), timeout=5)
                    valid = {"player_id": "12345", "zone_id": "12"}
                    for body, request_origin, expected in (
                        (valid, "https://other.invalid", 403),
                        ({**valid, "zone_id": "bad"}, origin, 400),
                        ({**valid, "zone_id": "Shared Zone"}, origin, 400),
                    ):
                        with self.assertRaises(HTTPError) as error:
                            request(body, request_origin)
                        self.assertEqual(error.exception.code, expected)
                        error.exception.close()
                    supplier.validate_topup_recipient.assert_not_called()
                    with request({**valid, "zone_id": "Fixture Zone (12)"}, origin) as response:
                        self.assertEqual(json.load(response), {"player_name": "Fixture", "region": "Myanmar"})
                        self.assertEqual(response.headers["Cache-Control"], "no-store")
                    with self.assertRaises(HTTPError) as throttled:
                        request(valid, origin)
                    self.assertEqual(throttled.exception.code, 429)
                    throttled.exception.close()
                    supplier.validate_topup_recipient.assert_called_once_with("mobile_legends", valid)
                finally:
                    server.shutdown()
                    thread.join(timeout=2)

    def test_draft_analysis_http_contract(self):
        url = self.base_url + "/api/v1/strategy-guides/mlbb/draft-analysis"
        request = Request(url, data=b'{"ally":[],"enemy":[]}',
            headers={"Origin": self.base_url, "Content-Type": "application/json"}, method="POST")
        with urlopen(request) as response:
            result = json.load(response)
        self.assertEqual(result["algorithm"], "maximum_distinct_lane_matching_v1")
        self.assertEqual(result["teams"]["ally"]["matched_count"], 0)
        self.assertEqual(len(result["roster_revision"]), 64)
        for headers, body, expected in [
            ({"Content-Type": "application/json"}, b'{}', 403),
            ({"Origin": self.base_url, "Content-Type": "application/json"}, b'{"ally":["unknown-hero"]}', 400),
        ]:
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url, data=body, headers=headers, method="POST"))
            self.assertEqual(error.exception.code, expected)

    def test_catalog_get_supports_search_and_returns_security_headers(self) -> None:
        with urlopen(self.base_url + "/api/v1/catalog?q=pubg") as response:
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

        self.assertEqual([item["slug"] for item in payload["products"]], ["pubg-mobile"])
        self.assertFalse(payload["checkout_enabled"])

    def test_health_endpoint_is_public_and_does_not_expose_configuration(self) -> None:
        with urlopen(self.base_url + "/api/healthz") as response:
            payload = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(payload, {"status": "ok", "service": "aurix-games", "checkout_enabled": False})
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/api/healthz?secret=1")
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_readiness_is_separate_from_liveness_and_fails_closed_without_pilot_dependencies(self) -> None:
        with urlopen(self.base_url + "/api/healthz") as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(HTTPError) as failure:
            urlopen(self.base_url + "/api/readyz")
        self.assertEqual(failure.exception.code, 503)
        payload = json.loads(failure.exception.read())
        failure.exception.close()
        self.assertEqual(payload["status"], "not_ready")
        self.assertFalse(all(payload["checks"].values()))
        self.assertNotIn("token", json.dumps(payload).casefold())

    def test_static_server_never_serves_test_tokens(self) -> None:
        for path in ("/game-store/test_token.txt", "/game-store/%74est_token.txt"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                urlopen(self.base_url + path)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()

    def test_checkout_status_uses_the_same_gate_as_customer_checkout(self) -> None:
        with patch.dict(os.environ, {
            "AURIX_GAMES_ENABLE_CHECKOUT": "1",
            "AURIX_GAMES_CUSTOMER_CHECKOUT_ENABLED": "0",
        }):
            with urlopen(self.base_url + "/api/healthz") as response:
                self.assertFalse(json.loads(response.read())["checkout_enabled"])
            with urlopen(self.base_url + "/api/v1/navigation") as response:
                self.assertFalse(json.loads(response.read())["checkout_enabled"])

        with patch.dict(os.environ, {"AURIX_GAMES_CUSTOMER_CHECKOUT_ENABLED": "1"}):
            with urlopen(self.base_url + "/api/healthz") as response:
                self.assertTrue(json.loads(response.read())["checkout_enabled"])
            with urlopen(self.base_url + "/api/v1/navigation") as response:
                self.assertTrue(json.loads(response.read())["checkout_enabled"])

    def test_invalid_or_duplicate_parameters_fail_closed(self) -> None:
        for path in (
            "/api/v1/catalog?category=not-real",
            "/api/v1/catalog?q=a&q=b",
            "/api/v1/catalog?checkout=true",
        ):
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                urlopen(self.base_url + path)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()

    def test_order_write_route_is_not_implemented(self) -> None:
        request = Request(self.base_url + "/api/v1/orders", data=b"{}", method="POST")
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 501)
        error.exception.close()

    def test_navigation_manifest_exposes_only_safe_reference_surfaces(self) -> None:
        with urlopen(self.base_url + "/api/v1/navigation") as response:
            payload = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertFalse(payload["checkout_enabled"])
        items = {item["id"]: item for item in payload["items"]}
        self.assertEqual(items["order-history"]["tabs"], ["Orders", "Pre-orders"])
        self.assertEqual(items["top-up-history"]["empty_label"], "No Data")
        self.assertEqual(items["create-gift-code"]["status"], "unavailable")
        self.assertEqual(items["redeem-gift-code"]["status"], "unavailable")
        self.assertEqual(items["gift-code-history"]["status"], "unavailable")
        self.assertEqual(items["support-chat"]["status"], "unavailable")
        self.assertEqual(items["events-news"]["status"], "reference_only")
        self.assertEqual(items["mbb-hero-guide"]["status"], "reference_only")
        self.assertIn("attributed", items["mbb-hero-guide"]["notice"])
        self.assertEqual(len(items["events-news"]["items"]), 3)
        self.assertEqual(items["discount-events"]["empty_label"], "No Data")
        self.assertEqual(items["myanmar-game-guide"]["surface"], "strategy_guide")
        self.assertEqual(items["leaderboard"]["surface"], "leaderboard")
        self.assertEqual(items["leaderboard"]["status"], "available")
        self.assertEqual(items["leaderboard"]["label"], "Most active buyers")
        self.assertNotIn("customer_id", payload)

    def test_navigation_manifest_rejects_query_parameters(self) -> None:
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/api/v1/navigation?customer_id=private")
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_reference_news_index_and_detail_are_read_only(self) -> None:
        with urlopen(self.base_url + "/api/v1/reference/events-news") as response:
            feed = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(feed["status"], "reference_only")
        self.assertFalse(feed["checkout_enabled"])
        self.assertEqual(len(feed["items"]), 3)
        news_id = feed["items"][0]["id"]

        with urlopen(self.base_url + f"/api/v1/reference/events-news/{news_id}") as response:
            detail = json.loads(response.read())
        self.assertEqual(detail["item"]["id"], news_id)
        self.assertIn("original article images", detail["notice"])

        for path, code in (
            ("/api/v1/reference/events-news?source=live", 400),
            ("/api/v1/reference/events-news/not-real", 404),
            ("/api/v1/reference/events-news/invalid?x=1", 400),
        ):
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                urlopen(self.base_url + path)
            self.assertEqual(error.exception.code, code)
            error.exception.close()

    def test_reference_discount_events_is_an_empty_read_only_surface(self) -> None:
        with urlopen(self.base_url + "/api/v1/reference/discount-events") as response:
            payload = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "reference_empty")
        self.assertFalse(payload["checkout_enabled"])
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["empty_label"], "No Data")
        self.assertIn("empty Discount Events", payload["notice"])

        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/api/v1/reference/discount-events?source=live")
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_strategy_guide_index_and_detail_are_read_only(self) -> None:
        with urlopen(self.base_url + "/api/v1/strategy-guides") as response:
            index = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual({guide["slug"] for guide in index["guides"]}, {"mlbb", "pubg-mobile-myanmar", "free-fire-myanmar"})
        self.assertFalse(any(guide["player_lookup"] for guide in index["guides"]))

        with urlopen(self.base_url + "/api/v1/strategy-guides/mlbb") as response:
            mlbb = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(mlbb["source"]["license"], "CC BY-SA")
        self.assertEqual(
            mlbb["source"]["rankings_provider"],
            "Rone Arena community API, as disclosed by MLBBDex",
        )
        self.assertEqual(mlbb["source"]["rankings_provider_url"], "https://arena.rone.dev/api/docs")
        self.assertIn("educational, analytical, and community", mlbb["source"]["rankings_commercial_use_status"])
        self.assertFalse(mlbb["player_lookup"])
        self.assertTrue(any(section["id"] == "draft" for section in mlbb["sections"]))
        self.assertTrue(mlbb["snapshot"]["loaded"])
        self.assertEqual(mlbb["snapshot"]["patch"], "2.1.90")
        self.assertEqual(len(next(section for section in mlbb["sections"] if section["id"] == "hero-tiers")["heroes"]), 134)
        self.assertEqual(len(next(section for section in mlbb["sections"] if section["id"] == "items")["items"]), 102)
        self.assertEqual(mlbb["snapshot"]["rankings_measured_at"], "2026-09-21T10:00:42.336Z")
        self.assertIn(mlbb["rankings_freshness"]["status"], {"fresh", "stale", "unknown", "future_dated"})
        self.assertEqual(mlbb["rankings_freshness"]["last_measured_at"], "2026-09-21T10:00:42.336Z")
        self.assertEqual(mlbb["rankings_freshness"]["source_reported_measured_date"], "2026-09-21")
        self.assertEqual(
            mlbb["rankings_freshness"]["last_successful_snapshot_capture_at"],
            mlbb["snapshot"]["captured_at"],
        )
        self.assertEqual(mlbb["rankings_freshness"]["freshness_window_days"], 7)

        with urlopen(self.base_url + "/api/v1/strategy-guides/pubg-mobile-myanmar") as response:
            myanmar = json.loads(response.read())
        self.assertEqual(myanmar["locale"], "my-MM")
        self.assertEqual(myanmar["status"], "offline_reference")

        with urlopen(self.base_url + "/api/v1/strategy-guides/free-fire-myanmar") as response:
            free_fire = json.loads(response.read())
        self.assertEqual(free_fire["game"], "Garena Free Fire")
        self.assertEqual(free_fire["freshness"], "official_patch_snapshot")
        self.assertEqual(free_fire["patch_version"], "OB55")
        self.assertEqual(free_fire["patch_changes_effective_on"], "2026-10-01")
        self.assertIn(free_fire["patch_status"], {"scheduled_unverified", "verification_required"})
        self.assertEqual(free_fire["patch_verification_required"], free_fire["patch_status"] != "scheduled_unverified")
        self.assertFalse(free_fire["player_lookup"])
        self.assertEqual(len(free_fire["sections"]), 6)
        self.assertEqual(free_fire["sections"][-1]["id"], "patch-watch")

        for path, code in (
            ("/api/v1/strategy-guides?customer_id=private", 400),
            ("/api/v1/strategy-guides/mlbb?refresh=true", 400),
            ("/api/v1/strategy-guides/not-real", 404),
        ):
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                urlopen(self.base_url + path)
            self.assertEqual(error.exception.code, code)
            error.exception.close()

    def test_product_detail_and_not_found(self) -> None:
        with urlopen(self.base_url + "/api/v1/catalog/mobile-legends") as response:
            detail = json.loads(response.read())
        self.assertEqual(detail["offers"], [])
        self.assertFalse(detail["checkout_enabled"])
        self.assertEqual(detail["product"]["name"], "Mobile Legends")
        self.assertEqual(detail["regions"], [])
        self.assertNotIn("reference_snapshot", detail)
        self.assertNotIn("supplier_preview", detail)

        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/api/v1/catalog/not-listed")
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_live_offer_contract_is_separate_and_empty_until_approval(self) -> None:
        with urlopen(
            self.base_url + "/api/v1/catalog/mobile-legends/offers?region=sea"
        ) as response:
            payload = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["requested_region"], "sea")
        self.assertEqual(payload["offers"], [])
        self.assertEqual(payload["regions"], [])
        self.assertFalse(payload["checkout_enabled"])
        self.assertNotIn("reference_snapshot", payload)

        for path in (
            "/api/v1/catalog/mobile-legends/offers?region=a&region=b",
            "/api/v1/catalog/mobile-legends/offers?source=supplier",
            "/api/v1/catalog/not-listed/offers",
        ):
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                urlopen(self.base_url + path)
            self.assertEqual(error.exception.code, 400 if "not-listed" not in path else 404)
            error.exception.close()

    def test_catalog_offset_pagination(self) -> None:
        with urlopen(self.base_url + "/api/v1/catalog?limit=50&offset=50") as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["result_count"], 15)
        self.assertEqual(payload["total_count"], 65)
        self.assertIsNone(payload["next_offset"])

    def test_resource_oriented_catalog_aliases_match_preview_contract(self) -> None:
        with urlopen(self.base_url + "/api/v1/catalog/categories") as response:
            categories = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertFalse(categories["checkout_enabled"])
        self.assertIn({"id": "mobile-games", "name": "Mobile Games"}, categories["categories"])

        with urlopen(self.base_url + "/api/v1/catalog/products?search=pubg&cursor=0&limit=1") as response:
            products = json.loads(response.read())
        self.assertEqual([item["slug"] for item in products["products"]], ["pubg-mobile"])
        self.assertIsNone(products["next_cursor"])
        self.assertEqual(products["next_offset"], None)

        with urlopen(self.base_url + "/api/v1/catalog/products/pubg-mobile") as response:
            detail = json.loads(response.read())
        self.assertEqual(detail["product"]["slug"], "pubg-mobile")
        self.assertEqual(detail["regions"], [])
        self.assertEqual(detail["offers"], [])
        self.assertNotIn("reference_snapshot", detail)
        self.assertNotIn("supplier_preview", detail)

        with urlopen(self.base_url + "/api/v1/catalog/products/mobile-legends/offers?region=sea") as response:
            offers = json.loads(response.read())
        self.assertEqual(offers["requested_region"], "sea")
        self.assertEqual(offers["offers"], [])

        for path in (
            "/api/v1/catalog/categories?search=pubg",
            "/api/v1/catalog/products?q=mlbb&search=pubg",
            "/api/v1/catalog/products?cursor=0&offset=1",
            "/api/v1/catalog/products/mobile-legends?region=sea",
        ):
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                urlopen(self.base_url + path)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()

    def test_head_does_not_return_a_static_body(self) -> None:
        request = Request(self.base_url + "/game-store/", method="HEAD")
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")

    def test_frontend_assets_are_served_from_web_root(self) -> None:
        with urlopen(self.base_url + "/game-store/") as response:
            body = response.read().decode()
            csp = response.headers["Content-Security-Policy"]
        self.assertIn("AuriX Game Store", body)
        self.assertIn("checkout locked", body)
        self.assertIn("YOUR GAME SPACE", body)
        self.assertIn("WALLET · REFERENCE PREVIEW", body)
        self.assertIn("Check the player name and region before reviewing the exact total and receiver instructions", body)
        self.assertIn("img-src 'self' data:", csp)
        self.assertNotIn("wikimedia.org", csp)
        self.assertNotIn("img-src *", csp)


class GameStoreAdminWebhookAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.admin = _WebhookAdminStub()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(admin_service=cls.admin))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_webhook_admin_route_auth_status_and_configuration(self) -> None:
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/api/v1/admin/supplier-webhook")
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

    def test_staff_session_forwards_put_and_delete_to_protected_admin_routes(self) -> None:
        from datetime import datetime, timezone
        from aurix_games.customer_api import CustomerIdentityService, CustomerIdentityStore, SESSION_COOKIE_NAME

        with tempfile.TemporaryDirectory() as directory:
            identities = CustomerIdentityStore(Path(directory) / "identities.sqlite3")
            now = datetime.now(timezone.utc)
            owner = identities.customer_for("google", "staff-owner", now=now)
            identities.link(owner, "telegram", "staff-owner-telegram", now=now)
            identity = CustomerIdentityService("staff-session-test-secret-long-enough-012345", identities,
                                               cookie_secure=False)
            session, _ = identity._issue(owner, now)
            with patch.dict(os.environ, {"AURIX_GAMES_SUPER_ADMIN_CUSTOMER_ID": owner}):
                server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
                    admin_service=self.admin, customer_identity_service=identity))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            origin = f"http://127.0.0.1:{server.server_port}"
            headers = {"Origin": origin, "Cookie": f"{SESSION_COOKIE_NAME}={session}",
                       "Content-Type": "application/json"}
            policy = {"enabled_families": ["topups"], "hidden_categories": [], "hidden_offers": []}
            request = Request(origin + "/api/v1/store-staff/admin/catalog-policy", data=b"{}",
                              method="PUT", headers={"Origin": origin, "Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as error:
                urlopen(request)
            self.assertEqual(error.exception.code, 401)
            error.exception.close()

            request = Request(origin + "/api/v1/store-staff/admin/catalog-policy",
                              data=json.dumps({"policy": policy, "expected_revision": 0}).encode(),
                              method="PUT", headers=headers)
            with urlopen(request) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["policy"], policy)
            self.assertEqual(self.admin.catalog_policy, {"policy": policy, "expected_revision": 0})

            body = json.dumps({"url": "https://store.example.test/api/v1/webhooks/fazercards",
                               "enabled": False}).encode()
            request = Request(origin + "/api/v1/store-staff/admin/supplier-webhook", data=body,
                              method="PUT", headers=headers)
            try:
                response = urlopen(request)
            except HTTPError as error:
                self.fail(f"staff PUT returned {error.code}: {error.read().decode()}")
            with response:
                configured = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertFalse(configured["webhook"]["enabled"])
            self.assertEqual(self.admin.configured["url"], "https://store.example.test/api/v1/webhooks/fazercards")

            request = Request(origin + "/api/v1/store-staff/admin/supplier-webhook", method="DELETE",
                              headers={"Origin": origin, "Cookie": f"{SESSION_COOKIE_NAME}={session}"})
            with urlopen(request) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(json.loads(response.read())["write_applied"])
            self.assertTrue(self.admin.deleted)


    def test_mapping_review_route_is_auth_protected_and_read_only(self) -> None:
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/api/v1/admin/mapping-review")
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

        request = Request(
            self.base_url + "/api/v1/admin/mapping-review",
            headers={"Authorization": "Bearer admin-secret"},
        )
        with urlopen(request) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["mapping_review"]["reason"], "mapping_config_missing")

        request = Request(
            self.base_url + "/api/v1/admin/mapping-review?publish=true",
            headers={"Authorization": "Bearer admin-secret"},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_mapping_history_route_is_auth_protected_and_read_only(self) -> None:
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/api/v1/admin/mapping-history")
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

        request = Request(
            self.base_url + "/api/v1/admin/mapping-history",
            headers={"Authorization": "Bearer admin-secret"},
        )
        with urlopen(request) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["mapping_history"]["status"], "empty")
        self.assertFalse(payload["mapping_history"]["writes"])

        request = Request(
            self.base_url + "/api/v1/admin/mapping-history?limit=1",
            headers={"Authorization": "Bearer admin-secret"},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_catalog_inventory_route_returns_exact_rows_only_to_admin(self) -> None:
        request = Request(
            self.base_url + "/api/v1/admin/catalog-inventory?family=topups&q=d55&limit=1",
            headers={"Authorization": "Bearer admin-secret"},
        )
        with urlopen(request) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["items"][0]["sku"], "d55")
        self.assertFalse(payload["customer_visible"])

        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/api/v1/admin/catalog-inventory?family=topups")
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

        request = Request(
            self.base_url + "/api/v1/admin/supplier-webhook",
            headers={"Authorization": "Bearer admin-secret"},
        )
        with urlopen(request) as response:
            status = json.loads(response.read())
        self.assertEqual(status["webhook"]["status"], "active")

        body = json.dumps({"url": "https://store.example.test/api/v1/webhooks/fazercards", "enabled": False}).encode()
        request = Request(
            self.base_url + "/api/v1/admin/supplier-webhook",
            data=body,
            method="PUT",
            headers={"Authorization": "Bearer admin-secret", "Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            configured = json.loads(response.read())
        self.assertFalse(configured["webhook"]["enabled"])
        self.assertEqual(self.admin.configured["url"], "https://store.example.test/api/v1/webhooks/fazercards")

        request = Request(
            self.base_url + "/api/v1/admin/supplier-webhook",
            method="DELETE",
            headers={"Authorization": "Bearer admin-secret"},
        )
        with urlopen(request) as response:
            deleted = json.loads(response.read())
        self.assertTrue(deleted["write_applied"])
        self.assertTrue(self.admin.deleted)

        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/api/v1/admin/supplier-webhook?secret=1")
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_mapping_approval_route_requires_auth_and_has_no_query_inputs(self) -> None:
        body = json.dumps({
            "family": "topups", "category_id": "mlbb", "sku": "d55",
            "offer_id": "mobile-legends-d55", "confirm_approval": True,
        }).encode()
        request = Request(
            self.base_url + "/api/v1/admin/mapping-approval",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

        request = Request(
            self.base_url + "/api/v1/admin/mapping-approval?confirm=true",
            data=body,
            method="POST",
            headers={"Authorization": "Bearer admin-secret", "Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

        request = Request(
            self.base_url + "/api/v1/admin/mapping-approval",
            data=body,
            method="POST",
            headers={"Authorization": "Bearer admin-secret", "Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            payload = json.loads(response.read())
            self.assertEqual(response.status, 201)
        self.assertTrue(payload["mapping_approval"]["local_config_updated"])
        self.assertFalse(payload["mapping_approval"]["supplier_write"])
        self.assertFalse(payload["mapping_approval"]["customer_publication"])
        self.assertTrue(self.admin.approved_mapping["confirm_approval"])

class GameOrderAttentionAPITests(unittest.TestCase):
    def test_integrity_requires_auth_and_fails_closed_when_store_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            admin = GameStoreAdminService(admin_token='admin-secret', supplier_client=None,
                wallet_alert_store=SupplierWalletAlertStore(root / 'alerts.db'),
                integrity_paths=(root / 'missing-orders', root / 'missing-ledger', root / 'missing-tasks'))
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(admin_service=admin))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            endpoint = f'http://127.0.0.1:{server.server_port}/api/v1/admin/money-integrity'
            try:
                for suffix, headers, expected in [('', {}, 401), ('?path=private', {'Authorization':'Bearer admin-secret'},400),
                        ('', {'Authorization':'Bearer admin-secret'},503)]:
                    with self.subTest(expected=expected), self.assertRaises(HTTPError) as error:
                        urlopen(Request(endpoint + suffix, headers=headers))
                    self.assertEqual(error.exception.code, expected)
                    self.assertNotIn(str(root), error.exception.read().decode())
                    error.exception.close()
                self.assertFalse((root / 'missing-orders').exists())
                with patch.object(admin, 'money_integrity', return_value={'status':'empty', 'read_only':True}):
                    with urlopen(Request(endpoint, headers={'Authorization':'Bearer admin-secret'})) as response:
                        self.assertEqual(json.load(response)['status'], 'empty')
                        self.assertEqual(response.headers['Cache-Control'], 'no-store')
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_attention_queue_requires_admin_and_returns_sanitized_unresolved_orders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = GameOrderRepository(Path(directory) / "orders.sqlite3")
            now = datetime.now(timezone.utc)
            order = create_order(order_id="order-unknown", customer_id="private-customer",
                customer_idempotency_key="private-key", snapshot=OrderSnapshot(
                    offer_id="private-offer", product_slug="mobile-legends", fulfillment_kind="direct_topup",
                    amount_minor=4500, currency="MMK", recipient_fingerprint="private-fingerprint"))
            repository.create(order, created_at=now)
            for offset, state in enumerate((OrderState.AWAITING_PAYMENT, OrderState.PAID_RESERVED,
                OrderState.FULFILLMENT_QUEUED, OrderState.SUBMITTED, OrderState.UNKNOWN), start=1):
                repository.transition(order.order_id, state, at=now + timedelta(seconds=offset), reason="private reason")
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
                admin_service=_WebhookAdminStub(), customer_order_service=CustomerOrderService(repository)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + "/api/v1/admin/game-orders/attention")
                self.assertEqual(error.exception.code, 401)
                error.exception.close()
                request = Request(base + "/api/v1/admin/game-orders/attention",
                    headers={"Authorization": "Bearer admin-secret"})
                with urlopen(request) as response:
                    body = response.read().decode()
                    payload = json.loads(body)
                self.assertEqual(payload["orders"][0]["order_id"], "order-unknown")
                self.assertEqual(payload["orders"][0]["state"], "unknown")
                self.assertFalse(payload["orders"][0]["reconcile_available"])
                self.assertEqual(payload["orders"][0]["reconcile_unavailable_reason"], "supplier_attempt_missing")
                for private in ("private-customer", "private-key", "private-offer", "private-fingerprint", "private reason"):
                    self.assertNotIn(private, body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class SupplierFundingReconciliationAPITests(unittest.TestCase):
    def test_unknown_attempt_requires_admin_and_provider_absence_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WalletFundingIntentStore(root / "funding.sqlite3")
            store.claim_creation("fund-lost", "usdt", Decimal("50"))
            admin = GameStoreAdminService(
                admin_token="admin-secret",
                supplier_client=None,
                wallet_alert_store=SupplierWalletAlertStore(root / "alerts.sqlite3"),
                funding_intent_store=store,
                treasury_reviewers={"staff:alice": hashlib.sha256(b"t" * 32).hexdigest()},
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(admin_service=admin))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            collection = base + "/api/v1/admin/supplier-wallet/funding-attempts"
            action = collection + "/fund-lost/resolve-absent"
            headers = {"Authorization": "Bearer admin-secret", "Content-Type": "application/json", "X-Aurix-Treasury-Token": "t" * 32}
            try:
                with self.assertRaises(HTTPError) as error:
                    urlopen(collection)
                self.assertEqual(error.exception.code, 401)
                error.exception.close()
                request = Request(collection, headers={"Authorization": "Bearer admin-secret"})
                with urlopen(request) as response:
                    queued = json.loads(response.read())
                self.assertTrue(queued["invoice_creation_blocked"])
                self.assertEqual(queued["unresolved_attempts"][0]["intent_id"], "fund-lost")
                unauthorized = Request(action, data=json.dumps({"provider_reference": "case", "confirmed_absent": True}).encode(),
                    method="POST", headers={"Authorization": "Bearer admin-secret", "Content-Type": "application/json"})
                with self.assertRaises(HTTPError) as error:
                    urlopen(unauthorized)
                self.assertEqual(error.exception.code, 401)
                error.exception.close()
                found = Request(collection + "/fund-lost/link-invoice", data=json.dumps({
                    "payment_id": "tx1", "provider_reference": "case", "confirmed_match": True}).encode(), method="POST", headers=headers)
                with self.assertRaises(HTTPError) as error:
                    urlopen(found)
                self.assertEqual(error.exception.code, 400)  # No supplier read client; the attempt remains fenced.
                error.exception.close()

                request = Request(action, data=json.dumps({
                    "provider_reference": "ticket-17", "confirmed_absent": False,
                }).encode(), method="POST", headers=headers)
                with self.assertRaises(HTTPError) as error:
                    urlopen(request)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
                self.assertEqual(len(store.unresolved_attempts()), 1)

                request = Request(action, data=json.dumps({
                    "provider_reference": "ticket-17", "confirmed_absent": True,
                }).encode(), method="POST", headers=headers)
                with urlopen(request) as response:
                    resolved = json.loads(response.read())
                self.assertEqual(resolved["resolution"]["status"], "confirmed_absent")
                self.assertFalse(resolved["invoice_creation_blocked"])
                self.assertEqual(store.unresolved_attempts(), ())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_operator_reconcile_is_get_only_authenticated_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = GameOrderRepository(Path(directory) / "orders.sqlite3")
            now = datetime.now(timezone.utc)
            order = create_order(order_id="order-pending", customer_id="private-customer",
                customer_idempotency_key="private-key", snapshot=OrderSnapshot(
                    offer_id="private-offer", product_slug="mobile-legends", fulfillment_kind="direct_topup",
                    amount_minor=4500, currency="MMK", recipient_fingerprint="private-fingerprint"))
            repository.create(order, created_at=now)
            for state in (OrderState.AWAITING_PAYMENT, OrderState.PAID_RESERVED,
                          OrderState.FULFILLMENT_QUEUED):
                repository.transition(order.order_id, state, at=now, reason="test")
            repository.transition(order.order_id, OrderState.SUBMITTED, at=now, reason="accepted",
                provider_order_id="ord-12")

            class SupplierReadStub:
                calls = []

                def get_order(self, provider_order_id):
                    self.calls.append(provider_order_id)
                    return {"ok": True, "order": {"id": provider_order_id, "status": "completed"}}

            supplier = SupplierReadStub()
            admin = _WebhookAdminStub()
            admin.supplier_client = supplier
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
                admin_service=admin, customer_order_service=CustomerOrderService(repository)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            route = base + "/api/v1/admin/game-orders/order-pending/reconcile"
            headers = {"Content-Type": "application/json"}
            try:
                unauthorized = Request(route, data=b"{}", method="POST", headers=headers)
                with self.assertRaises(HTTPError) as error:
                    urlopen(unauthorized)
                self.assertEqual(error.exception.code, 401)
                error.exception.close()
                self.assertEqual(supplier.calls, [])

                request = Request(route, data=b"{}", method="POST", headers={
                    **headers, "Authorization": "Bearer admin-secret"})
                with urlopen(request) as response:
                    payload = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertFalse(payload["supplier_writes"])
                self.assertTrue(payload["outcome"]["applied"])
                self.assertEqual(payload["outcome"]["state"], "delivered")
                self.assertEqual(supplier.calls, ["ord-12"])
                self.assertEqual(repository.get(order.order_id).state, OrderState.DELIVERED)

                request = Request(route, data=b"{}", method="POST", headers={
                    **headers, "Authorization": "Bearer admin-secret"})
                with urlopen(request) as response:
                    replay = json.loads(response.read())
                self.assertTrue(replay["outcome"]["duplicate"])
                self.assertEqual(supplier.calls, ["ord-12", "ord-12"])

                query_request = Request(route + "?force=true", data=b"{}", method="POST", headers={
                    **headers, "Authorization": "Bearer admin-secret"})
                with self.assertRaises(HTTPError) as error:
                    urlopen(query_request)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
                self.assertEqual(supplier.calls, ["ord-12", "ord-12"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class CustomerPaymentQrAPITests(unittest.TestCase):
    def test_payment_qr_is_owner_scoped_and_uses_the_pinned_private_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            qr_dir = root / "private-qr"
            qr_dir.mkdir(mode=0o700)
            qr_bytes = b"\x89PNG\r\n\x1a\nowner-qr"
            (qr_dir / "kpay-r1.png").write_bytes(qr_bytes)
            (qr_dir / "kpay-r1.png").chmod(0o600)
            (root / "outside.png").write_bytes(qr_bytes)
            (qr_dir / "bad.png").write_bytes(b"not an image")
            (qr_dir / "bad.png").chmod(0o600)
            repository = GameOrderRepository(root / "orders.sqlite3")
            now = datetime.now(timezone.utc)
            order = create_order(order_id="order-qr", customer_id="customer-1",
                customer_idempotency_key="key-qr", snapshot=OrderSnapshot(
                    offer_id="offer-1", product_slug="mobile-legends", fulfillment_kind="direct_topup",
                    amount_minor=4500, currency="MMK", recipient_fingerprint="opaque-fp"))
            repository.create(order, created_at=now)
            repository.transition(order.order_id, OrderState.AWAITING_PAYMENT, at=now, reason="await payment")
            repository.freeze_payment_intent(order_id=order.order_id, customer_id="customer-1", rail="kpay",
                receiver_id="merchant-1", receiver_revision="r1", opened_at=now,
                expires_at=now + timedelta(minutes=10), instructions={
                    "account": "See QR", "account_name": "AuriX Shop", "qr_asset": "kpay-r1.png",
                    "qr_sha256": hashlib.sha256(qr_bytes).hexdigest()})
            expired = create_order(order_id="order-expired-qr", customer_id="customer-1",
                customer_idempotency_key="key-expired-qr", snapshot=OrderSnapshot(
                    offer_id="offer-1", product_slug="mobile-legends", fulfillment_kind="direct_topup",
                    amount_minor=4500, currency="MMK", recipient_fingerprint="expired-fp"))
            repository.create(expired, created_at=now)
            repository.transition(expired.order_id, OrderState.AWAITING_PAYMENT, at=now, reason="await payment")
            repository.freeze_payment_intent(order_id=expired.order_id, customer_id="customer-1", rail="kpay",
                receiver_id="merchant-1", receiver_revision="r1", opened_at=now - timedelta(minutes=2),
                expires_at=now - timedelta(seconds=1), instructions={
                    "account": "See QR", "account_name": "AuriX Shop", "qr_asset": "kpay-r1.png",
                    "qr_sha256": hashlib.sha256(qr_bytes).hexdigest()})
            verifier = SignedCustomerTokenVerifier("customer-secret-012345678901234567890123")
            service = CustomerOrderService(repository)
            journey = CheckoutJourney(None, None, receiver_qr_dir=qr_dir)
            self.assertIsNone(journey.read_receiver_qr("../outside.png"))
            self.assertIsNone(journey.read_receiver_qr("bad.png"))
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
                customer_order_service=service, customer_auth=verifier, receiver_qr_dir=qr_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"

            def token(customer_id: str) -> str:
                return issue_test_token(customer_id, "customer-secret-012345678901234567890123",
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

            try:
                url = base + "/api/v1/orders/order-qr/payment-qr"
                with self.assertRaises(HTTPError) as failure:
                    urlopen(url)
                self.assertEqual(failure.exception.code, 401)
                failure.exception.close()

                request = Request(url, headers={"Authorization": "Bearer " + token("customer-1")})
                with urlopen(request) as response:
                    self.assertEqual(response.headers.get_content_type(), "image/png")
                    self.assertEqual(response.headers.get("Cache-Control"), "private, no-store")
                    self.assertEqual(response.read(), qr_bytes)

                foreign = Request(url, headers={"Authorization": "Bearer " + token("customer-2")})
                with self.assertRaises(HTTPError) as failure:
                    urlopen(foreign)
                self.assertEqual(failure.exception.code, 404)
                failure.exception.close()

                expired = Request(base + "/api/v1/orders/order-expired-qr/payment-qr",
                    headers={"Authorization": "Bearer " + token("customer-1")})
                with self.assertRaises(HTTPError) as failure:
                    urlopen(expired)
                self.assertEqual(failure.exception.code, 404)
                failure.exception.close()

                (qr_dir / "kpay-r1.png").write_bytes(b"\x89PNG\r\n\x1a\nchanged-qr")
                (qr_dir / "kpay-r1.png").chmod(0o600)
                with self.assertRaises(HTTPError) as failure:
                    urlopen(request)
                self.assertEqual(failure.exception.code, 404)
                failure.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
