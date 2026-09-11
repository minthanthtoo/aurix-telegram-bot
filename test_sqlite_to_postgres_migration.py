import importlib.util
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from commerce import CommerceDatabase, PostgresCommerceDatabase
from deploy.migrate_sqlite_to_postgres import dependency_order, migrate, sqlite_manifest
from connectivity import EndpointRegistry
from identity import IdentityService
from route_failover import RouteFailoverService


def _postgres_rehearsal_available() -> bool:
    return (
        all(shutil.which(command) for command in ("initdb", "pg_ctl"))
        and importlib.util.find_spec("psycopg_pool") is not None
    )


class SqliteToPostgresMigrationTest(unittest.TestCase):
    @unittest.skipUnless(
        _postgres_rehearsal_available(),
        "local PostgreSQL binaries and psycopg-pool are required",
    )
    def test_postgres_initializer_and_migration_rehearsal(self):
        with tempfile.TemporaryDirectory(prefix="aurix-postgres-") as directory:
            root = Path(directory)
            data_directory = root / "data"
            socket_directory = root / "socket"
            socket_directory.mkdir()
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            subprocess.run(
                [
                    shutil.which("initdb"),
                    "-D",
                    str(data_directory),
                    "--no-locale",
                    "--encoding=UTF8",
                    "--auth=trust",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                [
                    shutil.which("pg_ctl"),
                    "-D",
                    str(data_directory),
                    "-o",
                    f"-p {port} -k {socket_directory}",
                    "-w",
                    "start",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            database = PostgresCommerceDatabase(
                f"postgresql://127.0.0.1:{port}/postgres"
            )
            try:
                database.initialize()
                source_path = root / "source.db"
                source = CommerceDatabase(source_path)
                source.initialize()
                with source.connect() as connection:
                    connection.execute(
                        "INSERT INTO users (telegram_id, first_name, created_at) VALUES (?, ?, ?)",
                        (777, "Postgres rehearsal", "2026-09-12T00:00:00+00:00"),
                    )
                    connection.execute(
                        """INSERT INTO orders
                           (id, telegram_id, plan_code, amount_minor, currency, status, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            "source-order",
                            777,
                            "basic_50gb",
                            3000,
                            "MMK",
                            "awaiting_payment",
                            "2026-09-12T00:00:00+00:00",
                        ),
                    )
                    connection.execute(
                        """INSERT INTO subscriptions
                           (id, order_id, telegram_id, plan_code, starts_at, expires_at,
                            quota_bytes, duration_days, status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            "source-sub",
                            "source-order",
                            777,
                            "basic_50gb",
                            "2026-09-12T00:00:00+00:00",
                            "2026-10-12T00:00:00+00:00",
                            50 * 1024**3,
                            30,
                            "pending",
                        ),
                    )
                    connection.execute(
                        """INSERT INTO accounts
                           (account_id, status, created_at, updated_at)
                           VALUES (?, ?, ?, ?)""",
                        (
                            "account-777",
                            "active",
                            "2026-09-12T00:00:00+00:00",
                            "2026-09-12T00:00:00+00:00",
                        ),
                    )
                    connection.execute(
                        """INSERT INTO account_identities
                           (account_id, identity_type, identity_value, verified_at, created_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (
                            "account-777",
                            "telegram",
                            "777",
                            "2026-09-12T00:00:00+00:00",
                            "2026-09-12T00:00:00+00:00",
                        ),
                    )
                    connection.execute(
                        """INSERT INTO endpoint_protocol_observations
                           (observation_id, profile_id, endpoint_id, protocol, signal, status,
                            details_json, latency_ms, observed_at, expires_at, source, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            "observation-777",
                            "outline:legacy-default",
                            "legacy-default",
                            "outline",
                            "management",
                            "healthy",
                            "{}",
                            5.0,
                            "2026-09-12T00:00:00+00:00",
                            "2026-09-13T00:00:00+00:00",
                            "postgres-rehearsal",
                            "2026-09-12T00:00:00+00:00",
                        ),
                    )
                result = migrate(source_path, f"postgresql://127.0.0.1:{port}/postgres")
                self.assertEqual(result["total_rows"], 11)
                with database.connect() as connection:
                    profile_type = connection.execute(
                        """SELECT data_type FROM information_schema.columns
                           WHERE table_schema = 'public'
                             AND table_name = 'endpoint_protocol_profiles'
                             AND column_name = 'verified_at'"""
                    ).fetchone()["data_type"]
                    profile_count = connection.execute(
                        """SELECT COUNT(*) AS n FROM endpoint_protocol_profiles
                           WHERE endpoint_id = 'legacy-default' AND protocol = 'outline'"""
                    ).fetchone()["n"]
                    migrated_users = connection.execute(
                        "SELECT COUNT(*) AS n FROM users WHERE telegram_id = 777"
                    ).fetchone()["n"]
                    migrated_accounts = connection.execute(
                        "SELECT COUNT(*) AS n FROM accounts WHERE account_id = 'account-777'"
                    ).fetchone()["n"]
                    migrated_observations = connection.execute(
                        """SELECT COUNT(*) AS n FROM endpoint_protocol_observations
                           WHERE observation_id = 'observation-777'"""
                    ).fetchone()["n"]
                self.assertEqual(profile_type, "timestamp with time zone")
                self.assertEqual(int(profile_count), 1)
                self.assertEqual(int(migrated_users), 1)
                self.assertEqual(int(migrated_accounts), 1)
                self.assertEqual(int(migrated_observations), 1)
                identity = IdentityService(database)
                account_id = identity.ensure_account(
                    778, now="2026-09-12T00:00:00+00:00"
                )
                token = identity.create_pairing_token(
                    778, now="2026-09-12T00:00:00+00:00"
                )
                paired = identity.consume_pairing_token(
                    token,
                    "k" * 32,
                    label="PostgreSQL rehearsal device",
                    now="2026-09-12T00:01:00+00:00",
                )
                self.assertEqual(paired["account_id"], account_id)
                self.assertEqual(
                    identity.device_auth_record(paired["device_id"])["status"],
                    "active",
                )
                self.assertTrue(
                    identity.revoke_device(
                        778,
                        paired["device_id"],
                        now="2026-09-12T00:02:00+00:00",
                    )
                )
                self.assertEqual(
                    identity.device_auth_record(paired["device_id"])["status"],
                    "revoked",
                )
                # PostgreSQL cannot infer the type of an unbound NULL
                # parameter in the optional VPN selectors. Exercise the
                # production repositories with no preferred endpoint and no
                # entitlement filter after the migration rehearsal.
                now = datetime.now(timezone.utc).replace(microsecond=0)
                with database.connect() as connection:
                    connection.execute(
                        "UPDATE vpn_endpoints SET last_healthy_at = ? WHERE id = 'legacy-default'",
                        (now.isoformat(),),
                    )
                registry = EndpointRegistry(database, Fernet.generate_key())
                assignment = registry.ensure_subscription_assignment(
                    "source-sub",
                    "basic_50gb",
                    50 * 1024**3,
                    protocol="outline",
                    now=now,
                )
                self.assertEqual(assignment.endpoint_id, "legacy-default")
                self.assertEqual(RouteFailoverService(database).decisions(limit=10), [])
                self.assertEqual(IdentityService(database).generations_for_accounting(), [])
            finally:
                database.close()
                subprocess.run(
                    [
                        shutil.which("pg_ctl"),
                        "-D",
                        str(data_directory),
                        "-m",
                        "fast",
                        "-w",
                        "stop",
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

    def test_manifest_checks_integrity_counts_rows_and_orders_parents_first(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE orders (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id)
                );
                CREATE TABLE schema_migrations (component TEXT PRIMARY KEY);
                INSERT INTO users VALUES (1, 'A');
                INSERT INTO orders VALUES ('order-1', 1);
                """
            )
            connection.close()

            counts, order, digest = sqlite_manifest(path)

            self.assertEqual(counts, {"users": 1, "orders": 1})
            self.assertLess(order.index("users"), order.index("orders"))
            self.assertEqual(len(digest), 64)

    def test_dependency_cycle_fails_closed(self):
        connection = sqlite3.connect(":memory:")
        connection.executescript(
            """
            CREATE TABLE left_side (
                id INTEGER PRIMARY KEY,
                right_id INTEGER REFERENCES right_side(id)
            );
            CREATE TABLE right_side (
                id INTEGER PRIMARY KEY,
                left_id INTEGER REFERENCES left_side(id)
            );
            """
        )
        with self.assertRaisesRegex(RuntimeError, "dependency cycle"):
            dependency_order(connection, ["left_side", "right_side"])
        connection.close()


if __name__ == "__main__":
    unittest.main()
