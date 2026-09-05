import tempfile
import unittest
import uuid
from pathlib import Path

from commerce_repositories import CommerceDatabase
from connectivity_registry import ConnectivityRegistry
from identity import IdentityError, IdentityService


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
        self.assertNotEqual(account_id, "123")
        self.assertEqual(str(uuid.UUID(account_id)), account_id)
        self.assertNotEqual(account_id, self.identity.ensure_account(456))
        self.assertEqual(self.identity.ensure_account(123), account_id)
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

    def test_reissued_credential_gets_a_new_generation_after_revocation(self):
        entitlement_id = self.identity.ensure_key_entitlement(
            123,
            server_id="sg-a",
            local_key_ref="key-1",
            kind="free",
            quota_bytes=1000,
            expires_at="2026-09-06T00:00:00+00:00",
            now="2026-09-05T00:00:00+00:00",
        )
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO connectivity_profiles
                   (profile_id, telegram_id, profile_kind, status, created_at, updated_at)
                   VALUES ('profile-test', 123, 'free', 'active', ?, ?)""",
                ("2026-09-05T00:00:00+00:00", "2026-09-05T00:00:00+00:00"),
            )
            connection.execute(
                """INSERT INTO connectivity_credentials
                   (credential_id, profile_id, endpoint_id, transport_id, external_id,
                    status, created_at)
                   VALUES ('credential-test', 'profile-test', ?, 'outline', 'key-1', 'active', ?)""",
                (self.endpoint_id, "2026-09-05T00:00:00+00:00"),
            )
        first = self.identity.ensure_generation_for_credential(
            entitlement_id,
            self.endpoint_id,
            credential_id="credential-test",
            now="2026-09-05T00:00:00+00:00",
        )
        self.assertTrue(self.identity.revoke_generation(first, now="2026-09-05T00:01:00+00:00"))
        second = self.identity.ensure_generation_for_credential(
            entitlement_id,
            self.endpoint_id,
            credential_id="credential-test",
            now="2026-09-05T00:02:00+00:00",
        )
        self.assertNotEqual(first, second)
        with self.database.connect() as connection:
            generations = connection.execute(
                "SELECT generation_no, status FROM credential_generations WHERE entitlement_id = ? ORDER BY generation_no",
                (entitlement_id,),
            ).fetchall()
        self.assertEqual([(row["generation_no"], row["status"]) for row in generations], [(1, "revoked"), (2, "active")])


if __name__ == "__main__":
    unittest.main()
