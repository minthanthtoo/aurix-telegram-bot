from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from aurix_games.customer_api import SignedCustomerTokenVerifier, issue_test_token
from aurix_games.order_repository import GameOrderRepository
from aurix_games.order_state import OrderSnapshot, OrderState, create_order
from aurix_games.store_features import StoreFeatureError, StoreFeatureStore
from aurix_games.web_api import make_handler


UTC = timezone.utc


class GameStoreLeaderboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.base.chmod(0o700)
        self.database = self.base / "game-orders.sqlite3"
        self.features = StoreFeatureStore(self.database)
        self.orders = GameOrderRepository(self.database)

    def _save_profile(self, customer_id: str, name: str, opted_in: bool, now: datetime) -> None:
        self.features.save_leaderboard_profile(
            customer_id, {"display_name": name, "opted_in": opted_in}, now=now,
        )

    def _deliver(self, customer_id: str, count: int, delivered_at: datetime) -> None:
        for index in range(count):
            order_id = f"order-{customer_id}-{index}"
            created_at = delivered_at - timedelta(seconds=8)
            order = create_order(
                order_id=order_id,
                customer_id=customer_id,
                customer_idempotency_key=f"intent-{customer_id}-{index}",
                snapshot=OrderSnapshot(
                    offer_id="mlbb-14", product_slug="mobile-legends",
                    fulfillment_kind="direct_topup", amount_minor=1300,
                    currency="MMK", recipient_fingerprint="private-fingerprint",
                ),
            )
            self.orders.create(order, created_at=created_at)
            for step, state in enumerate((
                OrderState.AWAITING_PAYMENT, OrderState.PAID_RESERVED,
                OrderState.FULFILLMENT_QUEUED, OrderState.SUBMITTED, OrderState.DELIVERED,
            ), 1):
                self.orders.transition(
                    order_id, state, at=created_at + timedelta(seconds=step),
                    reason="leaderboard fixture",
                    provider_order_id=f"provider-{order_id}" if state is OrderState.SUBMITTED else None,
                )

    def test_board_counts_only_opted_in_deliveries_and_shares_tied_ranks(self) -> None:
        now = datetime(2026, 11, 16, 12, tzinfo=UTC)
        self._save_profile("buyer-alpha", "Alpha", True, now)
        self._save_profile("buyer-bravo", "Bravo", True, now)
        self._save_profile("buyer-single", "Single", True, now)
        self._save_profile("buyer-opted-out", "Hidden", False, now)
        self._save_profile("buyer-old", "Old", True, now)
        self._save_profile("buyer-no-orders", "No orders", True, now)
        self._save_profile("buyer-future", "Future", True, now)
        self._deliver("buyer-alpha", 5, now - timedelta(days=1))
        self._deliver("buyer-bravo", 5, now - timedelta(days=1))
        self._deliver("buyer-single", 1, now - timedelta(days=1))
        self._deliver("buyer-opted-out", 7, now - timedelta(days=1))
        self._deliver("buyer-old", 2, now - timedelta(days=31))
        self._deliver("buyer-future", 3, now + timedelta(days=1))
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE game_orders SET updated_at=? WHERE customer_id='buyer-old'", (now.isoformat(),))

        payload = self.features.leaderboard(now=now)
        entries = payload["entries"]
        self.assertEqual(payload["period"], "rolling_30_days")
        self.assertEqual([(item["display_name"], item["rank"]) for item in entries], [
            ("Alpha", 1), ("Bravo", 1), ("Single", 3),
        ])
        self.assertEqual(entries[0]["achievement"], "Rising")
        self.assertEqual(entries[0]["next_achievement"], "Elite")
        self.assertEqual(entries[0]["orders_to_next"], 5)
        self.assertEqual(entries[2]["achievement"], "Starter")
        self.assertTrue(all("customer_id" not in entry and "amount" not in entry for entry in entries))

    def test_opted_in_names_are_unicode_normalized_and_unique(self) -> None:
        now = datetime(2026, 11, 16, tzinfo=UTC)
        self._save_profile("buyer-one", "Café", True, now)
        with self.assertRaisesRegex(StoreFeatureError, "already in use"):
            self._save_profile("buyer-two", "Cafe\u0301", True, now)
        profile = self.features.leaderboard_profile("buyer-one")
        self.assertEqual(profile, {"display_name": "Café", "opted_in": True})

    def test_api_keeps_profile_private_and_public_board_opt_in_only(self) -> None:
        now = datetime.now(UTC)
        verifier = SignedCustomerTokenVerifier(b"leaderboard-test-secret-32-bytes-minimum")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            customer_auth=verifier, store_features=self.features,
        ))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        token = issue_test_token(
            "buyer-api", b"leaderboard-test-secret-32-bytes-minimum",
            expires_at=now + timedelta(hours=1),
        )
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(base_url + "/api/v1/leaderboard/profile", timeout=5)
            self.assertEqual(error.exception.code, 401)
            error.exception.close()

            headers = {"Authorization": f"Bearer {token}"}
            with urlopen(Request(base_url + "/api/v1/leaderboard/profile", headers=headers), timeout=5) as response:
                self.assertEqual(json.load(response), {"display_name": "", "opted_in": False})

            headers.update({"Origin": base_url, "Content-Type": "application/json"})
            update = Request(
                base_url + "/api/v1/leaderboard/profile",
                data=json.dumps({"display_name": "GG Player", "opted_in": True}).encode(),
                headers=headers, method="PUT",
            )
            with urlopen(update, timeout=5) as response:
                self.assertEqual(json.load(response), {"display_name": "GG Player", "opted_in": True})

            with urlopen(base_url + "/api/v1/store/leaderboard", timeout=5) as response:
                public = json.load(response)
            self.assertEqual(public["entries"], [])
            self.assertNotIn("buyer-api", json.dumps(public))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
