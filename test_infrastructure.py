import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from commerce import CommerceDatabase
from infrastructure import DigitalOceanClient, FleetController, InfrastructureError


UTC = timezone.utc


class FakeProvider:
    def __init__(self, usage="1", droplets=None):
        self.usage = usage
        self.specifications = []
        self.droplets = list(droplets or [])

    def billing_balance(self):
        return {"month_to_date_usage": self.usage}

    def create_droplet(self, specification):
        self.specifications.append(specification)
        return {"id": 42, "action_ids": [77]}

    def list_droplets(self):
        return list(self.droplets)

    def action(self, _action_id):
        return {"status": "completed"}

    def droplet(self, _droplet_id):
        return {
            "status": "active",
            "networks": {"v4": [{"type": "public", "ip_address": "203.0.113.10"}]},
        }


class FleetControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = CommerceDatabase(Path(self.tmp.name) / "fleet.db")
        self.database.initialize()
        self.now = datetime(2026, 9, 2, 2, 0, tzinfo=UTC)

    def tearDown(self):
        self.tmp.cleanup()

    def test_queue_is_allowlisted_and_idempotent_within_an_hour(self):
        controller = FleetController(self.database)
        first = controller.queue_provision(
            region="sgp1", size="s-1vcpu-1gb", image="ubuntu-24-04-x64",
            requested_by=123, now=self.now,
        )
        second = controller.queue_provision(
            region="sgp1", size="s-1vcpu-1gb", image="ubuntu-24-04-x64",
            requested_by=123, now=self.now,
        )
        self.assertEqual(first, second)
        with self.assertRaisesRegex(InfrastructureError, "allowlist"):
            controller.queue_provision(
                region="nyc1", size="s-1vcpu-1gb", image="ubuntu-24-04-x64",
                requested_by=123, now=self.now,
            )

    def test_provider_mutation_is_disabled_by_default(self):
        controller = FleetController(self.database, FakeProvider())
        job_id = controller.queue_provision(
            region="sgp1", size="s-1vcpu-1gb", image="ubuntu-24-04-x64",
            requested_by=123, now=self.now,
        )
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(InfrastructureError, "disabled"):
                controller.execute_provision(job_id)

    def test_budget_guard_fails_closed(self):
        controller = FleetController(self.database, FakeProvider(usage="5"))
        job_id = controller.queue_provision(
            region="sgp1", size="s-1vcpu-1gb", image="ubuntu-24-04-x64",
            requested_by=123, now=self.now,
        )
        environment = {
            "AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED": "1",
            "AURIX_MAX_MONTHLY_INFRA_BUDGET_USD": "10",
            "AURIX_DROPLET_MONTHLY_COST_ESTIMATE_USD": "6",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(InfrastructureError, "budget"):
                controller.execute_provision(job_id)

    def test_success_stops_at_manual_outline_verification_gate(self):
        provider = FakeProvider()
        controller = FleetController(self.database, provider)
        job_id = controller.queue_provision(
            region="sgp1", size="s-1vcpu-1gb", image="ubuntu-24-04-x64",
            requested_by=123, now=self.now,
        )
        environment = {
            "AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED": "1",
            "AURIX_MAX_MONTHLY_INFRA_BUDGET_USD": "10",
            "AURIX_DROPLET_MONTHLY_COST_ESTIMATE_USD": "6",
        }
        with patch.dict(os.environ, environment, clear=True):
            created = controller.execute_provision(job_id)
            result = controller.reconcile_provision(job_id)
        self.assertEqual(created["status"], "creating")
        self.assertEqual(result["status"], "awaiting_verification")
        self.assertNotIn("user_data", provider.specifications[0])
        with self.database.connect() as connection:
            status = connection.execute(
                "SELECT status FROM infrastructure_jobs WHERE id = ?", (job_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "awaiting_verification")

    def test_existing_managed_provider_nodes_count_toward_limit(self):
        provider = FakeProvider(
            droplets=[
                {"id": 101, "status": "active", "tags": ["aurix-vpn-node"]},
                {"id": 102, "status": "active", "tags": ["aurix-vpn-node"]},
                {"id": 999, "status": "active", "tags": ["unrelated"]},
            ]
        )
        controller = FleetController(self.database, provider)
        with patch.dict(os.environ, {"AURIX_MAX_VPN_NODES": "2"}, clear=False):
            with self.assertRaisesRegex(InfrastructureError, "node limit"):
                controller.queue_provision(
                    region="sgp1", size="s-1vcpu-1gb", image="ubuntu-24-04-x64",
                    requested_by=123, now=self.now,
                )

    def test_explicit_managed_provider_ids_count_before_tags_are_visible(self):
        controller = FleetController(self.database, FakeProvider(droplets=[]))
        environment = {
            "AURIX_MANAGED_DROPLET_IDS": "101,102",
            "AURIX_MAX_VPN_NODES": "2",
        }
        with patch.dict(os.environ, environment, clear=False):
            with self.assertRaisesRegex(InfrastructureError, "node limit"):
                controller.queue_provision(
                    region="sgp1", size="s-1vcpu-1gb", image="ubuntu-24-04-x64",
                    requested_by=123, now=self.now,
                )

    def test_budget_uses_existing_nodes_not_only_month_to_date_usage(self):
        provider = FakeProvider(
            usage="0.10",
            droplets=[
                {"id": 101, "status": "active", "tags": ["aurix-vpn-node"]},
                {"id": 102, "status": "active", "tags": ["aurix-vpn-node"]},
            ],
        )
        controller = FleetController(self.database, provider)
        job_id = controller.queue_provision(
            region="sgp1", size="s-1vcpu-1gb", image="ubuntu-24-04-x64",
            requested_by=123, now=self.now,
        )
        environment = {
            "AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED": "1",
            "AURIX_MAX_MONTHLY_INFRA_BUDGET_USD": "17",
            "AURIX_DROPLET_MONTHLY_COST_ESTIMATE_USD": "6",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(InfrastructureError, "budget"):
                controller.execute_provision(job_id)

    def test_provider_inventory_persists_status_without_secret_metadata(self):
        provider = FakeProvider(
            droplets=[
                {
                    "id": 101,
                    "status": "active",
                    "region": {"slug": "sgp1"},
                    "size_slug": "s-1vcpu-1gb",
                    "tags": ["aurix-vpn-node"],
                }
            ]
        )
        controller = FleetController(self.database, provider)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO outline_servers (server_id, label, provider_resource_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("sg-a", "Singapore A", "101", self.now.isoformat(), self.now.isoformat()),
            )
        result = controller.reconcile_provider_inventory()
        controller.reconcile_provider_inventory()
        self.assertEqual(result, {"managed": 1, "matched": 1, "unmatched": 0})
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT provider_status, provider_last_seen_at FROM outline_servers WHERE server_id = ?",
                ("sg-a",),
            ).fetchone()
        self.assertEqual(row["provider_status"], "active")
        self.assertTrue(row["provider_last_seen_at"])
        with self.database.connect() as connection:
            event_count = connection.execute(
                "SELECT COUNT(*) AS n FROM infrastructure_events WHERE event_type = 'provider_inventory_observed'"
            ).fetchone()["n"]
        self.assertEqual(event_count, 1)

    def test_digitalocean_inventory_follows_pagination(self):
        client = DigitalOceanClient("token")
        calls = []

        def request(_method, path):
            calls.append(path)
            page = 1 if "?page=1&" in path else 2
            count = 100 if page == 1 else 1
            return {
                "droplets": [{"id": page * 1000 + index} for index in range(count)],
                "meta": {"total": 101},
            }

        client._request = request
        droplets = client.list_droplets()
        self.assertEqual(len(droplets), 101)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
