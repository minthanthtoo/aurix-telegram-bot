import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from commerce import CommerceDatabase
from identity import IdentityService
from route_failover import RouteFailoverService


UTC = timezone.utc


class RouteFailoverTest(unittest.TestCase):
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
            connection.execute(
                """INSERT INTO vpn_endpoints
                   (id, code, provider, region, state, accepts_new_assignments, created_at)
                   VALUES ('sg-a', 'SG-A', 'test', 'sgp1', 'ACTIVE', 1, ?)""",
                (self.now.isoformat(),),
            )
            connection.execute(
                """INSERT INTO vpn_endpoints
                   (id, code, provider, region, state, accepts_new_assignments, created_at)
                   VALUES ('bkk-a', 'BKK-A', 'test', 'bkk1', 'ACTIVE', 1, ?)""",
                (self.now.isoformat(),),
            )
        self.identity = IdentityService(self.database)
        self.failover = RouteFailoverService(self.database)

    def tearDown(self):
        self.tmp.cleanup()

    def test_commit_retires_selection_but_keeps_source_accountable(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1")
        source = self.identity.create_generation(
            entitlement, "sg-a", external_id="source-key", usage_baseline_provenance="new"
        )
        self.failover.configure_policy(entitlement, enabled=True, failure_threshold=2, now=self.now)
        first = self.failover.observe(source, outcome="failure", observed_at=self.now)
        second = self.failover.observe(
            source, outcome="failure", observed_at=self.now + timedelta(minutes=1)
        )
        self.assertIsNone(first["decision_id"])
        self.assertIsNotNone(second["decision_id"])
        decision = self.failover.claim(now=self.now + timedelta(minutes=1))
        self.assertEqual(decision["state"], "creating")
        target = self.identity.create_generation(
            entitlement, "bkk-a", external_id="target-key", usage_baseline_provenance="new"
        )
        self.failover.attach_target_generation(decision["decision_id"], target, now=self.now)
        self.failover.mark_committed(decision["decision_id"], now=self.now)
        generations = self.identity.generations_for_accounting(entitlement)
        statuses = {item["external_id"]: item["status"] for item in generations}
        self.assertEqual(statuses, {"source-key": "retiring", "target-key": "active"})

        # The source remains billable while its remote credential may still be
        # usable; failover selection state is not revocation/accounting state.
        usage = self.identity.record_usage(
            entitlement, source, 100, observed_at=self.now + timedelta(minutes=2)
        )
        self.assertEqual(usage["credited_bytes"], 100)  # newly owned credentials start at zero
        self.assertEqual(self.identity.generations_for_accounting(entitlement)[0]["status"], "retiring")

    def test_duplicate_observation_and_stale_worker_claim_are_safe(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1")
        source = self.identity.create_generation(
            entitlement, "sg-a", external_id="source-key", usage_baseline_provenance="new"
        )
        self.failover.configure_policy(entitlement, enabled=True, failure_threshold=1, now=self.now)
        observed = self.failover.observe(source, outcome="failure", observed_at=self.now)
        duplicate = self.failover.observe(source, outcome="failure", observed_at=self.now)
        self.assertFalse(observed["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE failover_decisions SET state = 'creating', locked_at = ?
                   WHERE decision_id = ?""",
                ((self.now - timedelta(hours=1)).isoformat(), observed["decision_id"]),
            )
        reclaimed = self.failover.claim(now=self.now)
        self.assertEqual(reclaimed["state"], "creating")
        self.assertEqual(reclaimed["attempts"], 1)

    def test_operator_drain_pauses_source_and_queues_idempotent_cohort(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1")
        source = self.identity.create_generation(
            entitlement, "sg-a", external_id="source-key", usage_baseline_provenance="new"
        )
        self.identity.ensure_generation_lease(
            entitlement,
            source,
            "sg-a",
            1000,
            (self.now + timedelta(days=30)).isoformat(),
            now=self.now,
        )
        preview = self.failover.endpoint_drain_preview("sg-a", limit=10)
        self.assertEqual(preview["target_endpoint_id"], "bkk-a")
        self.assertEqual(preview["active_generations"], 1)
        result = self.failover.request_endpoint_drain(
            "sg-a", actor_id=999, limit=10, now=self.now
        )
        self.assertEqual(result["queued"], 1)
        self.assertTrue(result["paused"])
        repeated = self.failover.request_endpoint_drain(
            "sg-a", actor_id=999, limit=10, now=self.now
        )
        self.assertEqual(repeated["queued"], 0)
        self.assertEqual(repeated["existing"], 1)
        with self.database.connect() as connection:
            endpoint = connection.execute(
                "SELECT accepts_new_assignments FROM vpn_endpoints WHERE id = 'sg-a'"
            ).fetchone()
        self.assertFalse(bool(endpoint["accepts_new_assignments"]))
        decision = self.failover.claim(now=self.now)
        self.assertEqual(decision["decision_id"], result["decision_ids"][0])


if __name__ == "__main__":
    unittest.main()
