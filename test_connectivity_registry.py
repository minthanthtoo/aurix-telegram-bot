import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from commerce import CommerceDatabase, CommerceService
from connectivity_registry import ConnectivityRegistry
from persistence import open_sqlite_connection


class _Pool:
    default_server_id = "sg-b"

    def server_ids(self):
        return ("sg-a", "sg-b")


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


if __name__ == "__main__":
    unittest.main()
