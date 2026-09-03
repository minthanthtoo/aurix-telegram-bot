from __future__ import annotations

import json
import base64
import tempfile
import unittest
from pathlib import Path

from deploy.fleet_reconcile import (
    FleetError,
    parse_access_text,
    parse_manifest,
    materialize_trust_files,
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


if __name__ == "__main__":
    unittest.main()
