import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from commerce import CommerceDatabase
from connectivity import (
    ConnectivityError,
    DigitalOceanClient,
    EndpointRegistry,
    FleetController,
)
from free_repository import Database
from identity import IdentityService


UTC = timezone.utc


def initialized_database(path: Path):
    free = Database(path)
    free.initialize()
    commerce = CommerceDatabase(path)
    commerce.initialize()
    return commerce


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class EndpointRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "aurix.db"
        self.database = initialized_database(self.path)
        self.key = Fernet.generate_key()
        self.registry = EndpointRegistry(self.database, self.key)
        self.registry.configure_bootstrap(
            "https://outline.invalid:1234/secret",
            "0" * 64,
            outline_version="1.12.3",
            now=datetime.now(UTC),
        )
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO users (telegram_id, first_name, created_at)
                   VALUES (1, 'Test', '2026-09-01T00:00:00+00:00')"""
            )
            connection.execute(
                """INSERT INTO plans
                   (code, name, price_minor, currency, quota_bytes, duration_days, active)
                   VALUES ('basic', 'Basic', 3000, 'MMK', 50000000000, 30, 1)"""
            )
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, plan_name,
                    quota_bytes_snapshot, duration_days_snapshot, status, created_at)
                   VALUES ('order-1', 1, 'basic', 3000, 'MMK', 'Basic', 50000000000,
                           30, 'approved', '2026-09-01T00:00:00+00:00')"""
            )
            connection.execute(
                """INSERT INTO subscriptions
                   (id, order_id, telegram_id, plan_code, starts_at, expires_at,
                    plan_name, quota_bytes, duration_days, status)
                   VALUES ('sub-1', 'order-1', 1, 'basic',
                           '2026-09-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00',
                           'Basic', 50000000000, 30, 'pending')"""
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_assignment_is_durable_and_idempotent(self):
        first = self.registry.ensure_subscription_assignment("sub-1", "basic", 50_000_000_000)
        second = self.registry.ensure_subscription_assignment("sub-1", "basic", 50_000_000_000)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.endpoint_id, "legacy-default")

    def test_endpoint_protocol_profiles_default_to_outline_and_can_stage_candidates(self):
        profiles = self.registry.list_protocol_profiles("legacy-default")
        self.assertEqual([item["protocol"] for item in profiles], ["outline"])
        self.assertEqual(profiles[0]["status"], "enabled")
        staged = self.registry.register_protocol_profile(
            "legacy-default",
            "xray",
            status="candidate",
            capabilities={"usage": True, "quota_cap": False},
        )
        self.assertEqual(staged["profile_id"], "xray:legacy-default")
        self.assertEqual(staged["status"], "candidate")
        self.assertEqual(staged["capabilities"]["usage"], True)
        self.assertEqual(
            [item["protocol"] for item in self.registry.list_protocol_profiles("legacy-default")],
            ["outline", "xray"],
        )
        self.assertEqual(
            [item["protocol"] for item in self.registry.list_protocol_profiles(enabled_only=True)],
            ["outline"],
        )
        with self.assertRaisesRegex(ConnectivityError, "require promote_protocol_profile"):
            self.registry.register_protocol_profile(
                "legacy-default", "xray", status="enabled"
            )
        readiness = self.registry.protocol_profile_promotion_readiness(
            "legacy-default",
            "xray",
            required_signals=("management",),
            required_capabilities=("usage",),
            now=datetime(2026, 9, 11, 0, 0, tzinfo=UTC),
        )
        self.assertFalse(readiness["promotable"])
        self.assertEqual(readiness["missing_signals"], ["management"])
        self.assertEqual(readiness["missing_capabilities"], [])
        with self.database.connect() as connection:
            with self.assertRaisesRegex(ConnectivityError, "capacity"):
                self.registry.select_endpoint_for_plan(connection, "basic", protocol="xray")
        now = datetime(2026, 9, 11, 0, 0, tzinfo=UTC)
        self.registry.record_protocol_observation(
            "legacy-default",
            "xray",
            signal="management",
            status="healthy",
            observed_at=now,
            expires_at=now.replace(hour=1),
            source="promotion-test",
            now=now,
        )
        promoted = self.registry.promote_protocol_profile(
            "legacy-default",
            "xray",
            required_signals=("management",),
            required_capabilities=("usage",),
            actor_id=7,
            now=now,
        )
        self.assertEqual(promoted["status"], "enabled")
        with self.database.connect() as connection:
            audit = connection.execute(
                """SELECT action, actor_id, target_id, metadata_json
                     FROM audit_events
                    WHERE action = 'protocol_profile_promoted'
                    ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        self.assertEqual(audit["action"], "protocol_profile_promoted")
        self.assertEqual(audit["actor_id"], "7")
        self.assertEqual(audit["target_id"], "xray:legacy-default")
        self.assertEqual(json.loads(audit["metadata_json"])["required_signals"], ["management"])
        with self.database.connect() as connection:
            self.assertEqual(
                self.registry.select_endpoint_for_plan(connection, "basic", protocol="xray"),
                "legacy-default",
            )
        disabled = self.registry.disable_protocol_profile(
            "legacy-default", "xray", actor_id=7, now=now
        )
        self.assertEqual(disabled["status"], "disabled")
        with self.database.connect() as connection:
            with self.assertRaisesRegex(ConnectivityError, "capacity"):
                self.registry.select_endpoint_for_plan(connection, "basic", protocol="xray")
            audit = connection.execute(
                """SELECT action, actor_id, target_id
                     FROM audit_events
                    WHERE action = 'protocol_profile_disabled'
                    ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        self.assertEqual(audit["action"], "protocol_profile_disabled")
        self.assertEqual(audit["actor_id"], "7")
        self.assertEqual(audit["target_id"], "xray:legacy-default")
        self.registry.register_protocol_profile(
            "legacy-default", "xray", status="retired", now=now
        )
        with self.assertRaisesRegex(ConnectivityError, "retired protocol profile"):
            self.registry.disable_protocol_profile("legacy-default", "xray", actor_id=7, now=now)

    def test_protocol_profile_promotion_requires_fresh_evidence_and_declared_capabilities(self):
        now = datetime(2026, 9, 11, 0, 0, tzinfo=UTC)
        self.registry.register_protocol_profile(
            "legacy-default",
            "hysteria2",
            status="candidate",
            capabilities={"usage": True, "quota_cap": False},
            now=now,
        )
        with self.assertRaisesRegex(ConnectivityError, "evidence is incomplete"):
            self.registry.promote_protocol_profile(
                "legacy-default",
                "hysteria2",
                required_signals=("management",),
                now=now,
            )
        self.registry.record_protocol_observation(
            "legacy-default",
            "hysteria2",
            signal="management",
            status="healthy",
            observed_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
            source="promotion-test",
            now=now,
        )
        with self.assertRaisesRegex(ConnectivityError, "evidence is incomplete"):
            self.registry.promote_protocol_profile(
                "legacy-default",
                "hysteria2",
                required_signals=("management",),
                now=now,
            )
        self.registry.record_protocol_observation(
            "legacy-default",
            "hysteria2",
            signal="management",
            status="healthy",
            observed_at=now,
            expires_at=now.replace(hour=1),
            source="promotion-test",
            now=now,
        )
        with self.assertRaisesRegex(ConnectivityError, "lacks required capabilities"):
            self.registry.promote_protocol_profile(
                "legacy-default",
                "hysteria2",
                required_signals=("management",),
                required_capabilities=("quota_cap",),
                now=now,
            )
        with self.assertRaisesRegex(ConnectivityError, "must be a sequence"):
            self.registry.promote_protocol_profile(
                "legacy-default",
                "hysteria2",
                required_signals="management",
                now=now,
            )

    def test_protocol_observations_are_safe_append_only_evidence(self):
        now = datetime(2026, 9, 11, 0, 0, tzinfo=UTC)
        self.registry.register_protocol_profile(
            "legacy-default", "xray", status="candidate", now=now
        )
        observation = self.registry.record_protocol_observation(
            "legacy-default",
            "xray",
            signal="client_path",
            status="degraded",
            details={
                "network_bucket": "mm-mpt",
                "client_path": "udp-timeout",
                "secret": "must-not-persist",
            },
            latency_ms=321.5,
            observed_at=now,
            expires_at=now.replace(hour=1),
            source="canary",
            now=now,
        )
        self.assertEqual(observation["profile_id"], "xray:legacy-default")
        self.assertNotIn("secret", observation["details"])
        listed = self.registry.list_protocol_observations(
            "legacy-default", protocol="xray", signal="client_path"
        )
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["status"], "degraded")
        self.assertEqual(listed[0]["details"]["client_path"], "udp-timeout")
        readiness = self.registry.protocol_profile_promotion_readiness(
            "legacy-default",
            "xray",
            required_signals=("client_path",),
            now=now,
        )
        self.assertFalse(readiness["promotable"])
        self.assertEqual(readiness["fresh_healthy_signals"], [])
        self.assertEqual(readiness["reasons"], ["protocol profile evidence is incomplete: client_path"])
        with self.assertRaisesRegex(ConnectivityError, "profile does not exist"):
            self.registry.record_protocol_observation(
                "legacy-default", "hysteria2", now=now
            )

    def test_transfer_assignment_preserves_identity_and_moves_capacity(self):
        now = datetime.now(UTC)
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO vpn_endpoints
                   (id, code, region, state, accepts_new_assignments,
                    created_at, last_healthy_at)
                   VALUES ('bkk-a', 'BKK-A', 'bkk1', 'ACTIVE', 1, ?, ?)""",
                (now.isoformat(), now.isoformat()),
            )
        self.registry.register_protocol_profile("bkk-a", "outline", status="enabled", now=now)
        assignment = self.registry.ensure_subscription_assignment(
            "sub-1", "basic", 50_000_000_000,
            preferred_endpoint_id="legacy-default", now=now
        )
        moved = self.registry.transfer_assignment(
            "paid:sub-1", "bkk-a", reason="operator-drain", now=now
        )
        self.assertTrue(moved["changed"])
        self.assertEqual(moved["source_endpoint_id"], assignment.endpoint_id)
        self.assertEqual(moved["target_endpoint_id"], "bkk-a")
        repeated = self.registry.transfer_assignment("paid:sub-1", "bkk-a", now=now)
        self.assertFalse(repeated["changed"])
        self.assertEqual(self.registry.assignment_for_subscription("sub-1").endpoint_id, "bkk-a")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE vpn_endpoints SET accepts_new_assignments = 0 WHERE id = 'legacy-default'"
            )
        restored = self.registry.transfer_assignment(
            "paid:sub-1", "legacy-default", reason="failover-rollback", now=now
        )
        self.assertTrue(restored["changed"])
        self.assertEqual(self.registry.assignment_for_subscription("sub-1").endpoint_id, "legacy-default")

    def test_transfer_assignment_rejects_target_without_source_protocol_profile(self):
        now = datetime.now(UTC)
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO vpn_endpoints
                   (id, code, region, state, accepts_new_assignments,
                    created_at, last_healthy_at)
                   VALUES ('bkk-xray', 'BKK-XRAY', 'bkk1', 'ACTIVE', 1, ?, ?)""",
                (now.isoformat(), now.isoformat()),
            )
        assignment = self.registry.ensure_subscription_assignment(
            "sub-1", "basic", 50_000_000_000,
            preferred_endpoint_id="legacy-default", now=now
        )
        identity = IdentityService(self.database)
        entitlement = identity.ensure_subscription_entitlement(1, "sub-1", now=now.isoformat())
        identity.create_generation(
            entitlement,
            assignment.endpoint_id,
            protocol="xray",
            external_id="xray-source",
            usage_baseline_provenance="new",
            now=now.isoformat(),
        )
        with self.assertRaisesRegex(ConnectivityError, "enabled xray protocol profile"):
            self.registry.transfer_assignment("paid:sub-1", "bkk-xray", now=now)

    def test_preferred_endpoint_is_honored_inside_capacity_selection(self):
        now = datetime.now(UTC)
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO vpn_endpoints
                   (id, code, region, state, accepts_new_assignments,
                    created_at, last_healthy_at)
                   VALUES ('sgp-02', 'SGP-02', 'sgp1', 'ACTIVE', 1, ?, ?)""",
                (now.isoformat(), now.isoformat()),
            )
        self.registry.register_protocol_profile("sgp-02", "outline", status="enabled")
        assignment = self.registry.ensure_subscription_assignment(
            "sub-1", "basic", 50_000_000_000,
            preferred_endpoint_id="sgp-02", now=now,
        )
        self.assertEqual(assignment.endpoint_id, "sgp-02")

    def test_bootstrap_without_health_mark_stays_unavailable_to_customers(self):
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE vpn_endpoints SET last_healthy_at = NULL WHERE id = 'legacy-default'"
            )
        self.registry.configure_bootstrap(
            "https://outline.invalid:1234/secret", "0" * 64, mark_healthy=False
        )
        endpoint = self.registry.list_customer_endpoints("basic")[0]
        self.assertFalse(endpoint["healthy"])
        self.assertFalse(endpoint["eligible"])

    def test_customer_directory_requires_enabled_protocol_profile(self):
        self.registry.register_protocol_profile("legacy-default", "outline", status="disabled")
        endpoint = self.registry.list_customer_endpoints("basic")[0]
        self.assertEqual(endpoint["protocol"], "outline")
        self.assertEqual(endpoint["protocol_status"], "disabled")
        self.assertFalse(endpoint["eligible"])

    def test_full_endpoint_is_not_selected(self):
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE vpn_endpoints SET max_active_keys = 0 WHERE id = 'legacy-default'"
            )
        with self.assertRaisesRegex(ConnectivityError, "capacity"):
            self.registry.ensure_subscription_assignment("sub-1", "basic", 50_000_000_000)

    def test_stale_endpoint_is_not_selected(self):
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE vpn_endpoints SET last_healthy_at = ? WHERE id = 'legacy-default'",
                ("2020-01-01T00:00:00+00:00",),
            )
        with self.assertRaisesRegex(ConnectivityError, "capacity"):
            self.registry.ensure_subscription_assignment("sub-1", "basic", 50_000_000_000)

    def test_identical_external_ids_can_exist_on_different_endpoints(self):
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO vpn_endpoints
                   (id, code, provider, region, state, accepts_new_assignments, created_at)
                   VALUES ('sgp-02', 'SGP-02', 'manual', 'sgp1', 'ACTIVE', 1,
                           '2026-09-01T00:00:00+00:00')"""
            )
            connection.execute(
                """INSERT INTO keys
                   (telegram_id, outline_key_id, key_type, created_at, expires_at,
                    data_limit_bytes, status, endpoint_id)
                   VALUES (1, '1', 'daily_free', '2026-09-01T00:00:00+00:00',
                           '2026-09-02T00:00:00+00:00', 100, 'active', 'legacy-default')"""
            )
            connection.execute(
                """INSERT INTO keys
                   (telegram_id, outline_key_id, key_type, created_at, expires_at,
                    data_limit_bytes, status, endpoint_id)
                   VALUES (1, '1', 'daily_free', '2026-09-01T00:00:00+00:00',
                           '2026-09-02T00:00:00+00:00', 100, 'active', 'sgp-02')"""
            )
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM keys WHERE outline_key_id = '1'"
            ).fetchone()["n"]
        self.assertEqual(count, 2)

    def test_scoped_usage_never_flattens_identical_key_ids(self):
        metrics = {
            "byEndpoint": {
                "legacy-default": {"1": 100},
                "sgp-02": {"1": 900},
            }
        }
        self.assertEqual(self.registry.usage_for_endpoint(metrics, "legacy-default")["1"], 100)
        self.assertEqual(self.registry.usage_for_endpoint(metrics, "sgp-02")["1"], 900)

    def test_existing_free_keys_are_backfilled_once(self):
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO keys
                   (telegram_id, outline_key_id, key_type, created_at, expires_at,
                    data_limit_bytes, status, endpoint_id)
                   VALUES (1, 'legacy-key', 'daily_free', '2026-09-01T00:00:00+00:00',
                           '2026-09-02T00:00:00+00:00', 300000000, 'active', 'legacy-default')"""
            )
        self.assertEqual(self.registry.backfill_free_assignments(), 1)
        self.assertEqual(self.registry.backfill_free_assignments(), 0)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT plan_code, status FROM endpoint_assignments WHERE free_key_id IS NOT NULL"
            ).fetchone()
        self.assertEqual((row["plan_code"], row["status"]), ("FREE300MB", "active"))


class DigitalOceanAndFleetTest(unittest.TestCase):
    def test_api_client_returns_created_droplet_without_leaking_token(self):
        client = DigitalOceanClient("private-token")
        response = FakeResponse({"droplet": {"id": 42, "action_ids": [99]}})
        with patch("connectivity.urllib.request.urlopen", return_value=response) as request:
            droplet = client.create_droplet(
                {"name": "aurix-sgp-02", "region": "sgp1", "size": "s-1vcpu-1gb", "image": "ubuntu-24-04-x64"}
            )
        self.assertEqual(droplet["id"], 42)
        self.assertEqual(request.call_args.args[0].headers["Authorization"], "Bearer private-token")

    def test_provider_mutation_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = initialized_database(Path(tmp) / "fleet.db")
            controller = FleetController(database, provider=None)
            with patch.dict(os.environ, {}, clear=True):
                job = controller.queue_provision(
                    region="sgp1",
                    size="s-1vcpu-1gb",
                    image="ubuntu-24-04-x64",
                    requested_by=1,
                )
                with self.assertRaisesRegex(ConnectivityError, "disabled"):
                    controller.execute_provision(job, {})

    def test_permanent_secret_names_are_rejected_from_user_data(self):
        class Provider:
            def create_droplet(self, specification):
                raise AssertionError("provider must not be called")

        with tempfile.TemporaryDirectory() as tmp:
            database = initialized_database(Path(tmp) / "fleet.db")
            controller = FleetController(database, Provider())
            job = controller.queue_provision(
                region="sgp1",
                size="s-1vcpu-1gb",
                image="ubuntu-24-04-x64",
                requested_by=1,
            )
            with patch.dict(os.environ, {"AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED": "1"}, clear=False):
                with self.assertRaisesRegex(ConnectivityError, "forbidden"):
                    controller.execute_provision(
                        job, {"user_data": "TELEGRAM_BOT_TOKEN=do-not-store"}
                    )

    def test_provider_create_failure_is_persisted(self):
        class Provider:
            def create_droplet(self, specification):
                raise ConnectivityError("provider unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            database = initialized_database(Path(tmp) / "fleet.db")
            controller = FleetController(database, Provider())
            job = controller.queue_provision(
                region="sgp1",
                size="s-1vcpu-1gb",
                image="ubuntu-24-04-x64",
                requested_by=1,
            )
            with patch.dict(os.environ, {"AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED": "1"}):
                with self.assertRaises(ConnectivityError):
                    controller.execute_provision(job, {"name": "aurix-sgp-02"})
            with database.connect() as connection:
                row = connection.execute(
                    "SELECT status, last_error FROM infrastructure_jobs WHERE id = ?", (job,)
                ).fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertNotIn("token", str(row["last_error"]).lower())

    def test_monthly_budget_guard_blocks_provider_create(self):
        class Provider:
            def billing_balance(self):
                return {"month_to_date_usage": "9.50"}

            def create_droplet(self, specification):
                raise AssertionError("budget guard must run before provider create")

        with tempfile.TemporaryDirectory() as tmp:
            database = initialized_database(Path(tmp) / "fleet.db")
            controller = FleetController(database, Provider())
            job = controller.queue_provision(
                region="sgp1",
                size="s-1vcpu-1gb",
                image="ubuntu-24-04-x64",
                requested_by=1,
            )
            environment = {
                "AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED": "1",
                "AURIX_MAX_MONTHLY_INFRA_BUDGET_USD": "10",
                "AURIX_DROPLET_MONTHLY_COST_ESTIMATE_USD": "6",
            }
            with patch.dict(os.environ, environment, clear=False):
                with self.assertRaisesRegex(ConnectivityError, "budget"):
                    controller.execute_provision(job, {"name": "aurix-sgp-02"})


if __name__ == "__main__":
    unittest.main()
