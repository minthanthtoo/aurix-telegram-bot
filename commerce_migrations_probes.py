"""Probes-owned commerce migration definitions."""

from __future__ import annotations

from schema_migrations import Migration
from migration_hooks import (
    _add_normalized_payment_reference_guard,
    _canonicalize_payment_provider_identity,
    _rebuild_paid_keys_for_server_identity,
)


COMMERCE_MIGRATIONS_PROBES = (
    Migration(
        22,
        "fleet_probe_control_loop",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS probe_targets (
                   target_id TEXT PRIMARY KEY,
                   label TEXT NOT NULL,
                   target_kind TEXT NOT NULL CHECK (
                       target_kind IN ('public', 'control_plane', 'server')
                   ),
                   host TEXT NOT NULL,
                   port INTEGER CHECK (port IS NULL OR port BETWEEN 1 AND 65535),
                   scheme TEXT,
                   enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS probe_schedules (
                   schedule_id TEXT PRIMARY KEY,
                   source_server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   target_id TEXT NOT NULL REFERENCES probe_targets(target_id),
                   probe_type TEXT NOT NULL CHECK (
                       probe_type IN ('icmp', 'tcp', 'udp', 'dns', 'https', 'download', 'node_to_node')
                   ),
                   interval_seconds INTEGER NOT NULL CHECK (interval_seconds BETWEEN 10 AND 86400),
                   timeout_ms INTEGER NOT NULL CHECK (timeout_ms BETWEEN 100 AND 30000),
                   payload_bytes INTEGER NOT NULL DEFAULT 0 CHECK (payload_bytes BETWEEN 0 AND 10485760),
                   enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                   next_run_at TEXT NOT NULL,
                   last_enqueued_at TEXT,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   UNIQUE(source_server_id, target_id, probe_type)
               )""",
            """CREATE TABLE IF NOT EXISTS probe_jobs (
                   job_id TEXT PRIMARY KEY,
                   schedule_id TEXT NOT NULL REFERENCES probe_schedules(schedule_id),
                   source_server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   target_id TEXT NOT NULL REFERENCES probe_targets(target_id),
                   probe_type TEXT NOT NULL CHECK (
                       probe_type IN ('icmp', 'tcp', 'udp', 'dns', 'https', 'download', 'node_to_node')
                   ),
                   instruction_json TEXT NOT NULL,
                   nonce TEXT NOT NULL UNIQUE,
                   status TEXT NOT NULL DEFAULT 'pending' CHECK (
                       status IN ('pending', 'claimed', 'completed', 'failed', 'expired')
                   ),
                   attempts INTEGER NOT NULL DEFAULT 0,
                   claimed_by TEXT,
                   claimed_at TEXT,
                   expires_at TEXT NOT NULL,
                   last_error TEXT,
                   created_at TEXT NOT NULL,
                   completed_at TEXT,
                   UNIQUE(schedule_id, created_at)
               )""",
            """CREATE INDEX IF NOT EXISTS probe_jobs_due
               ON probe_jobs(status, expires_at, created_at)""",
            """CREATE TABLE IF NOT EXISTS probe_observations (
                   observation_id TEXT PRIMARY KEY,
                   job_id TEXT NOT NULL UNIQUE REFERENCES probe_jobs(job_id),
                   source_server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   target_id TEXT NOT NULL REFERENCES probe_targets(target_id),
                   probe_type TEXT NOT NULL,
                   agent_id TEXT NOT NULL,
                   status TEXT NOT NULL CHECK (
                       status IN ('success', 'timeout', 'refused', 'unavailable', 'error')
                   ),
                   latency_ms REAL CHECK (latency_ms IS NULL OR latency_ms BETWEEN 0 AND 86400000),
                   packet_loss_percent REAL CHECK (
                       packet_loss_percent IS NULL OR packet_loss_percent BETWEEN 0 AND 100
                   ),
                   bytes_transferred INTEGER CHECK (
                       bytes_transferred IS NULL OR bytes_transferred BETWEEN 0 AND 104857600
                   ),
                   duration_ms REAL CHECK (duration_ms IS NULL OR duration_ms BETWEEN 0 AND 86400000),
                   error_class TEXT,
                   result_json TEXT NOT NULL DEFAULT '{}',
                   signature TEXT NOT NULL,
                   observed_at TEXT NOT NULL,
                   created_at TEXT NOT NULL
               )""",
            """CREATE INDEX IF NOT EXISTS probe_observations_route_time
               ON probe_observations(source_server_id, observed_at)""",
            """CREATE INDEX IF NOT EXISTS probe_observations_target_time
               ON probe_observations(target_id, observed_at)""",
            """CREATE TABLE IF NOT EXISTS route_health_snapshots (
                   server_id TEXT PRIMARY KEY REFERENCES outline_servers(server_id),
                   status TEXT NOT NULL CHECK (
                       status IN ('unknown', 'healthy', 'degraded', 'unreachable')
                   ),
                   score REAL CHECK (score IS NULL OR score BETWEEN 0 AND 100),
                   availability_score REAL CHECK (
                       availability_score IS NULL OR availability_score BETWEEN 0 AND 100
                   ),
                   latency_score REAL CHECK (latency_score IS NULL OR latency_score BETWEEN 0 AND 100),
                   loss_score REAL CHECK (loss_score IS NULL OR loss_score BETWEEN 0 AND 100),
                   throughput_score REAL CHECK (
                       throughput_score IS NULL OR throughput_score BETWEEN 0 AND 100
                   ),
                   sample_count INTEGER NOT NULL DEFAULT 0 CHECK (sample_count >= 0),
                   freshness_seconds INTEGER,
                   last_observed_at TEXT,
                   reason TEXT,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS route_decisions (
                   decision_id TEXT PRIMARY KEY,
                   telegram_id INTEGER,
                   entitlement_ref TEXT,
                   requested_region TEXT,
                   selected_server_id TEXT REFERENCES outline_servers(server_id),
                   decision_mode TEXT NOT NULL CHECK (
                       decision_mode IN ('automatic', 'manual', 'fallback')
                   ),
                   score REAL,
                   evidence_json TEXT NOT NULL DEFAULT '{}',
                   created_at TEXT NOT NULL
               )""",
            """CREATE INDEX IF NOT EXISTS route_decisions_recent
               ON route_decisions(created_at, requested_region)""",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS probe_targets (
                   target_id TEXT PRIMARY KEY,
                   label TEXT NOT NULL,
                   target_kind TEXT NOT NULL CHECK (
                       target_kind IN ('public', 'control_plane', 'server')
                   ),
                   host TEXT NOT NULL,
                   port INTEGER CHECK (port IS NULL OR port BETWEEN 1 AND 65535),
                   scheme TEXT,
                   enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                   created_at TIMESTAMPTZ NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS probe_schedules (
                   schedule_id TEXT PRIMARY KEY,
                   source_server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   target_id TEXT NOT NULL REFERENCES probe_targets(target_id),
                   probe_type TEXT NOT NULL CHECK (
                       probe_type IN ('icmp', 'tcp', 'udp', 'dns', 'https', 'download', 'node_to_node')
                   ),
                   interval_seconds INTEGER NOT NULL CHECK (interval_seconds BETWEEN 10 AND 86400),
                   timeout_ms INTEGER NOT NULL CHECK (timeout_ms BETWEEN 100 AND 30000),
                   payload_bytes INTEGER NOT NULL DEFAULT 0 CHECK (payload_bytes BETWEEN 0 AND 10485760),
                   enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                   next_run_at TIMESTAMPTZ NOT NULL,
                   last_enqueued_at TIMESTAMPTZ,
                   created_at TIMESTAMPTZ NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL,
                   UNIQUE(source_server_id, target_id, probe_type)
               )""",
            """CREATE TABLE IF NOT EXISTS probe_jobs (
                   job_id TEXT PRIMARY KEY,
                   schedule_id TEXT NOT NULL REFERENCES probe_schedules(schedule_id),
                   source_server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   target_id TEXT NOT NULL REFERENCES probe_targets(target_id),
                   probe_type TEXT NOT NULL CHECK (
                       probe_type IN ('icmp', 'tcp', 'udp', 'dns', 'https', 'download', 'node_to_node')
                   ),
                   instruction_json TEXT NOT NULL,
                   nonce TEXT NOT NULL UNIQUE,
                   status TEXT NOT NULL DEFAULT 'pending' CHECK (
                       status IN ('pending', 'claimed', 'completed', 'failed', 'expired')
                   ),
                   attempts INTEGER NOT NULL DEFAULT 0,
                   claimed_by TEXT,
                   claimed_at TIMESTAMPTZ,
                   expires_at TIMESTAMPTZ NOT NULL,
                   last_error TEXT,
                   created_at TIMESTAMPTZ NOT NULL,
                   completed_at TIMESTAMPTZ,
                   UNIQUE(schedule_id, created_at)
               )""",
            """CREATE INDEX IF NOT EXISTS probe_jobs_due
               ON probe_jobs(status, expires_at, created_at)""",
            """CREATE TABLE IF NOT EXISTS probe_observations (
                   observation_id TEXT PRIMARY KEY,
                   job_id TEXT NOT NULL UNIQUE REFERENCES probe_jobs(job_id),
                   source_server_id TEXT NOT NULL REFERENCES outline_servers(server_id),
                   target_id TEXT NOT NULL REFERENCES probe_targets(target_id),
                   probe_type TEXT NOT NULL,
                   agent_id TEXT NOT NULL,
                   status TEXT NOT NULL CHECK (
                       status IN ('success', 'timeout', 'refused', 'unavailable', 'error')
                   ),
                   latency_ms DOUBLE PRECISION CHECK (latency_ms IS NULL OR latency_ms BETWEEN 0 AND 86400000),
                   packet_loss_percent DOUBLE PRECISION CHECK (
                       packet_loss_percent IS NULL OR packet_loss_percent BETWEEN 0 AND 100
                   ),
                   bytes_transferred BIGINT CHECK (
                       bytes_transferred IS NULL OR bytes_transferred BETWEEN 0 AND 104857600
                   ),
                   duration_ms DOUBLE PRECISION CHECK (duration_ms IS NULL OR duration_ms BETWEEN 0 AND 86400000),
                   error_class TEXT,
                   result_json TEXT NOT NULL DEFAULT '{}',
                   signature TEXT NOT NULL,
                   observed_at TIMESTAMPTZ NOT NULL,
                   created_at TIMESTAMPTZ NOT NULL
               )""",
            """CREATE INDEX IF NOT EXISTS probe_observations_route_time
               ON probe_observations(source_server_id, observed_at)""",
            """CREATE INDEX IF NOT EXISTS probe_observations_target_time
               ON probe_observations(target_id, observed_at)""",
            """CREATE TABLE IF NOT EXISTS route_health_snapshots (
                   server_id TEXT PRIMARY KEY REFERENCES outline_servers(server_id),
                   status TEXT NOT NULL CHECK (
                       status IN ('unknown', 'healthy', 'degraded', 'unreachable')
                   ),
                   score DOUBLE PRECISION CHECK (score IS NULL OR score BETWEEN 0 AND 100),
                   availability_score DOUBLE PRECISION CHECK (
                       availability_score IS NULL OR availability_score BETWEEN 0 AND 100
                   ),
                   latency_score DOUBLE PRECISION CHECK (latency_score IS NULL OR latency_score BETWEEN 0 AND 100),
                   loss_score DOUBLE PRECISION CHECK (loss_score IS NULL OR loss_score BETWEEN 0 AND 100),
                   throughput_score DOUBLE PRECISION CHECK (
                       throughput_score IS NULL OR throughput_score BETWEEN 0 AND 100
                   ),
                   sample_count INTEGER NOT NULL DEFAULT 0 CHECK (sample_count >= 0),
                   freshness_seconds INTEGER,
                   last_observed_at TIMESTAMPTZ,
                   reason TEXT,
                   updated_at TIMESTAMPTZ NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS route_decisions (
                   decision_id TEXT PRIMARY KEY,
                   telegram_id BIGINT,
                   entitlement_ref TEXT,
                   requested_region TEXT,
                   selected_server_id TEXT REFERENCES outline_servers(server_id),
                   decision_mode TEXT NOT NULL CHECK (
                       decision_mode IN ('automatic', 'manual', 'fallback')
                   ),
                   score DOUBLE PRECISION,
                   evidence_json TEXT NOT NULL DEFAULT '{}',
                   created_at TIMESTAMPTZ NOT NULL
               )""",
            """CREATE INDEX IF NOT EXISTS route_decisions_recent
               ON route_decisions(created_at, requested_region)""",
        ),
    ),
)
