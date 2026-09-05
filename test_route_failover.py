import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet

from commerce import CommerceService
from commerce_repositories import CommerceDatabase
from connectivity_registry import ConnectivityRegistry
from identity import IdentityService
from route_failover import FailoverError, RouteFailoverService


class _Node:
    def __init__(self, key_id: str | None = None):
        self.keys = {}
        self.usage = {}
        if key_id:
            self.keys[key_id] = {"id": key_id, "name": "source", "accessUrl": f"ss://{key_id}"}
            self.usage[key_id] = 0

    def get_key(self, key_id):
        value = self.keys.get(str(key_id))
        return dict(value) if value else None

    def create_key_with_id(self, key_id, name, limit_bytes):
        value = {"id": str(key_id), "name": name, "accessUrl": f"ss://{key_id}"}
        self.keys[str(key_id)] = value
        self.usage[str(key_id)] = 0
        return dict(value)

    def create_key(self, name, limit_bytes):
        return self.create_key_with_id(f"generated-{len(self.keys)}", name, limit_bytes)

    def set_data_limit(self, key_id, limit_bytes):
        self.limit = (str(key_id), int(limit_bytes))

    def transfer_metrics(self):
        return {"bytesTransferredByUserId": dict(self.usage)}

    def delete_key(self, key_id):
        self.keys.pop(str(key_id), None)

    def server_info(self):
        return {"name": "healthy-outline"}

    def list_keys(self):
        return {"accessKeys": list(self.keys.values())}


class _Pool:
    default_server_id = "sg-a"

    def __init__(self):
        self.clients = {"sg-a": _Node("key-a"), "sg-b": _Node()}

    def client(self, server_id=None):
        return self.clients[str(server_id or self.default_server_id)]


class RouteFailoverServiceTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = CommerceDatabase(Path(self.tempdir.name) / "failover.db")
        self.database.initialize()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO users (telegram_id, first_name, created_at) VALUES (123, 'Member', ?)",
                ("2026-09-05T00:00:00+00:00",),
            )
            for server_id in ("sg-a", "sg-b"):
                connection.execute(
                    """INSERT INTO outline_servers
                       (server_id, label, enabled, health_status, lifecycle_state, created_at, updated_at)
                       VALUES (?, ?, 1, 'healthy', 'active', ?, ?)""",
                    (server_id, server_id, "2026-09-05T00:00:00+00:00", "2026-09-05T00:00:00+00:00"),
                )
                ConnectivityRegistry.sync_outline_endpoint(
                    connection,
                    server_id=server_id,
                    label=server_id,
                    region="Singapore",
                    health_status="healthy",
                    now_text="2026-09-05T00:00:00+00:00",
                )
        self.identity = IdentityService(self.database)
        self.failover = RouteFailoverService(self.database)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_repeated_failure_queues_one_cooldown_gated_decision(self):
        entitlement_id = self.identity.ensure_key_entitlement(
            123,
            server_id="sg-a",
            local_key_ref="key-a",
            kind="free",
            quota_bytes=10_000,
            expires_at="2026-09-06T00:00:00+00:00",
            now="2026-09-05T00:00:00+00:00",
        )
        with self.database.connect() as connection:
            endpoint = connection.execute(
                "SELECT endpoint_id FROM connectivity_endpoints WHERE outline_server_id = 'sg-a'"
            ).fetchone()
            ConnectivityRegistry.bind_credential(
                connection,
                telegram_id=123,
                server_id="sg-a",
                external_id="key-a",
                secret_ciphertext="cipher-a",
                now_text="2026-09-05T00:00:00+00:00",
                profile_kind="free",
            )
            credential = connection.execute(
                "SELECT credential_id FROM connectivity_credentials WHERE external_id = 'key-a'"
            ).fetchone()
        generation_id = self.identity.ensure_generation_for_credential(
            entitlement_id,
            str(endpoint["endpoint_id"]),
            credential_id=str(credential["credential_id"]),
            now="2026-09-05T00:00:00+00:00",
        )
        self.failover.configure_policy(
            entitlement_id,
            enabled=True,
            failure_threshold=3,
            cooldown_seconds=300,
            now="2026-09-05T00:00:00+00:00",
        )
        for second in (1, 2, 3):
            result = self.failover.observe(
                generation_id,
                outcome="failure",
                network_bucket="mobile-mm",
                reason="data_plane_timeout",
                observed_at=f"2026-09-05T00:00:0{second}+00:00",
            )
        self.assertIsNotNone(result["decision_id"])
        duplicate = self.failover.observe(
            generation_id,
            outcome="failure",
            network_bucket="mobile-mm",
            reason="data_plane_timeout",
            observed_at="2026-09-05T00:00:03+00:00",
        )
        self.assertTrue(duplicate["duplicate"])
        decisions = self.failover.decisions(entitlement_id=entitlement_id)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["state"], "pending")
        self.assertNotEqual(decisions[0]["source_route_id"], decisions[0]["target_route_id"])
        self.assertEqual(self.failover.claim(now="2026-09-05T00:00:04+00:00")["state"], "creating")

    def test_disabled_policy_does_not_create_decision(self):
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT route_id FROM connectivity_routes ORDER BY route_id LIMIT 1"
            ).fetchone()
        # An unknown generation is rejected before any state is created.
        with self.assertRaisesRegex(FailoverError, "generation"):
            self.failover.observe("missing-generation", outcome="failure")
        self.assertIsNotNone(row)

    def test_commerce_worker_verifies_target_and_commits_new_generation(self):
        pool = _Pool()
        service = CommerceService(self.database, pool, Fernet.generate_key())
        entitlement_id = self.identity.ensure_key_entitlement(
            123,
            server_id="sg-a",
            local_key_ref="key-a",
            kind="free",
            quota_bytes=10_000,
            expires_at="2026-09-06T00:00:00+00:00",
            now="2026-09-05T00:00:00+00:00",
        )
        with self.database.connect() as connection:
            endpoint = connection.execute(
                "SELECT endpoint_id FROM connectivity_endpoints WHERE outline_server_id = 'sg-a'"
            ).fetchone()
            ConnectivityRegistry.bind_credential(
                connection,
                telegram_id=123,
                server_id="sg-a",
                external_id="key-a",
                secret_ciphertext=service._encrypt_access_url("ss://key-a"),
                now_text="2026-09-05T00:00:00+00:00",
                profile_kind="free",
            )
            credential = connection.execute(
                "SELECT credential_id FROM connectivity_credentials WHERE external_id = 'key-a'"
            ).fetchone()
            connection.execute(
                """INSERT INTO route_health_snapshots
                   (server_id, status, score, sample_count, last_observed_at, reason, updated_at)
                   VALUES ('sg-b', 'healthy', 99, 3, ?, 'fresh_probe_evidence', ?)""",
                ("2026-09-05T00:00:03+00:00", "2026-09-05T00:00:03+00:00"),
            )
        generation_id = self.identity.ensure_generation_for_credential(
            entitlement_id,
            str(endpoint["endpoint_id"]),
            credential_id=str(credential["credential_id"]),
            now="2026-09-05T00:00:00+00:00",
        )
        self.identity.grant_lease(
            entitlement_id,
            str(endpoint["endpoint_id"]),
            generation_id=generation_id,
            lease_bytes=1_000,
            now="2026-09-05T00:00:00+00:00",
        )
        service.configure_route_failover_policy(
            entitlement_id,
            enabled=True,
            failure_threshold=2,
            standby_lease_bytes=100,
            now="2026-09-05T00:00:00+00:00",
        )
        service.observe_route_result(
            generation_id,
            outcome="failure",
            network_bucket="mobile-mm",
            observed_at="2026-09-05T00:00:01+00:00",
        )
        queued = service.observe_route_result(
            generation_id,
            outcome="failure",
            network_bucket="mobile-mm",
            observed_at="2026-09-05T00:00:02+00:00",
        )
        self.assertIsNotNone(queued["decision_id"])
        self.assertEqual(service.process_route_failovers(
            now=datetime.fromisoformat("2026-09-05T00:00:04+00:00")
        ), 1)
        decision = service.route_failover_decisions(entitlement_id=entitlement_id)[0]
        self.assertEqual(decision["state"], "committed")
        with self.database.connect() as connection:
            generations = connection.execute(
                "SELECT endpoint_id, status FROM credential_generations WHERE entitlement_id = ? ORDER BY generation_no",
                (entitlement_id,),
            ).fetchall()
        self.assertEqual([row["status"] for row in generations], ["revoked", "active"])
        self.assertEqual(len(pool.clients["sg-b"].keys), 1)


if __name__ == "__main__":
    unittest.main()
