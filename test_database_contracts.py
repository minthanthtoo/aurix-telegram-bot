"""Execute the same persistence contracts on SQLite and disposable PostgreSQL."""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from commerce_sqlite_database import CommerceDatabase
from commerce_postgres_database import PostgresCommerceDatabase
from commerce_endpoint_migration_repository import EndpointMigrationRepository
from free_repository import Database as FreeDatabase
from notification_outbox import NotificationOutbox
from schema_migrations import MigrationError


class DatabaseContracts:
    database: CommerceDatabase | PostgresCommerceDatabase

    def test_outbox_claims_are_exclusive_and_expired_lease_is_recoverable(self):
        now = "2026-09-06T00:00:00+00:00"
        expiry = "2026-09-06T00:02:00+00:00"
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO notifications
                   (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("outbox-contract", "outbox-contract", 7004, "contract", "ready", "pending", now, now),
            )
        outbox = NotificationOutbox(self.database)
        barrier = Barrier(2)

        def claim():
            barrier.wait(timeout=10)
            with outbox(write=True) as transaction:
                return transaction.claim(now, 1, expiry)

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(lambda _: claim(), range(2)))
        self.assertEqual(sorted(len(batch) for batch in claims), [0, 1])
        with outbox(write=True) as transaction:
            recovered = transaction.claim(expiry, 1, "2026-09-06T00:04:00+00:00")
        self.assertEqual([row.values["id"] for row in recovered], ["outbox-contract"])

    def test_schema_startup_is_idempotent(self):
        with self.database.connect() as connection:
            before = connection.execute(
                "SELECT component, version, name FROM schema_migrations ORDER BY component, version"
            ).fetchall()
        self.database.initialize()
        with self.database.connect() as connection:
            after = connection.execute(
                "SELECT component, version, name FROM schema_migrations ORDER BY component, version"
            ).fetchall()
        self.assertEqual([dict(r) for r in before], [dict(r) for r in after])
        self.assertTrue(after)

    def test_base_adoption_history_is_versioned_and_immutable(self):
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT name FROM schema_migrations WHERE component = ? AND version = 1",
                ("commerce_base",),
            ).fetchone()
            self.assertIsNotNone(row)
            connection.execute(
                "UPDATE schema_migrations SET name = ? WHERE component = ? AND version = 1",
                ("renamed-by-contract", "commerce_base"),
            )
        with self.assertRaises(MigrationError):
            self.database.initialize()

    def test_transaction_rollback_preserves_committed_state(self):
        with self.assertRaisesRegex(RuntimeError, "abort contract"):
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                connection.execute(
                    "INSERT INTO users (telegram_id, first_name, created_at) VALUES (?, ?, ?)",
                    (7001, "Rollback", "2026-09-06T00:00:00+00:00"),
                )
                raise RuntimeError("abort contract")
        with self.database.connect() as connection:
            self.assertIsNone(connection.execute(
                "SELECT telegram_id FROM users WHERE telegram_id = ?", (7001,),
            ).fetchone())

    def test_concurrent_unique_insert_has_one_winner(self):
        barrier = Barrier(2)

        def insert_user():
            barrier.wait(timeout=10)
            try:
                with self.database.connect() as connection:
                    self.database.begin_write(connection)
                    connection.execute(
                        "INSERT INTO users (telegram_id, first_name, created_at) VALUES (?, ?, ?)",
                        (7002, "Concurrent", "2026-09-06T00:00:00+00:00"),
                    )
                return True
            except Exception as error:
                if self.database.is_integrity_error(error):
                    return False
                raise

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: insert_user(), range(2)))
        self.assertEqual(sorted(results), [False, True])
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM users WHERE telegram_id = ?", (7002,),
            ).fetchone()
        self.assertEqual(row["count"], 1)

    def test_migration_reads_use_real_schema_and_dialect(self):
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self.assertIsNone(EndpointMigrationRepository.request_context(
                connection, target="missing-target", source="missing-source", external_id="missing",
            ))
            self.assertIsNone(EndpointMigrationRepository.next_request(
                connection, now_text="2026-09-06T00:00:00+00:00", limit=1,
            ))
            result = EndpointMigrationRepository.complete(
                connection, completed_at="2026-09-06T00:00:00+00:00",
                updated_at="2026-09-06T00:00:00+00:00", job_id="missing", attempt=1,
            )
            self.assertEqual(result.rowcount, 0)

    def test_admin_confirmation_has_one_concurrent_consumer(self):
        now = "2026-09-06T00:00:00+00:00"
        self.confirmations.create_admin_challenge(
            "contract-token", 7003, 7003, "approve", "[]", "contract-state",
            now, "2026-09-06T00:05:00+00:00",
        )
        barrier = Barrier(2)

        def consume():
            barrier.wait(timeout=10)
            return self.confirmations.consume_admin_challenge(
                "contract-token", 7003, 7003, "contract-state", now,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: consume(), range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)


class SQLiteDatabaseContracts(DatabaseContracts, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = CommerceDatabase(Path(self.tmp.name) / "contract.db")
        self.database.initialize()
        self.confirmations = FreeDatabase(self.database.path)
        self.confirmations.initialize()


class PostgreSQLDatabaseContracts(DatabaseContracts, unittest.TestCase):
    def setUp(self):
        dsn = os.environ.get("AURIX_TEST_POSTGRES_DSN")
        if not dsn:
            if os.environ.get("AURIX_REQUIRE_POSTGRES_TESTS") == "1":
                self.fail("Required PostgreSQL contracts need AURIX_TEST_POSTGRES_DSN")
            self.skipTest("PostgreSQL contracts run in the required CI database job")
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        settings = conninfo_to_dict(dsn)
        host = settings.get("host", "")
        if host not in {"localhost", "127.0.0.1", "::1"} and not host.startswith("/"):
            self.fail("Disposable database tests require an explicit local test server")
        # Only this newly created, random database can be removed by cleanup.
        name = "aurix_contract_" + uuid.uuid4().hex
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))

        def drop_database():
            with psycopg.connect(dsn, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))

        self.addCleanup(drop_database)
        self.database = PostgresCommerceDatabase(make_conninfo(dsn, dbname=name))
        self.confirmations = self.database
        self.addCleanup(self.database.close)
        self.database.initialize()
