from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from deploy.fleet_backup import metadata_path, write_private
from deploy import database_backup
from deploy.recovery_readiness import run_audit


def archive_with(names: list[str]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name in names:
            payload = b"test"
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return output.getvalue()


class RecoveryReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env_file = self.root / "aurix.env"
        self.key = Fernet.generate_key().decode()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_env(self, values: dict[str, str]) -> None:
        self.env_file.write_text(
            "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
            encoding="utf-8",
        )

    def base_env(self) -> dict[str, str]:
        database = self.root / "bot.db"
        import sqlite3

        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE ready (id INTEGER)")
        backup = self.root / "db-backups"
        backup.mkdir()
        return {
            "TELEGRAM_BOT_TOKEN": "123456:test-token",
            "OWNER_TELEGRAM_ID": "123456",
            "AURIX_ACCESS_URL_KEY": self.key,
            "SUPABASE_URL": "https://project.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service-role-test-only",
            "RECEIPT_LLM_BASE_URL": "https://vision.example/v1",
            "RECEIPT_LLM_MODEL": "vision-model",
            "RECEIPT_LLM_API_KEY": "test-key-long-enough",
            "DATABASE_PATH": str(database),
            "AURIX_DATABASE_BACKUP_KEY": self.key,
            "AURIX_DATABASE_BACKUP_DIR": str(self.root / "db-local"),
            "AURIX_DATABASE_BACKUP_OFFSITE_DIR": str(backup),
            "AURIX_DATABASE_BACKUP_REQUIRE_OFFSITE": "1",
        }

    def fleet_env(self) -> dict[str, str]:
        local = self.root / "local-backups"
        offsite = self.root / "offsite-backups"
        manifest = json.dumps([{
            "id": "sg-a",
            "label": "Singapore A",
            "host": "192.0.2.10",
            "dns_name": "sg-a.vpn.example.com",
            "api_port": 61603,
            "keys_port": 443,
            "max_keys": 10,
            "reserved_keys": 2,
        }])
        values = self.base_env()
        values.update({
            "AURIX_FLEET_NODES_JSON": manifest,
            "AURIX_FLEET_BACKUP_KEY": self.key,
            "AURIX_FLEET_BACKUP_DIR": str(local),
            "AURIX_FLEET_BACKUP_OFFSITE_DIR": str(offsite),
            "AURIX_FLEET_BACKUP_REQUIRE_OFFSITE": "1",
        })
        database_backup.backup(values)
        ciphertext = Fernet(self.key.encode()).encrypt(
            archive_with(["access.txt", "persisted-state/config.yml"])
        )
        for root in (local, offsite):
            archive = root / "sg-a" / "20260903T000000Z.tar.gz.fernet"
            write_private(archive, ciphertext)
            write_private(metadata_path(archive), json.dumps({
                "node_id": "sg-a",
                "created_at": "2026-09-03T00:00:00+00:00",
                "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
            }).encode())
        return values

    def test_minimal_recovery_state_reports_warnings(self) -> None:
        values = self.base_env()
        values["AURIX_DATABASE_BACKUP_REQUIRE_OFFSITE"] = "0"
        self.write_env(values)

        report = run_audit(self.env_file, verify_archives=False)

        self.assertEqual(report["status"], "warn")
        checks = {item["name"]: item["status"] for item in report["checks"]}
        self.assertEqual(checks["required_secrets"], "pass")
        self.assertEqual(checks["fleet_manifest"], "warn")
        self.assertEqual(checks["dns_automation"], "warn")

    def test_fleet_without_offsite_fails(self) -> None:
        values = self.fleet_env()
        values.pop("AURIX_FLEET_BACKUP_OFFSITE_DIR")
        self.write_env(values)

        report = run_audit(self.env_file, verify_archives=True)

        self.assertEqual(report["status"], "fail")
        checks = {item["name"]: item["status"] for item in report["checks"]}
        self.assertEqual(checks["fleet_offsite"], "fail")

    def test_full_configured_recovery_state_passes(self) -> None:
        values = self.fleet_env()
        values.update({
            "AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED": "1",
            "DIGITALOCEAN_API_TOKEN": "dop_v1_test",
            "AURIX_DNS_PROVIDER": "cloudflare",
            "AURIX_DNS_ZONE_ID": "zone-test",
            "AURIX_DNS_API_TOKEN": "dns-token",
            "AURIX_DNS_REQUIRE": "1",
        })
        self.write_env(values)

        report = run_audit(self.env_file, verify_archives=True)

        self.assertEqual(report["status"], "pass")

    def test_object_store_satisfies_recovery_offsite_checks(self) -> None:
        values = self.fleet_env()
        values.pop("AURIX_DATABASE_BACKUP_OFFSITE_DIR")
        values.pop("AURIX_FLEET_BACKUP_OFFSITE_DIR")
        values.update({
            "AURIX_BACKUP_OBJECT_STORE_URL": "s3://aurix-backups/prod",
            "AURIX_BACKUP_OBJECT_STORE_ENDPOINT": "https://object.example",
            "AURIX_BACKUP_OBJECT_STORE_REGION": "auto",
            "AURIX_BACKUP_OBJECT_STORE_ACCESS_KEY_ID": "access",
            "AURIX_BACKUP_OBJECT_STORE_SECRET_ACCESS_KEY": "secret",
            "AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED": "1",
            "DIGITALOCEAN_API_TOKEN": "dop_v1_test",
            "AURIX_DNS_PROVIDER": "cloudflare",
            "AURIX_DNS_ZONE_ID": "zone-test",
            "AURIX_DNS_API_TOKEN": "dns-token",
            "AURIX_DNS_REQUIRE": "1",
        })
        self.write_env(values)

        with patch("deploy.recovery_readiness.database_backup.verify",
                   return_value={"local": {}, "offsite": {}}), \
                patch("deploy.recovery_readiness.verify_node",
                      return_value={"node": "sg-a", "local": {}, "offsite": {}}):
            report = run_audit(self.env_file, verify_archives=True)

        self.assertEqual(report["status"], "pass")

    def test_supabase_storage_satisfies_recovery_offsite_checks(self) -> None:
        values = self.fleet_env()
        values.pop("AURIX_DATABASE_BACKUP_OFFSITE_DIR")
        values.pop("AURIX_FLEET_BACKUP_OFFSITE_DIR")
        values.update({
            "AURIX_BACKUP_SUPABASE_BUCKET": "aurix-recovery",
            "AURIX_BACKUP_SUPABASE_PREFIX": "production",
            "AURIX_DATABASE_BACKUP_REQUIRE_OFFSITE": "1",
            "AURIX_FLEET_BACKUP_REQUIRE_OFFSITE": "1",
            "AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED": "1",
            "DIGITALOCEAN_API_TOKEN": "dop_v1_test",
            "AURIX_DNS_PROVIDER": "cloudflare",
            "AURIX_DNS_ZONE_ID": "zone-test",
            "AURIX_DNS_API_TOKEN": "dns-token",
            "AURIX_DNS_REQUIRE": "1",
        })
        self.write_env(values)

        with patch("deploy.recovery_readiness.database_backup.verify",
                   return_value={"local": {}, "offsite": {}}), \
                patch("deploy.recovery_readiness.verify_node",
                      return_value={"node": "sg-a", "local": {}, "offsite": {}}):
            report = run_audit(self.env_file, verify_archives=True)

        self.assertEqual(report["status"], "pass")

    def test_required_dns_without_configuration_fails(self) -> None:
        values = self.fleet_env()
        values["AURIX_DNS_REQUIRE"] = "1"
        self.write_env(values)

        report = run_audit(self.env_file, verify_archives=False)

        checks = {item["name"]: item["status"] for item in report["checks"]}
        self.assertEqual(checks["dns_automation"], "fail")


if __name__ == "__main__":
    unittest.main()
