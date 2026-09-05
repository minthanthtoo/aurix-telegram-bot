from __future__ import annotations

import json
import base64
import gzip
import hashlib
import os
import tempfile
import tarfile
from io import BytesIO
import unittest
from unittest.mock import patch
from pathlib import Path

from deploy.fleet_reconcile import (
    FleetError,
    environment,
    parse_access_text,
    parse_manifest,
    materialize_trust_files,
    bootstrap,
    probe_agent_settings,
    server_config,
    update_env_file,
)


def manifest(**overrides: object) -> str:
    node = {
        "id": "sg-a",
        "label": "Singapore A",
        "host": "192.0.2.10",
        "api_port": 61603,
        "keys_port": 443,
        "dns_name": "sg-a.vpn.example.com",
        "max_keys": 20,
        "reserved_keys": 2,
        "tier_slots": {"FREE300MB": 4},
        "plan_slots": {"basic_50gb": 3},
    }
    node.update(overrides)
    return json.dumps([node])


class FleetManifestTests(unittest.TestCase):
    def test_explicit_fleet_env_file_overrides_stale_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "fleet.env"
            env_file.write_text("AURIX_FLEET_NODES_JSON=file-value\n", encoding="utf-8")
            previous = os.environ.get("AURIX_FLEET_NODES_JSON")
            os.environ["AURIX_FLEET_NODES_JSON"] = "stale-value"
            try:
                values = environment(env_file)
            finally:
                if previous is None:
                    os.environ.pop("AURIX_FLEET_NODES_JSON", None)
                else:
                    os.environ["AURIX_FLEET_NODES_JSON"] = previous
        self.assertEqual(values["AURIX_FLEET_NODES_JSON"], "file-value")

    def test_parses_capacity_policy(self) -> None:
        node = parse_manifest(manifest())[0]
        self.assertEqual(node.node_id, "sg-a")
        self.assertEqual(node.dns_name, "sg-a.vpn.example.com")
        self.assertEqual(node.tier_slots["FREE300MB"], 4)

    def test_rejects_hostname_and_duplicate_endpoint(self) -> None:
        with self.assertRaises(FleetError):
            parse_manifest(manifest(host="vpn.example.com"))
        item = json.loads(manifest())[0]
        duplicate = {**item, "id": "sg-b"}
        with self.assertRaises(FleetError):
            parse_manifest(json.dumps([item, duplicate]))

    def test_rejects_invalid_capacity_and_provider_identity(self) -> None:
        with self.assertRaises(FleetError):
            parse_manifest(manifest(max_keys=2, reserved_keys=2))
        with self.assertRaises(FleetError):
            parse_manifest(manifest(provider="digitalocean", provider_resource_id="droplet-x"))
        with self.assertRaises(FleetError):
            parse_manifest(manifest(dns_name="not a hostname"))
        with self.assertRaises(FleetError):
            parse_manifest(manifest(dns_name="192.0.2.10"))

    def test_rejects_duplicate_provider_resource_identity(self) -> None:
        first = json.loads(manifest(provider="digitalocean", provider_resource_id="123"))[0]
        second = {**first, "id": "sg-b", "host": "192.0.2.11", "api_port": 61604}
        with self.assertRaisesRegex(FleetError, "more than one node"):
            parse_manifest(json.dumps([first, second]))

    def test_rejects_unknown_or_overallocated_plan_policy(self) -> None:
        with self.assertRaises(FleetError):
            parse_manifest(manifest(plan_slots={"enterprise_1tb": 1}))
        with self.assertRaises(FleetError):
            parse_manifest(manifest(
                max_keys=5,
                reserved_keys=2,
                tier_slots={"FREE300MB": 2},
                plan_slots={"basic_50gb": 2},
            ), strict_allocations=True)

    def test_access_identity_is_bound_to_manifest(self) -> None:
        node = parse_manifest(manifest())[0]
        fingerprint = "a" * 64
        identity = parse_access_text(
            f"apiUrl:https://192.0.2.10:61603/abcdefghijklmnop\ncertSha256:{fingerprint}\n",
            node,
        )
        self.assertEqual(identity["cert_sha256"], fingerprint)
        with self.assertRaises(FleetError):
            parse_access_text(
                f"apiUrl:https://192.0.2.11:61603/abcdefghijklmnop\ncertSha256:{fingerprint}\n",
                node,
            )

    def test_generated_runtime_config_excludes_ssh_coordinates(self) -> None:
        node = parse_manifest(manifest(provider="digitalocean", provider_resource_id="123"))[0]
        config = server_config(
            [node], {node.node_id: {"api_url": "https://secret", "cert_sha256": "a" * 64}}
        )
        self.assertNotIn("ssh_user", config[0])
        self.assertEqual(config[0]["provider_resource_id"], "123")

    def test_environment_update_is_atomic_and_preserves_unrelated_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.env"
            path.write_text("KEEP=this\nOUTLINE_DEFAULT_SERVER_ID=old\n", encoding="utf-8")
            self.assertTrue(update_env_file(path, {"OUTLINE_DEFAULT_SERVER_ID": "sg-a"}))
            self.assertIn("KEEP=this", path.read_text(encoding="utf-8"))
            self.assertIn("OUTLINE_DEFAULT_SERVER_ID=sg-a", path.read_text(encoding="utf-8"))
            self.assertFalse(update_env_file(path, {"OUTLINE_DEFAULT_SERVER_ID": "sg-a"}))

    def test_materializes_portable_trust_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "fleet-key"
            hosts = Path(directory) / "known-hosts"
            materialize_trust_files({
                "AURIX_FLEET_SSH_KEY": str(key),
                "AURIX_FLEET_KNOWN_HOSTS": str(hosts),
                "AURIX_FLEET_SSH_PRIVATE_KEY_B64": base64.b64encode(
                    b"-----BEGIN OPENSSH PRIVATE KEY-----\ntest\n"
                ).decode(),
                "AURIX_FLEET_KNOWN_HOSTS_B64": base64.b64encode(
                    b"192.0.2.10 ssh-ed25519 test\n"
                ).decode(),
            })
            self.assertIn(b"PRIVATE KEY", key.read_bytes())
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            self.assertIn(b"ssh-ed25519", hosts.read_bytes())

    def test_probe_agent_settings_is_explicit_and_bundle_is_non_secret(self) -> None:
        node = parse_manifest(manifest())[0]
        base_env = {
            "AURIX_PROBE_AGENT_INSTALL_ENABLED": "1",
            "AURIX_PROBE_API_URL": "https://control.example",
            "AURIX_PROBE_AGENT_SECRETS_JSON": json.dumps({"sg-a": "node-secret-123456"}),
            "AURIX_FLEET_REVISION": "a" * 40,
        }
        settings = probe_agent_settings(node, base_env)
        self.assertIsNotNone(settings)
        assert settings is not None
        self.assertEqual(settings["agent_id"], "sg-a")
        self.assertEqual(settings["secret"], "node-secret-123456")
        self.assertNotIn(settings["secret"], settings["bundle_b64"])
        archive = base64.b64decode(settings["bundle_b64"])
        self.assertEqual(settings["bundle_sha256"], hashlib.sha256(archive).hexdigest())
        with tarfile.open(fileobj=BytesIO(gzip.decompress(archive)), mode="r:") as bundle:
            self.assertEqual(
                bundle.getnames(),
                ["fleet_probe.py", "fleet_probe_api.py", "fleet_probe_agent.py"],
            )
        with self.assertRaisesRegex(FleetError, "public HTTPS"):
            probe_agent_settings(node, {**base_env, "AURIX_PROBE_API_URL": "http://control.example"})
        with self.assertRaisesRegex(FleetError, "secret is missing"):
            probe_agent_settings(node, {**base_env, "AURIX_PROBE_AGENT_SECRETS_JSON": "{}"})

    def test_bootstrap_keeps_agent_secret_out_of_ssh_command(self) -> None:
        node = parse_manifest(manifest())[0]
        env = {
            "AURIX_FLEET_CONTROL_PLANE_SOURCE": "192.0.2.7/32",
            "AURIX_FLEET_REVISION": "a" * 40,
            "AURIX_PROBE_AGENT_INSTALL_ENABLED": "1",
            "AURIX_PROBE_API_URL": "https://control.example",
            "AURIX_PROBE_AGENT_SECRETS_JSON": json.dumps({"sg-a": "node-secret-123456"}),
        }
        captured: dict[str, object] = {}

        def fake_ssh(_node, _env, command, *, stdin=None):
            captured["command"] = command
            captured["stdin"] = stdin
            return '{"status":"ready"}'

        with patch("deploy.fleet_reconcile.run_ssh", side_effect=fake_ssh):
            result = bootstrap(node, env)
        self.assertEqual(result["status"], "ready")
        self.assertNotIn("node-secret-123456", str(captured["command"]))
        self.assertTrue(bytes(captured["stdin"]).startswith(b"node-secret-123456\n"))


if __name__ == "__main__":
    unittest.main()
