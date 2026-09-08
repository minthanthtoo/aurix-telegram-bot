import time
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from test_telegram_web_app import _init_data
from vpn_web_api import AuriXVpnWebApplication


class _Outline:
    def transfer_metrics(self):
        return {}

    def list_keys(self):
        return {"accessKeys": []}


class _Claims:
    outline = _Outline()
    connectivity = None

    def giveaway_status(self, telegram_id):
        return {"exists": False, "winner": False, "access_lock_active": False}

    def user_usage(self, telegram_id, usage_by_key, access_by_key):
        return [{
            "outline_key_id": "free-key",
            "endpoint_id": "SGP-01",
            "key_type": "free",
            "tier": "Daily Free 300 MiB",
            "used_bytes": 10,
            "quota_bytes": 300,
            "remaining_bytes": 290,
            "usage_observed": True,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "status": "active",
            "access_url": "ss://private-key",
            "created_at": "2026-01-01T00:00:00+00:00",
        }]

    def claim(self, telegram_id, first_name, username=None):
        return SimpleNamespace(
            access_url="ss://daily",
            expires_at=datetime.now(UTC) + timedelta(days=1),
            next_claim_at=None,
            denied_reason=None,
        )

    def claim_trial(self, telegram_id, first_name, username=None):
        return SimpleNamespace(
            access_url=None,
            expires_at=None,
            next_claim_at=datetime.now(UTC) + timedelta(days=30),
            denied_reason=None,
        )

    def claim_giveaway(self, telegram_id, first_name, username=None, code=None):
        return SimpleNamespace(
            outcome="won",
            code=code,
            quota_bytes=100_000_000_000,
            duration_days=30,
            expires_at=datetime.now(UTC) + timedelta(days=30),
            winner_number=1,
            remaining_slots=4,
            reason=None,
        )


class _Commerce:
    PAYMENT_PROVIDERS = {"kpay": "KBZPay"}

    def user_usage(self, telegram_id, usage_by_key):
        return []

    def user_vpns(self, telegram_id):
        return []

    def list_user_orders(self, telegram_id, limit=20):
        return []

    def plans(self):
        return [SimpleNamespace(code="basic", name="Basic", price_minor=3000, currency="MMK", quota_bytes=50, duration_days=30)]

    def user_vpns(self, telegram_id):
        return []


class _Registry:
    def list_customer_endpoints(self, plan_code=None):
        return [{
            "id": "sgp-02",
            "code": "SGP-02",
            "region": "sgp1",
            "state": "ACTIVE",
            "healthy": True,
            "eligible": True,
            "management_latency_ms": 12.4,
            "public_address": "198.51.100.10",
            "management_url_ciphertext": "must-not-leak",
        }]


class VpnWebApplicationTest(unittest.TestCase):
    def setUp(self):
        self.runtime = SimpleNamespace(token="bot-token", claim_service=_Claims(), commerce=_Commerce())
        self.application = AuriXVpnWebApplication(self.runtime)

    def test_dashboard_is_bound_to_verified_telegram_identity(self):
        user = self.application.authenticate(_init_data("bot-token", auth_date=int(time.time())))
        dashboard = self.application.dashboard(user)
        self.assertEqual(dashboard["user"]["telegram_id"], 12345)
        self.assertEqual(dashboard["keys"][0]["access_url"], "ss://private-key")

    def test_invalid_identity_never_reaches_dashboard(self):
        with self.assertRaises(Exception):
            self.application.authenticate(_init_data("wrong-token"))

    def test_catalog_contains_no_payment_secret(self):
        catalog = self.application.plans_payload()
        self.assertEqual(catalog["plans"][0]["code"], "basic")
        self.assertNotIn("QR", str(catalog))

    def test_server_directory_contains_no_infrastructure_secrets(self):
        self.application.runtime.connectivity = _Registry()
        payload = self.application.servers_payload("basic")
        self.assertEqual(payload["servers"][0]["code"], "SGP-02")
        self.assertNotIn("public_address", str(payload))
        self.assertNotIn("management_url", str(payload))

    def test_text_payment_is_disabled_without_explicit_legacy_flag(self):
        user = self.application.authenticate(_init_data("bot-token", auth_date=int(time.time())))
        with self.assertRaisesRegex(Exception, "Telegram receipt flow"):
            self.application.submit_payment(user, "order-12345678", "kpay", "ref")

    def test_authenticated_claims_return_state_without_exposing_access_url(self):
        user = self.application.authenticate(_init_data("bot-token", auth_date=int(time.time())))

        daily = self.application.claim_daily(user)
        trial = self.application.claim_trial(user)
        promo = self.application.claim_promo(user, "launch_100")

        self.assertTrue(daily["issued"])
        self.assertNotIn("access_url", daily)
        self.assertFalse(trial["issued"])
        self.assertIsNotNone(trial["next_claim_at"])
        self.assertEqual(promo["outcome"], "won")
        self.assertEqual(promo["code"], "LAUNCH_100")

    def test_invalid_promo_code_is_rejected_before_service_call(self):
        user = self.application.authenticate(_init_data("bot-token", auth_date=int(time.time())))
        with self.assertRaisesRegex(Exception, "valid promo code"):
            self.application.claim_promo(user, "!!")

    def test_active_paid_access_blocks_regular_free_claim(self):
        self.runtime.commerce.user_vpns = lambda _telegram_id: [{
            "status": "active",
            "key_status": "active",
            "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        }]
        user = self.application.authenticate(_init_data("bot-token", auth_date=int(time.time())))
        with self.assertRaisesRegex(Exception, "paid VPN access is active"):
            self.application.claim_daily(user)


if __name__ == "__main__":
    unittest.main()
