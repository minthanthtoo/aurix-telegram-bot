import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
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
