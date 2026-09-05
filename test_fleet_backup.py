from __future__ import annotations

import io
import hashlib
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from deploy import fleet_backup
from deploy.fleet_backup import (
    metadata_path,
    mirror_offsite,
    validate_archive,
    verify_archive,
    verify_node,
    write_private,
)
from deploy.fleet_reconcile import FleetError, FleetNode


def archive_with(names: list[str]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name in names:
            payload = b"test"
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return output.getvalue()


class FleetBackupTests(unittest.TestCase):
    def test_accepts_complete_archive(self) -> None:
        validate_archive(archive_with(["access.txt", "persisted-state/config.yml"]))

    def test_rejects_path_traversal(self) -> None:
        with self.assertRaises(FleetError):
            validate_archive(archive_with(["access.txt", "persisted-state/../../root/.ssh/key"]))

    def test_rejects_incomplete_archive(self) -> None:
        with self.assertRaises(FleetError):
            validate_archive(archive_with(["access.txt"]))

    def test_verifies_encrypted_archive_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "20260903T000000Z.tar.gz.fernet"
            key = Fernet.generate_key().decode()
            ciphertext = Fernet(key.encode()).encrypt(
                archive_with(["access.txt", "persisted-state/config.yml"])
            )
            write_private(archive, ciphertext)
            write_private(metadata_path(archive), json.dumps({
                "node_id": "sg-a",
                "created_at": "2026-09-03T00:00:00+00:00",
                "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
            }).encode())

            result = verify_archive({"AURIX_FLEET_BACKUP_KEY": key}, archive)

            self.assertEqual(result["archive"], str(archive))

    def test_rejects_tampered_encrypted_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "20260903T000000Z.tar.gz.fernet"
            key = Fernet.generate_key().decode()
            ciphertext = Fernet(key.encode()).encrypt(
                archive_with(["access.txt", "persisted-state/config.yml"])
            )
            write_private(archive, ciphertext + b"x")
            write_private(metadata_path(archive), json.dumps({
                "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
            }).encode())

            with self.assertRaises(FleetError):
                verify_archive({"AURIX_FLEET_BACKUP_KEY": key}, archive)

    def test_rejects_archive_bound_to_a_different_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "20260903T000000Z.tar.gz.fernet"
            key = Fernet.generate_key().decode()
            ciphertext = Fernet(key.encode()).encrypt(
                archive_with(["access.txt", "persisted-state/config.yml"])
            )
            write_private(archive, ciphertext)
            write_private(metadata_path(archive), json.dumps({
                "node_id": "sg-a",
                "created_at": "2026-09-03T00:00:00+00:00",
                "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
            }).encode())

            with self.assertRaisesRegex(FleetError, "node mismatch"):
                verify_archive(
                    {"AURIX_FLEET_BACKUP_KEY": key},
                    archive,
                    expected_node_id="bkk-a",
                )

    def test_mirrors_and_verifies_offsite_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            node = FleetNode("sg-a", "Singapore A", "192.0.2.10", 61603, 443)
            archive = root / "local" / node.node_id / "20260903T000000Z.tar.gz.fernet"
            key = Fernet.generate_key().decode()
            ciphertext = Fernet(key.encode()).encrypt(
                archive_with(["access.txt", "persisted-state/config.yml"])
            )
            write_private(archive, ciphertext)
            write_private(metadata_path(archive), json.dumps({
                "node_id": node.node_id,
                "created_at": "2026-09-03T00:00:00+00:00",
                "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
            }).encode())
            env = {
                "AURIX_FLEET_BACKUP_KEY": key,
                "AURIX_FLEET_BACKUP_DIR": str(root / "local"),
                "AURIX_FLEET_BACKUP_OFFSITE_DIR": str(root / "offsite"),
            }

            offsite = mirror_offsite(node, env, archive)
            result = verify_node(node, env)

            self.assertEqual(offsite, root / "offsite" / node.node_id / archive.name)
            self.assertIn("offsite", result)

    def test_requires_offsite_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            node = FleetNode("sg-a", "Singapore A", "192.0.2.10", 61603, 443)
            archive = root / "local" / node.node_id / "20260903T000000Z.tar.gz.fernet"
            key = Fernet.generate_key().decode()
            ciphertext = Fernet(key.encode()).encrypt(
                archive_with(["access.txt", "persisted-state/config.yml"])
            )
            write_private(archive, ciphertext)
            write_private(metadata_path(archive), json.dumps({
                "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
            }).encode())
            env = {
                "AURIX_FLEET_BACKUP_KEY": key,
                "AURIX_FLEET_BACKUP_DIR": str(root / "local"),
                "AURIX_FLEET_BACKUP_REQUIRE_OFFSITE": "1",
            }

            with self.assertRaises(FleetError):
                verify_node(node, env)

    def test_verifies_offsite_node_archive_when_fresh_host_has_no_local_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            node = FleetNode("sg-a", "Singapore A", "192.0.2.10", 61603, 443)
            offsite = root / "offsite" / node.node_id
            offsite.mkdir(parents=True)
            archive = offsite / "20260904T000000Z.tar.gz.fernet"
            archive.write_bytes(b"ciphertext")
            write_private(metadata_path(archive), b"{}")
            env = {
                "AURIX_FLEET_BACKUP_KEY": Fernet.generate_key().decode(),
                "AURIX_FLEET_BACKUP_DIR": str(root / "local-missing"),
                "AURIX_FLEET_BACKUP_OFFSITE_DIR": str(root / "offsite"),
            }
            with patch.object(fleet_backup, "verify_archive", return_value={"ok": True}):
                result = verify_node(node, env)
            self.assertNotIn("local", result)
            self.assertEqual(result["offsite"], {"ok": True})

    def test_object_store_replaces_offsite_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            node = FleetNode("sg-a", "Singapore A", "192.0.2.10", 61603, 443)
            archive = root / "local" / node.node_id / "20260903T000000Z.tar.gz.fernet"
            key = Fernet.generate_key().decode()
            ciphertext = Fernet(key.encode()).encrypt(
                archive_with(["access.txt", "persisted-state/config.yml"])
            )
            write_private(archive, ciphertext)
            write_private(metadata_path(archive), json.dumps({
                "node_id": node.node_id,
                "created_at": "2026-09-03T00:00:00+00:00",
                "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
            }).encode())
            env = {
                "AURIX_FLEET_BACKUP_KEY": key,
                "AURIX_FLEET_BACKUP_DIR": str(root / "local"),
                "AURIX_BACKUP_OBJECT_STORE_URL": "s3://bucket/aurix",
            }
            objects: dict[str, bytes] = {}

            def put(_env: dict[str, str], object_key: str, content: bytes) -> str:
                objects[object_key] = content
                return f"s3://bucket/aurix/{object_key}"

            def get(_env: dict[str, str], object_key: str) -> bytes:
                return objects[object_key]

            with patch.object(fleet_backup.offsite_storage, "put", put), \
                    patch.object(fleet_backup.offsite_storage, "get", get), \
                    patch.object(fleet_backup.offsite_storage, "prune", return_value=0), \
                    patch.object(
                        fleet_backup.offsite_storage,
                        "latest_key",
                        lambda _env, prefix, suffix: sorted(
                            item for item in objects
                            if item.startswith(prefix) and item.endswith(suffix)
                        )[-1],
                    ):
                mirror_offsite(node, env, archive)
                result = verify_node(node, env)

            self.assertIn("offsite", result)
            self.assertTrue(any(item.startswith("fleet/sg-a/") for item in objects))


if __name__ == "__main__":
    unittest.main()
