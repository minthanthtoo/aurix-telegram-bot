import tempfile
import unittest
from pathlib import Path

from commerce_repositories import CommerceDatabase
from fleet_probe import FleetProbeError, FleetProbeService, sign_probe_result


class FleetProbeServiceTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = CommerceDatabase(Path(self.tempdir.name) / "probe.db")
        self.database.initialize()
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO outline_servers
                   (server_id, label, enabled, health_status, lifecycle_state, created_at, updated_at)
                   VALUES ('sg-a', 'Singapore A', 1, 'healthy', 'active', ?, ?)""",
                ("2026-09-05T00:00:00+00:00", "2026-09-05T00:00:00+00:00"),
            )
        self.service = FleetProbeService(
            self.database,
            agent_secrets={"sg-a": "test-agent-secret", "other-agent": "other-secret"},
            stale_after_seconds=900,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_server_agent_job_lifecycle_is_signed_idempotent_and_scored(self):
        self.service.register_target(
            target_id="public-https", label="Public HTTPS", target_kind="public",
            host="198.51.100.10", port=443, scheme="https",
            now="2026-09-05T00:00:00+00:00",
        )
        self.service.register_schedule(
            schedule_id="sg-a-public-https", source_server_id="sg-a", target_id="public-https",
            probe_type="https", interval_seconds=60, timeout_ms=2000,
            now="2026-09-05T00:00:00+00:00",
        )
        self.assertEqual(
            self.service.enqueue_due_probes(now="2026-09-05T00:00:01+00:00"), 1
        )
        jobs = self.service.claim_jobs(
            agent_id="sg-a", source_server_id="sg-a", now="2026-09-05T00:00:02+00:00"
        )
        self.assertEqual(len(jobs), 1)
        payload = {
            "status": "success",
            "latency_ms": 42.0,
            "packet_loss_percent": 0,
            "bytes_transferred": 1024,
            "duration_ms": 20,
            "observed_at": "2026-09-05T00:00:03+00:00",
        }
        signature = sign_probe_result(jobs[0].job_id, payload, "test-agent-secret")
        accepted = self.service.submit_result(
            job_id=jobs[0].job_id, agent_id="sg-a", payload=payload,
            signature=signature, now="2026-09-05T00:00:04+00:00",
        )
        self.assertTrue(accepted["accepted"])
        duplicate = self.service.submit_result(
            job_id=jobs[0].job_id, agent_id="sg-a", payload=payload,
            signature=signature, now="2026-09-05T00:00:05+00:00",
        )
        self.assertTrue(duplicate["duplicate"])
        health = self.service.recompute_health(
            server_id="sg-a", now="2026-09-05T00:00:06+00:00"
        )
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["sample_count"], 1)
        recommendations = self.service.recommendations(
            now="2026-09-05T00:00:07+00:00"
        )
        self.assertEqual(recommendations[0].server_id, "sg-a")
        self.assertEqual(recommendations[0].status, "healthy")

    def test_invalid_signature_and_wrong_agent_are_rejected(self):
        self.service.register_target(
            target_id="node-b", label="Node B", target_kind="server",
            host="198.51.100.11", port=443, now="2026-09-05T00:00:00+00:00",
        )
        self.service.register_schedule(
            schedule_id="sg-a-node-b", source_server_id="sg-a", target_id="node-b",
            probe_type="tcp", interval_seconds=60, timeout_ms=1000,
            now="2026-09-05T00:00:00+00:00",
        )
        self.service.enqueue_due_probes(now="2026-09-05T00:00:01+00:00")
        job = self.service.claim_jobs(
            agent_id="sg-a", source_server_id="sg-a", now="2026-09-05T00:00:02+00:00"
        )[0]
        payload = {"status": "timeout", "observed_at": "2026-09-05T00:00:03+00:00"}
        with self.assertRaisesRegex(FleetProbeError, "signature"):
            self.service.submit_result(
                job_id=job.job_id, agent_id="sg-a", payload=payload,
                signature="bad", now="2026-09-05T00:00:04+00:00",
            )
        signature = sign_probe_result(job.job_id, payload, "other-secret")
        with self.assertRaisesRegex(FleetProbeError, "not assigned"):
            self.service.submit_result(
                job_id=job.job_id, agent_id="other-agent", payload=payload,
                signature=signature, now="2026-09-05T00:00:04+00:00",
            )

    def test_expired_jobs_cannot_be_completed(self):
        self.service.register_target(
            target_id="control", label="Control", target_kind="control_plane",
            host="198.51.100.12", port=443, now="2026-09-05T00:00:00+00:00",
        )
        self.service.register_schedule(
            schedule_id="sg-a-control", source_server_id="sg-a", target_id="control",
            probe_type="tcp", interval_seconds=60, timeout_ms=1000,
            now="2026-09-05T00:00:00+00:00",
        )
        self.service.enqueue_due_probes(now="2026-09-05T00:00:01+00:00")
        jobs = self.service.claim_jobs(
            agent_id="sg-a", source_server_id="sg-a", now="2026-09-05T00:00:02+00:00"
        )
        payload = {"status": "success", "observed_at": "2026-09-05T00:04:00+00:00"}
        signature = sign_probe_result(jobs[0].job_id, payload, "test-agent-secret")
        with self.assertRaisesRegex(FleetProbeError, "expired"):
            self.service.submit_result(
                job_id=jobs[0].job_id, agent_id="sg-a", payload=payload,
                signature=signature, now="2026-09-05T00:04:01+00:00",
            )


if __name__ == "__main__":
    unittest.main()
