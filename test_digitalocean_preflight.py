import base64
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from deploy.digitalocean_preflight import _validate_configuration, _validate_live, main


class DigitalOceanPreflightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Path(self.tmp.name) / "bot.db"
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE ready (id INTEGER)")
        self.environment = {
            "TELEGRAM_BOT_TOKEN": "123456:test-token",
            "OWNER_TELEGRAM_ID": "123456",
            "ADMIN_TELEGRAM_IDS": "",
            "AURIX_ACCESS_URL_KEY": Fernet.generate_key().decode(),
            "OUTLINE_API_URL": "https://outline.example:443/secret-path",
            "OUTLINE_CERT_SHA256": "a" * 64,
            "DATABASE_PATH": str(self.database),
            "COMMERCE_DATABASE_URL": "",
            "ALLOW_TEXT_PAYMENT_REFERENCES": "0",
            "RECEIPT_STORAGE_REQUIRED": "1",
            "SUPABASE_URL": "https://project.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service-role-test-only",
            "SUPABASE_RECEIPTS_BUCKET": "payment-receipts",
            "RECEIPT_LLM_BASE_URL": "https://vision.example/v1",
            "RECEIPT_LLM_MODEL": "vision-model",
            "RECEIPT_LLM_API_KEY": "test-key-long-enough",
            "PAYMENT_RECIPIENTS_JSON": '{"kbzpay":{"names":["merchant"]},"wavepay":{"names":["merchant"]},"ayapay":{"names":["merchant"]},"uabpay":{"names":["merchant"]},"cbpay":{"names":["merchant"]}}',
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_configuration_passes_without_live_network_calls(self):
        with patch.dict(os.environ, self.environment, clear=True):
            main([])

    def test_env_file_parses_json_after_shell_environment_was_sourced(self):
        env_file = Path(self.tmp.name) / "aurix.env"
        lines = []
        for key, value in self.environment.items():
            if key == "PAYMENT_RECIPIENTS_JSON":
                lines.append(f"{key}='{value}'")
            else:
                lines.append(f"{key}={value}")
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        shell_environment = dict(self.environment)
        shell_environment["PAYMENT_RECIPIENTS_JSON"] = "{kbzpay:broken}"
        with patch.dict(os.environ, shell_environment, clear=True):
            main(["--env-file", str(env_file)])

    def test_receipt_storage_cannot_be_disabled(self):
        environment = dict(self.environment, RECEIPT_STORAGE_REQUIRED="0")
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(SystemExit, "RECEIPT_STORAGE_REQUIRED"):
                main([])

    def test_receipt_vision_configuration_is_required(self):
        environment = dict(self.environment)
        environment.pop("RECEIPT_LLM_API_KEY")
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(SystemExit, "RECEIPT_LLM_API_KEY"):
                main([])

    def test_provider_mutation_requires_token_and_ssh_key_attachment(self):
        environment = dict(self.environment)
        environment["AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED"] = "1"
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(SystemExit, "DIGITALOCEAN_API_TOKEN"):
                main([])
        environment["DIGITALOCEAN_API_TOKEN"] = "dop_v1_test"
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(SystemExit, "SSH_KEY_IDS"):
                main([])
        environment["AURIX_DIGITALOCEAN_SSH_KEY_IDS"] = "12345"
        with patch.dict(os.environ, environment, clear=True):
            main([])

    def test_orphan_cleanup_requires_mutation_gate_and_exact_confirmation(self):
        environment = dict(self.environment)
        environment["AURIX_ORPHAN_CLEANUP_ENABLED"] = "1"
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(SystemExit, "MUTATIONS_ENABLED"):
                main([])
        environment.update(
            {
                "AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED": "1",
                "DIGITALOCEAN_API_TOKEN": "dop_v1_test",
                "AURIX_DIGITALOCEAN_SSH_KEY_IDS": "12345",
                "AURIX_ORPHAN_CLEANUP_CONFIRMATION": "wrong",
            }
        )
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(SystemExit, "exactly equal"):
                main([])
        environment["AURIX_ORPHAN_CLEANUP_CONFIRMATION"] = (
            "DELETE-UNREGISTERED-AURIX-NODES"
        )
        with patch.dict(os.environ, environment, clear=True):
            main([])

    def test_live_preflight_canary_checks_provider_inventory_read_only(self):
        environment = dict(self.environment)
        environment["DIGITALOCEAN_API_TOKEN"] = "dop_v1_test"
        with patch.dict(os.environ, environment, clear=True), \
                patch(
                    "deploy.digitalocean_preflight._json_request",
                    side_effect=[{"ok": True}, {}, {"data": [{"id": "vision-model"}]}],
                ), \
                patch("infrastructure.DigitalOceanClient.list_droplets", return_value=[]):
            values = _validate_configuration()
            _validate_live(values)

    def test_live_preflight_rejects_unadvertised_receipt_model(self):
        environment = dict(self.environment)
        with patch.dict(os.environ, environment, clear=True), patch(
            "deploy.digitalocean_preflight._json_request",
            side_effect=[{"ok": True}, {}, {"data": [{"id": "different-model"}]}],
        ):
            values = _validate_configuration()
            with self.assertRaisesRegex(SystemExit, "not advertised"):
                _validate_live(values)

    def fleet_environment(self):
        ssh_key = Path(self.tmp.name) / "fleet_key"
        known_hosts = Path(self.tmp.name) / "known_hosts"
        ssh_key.write_text("test-key", encoding="utf-8")
        known_hosts.write_text("192.0.2.10 ssh-ed25519 test", encoding="utf-8")
        return dict(
            self.environment,
            AURIX_FLEET_NODES_JSON=json.dumps([{
                "id": "sg-a",
                "label": "Singapore A",
                "host": "192.0.2.10",
                "dns_name": "sg-a.vpn.example.com",
                "api_port": 61603,
                "keys_port": 443,
                "max_keys": 10,
                "reserved_keys": 2,
            }]),
            AURIX_FLEET_SSH_KEY=str(ssh_key),
            AURIX_FLEET_KNOWN_HOSTS=str(known_hosts),
            AURIX_FLEET_CONTROL_PLANE_SOURCE="203.0.113.7/32",
            AURIX_FLEET_BACKUP_KEY=Fernet.generate_key().decode(),
        )

    def test_required_offsite_backup_path_must_be_configured(self):
        environment = self.fleet_environment()
        environment["AURIX_FLEET_BACKUP_REQUIRE_OFFSITE"] = "1"
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(SystemExit, "REQUIRE_OFFSITE"):
                main([])

    def test_required_offsite_backup_path_must_exist(self):
        environment = self.fleet_environment()
        environment["AURIX_FLEET_BACKUP_REQUIRE_OFFSITE"] = "1"
        environment["AURIX_FLEET_BACKUP_OFFSITE_DIR"] = str(Path(self.tmp.name) / "missing")
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(SystemExit, "OFFSITE_DIR"):
                main([])

    def test_required_offsite_backup_path_can_pass(self):
        offsite = Path(self.tmp.name) / "offsite"
        offsite.mkdir()
        environment = self.fleet_environment()
        environment["AURIX_FLEET_BACKUP_REQUIRE_OFFSITE"] = "1"
        environment["AURIX_FLEET_BACKUP_OFFSITE_DIR"] = str(offsite)
        with patch.dict(os.environ, environment, clear=True):
            main([])

    def test_required_database_offsite_backup_path_must_exist(self):
        environment = dict(self.environment)
        environment["AURIX_DATABASE_BACKUP_REQUIRE_OFFSITE"] = "1"
        environment["AURIX_DATABASE_BACKUP_OFFSITE_DIR"] = str(Path(self.tmp.name) / "missing")
        environment["AURIX_DATABASE_BACKUP_KEY"] = Fernet.generate_key().decode()
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(SystemExit, "DATABASE_BACKUP_OFFSITE_DIR"):
                main([])

    def test_required_database_offsite_backup_path_can_pass(self):
        offsite = Path(self.tmp.name) / "database-offsite"
        offsite.mkdir()
        environment = dict(self.environment)
        environment["AURIX_DATABASE_BACKUP_REQUIRE_OFFSITE"] = "1"
        environment["AURIX_DATABASE_BACKUP_OFFSITE_DIR"] = str(offsite)
        environment["AURIX_DATABASE_BACKUP_KEY"] = Fernet.generate_key().decode()
        with patch.dict(os.environ, environment, clear=True):
            main([])

    def test_object_store_can_satisfy_required_offsite_paths(self):
        environment = self.fleet_environment()
        environment["AURIX_FLEET_BACKUP_REQUIRE_OFFSITE"] = "1"
        environment["AURIX_DATABASE_BACKUP_REQUIRE_OFFSITE"] = "1"
        environment["AURIX_DATABASE_BACKUP_KEY"] = Fernet.generate_key().decode()
        environment["AURIX_BACKUP_OBJECT_STORE_URL"] = "s3://aurix-backups/prod"
        environment["AURIX_BACKUP_OBJECT_STORE_ENDPOINT"] = "https://object.example"
        environment["AURIX_BACKUP_OBJECT_STORE_REGION"] = "auto"
        environment["AURIX_BACKUP_OBJECT_STORE_ACCESS_KEY_ID"] = "access"
        environment["AURIX_BACKUP_OBJECT_STORE_SECRET_ACCESS_KEY"] = "secret"
        with patch.dict(os.environ, environment, clear=True):
            main([])

    def test_portable_base64_trust_material_is_materialized_before_path_checks(self):
        ssh_key = b"-----BEGIN OPENSSH PRIVATE KEY-----\nrecovery\n-----END OPENSSH PRIVATE KEY-----\n"
        known_hosts = b"192.0.2.10 ssh-ed25519 test\n"
        environment = self.fleet_environment()
        key_path = Path(self.tmp.name) / "portable_key"
        hosts_path = Path(self.tmp.name) / "portable_known_hosts"
        key_path.unlink(missing_ok=True)
        hosts_path.unlink(missing_ok=True)
        environment.update(
            {
                "AURIX_FLEET_SSH_KEY": str(key_path),
                "AURIX_FLEET_KNOWN_HOSTS": str(hosts_path),
                "AURIX_FLEET_SSH_PRIVATE_KEY_B64": base64.b64encode(ssh_key).decode(),
                "AURIX_FLEET_KNOWN_HOSTS_B64": base64.b64encode(known_hosts).decode(),
            }
        )
        with patch.dict(os.environ, environment, clear=True):
            main([])
        self.assertEqual(key_path.read_bytes(), ssh_key)
        self.assertEqual(hosts_path.read_bytes(), known_hosts)
        self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(hosts_path.stat().st_mode & 0o777, 0o600)

    def test_dns_configuration_requires_node_dns_names(self):
        environment = self.fleet_environment()
        manifest = json.loads(environment["AURIX_FLEET_NODES_JSON"])
        manifest[0].pop("dns_name")
        environment["AURIX_FLEET_NODES_JSON"] = json.dumps(manifest)
        environment["AURIX_DNS_PROVIDER"] = "cloudflare"
        environment["AURIX_DNS_ZONE_ID"] = "zone-test"
        environment["AURIX_DNS_API_TOKEN"] = "dns-token"
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(SystemExit, "missing dns_name"):
                main([])

    def test_required_dns_without_configuration_fails(self):
        environment = self.fleet_environment()
        environment["AURIX_DNS_REQUIRE"] = "1"
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(SystemExit, "AURIX_DNS_REQUIRE"):
                main([])

    def test_auto_activation_requires_an_existing_canonical_fleet_env(self):
        environment = dict(self.fleet_environment())
        environment["AURIX_INFRASTRUCTURE_AUTO_ACTIVATION_ENABLED"] = "1"
        environment["AURIX_FLEET_ENV_FILE"] = str(Path(self.tmp.name) / "missing.env")
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(SystemExit, "AURIX_FLEET_ENV_FILE"):
                main([])

    def test_auto_activation_accepts_pinned_canonical_fleet_env(self):
        environment = dict(self.fleet_environment())
        env_file = Path(self.tmp.name) / "fleet.env"
        env_file.write_text("AURIX_FLEET_NODES_JSON=[]\n", encoding="utf-8")
        environment["AURIX_INFRASTRUCTURE_AUTO_ACTIVATION_ENABLED"] = "1"
        environment["AURIX_FLEET_ENV_FILE"] = str(env_file)
        with patch.dict(os.environ, environment, clear=True):
            main([])

    def test_auto_registration_requires_credential_free_endpoint_and_callback_service(self):
        environment = dict(self.fleet_environment())
        env_file = Path(self.tmp.name) / "fleet.env"
        env_file.write_text("AURIX_FLEET_NODES_JSON=[]\n", encoding="utf-8")
        environment.update(
            {
                "AURIX_FLEET_AUTO_REGISTRATION_ENABLED": "1",
                "AURIX_FLEET_REGISTRATION_URL": "http://insecure.example/register",
                "AURIX_FLEET_ENV_FILE": str(env_file),
                "AURIX_FLEET_ENROLLMENT_KEY": Fernet.generate_key().decode(),
            }
        )
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(SystemExit, "HTTPS"):
                main([])
        environment["AURIX_FLEET_REGISTRATION_URL"] = "https://control.example/fleet/register"
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(SystemExit, "REGISTRATION_ENABLED"):
                main([])
        environment["AURIX_FLEET_REGISTRATION_ENABLED"] = "1"
        with patch.dict(os.environ, environment, clear=True):
            main([])


if __name__ == "__main__":
    unittest.main()
