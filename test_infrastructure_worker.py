import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from commerce import CommerceDatabase
from deploy.infrastructure_worker import _auto_activate, _declared_activation_node, _due_jobs


class InfrastructureWorkerTest(unittest.TestCase):
    class Controller:
        def __init__(self):
            self.activated = []

        def mark_provision_activated(self, job_id, node_id):
            self.activated.append((job_id, node_id))
            return {"job_id": job_id, "status": "completed", "node_id": node_id}

    def test_activation_requires_exact_manifest_identity_and_pinned_paths(self):
        manifest = [{
            "id": "sg-b",
            "label": "Singapore B",
            "host": "203.0.113.10",
            "provider": "digitalocean",
            "provider_resource_id": "42",
            "api_port": 61603,
            "keys_port": 443,
            "max_keys": 10,
            "reserved_keys": 2,
        }]
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "aurix.env"
            env_file.write_text(
                "AURIX_FLEET_NODES_JSON='" + json.dumps(manifest) + "'\n"
                "AURIX_FLEET_SSH_KEY=/run/aurix/key\n"
                "AURIX_FLEET_KNOWN_HOSTS=/run/aurix/known_hosts\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                match = _declared_activation_node(
                    provider_resource_id="42", public_ip="203.0.113.10", env_file=env_file
                )
                mismatch = _declared_activation_node(
                    provider_resource_id="42", public_ip="203.0.113.11", env_file=env_file
                )
        self.assertIsNotNone(match)
        self.assertEqual(match[0], "sg-b")
        self.assertIsNone(mismatch)

    def test_due_jobs_include_waiting_only_when_activation_gate_is_on(self):
        with tempfile.TemporaryDirectory() as directory:
            database = CommerceDatabase(Path(directory) / "worker.db")
            database.initialize()
            now = "2026-09-01T00:00:00+00:00"
            with database.connect() as connection:
                connection.execute(
                    """INSERT INTO infrastructure_jobs
                       (id, operation, status, attempts, next_attempt_at,
                        request_fingerprint, created_at)
                       VALUES (?, 'provision', 'awaiting_verification', 1, ?, ?, ?)""",
                    ("job", now, "fingerprint", now),
                )
            with patch.dict(os.environ, {"AURIX_INFRASTRUCTURE_AUTO_ACTIVATION_ENABLED": "0"}, clear=False):
                self.assertEqual(_due_jobs(database), [])
            with patch.dict(os.environ, {"AURIX_INFRASTRUCTURE_AUTO_ACTIVATION_ENABLED": "1"}, clear=False):
                self.assertEqual([row["id"] for row in _due_jobs(database)], ["job"])

    def test_auto_activation_runs_reconciler_then_commits_audit_transition(self):
        manifest = [{
            "id": "sg-b",
            "label": "Singapore B",
            "host": "203.0.113.10",
            "provider": "digitalocean",
            "provider_resource_id": "42",
            "api_port": 61603,
            "keys_port": 443,
            "max_keys": 10,
            "reserved_keys": 2,
        }]
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "aurix.env"
            env_file.write_text(
                "AURIX_FLEET_NODES_JSON='" + json.dumps(manifest) + "'\n"
                "AURIX_FLEET_SSH_KEY=/run/aurix/key\n"
                "AURIX_FLEET_KNOWN_HOSTS=/run/aurix/known_hosts\n",
                encoding="utf-8",
            )
            controller = self.Controller()
            waiting = {
                "job_id": "job",
                "status": "awaiting_verification",
                "provider_resource_id": "42",
                "public_ip": "203.0.113.10",
            }
            with patch.dict(
                os.environ,
                {
                    "AURIX_INFRASTRUCTURE_AUTO_ACTIVATION_ENABLED": "1",
                    "AURIX_FLEET_ENV_FILE": str(env_file),
                },
                clear=False,
            ), patch("deploy.infrastructure_worker.subprocess.run") as run:
                run.return_value.returncode = 0
                result = _auto_activate(controller, "job", waiting)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(controller.activated, [("job", "sg-b")])
        command = run.call_args.args[0]
        self.assertIn("reconcile", command)
        self.assertIn(str(env_file), command)


if __name__ == "__main__":
    unittest.main()
