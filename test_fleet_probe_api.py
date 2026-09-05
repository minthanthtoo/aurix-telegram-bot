import io
import json
import tempfile
from datetime import datetime, timezone
import unittest
from pathlib import Path

from commerce_repositories import CommerceDatabase
from fleet_probe import FleetProbeService, sign_probe_result
from fleet_probe_api import create_probe_wsgi_app, sign_agent_request


class FleetProbeApiTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = CommerceDatabase(Path(self.tempdir.name) / "api.db")
        self.database.initialize()
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO outline_servers
                   (server_id, label, enabled, health_status, lifecycle_state, created_at, updated_at)
                   VALUES ('sg-a', 'Singapore A', 1, 'healthy', 'active', ?, ?)""",
                ("2026-09-05T00:00:00+00:00", "2026-09-05T00:00:00+00:00"),
            )
        self.service = FleetProbeService(
            self.database, agent_secrets={"sg-a": "secret"}, stale_after_seconds=900
        )
        self.now = datetime(2026, 9, 5, tzinfo=timezone.utc).timestamp() + 2
        self.app = create_probe_wsgi_app(self.service, clock=lambda: self.now)

    def tearDown(self):
        self.tempdir.cleanup()

    def request(self, method, path, body=b"", *, agent="sg-a", signature_secret="secret"):
        timestamp = str(self.now)
        signature = sign_agent_request(method, path, timestamp, body, signature_secret)
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path.split("?", 1)[0],
            "QUERY_STRING": path.split("?", 1)[1] if "?" in path else "",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
            "HTTP_X_AURIX_AGENT_ID": agent,
            "HTTP_X_AURIX_REQUEST_TIMESTAMP": timestamp,
            "HTTP_X_AURIX_REQUEST_SIGNATURE": signature,
        }
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = headers

        response = b"".join(self.app(environ, start_response))
        return captured["status"], json.loads(response)

    def test_authenticated_summary_and_rejected_tampering(self):
        status, value = self.request("GET", "/v1/probes/summary")
        self.assertEqual(status, "200 OK")
        self.assertEqual(value["server_count"], 1)
        status, value = self.request("GET", "/v1/probes/summary", agent="sg-a", signature_secret="wrong")
        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(value["error"], "request_not_authenticated")

    def test_pull_and_submit_round_trip(self):
        self.service.register_target(
            target_id="public", label="Public", target_kind="public",
            host="198.51.100.10", port=443, now="2026-09-05T00:00:00+00:00",
        )
        self.service.register_schedule(
            schedule_id="sg-a-public", source_server_id="sg-a", target_id="public",
            probe_type="tcp", interval_seconds=60, timeout_ms=1000,
            now="2026-09-05T00:00:00+00:00",
        )
        self.service.enqueue_due_probes(now="2026-09-05T00:00:01+00:00")
        status, value = self.request("GET", "/v1/probes/jobs?agent_id=sg-a&limit=1")
        self.assertEqual(status, "200 OK")
        self.assertEqual(len(value["jobs"]), 1)
        job = value["jobs"][0]
        payload = {"status": "success", "latency_ms": 12, "observed_at": "2026-09-05T00:00:02+00:00"}
        envelope = {
            "job_id": job["job_id"], "agent_id": "sg-a", "payload": payload,
            "signature": sign_probe_result(job["job_id"], payload, "secret"),
        }
        status, value = self.request(
            "POST", "/v1/probes/results",
            json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode(),
        )
        self.assertEqual(status, "200 OK")
        self.assertTrue(value["accepted"])


if __name__ == "__main__":
    unittest.main()
