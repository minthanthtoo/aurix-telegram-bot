import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from commerce import CommerceDatabase
from identity import IdentityService
from route_failover import RouteFailoverService
from aurix_vpn.failover_worker import RouteFailoverExecutor


UTC = timezone.utc


class _Adapter:
    def __init__(self, *, healthy=True):
        self.healthy = healthy
        self.revoked = []

    def provision(self, route, intent):
        return {
            "protocol": route["protocol"],
            "route_id": route["route_id"],
            "endpoint_id": route["endpoint_id"],
            "external_id": intent["external_id"],
            "access_url": "vless://target",
            "ownership": "owned",
        }

    def probe_management(self, route):
        return {"status": "healthy" if self.healthy else "failed"}

    def probe_data_plane(self, route):
        return {"status": "healthy" if self.healthy else "failed"}

    def revoke_auth(self, grant):
        self.revoked.append(grant["external_id"])

    def verify_auth_revoked(self, grant):
        return {"verified": grant["external_id"] in self.revoked}


class FailoverWorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = CommerceDatabase(Path(self.tmp.name) / "failover.db")
        self.database.initialize()
        self.now = datetime(2026, 9, 9, tzinfo=UTC)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO users (telegram_id, first_name, created_at) VALUES (123, 'Member', ?)",
                (self.now.isoformat(),),
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
                    quota_bytes, duration_days, status)
                   VALUES ('sub-1', 'order-1', 123, 'basic_50gb', ?, ?, 1000, 30, 'active')""",
                (self.now.isoformat(), (self.now + timedelta(days=30)).isoformat()),
            )
            for endpoint in ("sg-a", "bkk-a"):
                connection.execute(
                    """INSERT INTO vpn_endpoints
                       (id, code, provider, region, state, accepts_new_assignments, created_at)
                       VALUES (?, ?, 'test', 'test', 'ACTIVE', 1, ?)""",
                    (endpoint, endpoint.upper(), self.now.isoformat()),
                )
                connection.execute(
                    """INSERT INTO endpoint_protocol_profiles
                       (profile_id, endpoint_id, protocol, adapter_type, status, created_at)
                       VALUES (?, ?, 'xray', 'xray', 'enabled', ?)""",
                    (f"xray:{endpoint}", endpoint, self.now.isoformat()),
                )
        self.identity = IdentityService(self.database)
        self.failover = RouteFailoverService(self.database)
        self.entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1")
        self.source = self.identity.create_generation(
            self.entitlement,
            "sg-a",
            protocol="xray",
            external_id="source-key",
            access_url_ciphertext="enc:ss://source",
            usage_baseline_provenance="new",
        )
        self.identity.ensure_generation_lease(
            self.entitlement,
            self.source,
            "sg-a",
            1000,
            (self.now + timedelta(days=30)).isoformat(),
            now=self.now,
        )
        self.failover.configure_policy(self.entitlement, enabled=True, failure_threshold=1, now=self.now)
        self.failover.observe(self.source, outcome="failure", observed_at=self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def test_provision_probe_transfer_and_commit_are_ordered(self):
        adapter = _Adapter()
        transfers = []
        executor = RouteFailoverExecutor(
            self.database,
            identity=self.identity,
            failover=self.failover,
            route_provider=lambda endpoint: {"endpoint_id": endpoint, "protocol": "xray", "route_id": f"xray:{endpoint}"},
            adapter_provider=lambda route: adapter,
            assignment_transfer=lambda entitlement, endpoint, reason: transfers.append(
                (entitlement, endpoint, reason)
            ) or {"changed": True},
            access_url_encryptor=lambda value: f"enc:{value}",
            clock=lambda: self.now,
            require_data_plane_probe=True,
        )
        result = executor.run_once(now=self.now)
        self.assertEqual(result["status"], "committed")
        generations = self.identity.generations_for_accounting(self.entitlement)
        statuses = {item["external_id"]: item["status"] for item in generations}
        self.assertEqual(statuses["source-key"], "retiring")
        self.assertEqual(statuses["aurix-failover-" + result["decision_id"]], "active")
        with self.database.connect() as connection:
            lease = connection.execute(
                "SELECT generation_id FROM quota_leases WHERE entitlement_key = ? AND status = 'active'",
                (self.entitlement,),
            ).fetchone()
        self.assertEqual(lease["generation_id"], result["target_generation_id"])
        self.assertEqual(transfers, [(self.entitlement, "bkk-a", "failover")])

    def test_failed_probe_revokes_owned_target_and_rolls_back(self):
        adapter = _Adapter(healthy=False)
        executor = RouteFailoverExecutor(
            self.database,
            identity=self.identity,
            failover=self.failover,
            route_provider=lambda endpoint: {"endpoint_id": endpoint, "protocol": "xray", "route_id": f"xray:{endpoint}"},
            adapter_provider=lambda route: adapter,
            access_url_encryptor=lambda value: f"enc:{value}",
            clock=lambda: self.now,
            require_data_plane_probe=True,
        )
        result = executor.run_once(now=self.now)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(len(adapter.revoked), 1)
        self.assertEqual(self.failover.decisions()[0]["state"], "rolled_back")

    def test_protocol_aware_route_provider_receives_source_protocol(self):
        adapter = _Adapter()
        calls = []

        def route_provider(endpoint_id, protocol):
            calls.append((endpoint_id, protocol))
            return {
                "endpoint_id": endpoint_id,
                "protocol": protocol,
                "route_id": f"{protocol}:{endpoint_id}",
                "public_address": "198.51.100.10",
            }

        result = RouteFailoverExecutor(
            self.database,
            identity=self.identity,
            failover=self.failover,
            route_provider=route_provider,
            adapter_provider=lambda _route: adapter,
            access_url_encryptor=lambda value: f"enc:{value}",
            clock=lambda: self.now,
            require_data_plane_probe=True,
        ).run_once(now=self.now)
        self.assertEqual(result["status"], "committed")
        self.assertEqual(calls, [("bkk-a", "xray")])


if __name__ == "__main__":
    unittest.main()
