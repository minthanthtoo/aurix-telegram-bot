"""Capacity-owned commerce migration definitions."""

from __future__ import annotations

from schema_migrations import Migration
from migration_hooks import (
    _add_normalized_payment_reference_guard,
    _canonicalize_payment_provider_identity,
    _rebuild_paid_keys_for_server_identity,
)


COMMERCE_MIGRATIONS_CAPACITY = (
    Migration(
        4,
        "outline_server_capacity",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS outline_servers (
                   server_id TEXT PRIMARY KEY,
                   label TEXT NOT NULL,
                   enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                   max_keys INTEGER CHECK (max_keys IS NULL OR max_keys > 0),
                   reserved_keys INTEGER NOT NULL DEFAULT 2 CHECK (reserved_keys >= 0),
                   monthly_traffic_bytes INTEGER CHECK (monthly_traffic_bytes IS NULL OR monthly_traffic_bytes > 0),
                   remote_key_count INTEGER,
                   remote_transfer_bytes INTEGER,
                   current_bandwidth_bytes INTEGER,
                   peak_bandwidth_bytes INTEGER,
                   telemetry_experimental INTEGER NOT NULL DEFAULT 0 CHECK (telemetry_experimental IN (0, 1)),
                   health_status TEXT NOT NULL DEFAULT 'unknown',
                   last_error TEXT,
                   last_synced_at TEXT,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS server_plan_allocations (
                   server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   plan_code TEXT NOT NULL REFERENCES plans(code),
                   slot_limit INTEGER NOT NULL CHECK (slot_limit >= 0),
                   updated_at TEXT NOT NULL,
                   PRIMARY KEY (server_id, plan_code)
               )""",
            "ALTER TABLE orders ADD COLUMN server_id TEXT",
            "ALTER TABLE orders ADD COLUMN capacity_reserved_until TEXT",
            "ALTER TABLE subscriptions ADD COLUMN server_id TEXT",
            "ALTER TABLE paid_vpn_keys ADD COLUMN server_id TEXT",
            "CREATE INDEX IF NOT EXISTS orders_capacity_reservation ON orders(server_id, status, capacity_reserved_until)",
            "CREATE INDEX IF NOT EXISTS subscriptions_server_status ON subscriptions(server_id, status)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS outline_servers (
                   server_id TEXT PRIMARY KEY,
                   label TEXT NOT NULL,
                   enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                   max_keys INTEGER CHECK (max_keys IS NULL OR max_keys > 0),
                   reserved_keys INTEGER NOT NULL DEFAULT 2 CHECK (reserved_keys >= 0),
                   monthly_traffic_bytes BIGINT CHECK (monthly_traffic_bytes IS NULL OR monthly_traffic_bytes > 0),
                   remote_key_count INTEGER,
                   remote_transfer_bytes BIGINT,
                   current_bandwidth_bytes BIGINT,
                   peak_bandwidth_bytes BIGINT,
                   telemetry_experimental INTEGER NOT NULL DEFAULT 0 CHECK (telemetry_experimental IN (0, 1)),
                   health_status TEXT NOT NULL DEFAULT 'unknown',
                   last_error TEXT,
                   last_synced_at TEXT,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS server_plan_allocations (
                   server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   plan_code TEXT NOT NULL REFERENCES plans(code),
                   slot_limit INTEGER NOT NULL CHECK (slot_limit >= 0),
                   updated_at TEXT NOT NULL,
                   PRIMARY KEY (server_id, plan_code)
               )""",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS server_id TEXT",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS capacity_reserved_until TEXT",
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS server_id TEXT",
            "ALTER TABLE paid_vpn_keys ADD COLUMN IF NOT EXISTS server_id TEXT",
            "CREATE INDEX IF NOT EXISTS orders_capacity_reservation ON orders(server_id, status, capacity_reserved_until)",
            "CREATE INDEX IF NOT EXISTS subscriptions_server_status ON subscriptions(server_id, status)",
        ),
    ),
    Migration(
        5,
        "fleet_lifecycle_and_tier_capacity",
        sqlite_statements=(
            "CREATE UNIQUE INDEX IF NOT EXISTS paid_keys_server_external ON paid_vpn_keys(server_id, outline_key_id)",
            """CREATE TABLE IF NOT EXISTS server_tier_allocations (
                   server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   tier_code TEXT NOT NULL,
                   slot_limit INTEGER NOT NULL CHECK (slot_limit >= 0),
                   updated_at TEXT NOT NULL,
                   PRIMARY KEY (server_id, tier_code)
               )""",
            """CREATE TABLE IF NOT EXISTS infrastructure_jobs (
                   id TEXT PRIMARY KEY,
                   operation TEXT NOT NULL,
                   status TEXT NOT NULL,
                   attempts INTEGER NOT NULL DEFAULT 0,
                   next_attempt_at TEXT NOT NULL,
                   locked_at TEXT,
                   provider_resource_id TEXT,
                   provider_action_id TEXT,
                   request_fingerprint TEXT NOT NULL UNIQUE,
                   last_error TEXT,
                   created_at TEXT NOT NULL,
                   completed_at TEXT
               )""",
            """CREATE TABLE IF NOT EXISTS infrastructure_events (
                   id TEXT PRIMARY KEY,
                   infrastructure_job_id TEXT,
                   server_id TEXT,
                   event_type TEXT NOT NULL,
                   metadata_json TEXT NOT NULL DEFAULT '{}',
                   created_at TEXT NOT NULL
               )""",
        ),
        postgres_statements=(
            "ALTER TABLE paid_vpn_keys DROP CONSTRAINT IF EXISTS paid_vpn_keys_outline_key_id_key",
            "CREATE UNIQUE INDEX IF NOT EXISTS paid_keys_server_external ON paid_vpn_keys(server_id, outline_key_id)",
            """CREATE TABLE IF NOT EXISTS server_tier_allocations (
                   server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   tier_code TEXT NOT NULL,
                   slot_limit INTEGER NOT NULL CHECK (slot_limit >= 0),
                   updated_at TEXT NOT NULL,
                   PRIMARY KEY (server_id, tier_code)
               )""",
            """CREATE TABLE IF NOT EXISTS infrastructure_jobs (
                   id TEXT PRIMARY KEY,
                   operation TEXT NOT NULL,
                   status TEXT NOT NULL,
                   attempts INTEGER NOT NULL DEFAULT 0,
                   next_attempt_at TEXT NOT NULL,
                   locked_at TEXT,
                   provider_resource_id TEXT,
                   provider_action_id TEXT,
                   request_fingerprint TEXT NOT NULL UNIQUE,
                   last_error TEXT,
                   created_at TEXT NOT NULL,
                   completed_at TEXT
               )""",
            """CREATE TABLE IF NOT EXISTS infrastructure_events (
                   id TEXT PRIMARY KEY,
                   infrastructure_job_id TEXT,
                   server_id TEXT,
                   event_type TEXT NOT NULL,
                   metadata_json TEXT NOT NULL DEFAULT '{}',
                   created_at TEXT NOT NULL
               )""",
        ),
        sqlite_hook=_rebuild_paid_keys_for_server_identity,
    ),
    Migration(
        6,
        "provider_inventory_and_node_identity",
        sqlite_statements=(
            "ALTER TABLE outline_servers ADD COLUMN provider_resource_id TEXT",
            "ALTER TABLE outline_servers ADD COLUMN provider_status TEXT",
            "ALTER TABLE outline_servers ADD COLUMN provider_last_seen_at TEXT",
            "CREATE UNIQUE INDEX IF NOT EXISTS outline_servers_provider_resource ON outline_servers(provider_resource_id) WHERE provider_resource_id IS NOT NULL",
        ),
        postgres_statements=(
            "ALTER TABLE outline_servers ADD COLUMN IF NOT EXISTS provider_resource_id TEXT",
            "ALTER TABLE outline_servers ADD COLUMN IF NOT EXISTS provider_status TEXT",
            "ALTER TABLE outline_servers ADD COLUMN IF NOT EXISTS provider_last_seen_at TEXT",
            "CREATE UNIQUE INDEX IF NOT EXISTS outline_servers_provider_resource ON outline_servers(provider_resource_id) WHERE provider_resource_id IS NOT NULL",
        ),
    ),
    Migration(
        7,
        "remote_key_inventory_audit",
        sqlite_statements=(
            "ALTER TABLE outline_servers ADD COLUMN remote_orphan_key_count INTEGER NOT NULL DEFAULT 0",
            """CREATE TABLE IF NOT EXISTS outline_remote_keys (
                   server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   outline_key_id TEXT NOT NULL,
                   remote_name TEXT,
                   managed INTEGER NOT NULL DEFAULT 0 CHECK (managed IN (0, 1)),
                   status TEXT NOT NULL DEFAULT 'present'
                       CHECK (status IN ('present', 'missing')),
                   first_seen_at TEXT NOT NULL,
                   last_seen_at TEXT NOT NULL,
                   last_usage_bytes INTEGER,
                   PRIMARY KEY (server_id, outline_key_id)
               )""",
            """CREATE INDEX IF NOT EXISTS outline_remote_keys_audit
               ON outline_remote_keys(server_id, status, managed)""",
        ),
        postgres_statements=(
            "ALTER TABLE outline_servers ADD COLUMN IF NOT EXISTS remote_orphan_key_count INTEGER NOT NULL DEFAULT 0",
            """CREATE TABLE IF NOT EXISTS outline_remote_keys (
                   server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   outline_key_id TEXT NOT NULL,
                   remote_name TEXT,
                   managed INTEGER NOT NULL DEFAULT 0 CHECK (managed IN (0, 1)),
                   status TEXT NOT NULL DEFAULT 'present'
                       CHECK (status IN ('present', 'missing')),
                   first_seen_at TEXT NOT NULL,
                   last_seen_at TEXT NOT NULL,
                   last_usage_bytes BIGINT,
                   PRIMARY KEY (server_id, outline_key_id)
               )""",
            """CREATE INDEX IF NOT EXISTS outline_remote_keys_audit
               ON outline_remote_keys(server_id, status, managed)""",
        ),
    ),
    Migration(
        8,
        "scale_observation_history",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS scale_observations (
                   id TEXT PRIMARY KEY,
                   fleet_fingerprint TEXT NOT NULL,
                   observed_at TEXT NOT NULL,
                   status TEXT NOT NULL CHECK (
                       status IN ('stable', 'prepare', 'urgent', 'blocked', 'unconfigured')
                   ),
                   utilization_percent REAL,
                   remaining_slots INTEGER,
                   saleable_capacity INTEGER,
                   traffic_utilization_percent REAL,
                   healthy_server_count INTEGER NOT NULL DEFAULT 0,
                   created_at TEXT NOT NULL,
                   UNIQUE (fleet_fingerprint, observed_at)
               )""",
            """CREATE INDEX IF NOT EXISTS scale_observations_recent
               ON scale_observations(observed_at)""",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS scale_observations (
                   id TEXT PRIMARY KEY,
                   fleet_fingerprint TEXT NOT NULL,
                   observed_at TEXT NOT NULL,
                   status TEXT NOT NULL CHECK (
                       status IN ('stable', 'prepare', 'urgent', 'blocked', 'unconfigured')
                   ),
                   utilization_percent DOUBLE PRECISION,
                   remaining_slots INTEGER,
                   saleable_capacity INTEGER,
                   traffic_utilization_percent DOUBLE PRECISION,
                   healthy_server_count INTEGER NOT NULL DEFAULT 0,
                   created_at TEXT NOT NULL,
                   UNIQUE (fleet_fingerprint, observed_at)
               )""",
            """CREATE INDEX IF NOT EXISTS scale_observations_recent
               ON scale_observations(observed_at)""",
        ),
    ),
)
