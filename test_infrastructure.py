import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from commerce import CommerceDatabase
from infrastructure import DigitalOceanClient, FleetController, InfrastructureError


UTC = timezone.utc


class FakeProvider:
    def __init__(self, usage="1", droplets=None):
        self.usage = usage
        self.specifications = []
        self.droplets = list(droplets or [])
        self.deleted = []

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

    def delete_droplet(self, droplet_id):
        self.deleted.append(str(droplet_id))


class AmbiguousCreateProvider(FakeProvider):
    """Simulate a provider accepting POST before the client loses its reply."""

    def create_droplet(self, specification):
        self.specifications.append(specification)
        self.droplets.append(
            {
                "id": 42,
                "name": specification["name"],
                "tags": ["aurix-vpn-node", "aurix-awaiting-verification"],
                "action_ids": [77],
            }
        )
        raise InfrastructureError("provider response timed out")


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
            "AURIX_DIGITALOCEAN_SSH_KEY_IDS": "12345",
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
            "AURIX_DIGITALOCEAN_SSH_KEY_IDS": "12345",
            "AURIX_MAX_MONTHLY_INFRA_BUDGET_USD": "10",
            "AURIX_DROPLET_MONTHLY_COST_ESTIMATE_USD": "6",
        }
        with patch.dict(os.environ, environment, clear=True):
            created = controller.execute_provision(job_id)
            result = controller.reconcile_provision(job_id)
            waiting_again = controller.reconcile_provision(job_id)
        self.assertEqual(created["status"], "creating")
        self.assertEqual(result["status"], "awaiting_verification")
        self.assertEqual(waiting_again["provider_resource_id"], "42")
        self.assertEqual(waiting_again["public_ip"], "203.0.113.10")
        self.assertNotIn("user_data", provider.specifications[0])
        self.assertEqual(provider.specifications[0]["ssh_keys"], [12345])
        with self.database.connect() as connection:
            status = connection.execute(
                "SELECT status FROM infrastructure_jobs WHERE id = ?", (job_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "awaiting_verification")

    def test_ambiguous_provider_create_is_recovered_without_a_duplicate(self):
        provider = AmbiguousCreateProvider()
        controller = FleetController(self.database, provider)
        job_id = controller.queue_provision(
            region="sgp1", size="s-1vcpu-1gb", image="ubuntu-24-04-x64",
            requested_by=123, now=self.now,
        )
        environment = {
            "AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED": "1",
            "AURIX_DIGITALOCEAN_SSH_KEY_IDS": "12345",
            "AURIX_MAX_MONTHLY_INFRA_BUDGET_USD": "10",
            "AURIX_DROPLET_MONTHLY_COST_ESTIMATE_USD": "6",
        }
        with patch.dict(os.environ, environment, clear=True):
            recovered = controller.execute_provision(job_id)
            observed = controller.reconcile_provision(job_id)
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["droplet_id"], "42")
        self.assertEqual(observed["status"], "awaiting_verification")
        self.assertEqual(len(provider.specifications), 1)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT provider_resource_id, provider_action_id, status FROM infrastructure_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            event = connection.execute(
                "SELECT event_type FROM infrastructure_events WHERE infrastructure_job_id = ? ORDER BY created_at DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        self.assertEqual(tuple(row), ("42", "77", "awaiting_verification"))
        self.assertEqual(event["event_type"], "droplet_active")

    def test_provider_provisioning_fails_closed_without_ssh_key_attachment(self):
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
            with self.assertRaisesRegex(InfrastructureError, "SSH_KEY_IDS"):
                controller.execute_provision(job_id)

    def test_auto_enrollment_attaches_one_time_user_data_and_persists_pending_token(self):
        provider = FakeProvider()
        controller = FleetController(self.database, provider)
        job_id = controller.queue_provision(
            region="sgp1", size="s-1vcpu-1gb", image="ubuntu-24-04-x64",
            requested_by=123, now=self.now,
        )
        environment = {
            "AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED": "1",
            "AURIX_DIGITALOCEAN_SSH_KEY_IDS": "12345",
            "AURIX_MAX_MONTHLY_INFRA_BUDGET_USD": "10",
            "AURIX_DROPLET_MONTHLY_COST_ESTIMATE_USD": "6",
            "AURIX_FLEET_AUTO_REGISTRATION_ENABLED": "1",
            "AURIX_FLEET_REGISTRATION_ENABLED": "1",
            "AURIX_FLEET_REGISTRATION_URL": "https://control.example/fleet/register",
            "AURIX_FLEET_ENROLLMENT_KEY": Fernet.generate_key().decode(),
            "AURIX_FLEET_CONTROL_PLANE_SOURCE": "203.0.113.7/32",
        }
        with patch.dict(os.environ, environment, clear=True):
            created = controller.execute_provision(job_id)
        self.assertEqual(created["status"], "creating")
        specification = provider.specifications[0]
        self.assertIn("user_data", specification)
        self.assertIn("aurix-auto-enrollment", specification["tags"])
        self.assertNotIn(environment["AURIX_FLEET_ENROLLMENT_KEY"], specification["user_data"])
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status, payload_ciphertext FROM infrastructure_enrollments WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["payload_ciphertext"])

    def test_invalid_auto_enrollment_configuration_rolls_back_running_transition(self):
        controller = FleetController(self.database, FakeProvider())
        job_id = controller.queue_provision(
            region="sgp1", size="s-1vcpu-1gb", image="ubuntu-24-04-x64",
            requested_by=123, now=self.now,
        )
        environment = {
            "AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED": "1",
            "AURIX_DIGITALOCEAN_SSH_KEY_IDS": "12345",
            "AURIX_MAX_MONTHLY_INFRA_BUDGET_USD": "10",
            "AURIX_DROPLET_MONTHLY_COST_ESTIMATE_USD": "6",
            "AURIX_FLEET_AUTO_REGISTRATION_ENABLED": "1",
            "AURIX_FLEET_REGISTRATION_ENABLED": "1",
            "AURIX_FLEET_REGISTRATION_URL": "https://control.example/fleet/register",
            "AURIX_FLEET_ENROLLMENT_KEY": "invalid",
            "AURIX_FLEET_CONTROL_PLANE_SOURCE": "203.0.113.7/32",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(InfrastructureError, "encryption key"):
                controller.execute_provision(job_id)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status FROM infrastructure_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            enrollment = connection.execute(
                "SELECT 1 FROM infrastructure_enrollments WHERE job_id = ?", (job_id,)
            ).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(enrollment)

    def test_verified_activation_is_idempotent_and_audited(self):
        provider = FakeProvider()
        controller = FleetController(self.database, provider)
        job_id = controller.queue_provision(
            region="sgp1", size="s-1vcpu-1gb", image="ubuntu-24-04-x64",
            requested_by=123, now=self.now,
        )
        environment = {
            "AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED": "1",
            "AURIX_DIGITALOCEAN_SSH_KEY_IDS": "12345",
            "AURIX_MAX_MONTHLY_INFRA_BUDGET_USD": "10",
            "AURIX_DROPLET_MONTHLY_COST_ESTIMATE_USD": "6",
        }
        with patch.dict(os.environ, environment, clear=True):
            controller.execute_provision(job_id)
            controller.reconcile_provision(job_id)
            with self.database.connect() as connection:
                connection.execute(
                    """INSERT INTO outline_servers
                       (server_id, label, provider_resource_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    ("sg-b", "Singapore B", "42", self.now.isoformat(), self.now.isoformat()),
                )
            first = controller.mark_provision_activated(job_id, "sg-b")
            second = controller.mark_provision_activated(job_id, "sg-b")
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second, first)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status, completed_at FROM infrastructure_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            events = connection.execute(
                "SELECT event_type, server_id FROM infrastructure_events "
                "WHERE infrastructure_job_id = ? AND event_type = 'endpoint_activated'",
                (job_id,),
            ).fetchall()
        self.assertEqual(row["status"], "completed")
        self.assertTrue(row["completed_at"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["server_id"], "sg-b")

    def test_activation_requires_awaiting_verification(self):
        controller = FleetController(self.database, FakeProvider())
        job_id = controller.queue_provision(
            region="sgp1", size="s-1vcpu-1gb", image="ubuntu-24-04-x64",
            requested_by=123, now=self.now,
        )
        with self.assertRaisesRegex(InfrastructureError, "awaiting verification"):
            controller.mark_provision_activated(job_id, "sg-b")

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
            "AURIX_DIGITALOCEAN_SSH_KEY_IDS": "12345",
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

    def test_provider_orphan_candidates_require_two_old_observations_and_ignore_jobs(self):
        provider = FakeProvider(
            droplets=[
                {"id": 101, "status": "active", "tags": ["aurix-vpn-node"], "name": "old"},
                {"id": 102, "status": "active", "tags": ["aurix-vpn-node"], "name": "job"},
                {"id": 103, "status": "active", "tags": ["unrelated"], "name": "other"},
            ]
        )
        controller = FleetController(self.database, provider)
        first = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
        second = datetime(2026, 9, 1, 2, 0, tzinfo=UTC)
        with self.database.connect() as connection:
            for provider_id, observed_at in (("101", first), ("101", second), ("102", first), ("102", second)):
                connection.execute(
                    """INSERT INTO infrastructure_events
                       (id, event_type, metadata_json, created_at) VALUES (?, ?, ?, ?)""",
                    (
                        f"event-{provider_id}-{observed_at.hour}",
                        "provider_inventory_observed",
                        json.dumps({"provider_resource_id": provider_id}, sort_keys=True),
                        observed_at.isoformat(),
                    ),
                )
            connection.execute(
                """INSERT INTO infrastructure_jobs
                   (id, operation, status, attempts, next_attempt_at, provider_resource_id,
                    request_fingerprint, created_at)
                   VALUES (?, 'provision', 'awaiting_verification', 1, ?, ?, ?, ?)""",
                (
                    "job-102", second.isoformat(), "102", "fingerprint-102", second.isoformat()
                ),
            )
        candidates = controller.provider_orphan_candidates(
            now=datetime(2026, 9, 2, 0, 0, tzinfo=UTC), min_age_seconds=3600
        )
        self.assertEqual([item["provider_resource_id"] for item in candidates], ["101"])
        self.assertEqual(candidates[0]["observation_count"], 2)

    def test_provider_orphan_cleanup_is_gated_and_audited(self):
        provider = FakeProvider(
            droplets=[{"id": 101, "status": "active", "tags": ["aurix-vpn-node"], "name": "old"}]
        )
        controller = FleetController(self.database, provider)
        first = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
        second = datetime(2026, 9, 1, 2, 0, tzinfo=UTC)
        with self.database.connect() as connection:
            for index, observed_at in enumerate((first, second)):
                connection.execute(
                    """INSERT INTO infrastructure_events
                       (id, event_type, metadata_json, created_at) VALUES (?, ?, ?, ?)""",
                    (
                        f"orphan-event-{index}",
                        "provider_inventory_observed",
                        json.dumps({"provider_resource_id": "101"}, sort_keys=True),
                        observed_at.isoformat(),
                    ),
                )
        fixed_now = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
        with patch.dict(os.environ, {"AURIX_ORPHAN_CLEANUP_MIN_AGE_SECONDS": "3600"}, clear=False):
            with patch.dict(os.environ, {}, clear=True):
                disabled = controller.cleanup_provider_orphans(now=fixed_now)
            self.assertEqual(disabled["status"], "disabled")
            self.assertEqual(provider.deleted, [])
            environment = {
                "AURIX_ORPHAN_CLEANUP_ENABLED": "1",
                "AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED": "1",
                "AURIX_DIGITALOCEAN_SSH_KEY_IDS": "12345",
                "AURIX_ORPHAN_CLEANUP_CONFIRMATION": "DELETE-UNREGISTERED-AURIX-NODES",
                "AURIX_ORPHAN_CLEANUP_MIN_AGE_SECONDS": "3600",
            }
            with patch.dict(os.environ, environment, clear=True):
                cleaned = controller.cleanup_provider_orphans(now=fixed_now)
        self.assertEqual(cleaned["status"], "completed")
        self.assertEqual(cleaned["deleted"], 1)
        self.assertEqual(provider.deleted, ["101"])
        with self.database.connect() as connection:
            event = connection.execute(
                "SELECT event_type, metadata_json FROM infrastructure_events "
                "WHERE event_type = 'provider_orphan_deleted'"
            ).fetchone()
        self.assertIsNotNone(event)
        self.assertIn("101", event["metadata_json"])

    def test_provider_orphan_cleanup_requires_exact_confirmation(self):
        provider = FakeProvider()
        controller = FleetController(self.database, provider)
        with patch.dict(
            os.environ,
            {
                "AURIX_ORPHAN_CLEANUP_ENABLED": "1",
                "AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED": "1",
                "AURIX_ORPHAN_CLEANUP_CONFIRMATION": "yes",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(InfrastructureError, "exactly equal"):
                controller.cleanup_provider_orphans()

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
