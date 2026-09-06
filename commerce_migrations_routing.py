"""Routing-owned commerce migration definitions."""

from __future__ import annotations

from schema_migrations import Migration
from migration_hooks import (
    _add_normalized_payment_reference_guard,
    _canonicalize_payment_provider_identity,
    _rebuild_paid_keys_for_server_identity,
)


COMMERCE_MIGRATIONS_ROUTING = (
    Migration(
        26,
        "service_routes_and_failover_control",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS connectivity_routes (
                   route_id TEXT PRIMARY KEY,
                   endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   route_name TEXT NOT NULL,
                   protocol TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('provisioning', 'active', 'degraded', 'draining', 'failed', 'retired')),
                   priority INTEGER NOT NULL DEFAULT 100,
                   supports_managed_config INTEGER NOT NULL DEFAULT 0 CHECK (supports_managed_config IN (0, 1)),
                   supports_manual_export INTEGER NOT NULL DEFAULT 0 CHECK (supports_manual_export IN (0, 1)),
                   supports_quota_cap INTEGER NOT NULL DEFAULT 0 CHECK (supports_quota_cap IN (0, 1)),
                   supports_usage INTEGER NOT NULL DEFAULT 0 CHECK (supports_usage IN (0, 1)),
                   supports_rotation INTEGER NOT NULL DEFAULT 0 CHECK (supports_rotation IN (0, 1)),
                   supports_terminate_sessions INTEGER NOT NULL DEFAULT 0 CHECK (supports_terminate_sessions IN (0, 1)),
                   supports_management_probe INTEGER NOT NULL DEFAULT 0 CHECK (supports_management_probe IN (0, 1)),
                   supports_data_plane_probe INTEGER NOT NULL DEFAULT 0 CHECK (supports_data_plane_probe IN (0, 1)),
                   supports_reconcile INTEGER NOT NULL DEFAULT 0 CHECK (supports_reconcile IN (0, 1)),
                   capabilities_json TEXT NOT NULL DEFAULT '{}',
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   UNIQUE(endpoint_id, route_name)
               )""",
            "CREATE INDEX IF NOT EXISTS connectivity_routes_admission ON connectivity_routes(status, priority)",
            "ALTER TABLE connectivity_credentials ADD COLUMN route_id TEXT REFERENCES connectivity_routes(route_id)",
            "ALTER TABLE credential_generations ADD COLUMN route_id TEXT REFERENCES connectivity_routes(route_id)",
            "ALTER TABLE endpoint_assignments ADD COLUMN route_id TEXT REFERENCES connectivity_routes(route_id)",
            """INSERT INTO connectivity_routes
               (route_id, endpoint_id, route_name, protocol, status, priority,
                supports_managed_config, supports_manual_export, supports_quota_cap,
                supports_usage, supports_rotation, supports_terminate_sessions,
                supports_management_probe, supports_data_plane_probe, supports_reconcile,
                capabilities_json, created_at, updated_at)
               SELECT 'route-' || e.endpoint_id, e.endpoint_id, 'primary', t.protocol,
                      e.status, 100,
                      CASE WHEN t.protocol = 'outline' THEN 1 ELSE 0 END,
                      CASE WHEN t.protocol = 'outline' THEN 1 ELSE 0 END,
                      CASE WHEN t.protocol = 'outline' THEN 1 ELSE 0 END,
                      CASE WHEN t.protocol = 'outline' THEN 1 ELSE 0 END,
                      CASE WHEN t.protocol = 'outline' THEN 1 ELSE 0 END,
                      0,
                      CASE WHEN t.protocol = 'outline' THEN 1 ELSE 0 END,
                      CASE WHEN t.protocol = 'outline' THEN 1 ELSE 0 END,
                      CASE WHEN t.protocol = 'outline' THEN 1 ELSE 0 END,
                      CASE WHEN t.protocol = 'outline' THEN '{\"managed_config\":true,\"manual_export\":true,\"quota_cap\":true,\"usage\":true,\"rotation\":true,\"terminate_sessions\":false,\"management_probe\":true,\"data_plane_probe\":true,\"reconcile\":true}' ELSE '{}' END,
                      e.created_at, e.updated_at
                 FROM connectivity_endpoints e
                 JOIN connectivity_transports t ON t.transport_id = e.transport_id
                WHERE NOT EXISTS (
                    SELECT 1 FROM connectivity_routes r WHERE r.endpoint_id = e.endpoint_id AND r.route_name = 'primary'
                )""",
            "UPDATE connectivity_credentials SET route_id = (SELECT route_id FROM connectivity_routes WHERE endpoint_id = connectivity_credentials.endpoint_id AND route_name = 'primary') WHERE route_id IS NULL",
            "UPDATE credential_generations SET route_id = (SELECT route_id FROM connectivity_routes WHERE endpoint_id = credential_generations.endpoint_id AND route_name = 'primary') WHERE route_id IS NULL",
            "UPDATE endpoint_assignments SET route_id = (SELECT route_id FROM connectivity_routes WHERE endpoint_id = endpoint_assignments.endpoint_id AND route_name = 'primary') WHERE route_id IS NULL",
            """CREATE TABLE IF NOT EXISTS route_failover_policies (
                   policy_id TEXT PRIMARY KEY,
                   entitlement_id TEXT NOT NULL UNIQUE REFERENCES entitlements(entitlement_id),
                   enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
                   failure_threshold INTEGER NOT NULL DEFAULT 3 CHECK (failure_threshold BETWEEN 2 AND 10),
                   recovery_threshold INTEGER NOT NULL DEFAULT 2 CHECK (recovery_threshold BETWEEN 1 AND 10),
                   cooldown_seconds INTEGER NOT NULL DEFAULT 300 CHECK (cooldown_seconds BETWEEN 30 AND 86400),
                   standby_lease_bytes INTEGER NOT NULL DEFAULT 5242880 CHECK (standby_lease_bytes BETWEEN 1 AND 10737418240),
                   max_attempts INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 8),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS route_failover_state (
                   generation_id TEXT PRIMARY KEY REFERENCES credential_generations(generation_id),
                   failure_streak INTEGER NOT NULL DEFAULT 0 CHECK (failure_streak >= 0),
                   success_streak INTEGER NOT NULL DEFAULT 0 CHECK (success_streak >= 0),
                   last_outcome TEXT,
                   last_observed_at TEXT,
                   cooldown_until TEXT,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS route_observations (
                   observation_id TEXT PRIMARY KEY,
                   generation_id TEXT NOT NULL REFERENCES credential_generations(generation_id),
                   entitlement_id TEXT NOT NULL REFERENCES entitlements(entitlement_id),
                   route_id TEXT NOT NULL REFERENCES connectivity_routes(route_id),
                   network_bucket TEXT,
                   outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failure')),
                   latency_ms INTEGER,
                   reason TEXT,
                   observed_at TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   UNIQUE(generation_id, route_id, network_bucket, outcome, observed_at)
               )""",
            "CREATE INDEX IF NOT EXISTS route_observations_recent ON route_observations(route_id, network_bucket, observed_at)",
            """CREATE TABLE IF NOT EXISTS failover_decisions (
                   decision_id TEXT PRIMARY KEY,
                   idempotency_key TEXT NOT NULL UNIQUE,
                   entitlement_id TEXT NOT NULL REFERENCES entitlements(entitlement_id),
                   source_generation_id TEXT NOT NULL REFERENCES credential_generations(generation_id),
                   source_endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   source_route_id TEXT NOT NULL REFERENCES connectivity_routes(route_id),
                   target_endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   target_route_id TEXT NOT NULL REFERENCES connectivity_routes(route_id),
                   trigger TEXT NOT NULL,
                   network_bucket TEXT,
                   state TEXT NOT NULL DEFAULT 'pending'
                       CHECK (state IN ('pending', 'creating', 'verified', 'committed', 'rolled_back', 'failed', 'cancelled')),
                   attempts INTEGER NOT NULL DEFAULT 0,
                   next_attempt_at TEXT NOT NULL,
                   locked_at TEXT,
                   last_error TEXT,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   completed_at TEXT
               )""",
            "CREATE INDEX IF NOT EXISTS failover_decisions_due ON failover_decisions(state, next_attempt_at)",
            "CREATE INDEX IF NOT EXISTS failover_decisions_entitlement ON failover_decisions(entitlement_id, created_at)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS connectivity_routes (
                   route_id TEXT PRIMARY KEY,
                   endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   route_name TEXT NOT NULL,
                   protocol TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('provisioning', 'active', 'degraded', 'draining', 'failed', 'retired')),
                   priority INTEGER NOT NULL DEFAULT 100,
                   supports_managed_config BOOLEAN NOT NULL DEFAULT FALSE,
                   supports_manual_export BOOLEAN NOT NULL DEFAULT FALSE,
                   supports_quota_cap BOOLEAN NOT NULL DEFAULT FALSE,
                   supports_usage BOOLEAN NOT NULL DEFAULT FALSE,
                   supports_rotation BOOLEAN NOT NULL DEFAULT FALSE,
                   supports_terminate_sessions BOOLEAN NOT NULL DEFAULT FALSE,
                   supports_management_probe BOOLEAN NOT NULL DEFAULT FALSE,
                   supports_data_plane_probe BOOLEAN NOT NULL DEFAULT FALSE,
                   supports_reconcile BOOLEAN NOT NULL DEFAULT FALSE,
                   capabilities_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                   created_at TIMESTAMPTZ NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL,
                   UNIQUE(endpoint_id, route_name)
               )""",
            "CREATE INDEX IF NOT EXISTS connectivity_routes_admission ON connectivity_routes(status, priority)",
            "ALTER TABLE connectivity_credentials ADD COLUMN IF NOT EXISTS route_id TEXT REFERENCES connectivity_routes(route_id)",
            "ALTER TABLE credential_generations ADD COLUMN IF NOT EXISTS route_id TEXT REFERENCES connectivity_routes(route_id)",
            "ALTER TABLE endpoint_assignments ADD COLUMN IF NOT EXISTS route_id TEXT REFERENCES connectivity_routes(route_id)",
            """INSERT INTO connectivity_routes
               (route_id, endpoint_id, route_name, protocol, status, priority,
                supports_managed_config, supports_manual_export, supports_quota_cap,
                supports_usage, supports_rotation, supports_terminate_sessions,
                supports_management_probe, supports_data_plane_probe, supports_reconcile,
                capabilities_json, created_at, updated_at)
               SELECT 'route-' || e.endpoint_id, e.endpoint_id, 'primary', t.protocol,
                      e.status, 100,
                      (t.protocol = 'outline'), (t.protocol = 'outline'), (t.protocol = 'outline'),
                      (t.protocol = 'outline'), (t.protocol = 'outline'), FALSE,
                      (t.protocol = 'outline'), (t.protocol = 'outline'), (t.protocol = 'outline'),
                      CASE WHEN t.protocol = 'outline' THEN '{\"managed_config\":true,\"manual_export\":true,\"quota_cap\":true,\"usage\":true,\"rotation\":true,\"terminate_sessions\":false,\"management_probe\":true,\"data_plane_probe\":true,\"reconcile\":true}'::jsonb ELSE '{}'::jsonb END,
                      e.created_at::timestamptz, e.updated_at::timestamptz
                 FROM connectivity_endpoints e
                 JOIN connectivity_transports t ON t.transport_id = e.transport_id
                ON CONFLICT(endpoint_id, route_name) DO NOTHING""",
            "UPDATE connectivity_credentials c SET route_id = r.route_id FROM connectivity_routes r WHERE r.endpoint_id = c.endpoint_id AND r.route_name = 'primary' AND c.route_id IS NULL",
            "UPDATE credential_generations g SET route_id = r.route_id FROM connectivity_routes r WHERE r.endpoint_id = g.endpoint_id AND r.route_name = 'primary' AND g.route_id IS NULL",
            "UPDATE endpoint_assignments a SET route_id = r.route_id FROM connectivity_routes r WHERE r.endpoint_id = a.endpoint_id AND r.route_name = 'primary' AND a.route_id IS NULL",
            """CREATE TABLE IF NOT EXISTS route_failover_policies (
                   policy_id TEXT PRIMARY KEY,
                   entitlement_id TEXT NOT NULL UNIQUE REFERENCES entitlements(entitlement_id),
                   enabled BOOLEAN NOT NULL DEFAULT FALSE,
                   failure_threshold INTEGER NOT NULL DEFAULT 3 CHECK (failure_threshold BETWEEN 2 AND 10),
                   recovery_threshold INTEGER NOT NULL DEFAULT 2 CHECK (recovery_threshold BETWEEN 1 AND 10),
                   cooldown_seconds INTEGER NOT NULL DEFAULT 300 CHECK (cooldown_seconds BETWEEN 30 AND 86400),
                   standby_lease_bytes BIGINT NOT NULL DEFAULT 5242880 CHECK (standby_lease_bytes BETWEEN 1 AND 10737418240),
                   max_attempts INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 8),
                   created_at TIMESTAMPTZ NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS route_failover_state (
                   generation_id TEXT PRIMARY KEY REFERENCES credential_generations(generation_id),
                   failure_streak INTEGER NOT NULL DEFAULT 0 CHECK (failure_streak >= 0),
                   success_streak INTEGER NOT NULL DEFAULT 0 CHECK (success_streak >= 0),
                   last_outcome TEXT,
                   last_observed_at TIMESTAMPTZ,
                   cooldown_until TIMESTAMPTZ,
                   updated_at TIMESTAMPTZ NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS route_observations (
                   observation_id TEXT PRIMARY KEY,
                   generation_id TEXT NOT NULL REFERENCES credential_generations(generation_id),
                   entitlement_id TEXT NOT NULL REFERENCES entitlements(entitlement_id),
                   route_id TEXT NOT NULL REFERENCES connectivity_routes(route_id),
                   network_bucket TEXT,
                   outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failure')),
                   latency_ms INTEGER,
                   reason TEXT,
                   observed_at TIMESTAMPTZ NOT NULL,
                   created_at TIMESTAMPTZ NOT NULL,
                   UNIQUE(generation_id, route_id, network_bucket, outcome, observed_at)
               )""",
            "CREATE INDEX IF NOT EXISTS route_observations_recent ON route_observations(route_id, network_bucket, observed_at)",
            """CREATE TABLE IF NOT EXISTS failover_decisions (
                   decision_id TEXT PRIMARY KEY,
                   idempotency_key TEXT NOT NULL UNIQUE,
                   entitlement_id TEXT NOT NULL REFERENCES entitlements(entitlement_id),
                   source_generation_id TEXT NOT NULL REFERENCES credential_generations(generation_id),
                   source_endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   source_route_id TEXT NOT NULL REFERENCES connectivity_routes(route_id),
                   target_endpoint_id TEXT NOT NULL REFERENCES connectivity_endpoints(endpoint_id),
                   target_route_id TEXT NOT NULL REFERENCES connectivity_routes(route_id),
                   trigger TEXT NOT NULL,
                   network_bucket TEXT,
                   state TEXT NOT NULL DEFAULT 'pending'
                       CHECK (state IN ('pending', 'creating', 'verified', 'committed', 'rolled_back', 'failed', 'cancelled')),
                   attempts INTEGER NOT NULL DEFAULT 0,
                   next_attempt_at TIMESTAMPTZ NOT NULL,
                   locked_at TIMESTAMPTZ,
                   last_error TEXT,
                   created_at TIMESTAMPTZ NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL,
                   completed_at TIMESTAMPTZ
               )""",
            "CREATE INDEX IF NOT EXISTS failover_decisions_due ON failover_decisions(state, next_attempt_at)",
            "CREATE INDEX IF NOT EXISTS failover_decisions_entitlement ON failover_decisions(entitlement_id, created_at)",
        ),
    ),
)
