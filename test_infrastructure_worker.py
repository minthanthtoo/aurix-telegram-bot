import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from commerce import CommerceDatabase
from cryptography.fernet import Fernet
from deploy.infrastructure_worker import (
    _append_pinned_host,
    _auto_activate,
    _declared_activation_node,
    _due_jobs,
    _enrollment_identity,
)
from fleet_enrollment import create_pending_enrollment, generate_token, receive_enrollment


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
            with patch.dict(os.environ, {"AURIX_INFRASTRUCTURE_AUTO_ACTIVATION_ENABLED": "0"}, clear=True):
                self.assertEqual(_due_jobs(database), [])
            with patch.dict(os.environ, {"AURIX_INFRASTRUCTURE_AUTO_ACTIVATION_ENABLED": "1"}, clear=True):
                self.assertEqual([row["id"] for row in _due_jobs(database)], ["job"])
            with patch.dict(
                os.environ,
                {
                    "AURIX_INFRASTRUCTURE_AUTO_ACTIVATION_ENABLED": "0",
                    "AURIX_FLEET_AUTO_REGISTRATION_ENABLED": "1",
                    "AURIX_FLEET_REGISTRATION_ENABLED": "1",
                },
                clear=True,
            ):
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

    def test_enrollment_identity_requires_provider_bound_outline_url_and_host_key(self):
        payload = {
            "public_ip": "203.0.113.10",
            "access_txt": "apiUrl:https://203.0.113.10:61603/abcdefghijklmnop\ncertSha256:" + "a" * 64,
            "ssh_host_key": "ssh-ed25519 " + "A" * 44,
        }
        identity = _enrollment_identity(payload, "203.0.113.10")
        self.assertEqual(identity["api_port"], 61603)
        with self.assertRaisesRegex(Exception, "provider IP|provider"):
            _enrollment_identity(dict(payload, public_ip="203.0.113.11"), "203.0.113.10")

    def test_pinned_host_append_is_idempotent_and_conflicts_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            known_hosts = Path(directory) / "known_hosts"
            known_hosts.write_text("203.0.113.9 ssh-ed25519 BBB\n", encoding="utf-8")
            original = _append_pinned_host(known_hosts, "203.0.113.10", "ssh-ed25519 AAA")
            self.assertIn(b"203.0.113.10 ssh-ed25519 AAA", known_hosts.read_bytes())
            self.assertEqual(_append_pinned_host(known_hosts, "203.0.113.10", "ssh-ed25519 AAA"), known_hosts.read_bytes())
            with self.assertRaisesRegex(Exception, "conflicts"):
                _append_pinned_host(known_hosts, "203.0.113.10", "ssh-ed25519 CCC")
            self.assertIn(b"203.0.113.9", original)

    def test_auto_registration_reconciles_and_consumes_enrollment(self):
        class Provider:
            def droplet(self, resource_id):
                self.resource_id = resource_id
                return {
                    "status": "active",
                    "region": {"slug": "sgp1"},
                    "networks": {"v4": [{"type": "public", "ip_address": "203.0.113.10"}]},
                }

        class RegistrationController:
            def __init__(self, database):
                self.database = database
                self.provider = Provider()
                self.activated = []

            def mark_provision_activated(self, job_id, node_id):
                self.activated.append((job_id, node_id))
                return {"job_id": job_id, "status": "completed", "node_id": node_id}

        with tempfile.TemporaryDirectory() as directory:
            database = CommerceDatabase(Path(directory) / "worker.db")
            database.initialize()
            now = "2026-09-04T00:00:00+00:00"
            with database.connect() as connection:
                connection.execute(
                    """INSERT INTO infrastructure_jobs
                       (id, operation, status, attempts, next_attempt_at,
                        request_fingerprint, provider_resource_id, created_at)
                       VALUES (?, 'provision', 'awaiting_verification', 1, ?, ?, ?, ?)""",
                    ("job-auto-1", now, "fingerprint", "42", now),
                )
            key = Fernet.generate_key().decode()
            token = generate_token()
            create_pending_enrollment(database, job_id="job-auto-1", token=token)
            receive_enrollment(
                database,
                token=token,
                payload={
                    "job_id": "job-auto-1",
                    "node_id": "auto-node-1",
                    "public_ip": "203.0.113.10",
                    "access_txt": "apiUrl:https://203.0.113.10:61603/abcdefghijklmnop\ncertSha256:" + "a" * 64,
                    "ssh_host_key": "ssh-ed25519 " + "A" * 44,
                },
                encryption_key=key,
            )
            env_file = Path(directory) / "aurix.env"
            known_hosts = Path(directory) / "known_hosts"
            ssh_key = Path(directory) / "id_ed25519"
            ssh_key.write_text("private", encoding="utf-8")
            known_hosts.write_text("203.0.113.9 ssh-ed25519 BBB\n", encoding="utf-8")
            env_file.write_text(
                "AURIX_FLEET_NODES_JSON='" + json.dumps([{
                    "id": "sg-a", "label": "Singapore A", "host": "203.0.113.9",
                    "api_port": 61603, "keys_port": 443, "provider": "digitalocean",
                    "provider_resource_id": "9", "max_keys": 10, "reserved_keys": 2,
                }]) + "'\n",
                encoding="utf-8",
            )
            controller = RegistrationController(database)
            waiting = {"job_id": "job-auto-1", "status": "awaiting_verification", "provider_resource_id": "42"}
            with patch.dict(
                os.environ,
                {
                    "AURIX_FLEET_AUTO_REGISTRATION_ENABLED": "1",
                    "AURIX_FLEET_REGISTRATION_ENABLED": "1",
                    "AURIX_FLEET_ENROLLMENT_KEY": key,
                    "AURIX_FLEET_ENV_FILE": str(env_file),
                    "AURIX_FLEET_KNOWN_HOSTS": str(known_hosts),
                    "AURIX_FLEET_SSH_KEY": str(ssh_key),
                    "AURIX_SCALE_KEYS_PORT": "443",
                    "AURIX_SCALE_SSH_PORT": "22",
                },
                clear=False,
            ), patch("deploy.infrastructure_worker.subprocess.run") as run:
                run.return_value.returncode = 0
                result = _auto_activate(controller, "job-auto-1", waiting)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(controller.activated, [("job-auto-1", "auto-node-1")])
            self.assertIn(b"auto-node-1", env_file.read_bytes())
            with database.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM infrastructure_enrollments WHERE job_id = ?", ("job-auto-1",)
                    ).fetchone()["status"],
                    "consumed",
                )


if __name__ == "__main__":
    unittest.main()
