import tempfile
import unittest
from pathlib import Path

from commerce_repositories import CommerceDatabase
from connectivity_registry import ConnectivityRegistry
from identity import IdentityError, IdentityService, account_id_for_telegram


class IdentityServiceTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = CommerceDatabase(Path(self.tempdir.name) / "identity.db")
        self.database.initialize()
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO users (telegram_id, first_name, created_at)
                   VALUES (123, 'Member', '2026-09-05T00:00:00+00:00')"""
            )
            connection.execute(
                """INSERT INTO outline_servers
                   (server_id, label, enabled, health_status, lifecycle_state, created_at, updated_at)
                   VALUES ('sg-a', 'Singapore A', 1, 'healthy', 'active', ?, ?)""",
                ("2026-09-05T00:00:00+00:00", "2026-09-05T00:00:00+00:00"),
            )
            endpoint = ConnectivityRegistry.sync_outline_endpoint(
                connection, server_id="sg-a", label="Singapore A", region="Singapore",
                health_status="healthy", now_text="2026-09-05T00:00:00+00:00",
            )
        self.endpoint_id = endpoint["endpoint_id"]
        self.identity = IdentityService(self.database)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_account_is_opaque_and_pairing_token_is_single_use(self):
        account_id = self.identity.ensure_account(123, now="2026-09-05T00:00:00+00:00")
        self.assertEqual(account_id, account_id_for_telegram(123))
        self.assertNotEqual(account_id, "123")
        token = self.identity.create_pairing_token(123, now="2026-09-05T00:00:00+00:00")
        enrolled = self.identity.consume_pairing_token(
            token, "device-public-key-123456", label="Phone",
            now="2026-09-05T00:01:00+00:00",
        )
        self.assertEqual(enrolled["account_id"], account_id)
        with self.assertRaisesRegex(IdentityError, "invalid, expired"):
            self.identity.consume_pairing_token(
                token, "another-public-key-123456", now="2026-09-05T00:01:01+00:00"
            )
        self.assertTrue(self.identity.revoke_device(123, enrolled["device_id"], now="2026-09-05T00:02:00+00:00"))
        self.assertFalse(self.identity.revoke_device(123, enrolled["device_id"], now="2026-09-05T00:02:01+00:00"))

    def test_entitlement_leases_cannot_reserve_or_consume_more_than_quota(self):
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, status, created_at)
                   VALUES ('order-1', 123, 'basic_50gb', 3000, 'MMK', 'approved', ?)""",
                ("2026-09-05T00:00:00+00:00",),
            )
            connection.execute(
                """INSERT INTO subscriptions
                   (id, order_id, telegram_id, plan_code, starts_at, expires_at, status)
                   VALUES ('sub-1', 'order-1', 123, 'basic_50gb', ?, ?, 'active')""",
                ("2026-09-05T00:00:00+00:00", "2026-09-06T00:00:00+00:00"),
            )
        entitlement_id = self.identity.ensure_subscription_entitlement(
            123, "sub-1", kind="paid", quota_bytes=1000,
            expires_at="2026-09-06T00:00:00+00:00", now="2026-09-05T00:00:00+00:00",
        )
        generation = self.identity.create_generation(
            entitlement_id, self.endpoint_id,
            now="2026-09-05T00:00:00+00:00",
        )
        self.assertTrue(self.identity.revoke_generation(generation, now="2026-09-05T00:00:01+00:00"))
        first = self.identity.grant_lease(
            entitlement_id, self.endpoint_id, lease_bytes=600, now="2026-09-05T00:00:01+00:00"
        )
        with self.assertRaisesRegex(IdentityError, "insufficient"):
            self.identity.grant_lease(
                entitlement_id, self.endpoint_id, lease_bytes=401,
                now="2026-09-05T00:00:02+00:00",
            )
        self.assertEqual(
            self.identity.record_lease_usage(first, 500, now="2026-09-05T00:00:03+00:00")["used_bytes"],
            500,
        )
        second = self.identity.grant_lease(
            entitlement_id, self.endpoint_id, lease_bytes=400, now="2026-09-05T00:00:04+00:00"
        )
        self.assertEqual(self.identity.record_lease_usage(first, 100, now="2026-09-05T00:00:05+00:00")["used_bytes"], 500)
        self.assertEqual(self.identity.record_lease_usage(second, 400, now="2026-09-05T00:00:06+00:00")["status"], "exhausted")


if __name__ == "__main__":
    unittest.main()
