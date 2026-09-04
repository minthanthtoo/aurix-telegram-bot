import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from commerce import CommerceDatabase, CommerceService
from connectivity_registry import ConnectivityRegistry
from persistence import open_sqlite_connection


class _Pool:
    default_server_id = "sg-b"

    def server_ids(self):
        return ("sg-a", "sg-b")


class _Outline:
    def __init__(self, key_id: str, access_url: str):
        self.keys = {key_id: {"id": key_id, "name": "source", "accessUrl": access_url}}
        self.usage = {key_id: 1_000_000}
        self.fail_delete = False

    def get_key(self, key_id):
        value = self.keys.get(str(key_id))
        return dict(value) if value else None

    def list_keys(self):
        return {"accessKeys": [dict(value) for value in self.keys.values()]}

    def transfer_metrics(self):
        return {"bytesTransferredByUserId": dict(self.usage)}

    def create_key_with_id(self, key_id, name, limit_bytes):
        value = {"id": str(key_id), "name": name, "accessUrl": f"ss://{key_id}"}
        self.keys[str(key_id)] = value
        self.usage[str(key_id)] = 0
        return dict(value)

    def set_data_limit(self, key_id, limit_bytes):
        self.limit = (str(key_id), int(limit_bytes))

    def delete_key(self, key_id):
        if self.fail_delete:
            raise RuntimeError("source delete unavailable")
        self.keys.pop(str(key_id), None)


class _MigrationPool(_Pool):
    def __init__(self):
        self.default_server_id = "sg-a"
        self.clients = {"sg-a": _Outline("key-1", "ss://source"), "sg-b": _Outline("none", "ss://none")}

    def client(self, server_id=None):
        return self.clients[str(server_id or self.default_server_id)]


class ConnectivityRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bot.db"
        self.database = CommerceDatabase(self.path)
        self.database.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def test_outline_registration_mirrors_dimensions_without_secrets(self):
        service = CommerceService(self.database, _Pool(), Fernet.generate_key())
        service.register_outline_servers(
            {"sg-a": "Singapore A", "sg-b": "Singapore B"},
            endpoint_metadata={
                "sg-a": {"provider": "digitalocean", "region": "sgp1", "transport": "outline"},
                "sg-b": {"provider": "nube", "region": "sin1", "transport": "outline"},
            },
        )
        with open_sqlite_connection(self.path) as connection:
            endpoints = connection.execute(
                "SELECT endpoint_id, outline_server_id, provider_id, region_id, transport_id, "
                "status, accepts_new_keys, management_secret_ref FROM connectivity_endpoints "
                "ORDER BY outline_server_id"
            ).fetchall()
            self.assertEqual([row[1] for row in endpoints], ["sg-a", "sg-b"])
            self.assertEqual([row[2] for row in endpoints], ["digitalocean", "nube"])
            self.assertEqual([row[3] for row in endpoints], ["digitalocean-sgp1", "nube-sin1"])
            self.assertEqual([row[4] for row in endpoints], ["outline", "outline"])
            self.assertTrue(all(row[5] == "provisioning" for row in endpoints))
            self.assertTrue(all(row[6] == 0 for row in endpoints))
            self.assertTrue(all("OUTLINE_SERVERS_JSON" in row[7] for row in endpoints))
            registry_values = connection.execute(
                "SELECT management_secret_ref FROM connectivity_endpoints"
            ).fetchall()
            self.assertFalse(any("https://" in str(row[0]) for row in registry_values))

    def test_credential_binding_is_idempotent_and_revocable(self):
        service = CommerceService(self.database, _Pool(), Fernet.generate_key())
        service.register_outline_servers({"sg-b": "Singapore B"})
        with open_sqlite_connection(self.path) as connection:
            connection.execute(
                "INSERT INTO users (telegram_id, first_name, created_at) VALUES (?, ?, ?)",
                (123, "Test", "2026-09-04T00:00:00+00:00"),
            )
            ConnectivityRegistry.bind_credential(
                connection,
                telegram_id=123,
                server_id="sg-b",
                external_id="key-1",
                secret_ciphertext="ciphertext",
                now_text="2026-09-04T00:00:00+00:00",
                profile_kind="paid",
                subscription_id=None,
            )
            ConnectivityRegistry.bind_credential(
                connection,
                telegram_id=123,
                server_id="sg-b",
                external_id="key-1",
                secret_ciphertext=None,
                now_text="2026-09-04T00:01:00+00:00",
                profile_kind="paid",
                subscription_id=None,
            )
            count = connection.execute(
                "SELECT COUNT(*) FROM connectivity_credentials WHERE external_id = 'key-1'"
            ).fetchone()[0]
            self.assertEqual(count, 1)
            self.assertEqual(
                connection.execute(
                    "SELECT status, secret_ciphertext FROM connectivity_credentials "
                    "WHERE external_id = 'key-1'"
                ).fetchone()[0],
                "active",
            )
            ConnectivityRegistry.revoke_credential(
                connection,
                server_id="sg-b",
                external_id="key-1",
                now_text="2026-09-04T00:02:00+00:00",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM connectivity_credentials WHERE external_id = 'key-1'"
                ).fetchone()[0],
                "revoked",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM endpoint_assignments"
                ).fetchone()[0],
                "ended",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM connectivity_profiles"
                ).fetchone()[0],
                "ended",
            )

    def test_owner_queued_migration_preserves_remaining_quota_and_cuts_over(self):
        pool = _MigrationPool()
        service = CommerceService(self.database, pool, Fernet.generate_key())
        service.register_outline_servers(
            {"sg-a": "Singapore A", "sg-b": "Singapore B"},
            endpoint_metadata={"sg-a": {"provider": "digitalocean", "region": "sgp1"}, "sg-b": {"provider": "digitalocean", "region": "sgp1"}},
        )
        with open_sqlite_connection(self.path) as connection:
            for server in ("sg-a", "sg-b"):
                ConnectivityRegistry.sync_outline_health(
                    connection,
                    server_id=server,
                    lifecycle_state="active",
                    health_status="healthy",
                    now_text="2026-09-04T00:00:00+00:00",
                )
                connection.execute(
                    "UPDATE outline_servers SET health_status = 'healthy', last_synced_at = ? WHERE server_id = ?",
                    ("2026-09-04T00:00:00+00:00", server),
                )
        service.configure_server_capacity("sg-a", 123, max_keys=10, reserved_keys=1)
        service.configure_plan_allocation("sg-a", "basic_50gb", 5, 123)
        order = service.create_order(123, "Test", "basic_50gb", datetime(2026, 9, 4, tzinfo=timezone.utc))
        with open_sqlite_connection(self.path) as connection:
            connection.execute(
                """INSERT INTO subscriptions
                   (id, order_id, telegram_id, plan_code, starts_at, expires_at,
                    plan_name, quota_bytes, duration_days, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
                (
                    "sub-1", order.order_id, 123, "basic_50gb",
                    "2026-09-04T00:00:00+00:00", "2026-10-04T00:00:00+00:00",
                    "50 GB", 50_000_000_000, 30,
                ),
            )
            encrypted = service._encrypt_access_url("ss://source")
            connection.execute(
                """INSERT INTO paid_vpn_keys
                   (id, subscription_id, telegram_id, outline_key_id, access_url,
                    quota_bytes, status, created_at, server_id)
                   VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                ("paid-1", "sub-1", 123, "key-1", encrypted, 50_000_000_000, "2026-09-04T00:00:00+00:00", "sg-a"),
            )
            ConnectivityRegistry.bind_credential(
                connection,
                telegram_id=123,
                server_id="sg-a",
                external_id="key-1",
                secret_ciphertext=encrypted,
                now_text="2026-09-04T00:00:00+00:00",
                profile_kind="paid",
                subscription_id="sub-1",
            )
        queued = service.queue_endpoint_migration("sg-a", "key-1", "sg-b", 123)
        self.assertEqual(queued["status"], "pending")
        jobs = service.endpoint_migration_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_id"], queued["job_id"])
        self.assertEqual(jobs[0]["job_status"], "pending")
        self.assertNotIn("ss://", str(jobs[0]))
        migration_time = datetime.now(timezone.utc) + timedelta(minutes=2)
        self.assertEqual(
            service.process_endpoint_migrations(
                now=migration_time, max_jobs=1
            ),
            1,
        )
        with open_sqlite_connection(self.path) as connection:
            row = connection.execute(
                "SELECT server_id, outline_key_id, quota_bytes FROM paid_vpn_keys WHERE subscription_id = 'sub-1'"
            ).fetchone()
            self.assertEqual(row[0], "sg-b")
            self.assertTrue(str(row[1]).startswith("aurix-mig-"))
            self.assertEqual(row[2], 49_999_000_000)
            job = connection.execute(
                "SELECT status, source_used_bytes FROM connectivity_migration_jobs WHERE id = ?",
                (queued["job_id"],),
            ).fetchone()
            self.assertEqual(job[0], "completed")
            self.assertEqual(job[1], 1_000_000)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notifications WHERE kind = 'vpn_migrated'").fetchone()[0], 1)
        self.assertEqual(pool.clients["sg-a"].get_key("key-1"), None)

    def test_migration_retries_source_delete_after_cutover(self):
        pool = _MigrationPool()
        pool.clients["sg-a"].fail_delete = True
        service = CommerceService(self.database, pool, Fernet.generate_key())
        service.register_outline_servers(
            {"sg-a": "Singapore A", "sg-b": "Singapore B"},
            endpoint_metadata={"sg-a": {"provider": "digitalocean", "region": "sgp1"}, "sg-b": {"provider": "digitalocean", "region": "sgp1"}},
        )
        with open_sqlite_connection(self.path) as connection:
            for server in ("sg-a", "sg-b"):
                connection.execute(
                    "UPDATE outline_servers SET health_status = 'healthy', last_synced_at = ? WHERE server_id = ?",
                    ("2026-09-04T00:00:00+00:00", server),
                )
                ConnectivityRegistry.sync_outline_health(
                    connection,
                    server_id=server,
                    lifecycle_state="active",
                    health_status="healthy",
                    now_text="2026-09-04T00:00:00+00:00",
                )
        service.configure_server_capacity("sg-a", 123, max_keys=10, reserved_keys=1)
        service.configure_plan_allocation("sg-a", "basic_50gb", 5, 123)
        order = service.create_order(123, "Test", "basic_50gb", datetime(2026, 9, 4, tzinfo=timezone.utc))
        with open_sqlite_connection(self.path) as connection:
            connection.execute(
                """INSERT INTO subscriptions
                   (id, order_id, telegram_id, plan_code, starts_at, expires_at,
                    plan_name, quota_bytes, duration_days, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
                ("sub-1", order.order_id, 123, "basic_50gb", "2026-09-04T00:00:00+00:00", "2026-10-04T00:00:00+00:00", "50 GB", 50_000_000_000, 30),
            )
            encrypted = service._encrypt_access_url("ss://source")
            connection.execute(
                """INSERT INTO paid_vpn_keys
                   (id, subscription_id, telegram_id, outline_key_id, access_url,
                    quota_bytes, status, created_at, server_id)
                   VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                ("paid-1", "sub-1", 123, "key-1", encrypted, 50_000_000_000, "2026-09-04T00:00:00+00:00", "sg-a"),
            )
            ConnectivityRegistry.bind_credential(connection, telegram_id=123, server_id="sg-a", external_id="key-1", secret_ciphertext=encrypted, now_text="2026-09-04T00:00:00+00:00", profile_kind="paid", subscription_id="sub-1")
        queued = service.queue_endpoint_migration("sg-a", "key-1", "sg-b", 123)
        migration_time = datetime.now(timezone.utc) + timedelta(minutes=2)
        self.assertEqual(
            service.process_endpoint_migrations(
                now=migration_time, max_jobs=1
            ),
            1,
        )
        with open_sqlite_connection(self.path) as connection:
            self.assertEqual(connection.execute("SELECT status FROM connectivity_migration_jobs WHERE id = ?", (queued["job_id"],)).fetchone()[0], "source_delete_pending")
        pool.clients["sg-a"].fail_delete = False
        self.assertEqual(
            service.process_endpoint_migrations(
                now=migration_time + timedelta(minutes=2), max_jobs=1
            ),
            1,
        )
        with open_sqlite_connection(self.path) as connection:
            self.assertEqual(connection.execute("SELECT status FROM connectivity_migration_jobs WHERE id = ?", (queued["job_id"],)).fetchone()[0], "completed")


if __name__ == "__main__":
    unittest.main()
