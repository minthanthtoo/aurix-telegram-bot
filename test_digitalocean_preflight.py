import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from deploy.digitalocean_preflight import main


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


if __name__ == "__main__":
    unittest.main()
