from __future__ import annotations

import unittest
from unittest.mock import patch

from deploy import offsite_storage
from deploy.fleet_reconcile import FleetError


class OffsiteStorageTests(unittest.TestCase):
    def env(self) -> dict[str, str]:
        return {
            "AURIX_BACKUP_OBJECT_STORE_URL": "s3://aurix-backups/prod",
            "AURIX_BACKUP_OBJECT_STORE_ENDPOINT": "https://account.r2.cloudflarestorage.com",
            "AURIX_BACKUP_OBJECT_STORE_REGION": "auto",
            "AURIX_BACKUP_OBJECT_STORE_ACCESS_KEY_ID": "access",
            "AURIX_BACKUP_OBJECT_STORE_SECRET_ACCESS_KEY": "secret",
        }

    def test_parses_s3_compatible_store(self) -> None:
        store = offsite_storage.from_env(self.env())

        self.assertEqual(store.bucket, "aurix-backups")
        self.assertEqual(store.prefix, "prod")
        self.assertEqual(offsite_storage.join_key(store, "fleet/sg-a/file"), "prod/fleet/sg-a/file")

    def test_rejects_incomplete_store(self) -> None:
        env = self.env()
        env["AURIX_BACKUP_OBJECT_STORE_SECRET_ACCESS_KEY"] = ""

        with self.assertRaises(FleetError):
            offsite_storage.from_env(env)

    def test_lists_keys_under_configured_prefix(self) -> None:
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
          <Contents><Key>prod/database/a.sqlite3.fernet</Key></Contents>
          <Contents><Key>prod/database/a.sqlite3.fernet.json</Key></Contents>
        </ListBucketResult>"""

        with patch.object(offsite_storage, "_request", return_value=xml):
            keys = offsite_storage.list_keys(self.env(), "database/")

        self.assertEqual(keys, ["database/a.sqlite3.fernet", "database/a.sqlite3.fernet.json"])


if __name__ == "__main__":
    unittest.main()
