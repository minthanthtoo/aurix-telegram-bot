from __future__ import annotations

import unittest
from unittest.mock import call, patch

from deploy import offsite_storage
from deploy.fleet_reconcile import FleetError
from supabase_storage import SupabaseObjectStore


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

    def supabase_env(self) -> dict[str, str]:
        return {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service-role-secret",
            "SUPABASE_RECEIPTS_BUCKET": "payment-receipts",
            "AURIX_BACKUP_SUPABASE_BUCKET": "aurix-recovery",
            "AURIX_BACKUP_SUPABASE_PREFIX": "production",
        }

    def test_supabase_backend_is_explicit_and_separate_from_receipts(self) -> None:
        env = self.supabase_env()

        self.assertTrue(offsite_storage.configured(env))
        store = offsite_storage.from_env(env)
        self.assertIsInstance(store, SupabaseObjectStore)
        self.assertEqual(store.bucket, "aurix-recovery")
        self.assertEqual(store.prefix, "production")
        self.assertEqual(store._full_path("fleet/bkk-a/archive"), "production/fleet/bkk-a/archive")

        env["AURIX_BACKUP_SUPABASE_BUCKET"] = "payment-receipts"
        with self.assertRaises(FleetError):
            offsite_storage.from_env(env)

    def test_supabase_backend_rejects_partial_configuration(self) -> None:
        env = {"AURIX_BACKUP_SUPABASE_BUCKET": "aurix-recovery"}

        with self.assertRaises(FleetError):
            offsite_storage.from_env(env)

    def test_supabase_lists_and_strips_configured_prefix(self) -> None:
        env = self.supabase_env()
        store = offsite_storage.from_env(env)
        self.assertIsInstance(store, SupabaseObjectStore)
        with patch.object(
            store,
            "_request",
            side_effect=[
                [
                    {"name": "production/database/20260904.sqlite3.fernet"},
                    {"name": "production/database/20260904.sqlite3.fernet.json"},
                ],
            ],
        ):
            self.assertEqual(
                store.list_keys("database/", page_size=10),
                ["database/20260904.sqlite3.fernet", "database/20260904.sqlite3.fernet.json"],
            )

    def test_supabase_list_paginates_without_unbounded_requests(self) -> None:
        env = self.supabase_env()
        store = offsite_storage.from_env(env)
        self.assertIsInstance(store, SupabaseObjectStore)
        page = [{"name": f"production/database/{index:04d}.sqlite3.fernet"} for index in range(2)]
        with patch.object(
            store,
            "_request",
            side_effect=[page, [{"name": "production/database/last.sqlite3.fernet"}]],
        ):
            self.assertEqual(
                store.list_keys("database/", page_size=2),
                [
                    "database/0000.sqlite3.fernet",
                    "database/0001.sqlite3.fernet",
                    "database/last.sqlite3.fernet",
                ],
            )

    def test_prune_removes_old_archive_and_metadata_pairs(self) -> None:
        env = self.env()
        keys = [
            "prod/database/20260101.sqlite3.fernet",
            "prod/database/20260101.sqlite3.fernet.json",
            "prod/database/20260102.sqlite3.fernet",
            "prod/database/20260102.sqlite3.fernet.json",
            "prod/database/20260103.sqlite3.fernet",
            "prod/database/20260103.sqlite3.fernet.json",
        ]
        with patch.object(offsite_storage, "list_keys", return_value=[
            key.removeprefix("prod/") for key in keys
        ]), patch.object(offsite_storage, "delete") as remove:
            removed = offsite_storage.prune(env, "database/", ".sqlite3.fernet", keep=2)

        self.assertEqual(removed, 1)
        self.assertEqual(
            remove.call_args_list,
            [
                call(env, "database/20260101.sqlite3.fernet"),
                call(env, "database/20260101.sqlite3.fernet.json"),
            ],
        )

    def test_retention_count_is_bounded_and_defaults_empty_values(self) -> None:
        self.assertEqual(offsite_storage.retention_count("", "retention", 14), 14)
        self.assertEqual(offsite_storage.retention_count("3650", "retention", 14), 3650)
        for value in ("0", "3651", "not-a-number"):
            with self.assertRaises(FleetError):
                offsite_storage.retention_count(value, "retention", 14)


if __name__ == "__main__":
    unittest.main()
