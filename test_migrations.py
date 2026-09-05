import tempfile
import unittest
from pathlib import Path

from app import Database
from commerce import CommerceDatabase, PostgresCommerceDatabase
from migrations import (
    Migration,
    MigrationError,
    _add_normalized_payment_reference_guard,
    apply_migrations,
)
from persistence import open_sqlite_connection
from repositories import HostedRepositoryDatabase, RepositoryDatabase


class MigrationRegistryTest(unittest.TestCase):
    def test_payment_reference_guard_fails_closed_on_legacy_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payments.db"
            with open_sqlite_connection(path) as connection:
                connection.executescript(
                    """CREATE TABLE payments (
                           id TEXT PRIMARY KEY,
                           provider TEXT NOT NULL,
                           provider_reference TEXT NOT NULL,
                           normalized_reference TEXT NOT NULL
                       );
                       INSERT INTO payments VALUES ('payment-1', 'manual', 'tx-1', 'tx-1');
                       INSERT INTO payments VALUES ('payment-2', 'MANUAL', 'tx-1', 'tx-1');"""
                )
                with self.assertRaisesRegex(MigrationError, "manual reconciliation"):
                    _add_normalized_payment_reference_guard(connection)

    def test_provider_identity_migration_fails_closed_on_spacing_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "providers.db"
            with open_sqlite_connection(path) as connection:
                connection.executescript(
                    """CREATE TABLE payments (
                           id TEXT PRIMARY KEY,
                           provider TEXT NOT NULL,
                           provider_reference TEXT NOT NULL,
                           normalized_reference TEXT NOT NULL
                       );
                       INSERT INTO payments VALUES ('payment-1', 'Manual', 'tx-1', 'tx-1');
                       INSERT INTO payments VALUES ('payment-2', ' manual ', 'tx-1', 'tx-1');"""
                )
                with self.assertRaisesRegex(MigrationError, "providers/references"):
                    from migrations import _canonicalize_payment_provider_identity

                    _canonicalize_payment_provider_identity(connection)

    def test_component_history_is_idempotent_and_applies_statements_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migrations.db"
            migration = Migration(
                1,
                "create_sample",
                sqlite_statements=("CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY)",),
            )
            with open_sqlite_connection(path) as connection:
                apply_migrations(
                    connection,
                    component="test",
                    dialect="sqlite",
                    migrations=(migration,),
                    applied_at="2026-01-01T00:00:00+00:00",
                )
                apply_migrations(
                    connection,
                    component="test",
                    dialect="sqlite",
                    migrations=(migration,),
                    applied_at="2026-01-02T00:00:00+00:00",
                )
                rows = connection.execute(
                    "SELECT component, version, name, applied_at FROM schema_migrations"
                ).fetchall()
                table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'sample'"
                ).fetchone()

            self.assertEqual(
                [tuple(row) for row in rows],
                [("test", 1, "create_sample", "2026-01-01T00:00:00+00:00")],
            )
            self.assertEqual(table[0], "sample")

    def test_history_rejects_renamed_unknown_duplicate_and_nonpositive_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.db"
            with open_sqlite_connection(path) as connection:
                apply_migrations(
                    connection,
                    component="test",
                    dialect="sqlite",
                    migrations=(Migration(1, "original"),),
                )
                with self.assertRaisesRegex(MigrationError, "was renamed"):
                    apply_migrations(
                        connection,
                        component="test",
                        dialect="sqlite",
                        migrations=(Migration(1, "renamed"),),
                    )
                with self.assertRaisesRegex(MigrationError, "unknown"):
                    apply_migrations(
                        connection,
                        component="test",
                        dialect="sqlite",
                        migrations=(),
                    )

            for migrations, message in (
                ((Migration(1, "one"), Migration(1, "again")), "Duplicate"),
                ((Migration(0, "zero"),), "positive"),
            ):
                with open_sqlite_connection(Path(tmp) / f"{message}.db") as connection:
                    with self.assertRaisesRegex(MigrationError, message):
                        apply_migrations(
                            connection,
                            component="test",
                            dialect="sqlite",
                            migrations=migrations,
                        )

    def test_unsupported_dialect_is_rejected_before_recording_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dialect.db"
            with open_sqlite_connection(path) as connection:
                with self.assertRaisesRegex(MigrationError, "Unsupported"):
                    apply_migrations(
                        connection,
                        component="test",
                        dialect="oracle",
                        migrations=(Migration(1, "first"),),
                    )
                count = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            self.assertEqual(count, 0)

    def test_existing_initializers_adopt_component_scoped_version_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "application.db"
            Database(path).initialize()
            CommerceDatabase(path).initialize()
            with open_sqlite_connection(path) as connection:
                rows = connection.execute(
                    "SELECT component, version, name FROM schema_migrations ORDER BY component"
                ).fetchall()

            self.assertEqual(
                [tuple(row) for row in rows],
                [
                    ("commerce", 1, "legacy_commerce_schema"),
                    ("commerce", 2, "receipt_control_and_diagnostics"),
                    ("commerce", 3, "receipt_extraction_jobs"),
                    ("commerce", 4, "outline_server_capacity"),
                    ("commerce", 5, "fleet_lifecycle_and_tier_capacity"),
                    ("commerce", 6, "provider_inventory_and_node_identity"),
                    ("commerce", 7, "remote_key_inventory_audit"),
                    ("commerce", 8, "scale_observation_history"),
                    ("commerce", 9, "restart_safe_interaction_state"),
                    ("commerce", 10, "receipt_perceptual_fingerprint"),
                    ("commerce", 11, "remote_key_review_workflow"),
                    ("commerce", 12, "endpoint_health_observability"),
                    ("commerce", 13, "endpoint_lifecycle_drain_state"),
                    ("commerce", 14, "connectivity_registry_foundation"),
                    ("commerce", 15, "connectivity_migration_jobs"),
                    ("commerce", 16, "fleet_enrollment_tokens"),
                    ("commerce", 17, "termination_events_rls"),
                    ("commerce", 18, "normalized_payment_reference_uniqueness"),
                    ("commerce", 19, "canonical_payment_provider_identity"),
                    ("commerce", 20, "managed_key_repair_observations"),
                    ("commerce", 21, "durable_usage_snapshots"),
                    ("commerce", 22, "fleet_probe_control_loop"),
                    ("commerce", 23, "accounts_entitlements_devices_and_leases"),
                    ("commerce", 24, "entitlement_source_identity"),
                    ("free_access", 1, "legacy_free_access_schema"),
                    ("free_access", 2, "giveaway_campaigns"),
                    ("free_access", 3, "configurable_promo_campaigns"),
                    ("free_access", 4, "staff_access_control"),
                    ("free_access", 5, "staff_control_group_binding"),
                    ("free_access", 6, "staff_notification_preferences"),
                    ("free_access", 7, "customer_quota_alert_preferences"),
                    ("free_access", 8, "free_key_server_identity"),
                    ("free_access", 9, "durable_free_provisioning_intents"),
                    ("free_access", 10, "free_intent_server_identity"),
                    ("free_access", 11, "managed_key_repair_jobs"),
                    ("free_access", 12, "staff_key_repair_notifications"),
                ],
            )

    def test_legacy_sqlite_global_outline_ids_are_rebuilt_as_server_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            with open_sqlite_connection(path) as connection:
                connection.executescript(
                    """CREATE TABLE users (
                           telegram_id INTEGER PRIMARY KEY,
                           first_name TEXT NOT NULL DEFAULT '',
                           created_at TEXT NOT NULL
                       );
                       CREATE TABLE keys (
                           id INTEGER PRIMARY KEY AUTOINCREMENT,
                           telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                           outline_key_id TEXT NOT NULL UNIQUE,
                           key_type TEXT NOT NULL DEFAULT 'daily_free',
                           created_at TEXT NOT NULL,
                           expires_at TEXT NOT NULL,
                           data_limit_bytes INTEGER NOT NULL,
                           status TEXT NOT NULL,
                           quota_warning_percent INTEGER
                       );"""
                )
            Database(path).initialize()
            CommerceDatabase(path).initialize()
            with open_sqlite_connection(path) as connection:
                free_indexes = connection.execute("PRAGMA index_list(keys)").fetchall()
                connection.execute("DROP INDEX paid_keys_server_external")
                connection.execute(
                    "CREATE UNIQUE INDEX legacy_paid_outline_id ON paid_vpn_keys(outline_key_id)"
                )
                connection.execute(
                    "DELETE FROM schema_migrations WHERE component = 'commerce' AND version = 5"
                )
            CommerceDatabase(path).initialize()
            with open_sqlite_connection(path) as connection:
                paid_indexes = connection.execute("PRAGMA index_list(paid_vpn_keys)").fetchall()
                paid_unique_columns = [
                    [
                        item[2]
                        for item in connection.execute(f"PRAGMA index_info({row[1]})").fetchall()
                    ]
                    for row in paid_indexes
                    if row[2]
                ]
                foreign_error = connection.execute("PRAGMA foreign_key_check").fetchone()

            self.assertEqual(
                [row[1] for row in free_indexes if row[2]], ["free_keys_server_external"]
            )
            self.assertNotIn(["outline_key_id"], paid_unique_columns)
            self.assertIn(["server_id", "outline_key_id"], paid_unique_columns)
            self.assertIsNone(foreign_error)


class RepositoryContractTest(unittest.TestCase):
    def test_database_adapters_satisfy_structural_repository_contracts(self):
        self.assertIsInstance(Database(":memory:"), RepositoryDatabase)
        self.assertIsInstance(CommerceDatabase(":memory:"), RepositoryDatabase)
        self.assertIsInstance(
            PostgresCommerceDatabase("postgresql://example.invalid/aurix"),
            HostedRepositoryDatabase,
        )


if __name__ == "__main__":
    unittest.main()
