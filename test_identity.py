import tempfile
import unittest
import uuid
from pathlib import Path

from commerce_repositories import CommerceDatabase
from connectivity_registry import ConnectivityRegistry
from identity import IdentityError, IdentityService
from route_failover import RouteFailoverService


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

    def test_turnover_is_endpoint_scoped_for_one_pooled_entitlement(self):
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, status, created_at)
                   VALUES ('order-multi-node', 123, 'basic_50gb', 3000, 'MMK', 'approved', ?)""",
                ("2026-09-05T00:00:00+00:00",),
            )
            connection.execute(
                """INSERT INTO subscriptions
                   (id, order_id, telegram_id, plan_code, starts_at, expires_at, status)
                   VALUES ('sub-multi-node', 'order-multi-node', 123, 'basic_50gb', ?, ?, 'active')""",
                ("2026-09-05T00:00:00+00:00", "2026-09-06T00:00:00+00:00"),
            )
        entitlement_id = self.identity.ensure_subscription_entitlement(
            123,
            "sub-multi-node",
            kind="paid",
            quota_bytes=50_000,
            expires_at="2026-09-06T00:00:00+00:00",
            now="2026-09-05T00:00:00+00:00",
        )
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO outline_servers
                   (server_id, label, enabled, health_status, lifecycle_state, created_at, updated_at)
                   VALUES ('sg-b', 'Singapore B', 1, 'healthy', 'active', ?, ?)""",
                ("2026-09-05T00:00:00+00:00", "2026-09-05T00:00:00+00:00"),
            )
            second_endpoint = ConnectivityRegistry.sync_outline_endpoint(
                connection,
                server_id="sg-b",
                label="Singapore B",
                region="Singapore",
                health_status="healthy",
                now_text="2026-09-05T00:00:00+00:00",
            )["endpoint_id"]
            ConnectivityRegistry.bind_credential(
                connection,
                telegram_id=123,
                server_id="sg-a",
                external_id="route-a-1",
                secret_ciphertext="cipher-a",
                now_text="2026-09-05T00:00:00+00:00",
                profile_kind="paid",
                subscription_id="sub-multi-node",
            )
            ConnectivityRegistry.bind_credential(
                connection,
                telegram_id=123,
                server_id="sg-b",
                external_id="route-b-1",
                secret_ciphertext="cipher-b",
                now_text="2026-09-05T00:00:00+00:00",
                profile_kind="paid",
                subscription_id="sub-multi-node",
            )
            credentials = {
                row["external_id"]: row["credential_id"]
                for row in connection.execute(
                    "SELECT credential_id, external_id FROM connectivity_credentials WHERE external_id IN ('route-a-1', 'route-b-1')"
                ).fetchall()
            }
        first = self.identity.ensure_generation_for_credential(
            entitlement_id,
            self.endpoint_id,
            credential_id=str(credentials["route-a-1"]),
            now="2026-09-05T00:01:00+00:00",
        )
        second = self.identity.ensure_generation_for_credential(
            entitlement_id,
            second_endpoint,
            credential_id=str(credentials["route-b-1"]),
            now="2026-09-05T00:02:00+00:00",
        )
        self.assertNotEqual(first, second)
        with self.database.connect() as connection:
            routes = connection.execute(
                "SELECT endpoint_id, status FROM credential_generations WHERE entitlement_id = ? ORDER BY endpoint_id",
                (entitlement_id,),
            ).fetchall()
        self.assertEqual(
            {(str(row["endpoint_id"]), str(row["status"])) for row in routes},
            {(self.endpoint_id, "active"), (second_endpoint, "active")},
        )

        with self.database.connect() as connection:
            ConnectivityRegistry.bind_credential(
                connection,
                telegram_id=123,
                server_id="sg-a",
                external_id="route-a-2",
                secret_ciphertext="cipher-a2",
                now_text="2026-09-05T00:03:00+00:00",
                profile_kind="paid",
                subscription_id="sub-multi-node",
            )
            replacement = connection.execute(
                "SELECT credential_id FROM connectivity_credentials WHERE external_id = 'route-a-2'"
            ).fetchone()
        replacement_generation = self.identity.ensure_generation_for_credential(
            entitlement_id,
            self.endpoint_id,
            credential_id=str(replacement["credential_id"]),
            now="2026-09-05T00:03:00+00:00",
        )
        self.assertNotEqual(first, replacement_generation)
        with self.database.connect() as connection:
            routes = connection.execute(
                "SELECT endpoint_id, status FROM credential_generations WHERE entitlement_id = ? ORDER BY generation_no",
                (entitlement_id,),
            ).fetchall()
        self.assertEqual(
            [(str(row["endpoint_id"]), str(row["status"])) for row in routes],
            [(self.endpoint_id, "revoked"), (second_endpoint, "active"), (self.endpoint_id, "active")],
        )

    def test_failover_policy_queues_after_threshold_and_dedupes_observations(self):
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, status, created_at)
                   VALUES ('order-failover', 123, 'basic_50gb', 3000, 'MMK', 'approved', ?)""",
                ("2026-09-05T00:00:00+00:00",),
            )
            connection.execute(
                """INSERT INTO subscriptions
                   (id, order_id, telegram_id, plan_code, starts_at, expires_at, status)
                   VALUES ('sub-failover', 'order-failover', 123, 'basic_50gb', ?, ?, 'active')""",
                ("2026-09-05T00:00:00+00:00", "2026-10-05T00:00:00+00:00"),
            )
            connection.execute(
                """INSERT INTO outline_servers
                   (server_id, label, enabled, health_status, lifecycle_state, created_at, updated_at)
                   VALUES ('sg-b', 'Singapore B', 1, 'healthy', 'active', ?, ?)""",
                ("2026-09-05T00:00:00+00:00", "2026-09-05T00:00:00+00:00"),
            )
            second_endpoint = ConnectivityRegistry.sync_outline_endpoint(
                connection,
                server_id="sg-b",
                label="Singapore B",
                region="Singapore",
                health_status="healthy",
                now_text="2026-09-05T00:00:00+00:00",
            )["endpoint_id"]
            ConnectivityRegistry.bind_credential(
                connection,
                telegram_id=123,
                server_id="sg-a",
                external_id="failover-source",
                secret_ciphertext="cipher-source",
                now_text="2026-09-05T00:00:00+00:00",
                profile_kind="paid",
                subscription_id="sub-failover",
            )
            ConnectivityRegistry.bind_credential(
                connection,
                telegram_id=123,
                server_id="sg-b",
                external_id="failover-target",
                secret_ciphertext="cipher-target",
                now_text="2026-09-05T00:00:00+00:00",
                profile_kind="paid",
                subscription_id="sub-failover",
            )
            credentials = {
                row["external_id"]: row["credential_id"]
                for row in connection.execute(
                    """SELECT credential_id, external_id FROM connectivity_credentials
                       WHERE external_id IN ('failover-source', 'failover-target')"""
                ).fetchall()
            }
        entitlement_id = self.identity.ensure_subscription_entitlement(
            123,
            "sub-failover",
            kind="paid",
            quota_bytes=50_000,
            expires_at="2026-10-05T00:00:00+00:00",
            now="2026-09-05T00:00:00+00:00",
        )
        source_generation = self.identity.ensure_generation_for_credential(
            entitlement_id,
            self.endpoint_id,
            credential_id=str(credentials["failover-source"]),
            now="2026-09-05T00:00:00+00:00",
        )
        failover = RouteFailoverService(self.database)
        failover.configure_policy(
            entitlement_id,
            enabled=True,
            failure_threshold=2,
            now="2026-09-05T00:00:00+00:00",
        )
        first = failover.observe(
            source_generation,
            outcome="failure",
            reason="timeout",
            observed_at="2026-09-05T00:01:00+00:00",
        )
        self.assertIsNone(first["decision_id"])
        second = failover.observe(
            source_generation,
            outcome="failure",
            reason="timeout",
            observed_at="2026-09-05T00:02:00+00:00",
        )
        self.assertIsNotNone(second["decision_id"])
        duplicate = failover.observe(
            source_generation,
            outcome="failure",
            reason="timeout",
            observed_at="2026-09-05T00:02:00+00:00",
        )
        self.assertTrue(duplicate["duplicate"])
        decisions = failover.decisions(entitlement_id=entitlement_id)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["target_route_id"], f"route-{second_endpoint}")

    def _managed_entitlement(self, quota=1000, external_id="outline-usage-1"):
        entitlement_id = self.identity.ensure_key_entitlement(
            123,
            server_id="sg-a",
            local_key_ref=f"local-{external_id}",
            kind="free",
            quota_bytes=quota,
            expires_at="2026-09-06T00:00:00+00:00",
            now="2026-09-05T00:00:00+00:00",
        )
        with self.database.connect() as connection:
            ConnectivityRegistry.bind_credential(
                connection,
                telegram_id=123,
                server_id="sg-a",
                external_id=external_id,
                secret_ciphertext="ciphertext",
                now_text="2026-09-05T00:00:00+00:00",
                profile_kind="free",
            )
            credential = connection.execute(
                "SELECT credential_id FROM connectivity_credentials WHERE external_id = ?",
                (external_id,),
            ).fetchone()
        generation_id = self.identity.ensure_generation_for_credential(
            entitlement_id,
            self.endpoint_id,
            credential_id=str(credential["credential_id"]),
            now="2026-09-05T00:00:00+00:00",
        )
        self.identity.ensure_generation_lease(
            entitlement_id,
            generation_id,
            self.endpoint_id,
            quota,
            "2026-09-06T00:00:00+00:00",
            now="2026-09-05T00:00:00+00:00",
        )
        return entitlement_id, generation_id

    def test_remote_counter_epochs_preserve_aggregate_usage_across_reset(self):
        entitlement_id, _ = self._managed_entitlement()
        first = self.identity.record_remote_usage(
            self.endpoint_id, "outline-usage-1", 100,
            observed_at="2026-09-05T00:01:00+00:00",
            now="2026-09-05T00:01:00+00:00",
        )
        self.assertEqual(first["credited_bytes"], 100)
        duplicate = self.identity.record_remote_usage(
            self.endpoint_id, "outline-usage-1", 100,
            observed_at="2026-09-05T00:01:00+00:00",
            now="2026-09-05T00:01:01+00:00",
        )
        self.assertTrue(duplicate["duplicate"])
        monotonic = self.identity.record_remote_usage(
            self.endpoint_id, "outline-usage-1", 250,
            observed_at="2026-09-05T00:02:00+00:00",
            now="2026-09-05T00:02:00+00:00",
        )
        self.assertEqual(monotonic["delta_bytes"], 150)
        reset = self.identity.record_remote_usage(
            self.endpoint_id, "outline-usage-1", 50,
            observed_at="2026-09-05T00:03:00+00:00",
            now="2026-09-05T00:03:00+00:00",
        )
        self.assertTrue(reset["reset"])
        self.assertEqual(reset["consumed_bytes"], 300)
        snapshot = self.identity.quota_snapshot(entitlement_id)
        self.assertEqual(snapshot["consumed_bytes"], 300)
        self.assertEqual(snapshot["remaining_bytes"], 700)
        with self.database.connect() as connection:
            epochs = connection.execute(
                "SELECT status, reset_count FROM entitlement_usage_epochs WHERE entitlement_id = ? ORDER BY epoch_no",
                (entitlement_id,),
            ).fetchall()
            ledger = connection.execute(
                "SELECT event_type, bytes FROM entitlement_quota_ledger WHERE entitlement_id = ? ORDER BY created_at, entry_id",
                (entitlement_id,),
            ).fetchall()
        self.assertEqual([(row["status"], row["reset_count"]) for row in epochs], [("reset", 0), ("active", 1)])
        event_types = [row["event_type"] for row in ledger]
        self.assertEqual(event_types.count("grant"), 1)
        self.assertEqual(event_types.count("usage"), 3)
        self.assertEqual(event_types.count("counter_reset"), 1)

    def test_aggregate_exhaustion_revokes_all_generations_and_routes(self):
        entitlement_id, generation = self._managed_entitlement(quota=500, external_id="outline-usage-2")
        first = self.identity.record_remote_usage(
            self.endpoint_id, "outline-usage-2", 500,
            observed_at="2026-09-05T00:04:00+00:00",
            now="2026-09-05T00:04:00+00:00",
        )
        self.assertTrue(first["exhausted"])
        self.assertEqual(self.identity.quota_snapshot(entitlement_id)["status"], "revoked")
        self.assertEqual(self.identity.routes_for_account(self.identity.ensure_account(123)), [])
        with self.database.connect() as connection:
            state = connection.execute(
                "SELECT status FROM credential_generations WHERE generation_id = ?",
                (generation,),
            ).fetchone()
            lease = connection.execute(
                "SELECT status FROM quota_leases WHERE entitlement_id = ?",
                (entitlement_id,),
            ).fetchone()
        self.assertEqual(state["status"], "revoked")
        self.assertEqual(lease["status"], "exhausted")


if __name__ == "__main__":
    unittest.main()
