import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from commerce import CommerceDatabase, CommerceError
from aurix_vpn.commerce_repositories import _PostgresConnection
from aurix_vpn.commerce_worker import CommerceWorkerMixin
from identity import IdentityError, IdentityService


UTC = timezone.utc


class RecordingPostgresConnection(_PostgresConnection):
    def __init__(self):
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))

        class Cursor:
            rowcount = 1

            @staticmethod
            def fetchone():
                if "FROM credential_generations" in query:
                    return {
                        "generation_id": "generation-1",
                        "endpoint_id": "sg-a",
                        "external_id": "remote-a",
                        "status": "active",
                        "usage_baseline_provenance": "new",
                        "usage_baseline_bytes": None,
                    }
                if "FROM entitlement_usage_epochs" in query:
                    return None
                return {"id": "sub-1"}

        return Cursor()


class RecordingPostgresDatabase:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def connect(self):
        yield self.connection

    @staticmethod
    def begin_write(_connection):
        return None


class ManagedQuotaWorkerHarness(CommerceWorkerMixin):
    def __init__(self, database, identity):
        self.database = database
        self.identity = identity

    @staticmethod
    def _decrypt_access_url(value):
        return value


class ManagedUsageAdapter:
    def __init__(self, bytes_transferred):
        self.bytes_transferred = bytes_transferred
        self.grants = []

    def read_usage(self, grant):
        self.grants.append(dict(grant))
        return {
            "protocol": grant["protocol"],
            "external_id": grant["external_id"],
            "bytes_transferred": self.bytes_transferred,
            "counter_mode": "reset_on_decrease",
        }


class IdentityAccountingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = CommerceDatabase(Path(self.tmp.name) / "identity.db")
        self.database.initialize()
        self.now = datetime(2026, 9, 9, tzinfo=UTC)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO users (telegram_id, first_name, created_at) VALUES (?, ?, ?)",
                (123, "Member", self.now.isoformat()),
            )
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, status, created_at)
                   VALUES ('order-1', 123, 'basic_50gb', 1, 'MMK', 'approved', ?)""",
                (self.now.isoformat(),),
            )
            connection.execute(
                """INSERT INTO subscriptions
                   (id, order_id, telegram_id, plan_code, starts_at, expires_at,
                    quota_bytes, duration_days, status, consumed_bytes)
                   VALUES ('sub-1', 'order-1', 123, 'basic_50gb', ?, ?, 1000, 30, 'active', 0)""",
                (self.now.isoformat(), (self.now + timedelta(days=30)).isoformat()),
            )
        self.identity = IdentityService(self.database)

    def tearDown(self):
        self.tmp.cleanup()

    def test_admin_account_includes_safe_subscription_metadata(self):
        account_id = self.identity.ensure_account(123, now=self.now)
        account = self.identity.admin_account(account_id)
        self.assertIsNotNone(account)
        self.assertEqual(len(account["subscriptions"]), 1)
        subscription = account["subscriptions"][0]
        self.assertEqual(subscription["subscription_id"], "sub-1")
        self.assertEqual(subscription["quota_bytes"], 1000)
        self.assertEqual(subscription["status"], "active")
        self.assertNotIn("access_url", subscription)

    def test_pairing_rechecks_account_status_without_consuming_token(self):
        token = self.identity.create_pairing_token(123, now=self.now)
        account_id = self.identity.account_snapshot(123)["account_id"]
        for status in ("suspended", "closed"):
            with self.subTest(status=status):
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE accounts SET status = ? WHERE account_id = ?",
                        (status, account_id),
                    )
                with self.assertRaisesRegex(IdentityError, "account is not active"):
                    self.identity.consume_pairing_token(
                        token, "managed-device-public-key-0001", now=self.now
                    )
                self.assertEqual(self.identity.devices_for_account(123), [])
                with self.database.connect() as connection:
                    row = connection.execute(
                        "SELECT status, consumed_at FROM pairing_tokens WHERE account_id = ?",
                        (account_id,),
                    ).fetchone()
                self.assertEqual(row["status"], "pending")
                self.assertIsNone(row["consumed_at"])
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE accounts SET status = 'active' WHERE account_id = ?", (account_id,)
            )
        paired = self.identity.consume_pairing_token(
            token, "managed-device-public-key-0001", now=self.now
        )
        self.assertEqual(paired["active_device_count"], 1)

    def test_inactive_account_cannot_receive_route_metadata_or_secret(self):
        account_id = self.identity.ensure_account(123, now=self.now)
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1")
        generation = self.identity.ensure_generation_for_credential(
            entitlement,
            "sg-a",
            credential_id="paid-credential",
            external_id="paid-credential",
            protocol="outline",
            access_url_ciphertext="ss://secret",
            usage_baseline_provenance="new",
            now=self.now.isoformat(),
        )

        self.assertEqual(
            [item["generation_id"] for item in self.identity.routes_for_account(account_id)],
            [generation],
        )
        self.assertEqual(
            self.identity.route_secret_record(account_id, generation)["secret_ciphertext"],
            "ss://secret",
        )

        with self.database.connect() as connection:
            connection.execute(
                "UPDATE accounts SET status = 'suspended' WHERE account_id = ?",
                (account_id,),
            )

        self.assertEqual(self.identity.routes_for_account(account_id), [])
        self.assertIsNone(self.identity.route_secret_record(account_id, generation))

    def test_device_revocation_is_audited_once_without_key_material(self):
        token = self.identity.create_pairing_token(123, now=self.now)
        paired = self.identity.consume_pairing_token(
            token,
            "managed-device-public-key-0001",
            now=self.now,
        )

        with self.database.connect() as connection:
            enrollment_rows = connection.execute(
                """SELECT actor_type, actor_id, action, target_type, target_id,
                          metadata_json
                     FROM audit_events
                    WHERE action = 'managed_device_enrolled'"""
            ).fetchall()
        self.assertEqual(len(enrollment_rows), 1)
        enrollment = enrollment_rows[0]
        self.assertEqual(
            tuple(enrollment[key] for key in ("actor_type", "actor_id", "action", "target_type", "target_id")),
            ("customer", "123", "managed_device_enrolled", "managed_device", paired["device_id"]),
        )
        self.assertEqual(json.loads(enrollment["metadata_json"]), {})
        self.assertNotIn("public-key", json.dumps(dict(enrollment)))

        self.assertTrue(self.identity.revoke_device(123, paired["device_id"], now=self.now))
        self.assertFalse(self.identity.revoke_device(123, paired["device_id"], now=self.now))
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT actor_type, actor_id, action, target_type, target_id,
                          metadata_json
                     FROM audit_events
                    WHERE action = 'managed_device_revoked'"""
            ).fetchall()

        self.assertEqual(len(rows), 1)
        audit = rows[0]
        self.assertEqual(
            tuple(audit[key] for key in ("actor_type", "actor_id", "action", "target_type", "target_id")),
            ("customer", "123", "managed_device_revoked", "managed_device", paired["device_id"]),
        )
        self.assertEqual(json.loads(audit["metadata_json"]), {"revocation_epoch": 1})
        self.assertNotIn("public-key", json.dumps(dict(audit)))

    def test_admin_generations_include_safe_usage_and_lease_state(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1", quota_bytes=1000)
        generation = self.identity.ensure_generation_for_credential(
            entitlement,
            "sg-a",
            credential_id="xray-credential",
            external_id="xray-credential",
            protocol="xray",
            access_url_ciphertext="vless://secret@example.com:18443",
            usage_baseline_provenance="new",
            now=self.now.isoformat(),
        )
        self.identity.ensure_generation_lease(
            entitlement,
            generation,
            "sg-a",
            1000,
            (self.now + timedelta(days=1)).isoformat(),
            now=self.now,
        )

        initial = self.identity.admin_generations(protocol="xray")
        self.assertEqual(len(initial), 1)
        self.assertEqual(initial[0]["quota_bytes"], 1000)
        self.assertEqual(initial[0]["consumed_bytes"], 0)
        self.assertEqual(initial[0]["remaining_bytes"], 1000)
        self.assertEqual(initial[0]["active_lease_bytes"], 1000)
        self.assertEqual(initial[0]["lease_used_bytes"], 0)
        self.assertIsNone(initial[0]["last_usage_at"])
        self.assertEqual(initial[0]["lifecycle_phase"], "active")
        self.assertFalse(initial[0]["session_termination_pending"])
        self.assertNotIn("secret", json.dumps(initial))

        self.identity.record_usage(
            entitlement,
            generation,
            125,
            endpoint_id="sg-a",
            source_external_id="xray-credential",
            observed_at=self.now.isoformat(),
        )
        current = self.identity.admin_generations(protocol="xray")[0]
        self.assertEqual(current["consumed_bytes"], 125)
        self.assertEqual(current["remaining_bytes"], 875)
        self.assertEqual(current["last_usage_at"], self.now.isoformat())

        self.assertTrue(
            self.identity.mark_remote_revoked(
                generation,
                verified=True,
                sessions_terminated=False,
                now=self.now,
            )
        )
        pending = self.identity.admin_generations(protocol="xray")[0]
        self.assertEqual(pending["lifecycle_phase"], "session_termination_pending")
        self.assertTrue(pending["session_termination_pending"])
        self.assertEqual(
            [row["generation_id"] for row in self.identity.pending_session_termination_generations()],
            [generation],
        )

        self.assertTrue(self.identity.mark_sessions_terminated(generation, now=self.now))
        revoked = self.identity.admin_generations(protocol="xray")[0]
        self.assertEqual(revoked["lifecycle_phase"], "revoked")
        self.assertFalse(revoked["session_termination_pending"])

    def test_managed_quota_sweep_is_protocol_neutral_and_idempotent(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1", quota_bytes=1000)
        generation = self.identity.ensure_generation_for_credential(
            entitlement,
            "sg-a",
            credential_id="xray-credential",
            external_id="xray-credential",
            protocol="xray",
            access_url_ciphertext="vless://xray-credential@example.com:18443",
            usage_baseline_provenance="new",
            now=self.now.isoformat(),
        )
        self.identity.ensure_generation_lease(
            entitlement, generation, "sg-a", 1000, (self.now + timedelta(days=1)).isoformat(), now=self.now
        )
        worker = ManagedQuotaWorkerHarness(self.database, self.identity)
        adapter = ManagedUsageAdapter(1250)
        routes = []

        def route_provider(endpoint_id, protocol):
            routes.append((endpoint_id, protocol))
            return {
                "route_id": f"{protocol}:{endpoint_id}",
                "endpoint_id": endpoint_id,
                "protocol": protocol,
            }

        first = worker.enforce_managed_quotas(
            route_provider=route_provider,
            adapter_provider=lambda _route: adapter,
            now=self.now,
        )
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["protocol_counts"], {"xray": 1})
        self.assertEqual(first["observed"], 1)
        self.assertEqual(first["credited_bytes"], 1000)
        self.assertEqual(first["exhausted"], 1)
        self.assertEqual(first["queued_revocations"], 1)
        self.assertEqual(routes, [("sg-a", "xray")])
        self.assertEqual(adapter.grants[0]["external_id"], "xray-credential")

        second = worker.enforce_managed_quotas(
            route_provider=route_provider,
            adapter_provider=lambda _route: adapter,
            now=self.now,
        )
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["credited_bytes"], 0)
        self.assertEqual(second["queued_revocations"], 0)
        with self.database.connect() as connection:
            subscription = connection.execute(
                "SELECT status, consumed_bytes FROM subscriptions WHERE id = 'sub-1'"
            ).fetchone()
            jobs = connection.execute(
                "SELECT COUNT(*) AS n FROM provisioning_jobs WHERE subscription_id = 'sub-1' AND operation = 'revoke'"
            ).fetchone()
        self.assertEqual((subscription["status"], subscription["consumed_bytes"]), ("revoked", 1000))
        self.assertEqual(jobs["n"], 1)

    def test_managed_quota_sweep_skips_outline_generations(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1", quota_bytes=1000)
        generation = self.identity.ensure_generation_for_credential(
            entitlement,
            "legacy-default",
            credential_id="outline-credential",
            external_id="outline-credential",
            protocol="outline",
            access_url_ciphertext="ss://outline-credential@example.com",
            usage_baseline_provenance="new",
            now=self.now.isoformat(),
        )
        self.identity.ensure_generation_lease(
            entitlement,
            generation,
            "legacy-default",
            1000,
            (self.now + timedelta(days=1)).isoformat(),
            now=self.now,
        )
        worker = ManagedQuotaWorkerHarness(self.database, self.identity)

        result = worker.enforce_managed_quotas(
            route_provider=lambda *_args: self.fail("Outline must not use managed route discovery"),
            adapter_provider=lambda _route: self.fail("Outline must not use managed adapter discovery"),
            now=self.now,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["generations"], 1)
        self.assertEqual(result["eligible"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["errors"], [])

    def test_credential_generation_protocol_cannot_drift_on_retry(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1")
        generation = self.identity.ensure_generation_for_credential(
            entitlement,
            "sg-a",
            external_id="stable-credential",
            protocol="xray",
            usage_baseline_provenance="new",
        )
        with self.assertRaisesRegex(IdentityError, "protocol is immutable"):
            self.identity.ensure_generation_for_credential(
                entitlement,
                "sg-a",
                external_id="stable-credential",
                protocol="outline",
                usage_baseline_provenance="new",
            )
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT generation_id, protocol FROM credential_generations WHERE generation_id = ?",
                (generation,),
            ).fetchone()
        self.assertEqual((row["generation_id"], row["protocol"]), (generation, "xray"))

    def test_managed_quota_sweep_rejects_a_route_protocol_mismatch(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1", quota_bytes=1000)
        generation = self.identity.ensure_generation_for_credential(
            entitlement,
            "sg-a",
            credential_id="h2-credential",
            external_id="h2-credential",
            protocol="hysteria2",
            access_url_ciphertext="hysteria2://secret@example.com:8444/",
            usage_baseline_provenance="new",
            now=self.now.isoformat(),
        )
        self.identity.ensure_generation_lease(
            entitlement, generation, "sg-a", 1000, (self.now + timedelta(days=1)).isoformat(), now=self.now
        )
        worker = ManagedQuotaWorkerHarness(self.database, self.identity)
        adapter = ManagedUsageAdapter(50)
        result = worker.enforce_managed_quotas(
            route_provider=lambda _endpoint_id, _protocol: {
                "route_id": "xray:sg-a",
                "endpoint_id": "sg-a",
                "protocol": "xray",
            },
            adapter_provider=lambda _route: adapter,
            now=self.now,
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["observed"], 0)
        self.assertEqual(result["credited_bytes"], 0)
        self.assertEqual(len(adapter.grants), 0)
        self.assertEqual(result["errors"][0]["error"], "CommerceError")

    def test_managed_revoke_rejects_a_route_identity_mismatch(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1", quota_bytes=1000)
        generation = self.identity.ensure_generation_for_credential(
            entitlement,
            "sg-a",
            credential_id="xray-revoke",
            external_id="xray-revoke",
            protocol="xray",
            access_url_ciphertext="vless://xray-revoke@example.com:18443",
            usage_baseline_provenance="new",
            now=self.now.isoformat(),
        )
        self.identity.ensure_generation_lease(
            entitlement, generation, "sg-a", 1000, (self.now + timedelta(days=1)).isoformat(), now=self.now
        )
        worker = ManagedQuotaWorkerHarness(self.database, self.identity)
        worker.managed_route_provider = lambda _endpoint_id, _protocol: {
            "route_id": "xray:bkk-a",
            "endpoint_id": "bkk-a",
            "protocol": "xray",
        }
        worker.managed_adapter_provider = lambda _route: object()
        with self.assertRaisesRegex(CommerceError, "route does not match generation"):
            worker._revoke_generation_set(entitlement, self.now)

    def test_failover_generations_share_one_quota_and_never_double_credit(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1", quota_bytes=1000)
        first = self.identity.ensure_generation_for_credential(
            entitlement, "sg-a", credential_id="credential-a", external_id="remote-a",
            usage_baseline_provenance="new",
        )
        second = self.identity.ensure_generation_for_credential(
            entitlement, "bkk-a", credential_id="credential-b", external_id="remote-b",
            usage_baseline_provenance="new",
        )
        self.identity.ensure_generation_lease(entitlement, first, "sg-a", 600, self.now.isoformat(), now=self.now)
        self.identity.ensure_generation_lease(entitlement, second, "bkk-a", 400, self.now.isoformat(), now=self.now)
        with self.assertRaises(IdentityError):
            self.identity.grant_lease(entitlement, "sg-a", lease_bytes=1, now=self.now)

        t1 = self.now.isoformat()
        t2 = (self.now + timedelta(minutes=1)).isoformat()
        t3 = (self.now + timedelta(minutes=2)).isoformat()
        self.identity.record_usage(entitlement, first, 100, observed_at=t1)
        self.identity.record_usage(entitlement, second, 150, observed_at=t1)
        self.assertEqual(self.identity.record_usage(entitlement, first, 300, observed_at=t2)["consumed_bytes"], 450)
        self.assertEqual(self.identity.record_usage(entitlement, second, 500, observed_at=t2)["consumed_bytes"], 800)
        self.assertTrue(self.identity.record_usage(entitlement, first, 10, observed_at=t3)["counter_reset"])
        exhausted = self.identity.record_usage(entitlement, second, 1000, observed_at=t3)
        self.assertTrue(exhausted["exhausted"])
        self.assertEqual(exhausted["consumed_bytes"], 1000)
        self.assertEqual(self.identity.record_usage(entitlement, second, 1000, observed_at=t3)["duplicate"], True)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT consumed_bytes, status, quota_exhausted_at FROM subscriptions WHERE id = 'sub-1'"
            ).fetchone()
            generations = connection.execute(
                "SELECT COUNT(*) AS n FROM credential_generations WHERE entitlement_key = ? AND status IN ('active', 'retiring', 'unknown')",
                (entitlement,),
            ).fetchone()["n"]
        self.assertEqual((row["consumed_bytes"], row["status"]), (1000, "revoked"))
        self.assertIsNotNone(row["quota_exhausted_at"])
        self.assertEqual(generations, 2)

    def test_remote_revocation_is_the_only_lease_release_proof(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1", quota_bytes=1000)
        generation = self.identity.ensure_generation_for_credential(
            entitlement, "sg-a", credential_id="credential-a", external_id="remote-a",
            usage_baseline_provenance="new",
        )
        lease = self.identity.grant_lease(entitlement, "sg-a", lease_bytes=100, generation_id=generation, now=self.now)
        self.identity.mark_remote_revoked(generation, verified=False, now=self.now)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT status FROM quota_leases WHERE lease_id = ?", (lease,)).fetchone()[0], "active")
        self.identity.mark_remote_revoked(generation, verified=True, now=self.now)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT status FROM quota_leases WHERE lease_id = ?", (lease,)).fetchone()[0], "released")

    def test_recovery_authorization_requires_active_entitlement_and_generation_lease(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1", quota_bytes=1000)
        generation = self.identity.ensure_generation_for_credential(
            entitlement,
            "sg-a",
            credential_id="credential-a",
            external_id="remote-a",
            usage_baseline_provenance="new",
        )
        self.identity.ensure_generation_lease(
            entitlement,
            generation,
            "sg-a",
            600,
            (self.now + timedelta(days=30)).isoformat(),
            now=self.now,
        )
        authorized = self.identity.recovery_authorization(entitlement, generation, now=self.now)
        self.assertTrue(authorized["authorized"])
        self.assertEqual(authorized["remaining_bytes"], 600)

        with self.database.connect() as connection:
            connection.execute("UPDATE subscriptions SET status = 'revoked' WHERE id = 'sub-1'")
        denied = self.identity.recovery_authorization(entitlement, generation, now=self.now)
        self.assertFalse(denied["authorized"])
        self.assertEqual(denied["reason"], "entitlement_inactive")

        with self.database.connect() as connection:
            connection.execute("UPDATE subscriptions SET status = 'active' WHERE id = 'sub-1'")
        second = self.identity.ensure_generation_for_credential(
            entitlement,
            "bkk-a",
            credential_id="credential-b",
            external_id="remote-b",
            usage_baseline_provenance="new",
        )
        no_lease = self.identity.recovery_authorization(entitlement, second, now=self.now)
        self.assertFalse(no_lease["authorized"])
        self.assertEqual(no_lease["reason"], "no_active_lease")

    def test_unknown_usage_baseline_fails_closed(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1")
        generation = self.identity.create_generation(
            entitlement, "sg-a", external_id="legacy-key"
        )
        with self.assertRaisesRegex(IdentityError, "baseline provenance is unknown"):
            self.identity.record_usage(entitlement, generation, 100, observed_at=self.now)

    def test_migrated_baseline_accounts_only_post_baseline_bytes(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1")
        generation = self.identity.create_generation(
            entitlement,
            "sg-a",
            external_id="migrated-key",
            usage_baseline_provenance="migrated",
            usage_baseline_bytes=400,
        )
        first = self.identity.record_usage(entitlement, generation, 400, observed_at=self.now)
        second = self.identity.record_usage(
            entitlement, generation, 500, observed_at=self.now + timedelta(minutes=1)
        )
        reset = self.identity.record_usage(
            entitlement, generation, 50, observed_at=self.now + timedelta(minutes=2)
        )
        self.assertEqual(first["credited_bytes"], 0)
        self.assertEqual(second["credited_bytes"], 100)
        self.assertEqual(second["consumed_bytes"], 100)
        self.assertTrue(reset["counter_reset"])
        self.assertEqual(reset["credited_bytes"], 50)
        self.assertEqual(reset["consumed_bytes"], 150)

    def test_counter_reset_accounts_new_epoch_from_zero(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1")
        generation = self.identity.create_generation(
            entitlement, "sg-a", external_id="reset-key", usage_baseline_provenance="new"
        )
        self.assertEqual(
            self.identity.record_usage(entitlement, generation, 400, observed_at=self.now)["credited_bytes"],
            400,
        )
        self.assertEqual(
            self.identity.record_usage(
                entitlement, generation, 500, observed_at=self.now + timedelta(minutes=1)
            )["credited_bytes"],
            100,
        )
        reset = self.identity.record_usage(
            entitlement, generation, 50, observed_at=self.now + timedelta(minutes=2)
        )
        self.assertTrue(reset["counter_reset"])
        self.assertEqual(reset["credited_bytes"], 50)
        self.assertEqual(reset["consumed_bytes"], 550)

    def test_rolling_window_decrease_does_not_recredit_aged_out_bytes(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1")
        generation = self.identity.create_generation(
            entitlement, "sg-a", external_id="rolling-key", usage_baseline_provenance="new"
        )
        first = self.identity.record_usage(
            entitlement, generation, 400, observed_at=self.now, counter_mode="rolling_window"
        )
        second = self.identity.record_usage(
            entitlement,
            generation,
            500,
            observed_at=self.now + timedelta(minutes=1),
            counter_mode="rolling_window",
        )
        decreased = self.identity.record_usage(
            entitlement,
            generation,
            300,
            observed_at=self.now + timedelta(minutes=2),
            counter_mode="rolling_window",
        )
        increased = self.identity.record_usage(
            entitlement,
            generation,
            350,
            observed_at=self.now + timedelta(minutes=3),
            counter_mode="rolling_window",
        )
        self.assertEqual(first["credited_bytes"], 400)
        self.assertEqual(second["credited_bytes"], 100)
        self.assertEqual(decreased["credited_bytes"], 0)
        self.assertFalse(decreased["counter_reset"])
        self.assertTrue(decreased["rolling_window_decrease"])
        self.assertEqual(increased["credited_bytes"], 50)
        self.assertEqual(increased["consumed_bytes"], 550)

    def test_expired_local_lease_remains_reserved_without_remote_proof(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1")
        source = self.identity.create_generation(
            entitlement, "sg-a", external_id="source-key", usage_baseline_provenance="new"
        )
        target = self.identity.create_generation(
            entitlement, "bkk-a", external_id="target-key", usage_baseline_provenance="new"
        )
        self.identity.grant_lease(
            entitlement, "sg-a", lease_bytes=1000, generation_id=source,
            ttl_seconds=30, now=self.now,
        )
        with self.assertRaisesRegex(IdentityError, "insufficient unreserved quota"):
            self.identity.grant_lease(
                entitlement, "bkk-a", lease_bytes=1000, generation_id=target,
                now=self.now + timedelta(seconds=31),
            )
        with self.database.connect() as connection:
            status = connection.execute(
                "SELECT status FROM quota_leases WHERE generation_id = ?", (source,)
            ).fetchone()["status"]
        self.assertEqual(status, "active")

    def test_postgres_usage_path_locks_source_before_reading_consumed_counter(self):
        connection = RecordingPostgresConnection()
        database = RecordingPostgresDatabase(connection)
        events = []

        class RecordingIdentity(IdentityService):
            def _source_row(self, current, entitlement_key):
                events.append("source_read")
                return {
                    "source_id": "sub-1",
                    "telegram_id": 123,
                    "status": "active",
                    "expires_at": (self.now + timedelta(days=30)).isoformat(),
                    "quota_bytes": 1000,
                    "consumed_bytes": 0,
                    "quota_exhausted_at": None,
                    "source_type": "paid",
                }

        identity = RecordingIdentity(database)
        identity.now = self.now
        identity.record_usage("paid:sub-1", "generation-1", 10, observed_at=self.now)
        self.assertIn("FOR UPDATE", connection.queries[0][0])
        self.assertEqual(events, ["source_read"])
        generation_query_index = next(
            index for index, item in enumerate(connection.queries)
            if "FROM credential_generations" in item[0]
        )
        self.assertLess(0, generation_query_index)


if __name__ == "__main__":
    unittest.main()
