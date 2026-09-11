import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from commerce import CommerceDatabase
from identity import IdentityService
from route_failover import RouteFailoverService
from aurix_vpn.route_failover import FailoverError


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
            for endpoint in ("sg-a", "bkk-a"):
                connection.execute(
                    """INSERT INTO endpoint_protocol_profiles
                       (profile_id, endpoint_id, protocol, adapter_type, status, created_at)
                       VALUES (?, ?, 'outline', 'outline', 'enabled', ?)""",
                    (f"outline:{endpoint}", endpoint, self.now.isoformat()),
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
        self.assertEqual(decision["policy_version"], 1)
        target = self.identity.create_generation(
            entitlement, "bkk-a", external_id="target-key", usage_baseline_provenance="new"
        )
        self.failover.attach_target_generation(decision["decision_id"], target, now=self.now)
        self.failover.mark_committed(decision["decision_id"], now=self.now)
        with self.database.connect() as connection:
            audit_actions = connection.execute(
                """SELECT action FROM audit_events
                    WHERE target_type = 'failover_decision' AND target_id = ?
                    ORDER BY id""",
                (decision["decision_id"],),
            ).fetchall()
        self.assertEqual(
            [row["action"] for row in audit_actions],
            ["failover_decision_created", "failover_decision_committed"],
        )
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

    def test_global_safety_pause_blocks_failover_but_keeps_health_evidence(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1")
        source = self.identity.create_generation(
            entitlement, "sg-a", external_id="source-key", usage_baseline_provenance="new"
        )
        self.failover.configure_policy(entitlement, enabled=True, failure_threshold=1, now=self.now)
        configured = self.failover.configure_safety_control(
            "global", paused=True, actor_id=999, now=self.now
        )
        self.assertTrue(bool(configured["paused"]))

        blocked = self.failover.observe(source, outcome="failure", observed_at=self.now)
        self.assertIsNone(blocked["decision_id"])
        self.assertTrue(blocked["failover_blocked"])
        self.assertIn("global:global", blocked["failover_block_reason"])
        with self.database.connect() as connection:
            observation = connection.execute(
                "SELECT COUNT(*) AS n FROM route_observations WHERE generation_id = ?",
                (source,),
            ).fetchone()
            audit = connection.execute(
                """SELECT COUNT(*) AS n FROM audit_events
                    WHERE action = 'failover_blocked_by_safety_control'"""
            ).fetchone()
        self.assertEqual(int(observation["n"]), 1)
        self.assertEqual(int(audit["n"]), 1)

        self.failover.configure_safety_control("global", paused=False, now=self.now)
        resumed = self.failover.observe(
            source, outcome="failure", observed_at=self.now + timedelta(minutes=1)
        )
        self.assertIsNotNone(resumed["decision_id"])
        self.assertFalse(resumed["failover_blocked"])

    def test_policy_revision_is_captured_by_automatic_decision(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1")
        source = self.identity.create_generation(
            entitlement, "sg-a", external_id="source-key", usage_baseline_provenance="new"
        )
        first = self.failover.configure_policy(
            entitlement, enabled=True, failure_threshold=1, now=self.now
        )
        second = self.failover.configure_policy(
            entitlement, enabled=True, failure_threshold=1, cooldown_seconds=600, now=self.now
        )
        self.assertEqual(first["policy_version"], 1)
        self.assertEqual(second["policy_version"], 2)
        observed = self.failover.observe(source, outcome="failure", observed_at=self.now)
        self.assertIsNotNone(observed["decision_id"])
        with self.database.connect() as connection:
            decision = connection.execute(
                "SELECT policy_version FROM failover_decisions WHERE decision_id = ?",
                (observed["decision_id"],),
            ).fetchone()
        self.assertEqual(decision["policy_version"], 2)
        versions = self.failover.policy_versions(entitlement)
        self.assertEqual([item["policy_version"] for item in versions], [2, 1])
        self.assertEqual(versions[0]["cooldown_seconds"], 600)
        explanation = self.failover.decision_explanation(observed["decision_id"])
        self.assertTrue(bool(explanation["policy_snapshot_available"]))
        self.assertEqual(explanation["policy_version"], 2)
        self.assertEqual(explanation["policy_failure_threshold"], 1)
        self.assertEqual(explanation["policy_cooldown_seconds"], 600)

    def test_failover_target_reservation_counts_queued_decisions(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1")
        first = self.identity.create_generation(
            entitlement, "sg-a", external_id="first-key", usage_baseline_provenance="new"
        )
        second = self.identity.create_generation(
            entitlement, "sg-a", external_id="second-key", usage_baseline_provenance="new"
        )
        self.failover.configure_policy(entitlement, enabled=True, failure_threshold=1, now=self.now)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE vpn_endpoints SET max_active_keys = 1 WHERE id = 'bkk-a'"
            )
            connection.execute(
                "UPDATE vpn_endpoints SET state = 'RETIRED', accepts_new_assignments = 0 WHERE id = 'legacy-default'"
            )

        first_result = self.failover.observe(first, outcome="failure", observed_at=self.now)
        second_result = self.failover.observe(
            second, outcome="failure", observed_at=self.now + timedelta(minutes=1)
        )
        self.assertIsNotNone(first_result["decision_id"])
        self.assertIsNone(second_result["decision_id"])
        self.assertIsNone(second_result["target_endpoint_id"])
        with self.database.connect() as connection:
            reserved = connection.execute(
                """SELECT COUNT(*) AS n FROM failover_decisions
                    WHERE target_endpoint_id = 'bkk-a'
                      AND state IN ('pending', 'creating', 'verified')"""
            ).fetchone()
        self.assertEqual(reserved["n"], 1)

    def test_region_budget_blocks_new_failover_decisions_and_is_observable(self):
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO users (telegram_id, first_name, created_at) VALUES (456, 'Second', ?)",
                (self.now.isoformat(),),
            )
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, status, created_at)
                   VALUES ('order-2', 456, 'basic_50gb', 1, 'MMK', 'approved', ?)""",
                (self.now.isoformat(),),
            )
            connection.execute(
                """INSERT INTO subscriptions
                   (id, order_id, telegram_id, plan_code, starts_at, expires_at,
                    quota_bytes, duration_days, status)
                   VALUES ('sub-2', 'order-2', 456, 'basic_50gb', ?, ?, 1000, 30, 'active')""",
                (self.now.isoformat(), (self.now + timedelta(days=30)).isoformat()),
            )
        first_entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1")
        second_entitlement = self.identity.ensure_subscription_entitlement(456, "sub-2")
        first = self.identity.create_generation(
            first_entitlement, "sg-a", external_id="first-key", usage_baseline_provenance="new"
        )
        second = self.identity.create_generation(
            second_entitlement, "sg-a", external_id="second-key", usage_baseline_provenance="new"
        )
        for entitlement, generation in ((first_entitlement, first), (second_entitlement, second)):
            self.identity.ensure_generation_lease(
                entitlement,
                generation,
                "sg-a",
                1000,
                (self.now + timedelta(days=30)).isoformat(),
                now=self.now,
            )
            self.failover.configure_policy(
                entitlement, enabled=True, failure_threshold=1, now=self.now
            )
        self.failover.configure_safety_control(
            "region", "sgp1", max_migrations_per_window=1, now=self.now
        )

        first_result = self.failover.observe(first, outcome="failure", observed_at=self.now)
        second_result = self.failover.observe(
            second, outcome="failure", observed_at=self.now + timedelta(minutes=1)
        )
        self.assertIsNotNone(first_result["decision_id"])
        self.assertIsNone(second_result["decision_id"])
        self.assertTrue(second_result["failover_blocked"])
        self.assertIn("region:sgp1", second_result["failover_block_reason"])

        controls = self.failover.safety_controls(now=self.now + timedelta(minutes=1))
        region = next(item for item in controls if item["scope"] == "region")
        self.assertEqual(region["migration_count"], 1)
        self.assertEqual(region["remaining_migrations"], 0)

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
                "SELECT state, accepts_new_assignments FROM vpn_endpoints WHERE id = 'sg-a'"
            ).fetchone()
        self.assertEqual(endpoint["state"], "DRAINING")
        self.assertFalse(bool(endpoint["accepts_new_assignments"]))
        controls = self.failover.safety_controls(now=self.now)
        global_control = next(item for item in controls if item["scope"] == "global")
        self.assertEqual(global_control["migration_count"], 1)
        decision = self.failover.claim(now=self.now)
        self.assertEqual(decision["decision_id"], result["decision_ids"][0])
        self.assertEqual(decision["policy_version"], 1)

    def test_protocol_profile_is_required_for_failover_and_drain(self):
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-1")
        source = self.identity.create_generation(
            entitlement,
            "sg-a",
            protocol="xray",
            external_id="xray-source",
            usage_baseline_provenance="new",
        )
        self.identity.ensure_generation_lease(
            entitlement,
            source,
            "sg-a",
            1000,
            (self.now + timedelta(days=30)).isoformat(),
            now=self.now,
        )
        self.failover.configure_policy(entitlement, enabled=True, failure_threshold=1, now=self.now)
        observed = self.failover.observe(source, outcome="failure", observed_at=self.now)
        self.assertIsNone(observed["decision_id"])
        with self.assertRaisesRegex(FailoverError, "enabled xray protocol profile"):
            self.failover.request_endpoint_drain(
                "sg-a", target_endpoint_id="bkk-a", limit=10, now=self.now
            )
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO endpoint_protocol_profiles
                   (profile_id, endpoint_id, protocol, adapter_type, status, created_at)
                   VALUES ('xray:bkk-a', 'bkk-a', 'xray', 'xray', 'enabled', ?)""",
                (self.now.isoformat(),),
            )
        observed = self.failover.observe(
            source, outcome="failure", observed_at=self.now + timedelta(minutes=1)
        )
        self.assertIsNotNone(observed["decision_id"])

    def test_degraded_endpoint_is_not_selected_as_migration_target(self):
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE vpn_endpoints SET state = 'DEGRADED' WHERE id = 'bkk-a'"
            )
        preview = self.failover.endpoint_drain_preview(
            "sg-a", target_endpoint_id="bkk-a", limit=10
        )
        self.assertFalse(preview["target_available"])
        self.assertEqual(preview["target_endpoint_id"], "bkk-a")


if __name__ == "__main__":
    unittest.main()
