from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import threading

from aurix_games.commerce_settings import CommerceSettingsStore, SettingsConflict, FLAGS
from aurix_games.web_api import create_server
from aurix_games.game_admin import GameAdminAuthError


class CommerceSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.env = patch.dict(os.environ, {"AURIX_GAMES_BASE_DIR": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = CommerceSettingsStore(self.root / "commerce-settings.json")
        self.settings = {**dict.fromkeys(FLAGS, True), "daily_limit_usd": None, "maximum_order_mmk": None}

    def test_concurrent_saves_preserve_one_revision_and_reject_stale_writer(self):
        def save(_):
            try:
                self.store.save(self.settings, expected_revision=0, actor="owner")
                return "saved"
            except SettingsConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(save, range(2))), ["saved", "conflict"])
        self.assertEqual(self.store.status()["revision"], 1)
        self.assertEqual(self.store.path.stat().st_mode & 0o077, 0)
        before = self.store.path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.save({**self.settings, "daily_limit_usd": "NaN"}, expected_revision=1, actor="owner")
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_limit_and_supplier_control_apply_without_restart(self):
        from aurix_games.providers.fazercards_commands import FazerCardsCommandClient, SupplierWriteDisabledError
        self.store.save({**self.settings, "supplier_orders_enabled": False},
                        expected_revision=0, actor="owner")
        client = FazerCardsCommandClient("test-key", allow_writes=True)
        with self.assertRaises(SupplierWriteDisabledError):
            client.order_topup(category_id="test", offer_id="test", fields={"player_id": "1"}, idempotency_key="test-once")

    def test_admin_auth_and_revision_conflict_and_staff_auth(self):
        class Admin:
            def authenticate(self, authorization):
                if authorization != "Bearer admin-test":
                    raise GameAdminAuthError("Unauthorized")
        server = create_server("127.0.0.1", 0, admin_service=Admin())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        origin = f"http://127.0.0.1:{server.server_port}"
        path = origin + "/api/v1/admin/commerce-settings"
        with self.assertRaises(HTTPError) as denied:
            urlopen(path)
        self.assertEqual(denied.exception.code, 401)
        body = json.dumps({"settings": self.settings, "expected_revision": 0}).encode()
        request = Request(path, data=body, headers={"Authorization": "Bearer admin-test",
                          "Content-Type": "application/json"}, method="POST")
        with urlopen(request) as result:
            self.assertEqual(json.load(result)["revision"], 1)
        with self.assertRaises(HTTPError) as conflict:
            urlopen(request)
        self.assertEqual(conflict.exception.code, 409)
        with self.assertRaises(HTTPError) as staff:
            urlopen(origin + "/api/v1/store-staff/commerce-settings")
        self.assertEqual(staff.exception.code, 401)

    def test_order_limit_and_paused_payments_preserve_existing_instructions(self):
        from test_game_checkout_journey import CheckoutJourneyTests, NOW
        from aurix_games.customer_checkout import CheckoutInputError
        fixture = CheckoutJourneyTests()
        fixture.setUp()
        try:
            self.store.save({**self.settings, "maximum_order_mmk": 1000},
                            expected_revision=0, actor="owner")
            with self.assertRaisesRegex(CheckoutInputError, "order limit"):
                fixture.journey.review(fixture.principal, fixture.payload, now=NOW)
            self.store.save(self.settings, expected_revision=1, actor="owner")
            quote = fixture.journey.review(fixture.principal, fixture.payload, now=NOW)["quote"]
            order = fixture.journey.confirm(fixture.principal,
                {"quote_id": quote["quote_id"], "confirmed": True}, now=NOW)["order"]
            fixture.journey.payment_receivers = {"kpay": {"id": "receiver", "revision": "r1",
                "account": "test", "account_name": "test"}}
            request = {"order_id": order["order_id"], "rail": "kpay"}
            original = fixture.journey.payment(fixture.principal, request, now=NOW)
            self.store.save({**self.settings, "payments_enabled": False}, expected_revision=2, actor="owner")
            self.assertEqual(fixture.journey.payment(fixture.principal, request, now=NOW), original)
        finally:
            fixture.doCleanups()
