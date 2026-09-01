import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from commerce import CommerceDatabase
from infrastructure import FleetController, InfrastructureError


UTC = timezone.utc


class FakeProvider:
    def __init__(self, usage="1"):
        self.usage = usage
        self.specifications = []

    def billing_balance(self):
        return {"month_to_date_usage": self.usage}

    def create_droplet(self, specification):
        self.specifications.append(specification)
        return {"id": 42, "action_ids": [77]}

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


if __name__ == "__main__":
    unittest.main()
