"""Endpoint registry, deterministic allocation, and guarded fleet operations."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .commerce_repositories import _PostgresConnection
from .outline_adapter import OutlineClient


UTC = timezone.utc
DEFAULT_ENDPOINT_ID = "legacy-default"


class ConnectivityError(RuntimeError):
    pass


@dataclass(frozen=True)
class EndpointAssignment:
    id: str
    endpoint_id: str
    subscription_id: str | None
    free_key_id: int | None
    plan_code: str
    status: str
    reserved_quota_bytes: int | None


class EndpointRegistry:
    """PostgreSQL/SQLite-compatible endpoint and assignment repository."""

    _PROTOCOL_OBSERVATION_STATUSES = {
        "healthy",
        "degraded",
        "failed",
        "unsupported",
        "unknown",
    }
    _SAFE_OBSERVATION_DETAIL_KEYS = {
        "error",
        "reason",
        "network_bucket",
        "sample_count",
        "active_users",
        "status_code",
        "quota_enforced",
        "restart_persisted",
        "session_termination",
        "client_path",
    }

    def __init__(self, database: Any, secret_key: bytes | str):
        self.database = database
        try:
            self.cipher = Fernet(secret_key)
        except (TypeError, ValueError) as exc:
            raise ValueError("AURIX_ACCESS_URL_KEY must be a Fernet key") from exc

    def _encrypt(self, value: str) -> str:
        return self.cipher.encrypt(value.encode()).decode()

    def _decrypt(self, value: str | None) -> str:
        if not value:
            raise ConnectivityError("Endpoint management secret is unavailable")
        try:
            return self.cipher.decrypt(value.encode()).decode()
        except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
            raise ConnectivityError("Endpoint management secret cannot be decrypted") from exc

    def register_protocol_profile(
        self,
        endpoint_id: str,
        protocol: str,
        *,
        adapter_type: str | None = None,
        status: str = "candidate",
        capabilities: dict[str, Any] | None = None,
        verified_at: datetime | None = None,
        last_healthy_at: datetime | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist an endpoint/transport binding without storing secrets.

        A profile is an operational fact about one endpoint and one protocol;
        it is not an allocation authorization by itself.  Candidate profiles
        are useful during canary preparation, while only an explicitly enabled
        profile may be considered by a future protocol-aware selector.
        """
        endpoint = str(endpoint_id or "").strip()
        transport = str(protocol or "").strip().lower()
        profile_status = str(status or "").strip().lower()
        if not endpoint or len(endpoint) > 128:
            raise ConnectivityError("endpoint ID is invalid")
        if not transport or len(transport) > 64 or any(char.isspace() for char in transport):
            raise ConnectivityError("protocol is invalid")
        if profile_status not in {"candidate", "enabled", "degraded", "disabled", "retired"}:
            raise ConnectivityError("protocol profile status is invalid")
        adapter = str(adapter_type or transport).strip().lower()
        if not adapter or len(adapter) > 64 or any(char.isspace() for char in adapter):
            raise ConnectivityError("adapter type is invalid")
        capability_map = {
            str(key): bool(value)
            for key, value in dict(capabilities or {}).items()
            if str(key).strip()
        }
        timestamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        profile_id = f"{transport}:{endpoint}"
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            exists = connection.execute(
                "SELECT 1 FROM vpn_endpoints WHERE id = ?", (endpoint,)
            ).fetchone()
            if exists is None:
                raise ConnectivityError("VPN endpoint does not exist")
            connection.execute(
                """INSERT INTO endpoint_protocol_profiles
                   (profile_id, endpoint_id, protocol, adapter_type, status,
                    capabilities_json, verified_at, last_healthy_at, created_at,
                    retired_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(endpoint_id, protocol) DO UPDATE SET
                     adapter_type = excluded.adapter_type,
                     status = excluded.status,
                     capabilities_json = excluded.capabilities_json,
                     verified_at = COALESCE(excluded.verified_at,
                                            endpoint_protocol_profiles.verified_at),
                     last_healthy_at = COALESCE(excluded.last_healthy_at,
                                                endpoint_protocol_profiles.last_healthy_at),
                     retired_at = CASE WHEN excluded.status = 'retired'
                                       THEN COALESCE(endpoint_protocol_profiles.retired_at, excluded.last_healthy_at, excluded.verified_at)
                                       ELSE NULL END""",
                (
                    profile_id,
                    endpoint,
                    transport,
                    adapter,
                    profile_status,
                    json.dumps(capability_map, sort_keys=True, separators=(",", ":")),
                    verified_at.astimezone(UTC).isoformat() if verified_at else None,
                    last_healthy_at.astimezone(UTC).isoformat() if last_healthy_at else None,
                    timestamp,
                ),
            )
        return next(
            item
            for item in self.list_protocol_profiles(endpoint)
            if item["protocol"] == transport
        )

    def list_protocol_profiles(
        self, endpoint_id: str | None = None, *, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        """Return non-secret endpoint protocol bindings for operator policy."""
        clauses: list[str] = []
        values: list[Any] = []
        if endpoint_id is not None:
            clauses.append("endpoint_id = ?")
            values.append(str(endpoint_id))
        if enabled_only:
            clauses.append("status = 'enabled'")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT profile_id, endpoint_id, protocol, adapter_type, status,
                          capabilities_json, verified_at, last_healthy_at,
                          created_at, retired_at
                     FROM endpoint_protocol_profiles"""
                + where
                + " ORDER BY endpoint_id, protocol",
                tuple(values),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                capabilities = json.loads(str(item.pop("capabilities_json") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                capabilities = {}
            item["capabilities"] = capabilities if isinstance(capabilities, dict) else {}
            result.append(item)
        return result

    @classmethod
    def _safe_protocol_observation_details(
        cls, details: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Keep protocol evidence scalar and non-secret before persistence."""
        safe: dict[str, Any] = {}
        if details is None:
            return safe
        if not isinstance(details, dict):
            raise ConnectivityError("protocol observation details are invalid")
        for raw_key, value in details.items():
            key = str(raw_key).strip().lower()
            if key not in cls._SAFE_OBSERVATION_DETAIL_KEYS:
                continue
            if isinstance(value, bool):
                safe[key] = value
            elif isinstance(value, int):
                safe[key] = value
            elif isinstance(value, float):
                if value == value and abs(value) != float("inf"):
                    safe[key] = value
            elif isinstance(value, str):
                safe[key] = value[:256]
        return safe

    @staticmethod
    def _observation_time(value: datetime | str | None) -> datetime:
        if value is None:
            return datetime.now(UTC)
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)

    def record_protocol_observation(
        self,
        endpoint_id: str,
        protocol: str,
        *,
        signal: str = "management",
        status: str = "healthy",
        details: dict[str, Any] | None = None,
        latency_ms: float | None = None,
        observed_at: datetime | str | None = None,
        expires_at: datetime | str | None = None,
        source: str = "operator",
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Append one safe, protocol-scoped health/evidence observation."""
        endpoint = str(endpoint_id or "").strip()
        transport = str(protocol or "").strip().lower()
        observation_signal = str(signal or "").strip().lower()
        observation_status = str(status or "").strip().lower()
        observation_source = str(source or "").strip().lower()
        if not endpoint or len(endpoint) > 128:
            raise ConnectivityError("endpoint ID is invalid")
        if not transport or len(transport) > 64 or any(char.isspace() for char in transport):
            raise ConnectivityError("protocol is invalid")
        if not observation_signal or len(observation_signal) > 64 or any(
            char.isspace() for char in observation_signal
        ):
            raise ConnectivityError("protocol observation signal is invalid")
        if observation_status not in self._PROTOCOL_OBSERVATION_STATUSES:
            raise ConnectivityError("protocol observation status is invalid")
        if not observation_source or len(observation_source) > 64 or any(
            char.isspace() for char in observation_source
        ):
            raise ConnectivityError("protocol observation source is invalid")
        if latency_ms is not None and not 0 <= float(latency_ms) <= 120_000:
            raise ConnectivityError("protocol observation latency is invalid")
        created = self._observation_time(now)
        observed = self._observation_time(observed_at) if observed_at is not None else created
        if observed > created + timedelta(minutes=5):
            raise ConnectivityError("protocol observation cannot be far in the future")
        expires = self._observation_time(expires_at) if expires_at is not None else None
        if expires is not None and expires <= observed:
            raise ConnectivityError("protocol observation expiry must be after observation")
        timestamp = created.isoformat()
        observed_text = observed.isoformat()
        expires_text = expires.isoformat() if expires is not None else None
        safe_details = self._safe_protocol_observation_details(details)
        observation_id = uuid.uuid4().hex
        profile_id = f"{transport}:{endpoint}"
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            profile = connection.execute(
                """SELECT profile_id FROM endpoint_protocol_profiles
                    WHERE profile_id = ? AND endpoint_id = ? AND protocol = ?""",
                (profile_id, endpoint, transport),
            ).fetchone()
            if profile is None:
                raise ConnectivityError("protocol profile does not exist")
            connection.execute(
                """INSERT INTO endpoint_protocol_observations
                   (observation_id, profile_id, endpoint_id, protocol, signal, status,
                    details_json, latency_ms, observed_at, expires_at, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation_id,
                    profile_id,
                    endpoint,
                    transport,
                    observation_signal,
                    observation_status,
                    json.dumps(safe_details, sort_keys=True, separators=(",", ":")),
                    None if latency_ms is None else float(latency_ms),
                    observed_text,
                    expires_text,
                    observation_source,
                    timestamp,
                ),
            )
            if observation_status == "healthy":
                connection.execute(
                    """UPDATE endpoint_protocol_profiles
                          SET last_healthy_at = CASE
                                WHEN last_healthy_at IS NULL OR last_healthy_at < ? THEN ?
                                ELSE last_healthy_at END
                        WHERE profile_id = ?""",
                    (observed_text, observed_text, profile_id),
                )
        return {
            "observation_id": observation_id,
            "profile_id": profile_id,
            "endpoint_id": endpoint,
            "protocol": transport,
            "signal": observation_signal,
            "status": observation_status,
            "details": safe_details,
            "latency_ms": None if latency_ms is None else float(latency_ms),
            "observed_at": observed_text,
            "expires_at": expires_text,
            "source": observation_source,
            "created_at": timestamp,
        }

    def list_protocol_observations(
        self,
        endpoint_id: str | None = None,
        protocol: str | None = None,
        signal: str | None = None,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return recent protocol evidence without exposing credential material."""
        clauses: list[str] = []
        values: list[Any] = []
        if endpoint_id is not None:
            clauses.append("endpoint_id = ?")
            values.append(str(endpoint_id))
        if protocol is not None:
            clauses.append("protocol = ?")
            values.append(str(protocol).strip().lower())
        if signal is not None:
            clauses.append("signal = ?")
            values.append(str(signal).strip().lower())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        bounded_limit = max(1, min(int(limit), 200))
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT observation_id, profile_id, endpoint_id, protocol, signal,
                          status, details_json, latency_ms, observed_at, expires_at,
                          source, created_at
                     FROM endpoint_protocol_observations"""
                + where
                + " ORDER BY observed_at DESC, observation_id DESC LIMIT ?",
                tuple(values + [bounded_limit]),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                details = json.loads(str(item.pop("details_json") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                details = {}
            item["details"] = details if isinstance(details, dict) else {}
            result.append(item)
        return result

    def promote_protocol_profile(
        self,
        endpoint_id: str,
        protocol: str,
        *,
        required_signals: tuple[str, ...] | list[str],
        required_capabilities: tuple[str, ...] | list[str] = (),
        actor_id: str | int | None = None,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Enable a candidate profile only after explicit fresh evidence.

        This is an operator decision boundary, not an automatic health
        transition.  Every required signal must have a non-expired healthy
        observation, and every required capability must already be declared by
        the profile.  The decision is audited when the commerce audit table is
        available.
        """
        endpoint = str(endpoint_id or "").strip()
        transport = str(protocol or "").strip().lower()
        if isinstance(required_signals, str) or isinstance(required_capabilities, str):
            raise ConnectivityError("protocol evidence requirements must be a sequence")
        signals = tuple(
            sorted(
                {
                    str(value or "").strip().lower()
                    for value in required_signals
                    if str(value or "").strip()
                }
            )
        )
        capabilities = tuple(
            sorted(
                {
                    str(value or "").strip()
                    for value in required_capabilities
                    if str(value or "").strip()
                }
            )
        )
        if not endpoint or len(endpoint) > 128:
            raise ConnectivityError("endpoint ID is invalid")
        if not transport or len(transport) > 64 or any(char.isspace() for char in transport):
            raise ConnectivityError("protocol is invalid")
        if not signals:
            raise ConnectivityError("at least one protocol evidence signal is required")
        if any(len(value) > 64 or any(char.isspace() for char in value) for value in signals):
            raise ConnectivityError("protocol evidence signal is invalid")
        if any(len(value) > 64 or any(char.isspace() for char in value) for value in capabilities):
            raise ConnectivityError("protocol capability is invalid")
        timestamp_dt = self._observation_time(now)
        timestamp = timestamp_dt.isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            profile = connection.execute(
                """SELECT * FROM endpoint_protocol_profiles
                    WHERE endpoint_id = ? AND protocol = ?""",
                (endpoint, transport),
            ).fetchone()
            if profile is None:
                raise ConnectivityError("protocol profile does not exist")
            if str(profile["status"] or "").lower() == "retired":
                raise ConnectivityError("retired protocol profile cannot be promoted")
            try:
                profile_capabilities = json.loads(str(profile["capabilities_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                profile_capabilities = {}
            if not isinstance(profile_capabilities, dict):
                profile_capabilities = {}
            missing_capabilities = [
                name for name in capabilities if profile_capabilities.get(name) is not True
            ]
            if missing_capabilities:
                raise ConnectivityError(
                    "protocol profile lacks required capabilities: "
                    + ", ".join(missing_capabilities)
                )
            observations = connection.execute(
                """SELECT signal, observed_at, expires_at
                     FROM endpoint_protocol_observations
                    WHERE profile_id = ? AND status = 'healthy'""",
                (str(profile["profile_id"]),),
            ).fetchall()
            fresh_signals: set[str] = set()
            latest_healthy: datetime | None = None
            for observation in observations:
                try:
                    observed = self._observation_time(observation["observed_at"])
                    expires = (
                        self._observation_time(observation["expires_at"])
                        if observation["expires_at"]
                        else None
                    )
                except (TypeError, ValueError, OverflowError):
                    continue
                if observed > timestamp_dt or (expires is not None and expires <= timestamp_dt):
                    continue
                fresh_signals.add(str(observation["signal"]).strip().lower())
                if latest_healthy is None or observed > latest_healthy:
                    latest_healthy = observed
            missing_signals = [name for name in signals if name not in fresh_signals]
            if missing_signals:
                raise ConnectivityError(
                    "protocol profile evidence is incomplete: "
                    + ", ".join(missing_signals)
                )
            healthy_text = (latest_healthy or timestamp_dt).isoformat()
            connection.execute(
                """UPDATE endpoint_protocol_profiles
                      SET status = 'enabled', verified_at = COALESCE(verified_at, ?),
                          last_healthy_at = CASE
                              WHEN last_healthy_at IS NULL OR last_healthy_at < ? THEN ?
                              ELSE last_healthy_at END,
                          retired_at = NULL
                    WHERE profile_id = ?""",
                (timestamp, healthy_text, healthy_text, str(profile["profile_id"])),
            )
            if isinstance(connection, _PostgresConnection):
                audit_exists = connection.execute(
                    "SELECT to_regclass(?) AS table_name", ("public.audit_events",)
                ).fetchone()
                has_audit_events = bool(audit_exists and audit_exists["table_name"])
            else:
                audit_exists = connection.execute(
                    """SELECT 1 FROM sqlite_master
                        WHERE type = 'table' AND name = 'audit_events'"""
                ).fetchone()
                has_audit_events = audit_exists is not None
            if has_audit_events:
                connection.execute(
                    """INSERT INTO audit_events
                       (actor_type, actor_id, action, target_type, target_id,
                        metadata_json, created_at)
                       VALUES ('admin', ?, 'protocol_profile_promoted',
                               'endpoint_protocol_profile', ?, ?, ?)""",
                    (
                        None if actor_id is None else str(actor_id)[:128],
                        str(profile["profile_id"]),
                        json.dumps(
                            {
                                "required_signals": list(signals),
                                "required_capabilities": list(capabilities),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        timestamp,
                    ),
                )
        return next(
            item
            for item in self.list_protocol_profiles(endpoint)
            if item["protocol"] == transport
        )

    def configure_bootstrap(
        self,
        api_url: str,
        certificate_sha256: str,
        *,
        code: str = "SGP-01",
        region: str = "sgp1",
        outline_version: str | None = None,
        mark_healthy: bool = True,
        now: datetime | None = None,
    ) -> None:
        observed_at = now or datetime.now(UTC)
        timestamp = observed_at.astimezone(UTC).isoformat()
        parsed = urllib.parse.urlsplit(api_url)
        public_address = parsed.hostname
        encrypted = self._encrypt(api_url.rstrip("/"))
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE vpn_endpoints SET code = ?, region = ?,
                          state = CASE WHEN state IN ('DRAINING', 'RETIRED') THEN state ELSE 'ACTIVE' END,
                          accepts_new_assignments = CASE
                            WHEN state IN ('DRAINING', 'RETIRED') THEN accepts_new_assignments ELSE ? END,
                          management_url_ciphertext = ?,
                          certificate_sha256 = ?, outline_version = COALESCE(?, outline_version),
                          public_address = ?,
                          verified_at = CASE WHEN ? THEN COALESCE(verified_at, ?) ELSE verified_at END,
                          last_healthy_at = CASE WHEN ? THEN COALESCE(last_healthy_at, ?) ELSE last_healthy_at END
                   WHERE id = ?""",
                (
                    code[:64],
                    region[:32],
                    True,
                    encrypted,
                    certificate_sha256.lower().replace(":", ""),
                    outline_version,
                    public_address,
                    bool(mark_healthy),
                    timestamp,
                    bool(mark_healthy),
                    timestamp,
                    DEFAULT_ENDPOINT_ID,
                ),
            )
        self.register_protocol_profile(
            DEFAULT_ENDPOINT_ID,
            "outline",
            adapter_type="outline",
            status="enabled",
            verified_at=observed_at if mark_healthy else None,
            last_healthy_at=observed_at if mark_healthy else None,
            now=observed_at,
        )

    def register_verified_endpoint(
        self,
        *,
        endpoint_id: str,
        code: str,
        provider: str,
        provider_resource_id: str,
        region: str,
        api_url: str,
        certificate_sha256: str,
        max_active_keys: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Verify Outline first, then make a newly provisioned endpoint allocatable."""
        fingerprint = certificate_sha256.lower().replace(":", "")
        client = OutlineClient(api_url.rstrip("/"), fingerprint)
        info = client.server_info()
        timestamp = (now or datetime.now(UTC)).isoformat()
        parsed = urllib.parse.urlsplit(api_url)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """INSERT INTO vpn_endpoints
                   (id, code, provider, provider_resource_id, region, state,
                    accepts_new_assignments, management_url_ciphertext,
                    certificate_sha256, outline_version, public_address,
                    max_active_keys, created_at, verified_at, last_healthy_at)
                   VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     code = excluded.code, provider = excluded.provider,
                     provider_resource_id = excluded.provider_resource_id,
                     region = excluded.region, state = 'ACTIVE',
                     accepts_new_assignments = excluded.accepts_new_assignments,
                     management_url_ciphertext = excluded.management_url_ciphertext,
                     certificate_sha256 = excluded.certificate_sha256,
                     outline_version = excluded.outline_version,
                     public_address = excluded.public_address,
                     max_active_keys = excluded.max_active_keys,
                     verified_at = excluded.verified_at,
                     last_healthy_at = excluded.last_healthy_at""",
                (
                    endpoint_id,
                    code[:64],
                    provider[:32],
                    provider_resource_id,
                    region[:32],
                    True,
                    self._encrypt(api_url.rstrip("/")),
                    fingerprint,
                    str(info.get("version") or "unknown")[:64],
                    parsed.hostname,
                    max_active_keys,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
        self.register_protocol_profile(
            endpoint_id,
            "outline",
            adapter_type="outline",
            status="enabled",
            verified_at=now or datetime.now(UTC),
            last_healthy_at=now or datetime.now(UTC),
            now=now,
        )
        return self.endpoint(endpoint_id)

    def list_endpoints(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT e.id, e.code, e.provider, e.provider_resource_id, e.region,
                          e.state, e.accepts_new_assignments, e.outline_version,
                          e.public_address, e.max_active_keys, e.reserved_transfer_bytes,
                          e.last_healthy_at,
                          COUNT(a.id) AS active_assignments
                   FROM vpn_endpoints e
                   LEFT JOIN endpoint_assignments a
                     ON a.endpoint_id = e.id AND a.status = 'active'
                   GROUP BY e.id, e.code, e.provider, e.provider_resource_id, e.region,
                            e.state, e.accepts_new_assignments, e.outline_version,
                            e.public_address, e.max_active_keys, e.reserved_transfer_bytes,
                            e.last_healthy_at
                   ORDER BY e.code"""
            ).fetchall()
        profiles = self.list_protocol_profiles()
        by_endpoint: dict[str, list[dict[str, Any]]] = {}
        for profile in profiles:
            by_endpoint.setdefault(str(profile["endpoint_id"]), []).append(profile)
        result = []
        for row in rows:
            item = dict(row)
            item["protocols"] = by_endpoint.get(str(item["id"]), [])
            result.append(item)
        return result

    def list_customer_endpoints(
        self, plan_code: str | None = None, protocol: str = "outline"
    ) -> list[dict[str, Any]]:
        """Return the safe endpoint directory used by the customer portal.

        Management URLs, certificates, provider resource IDs, and public IPs
        are intentionally excluded. ``management_latency_ms`` is the latest
        control-plane observation and must not be presented as a user ping.
        """
        plan = str(plan_code or "").strip() or None
        selected_protocol = str(protocol or "").strip().lower()
        if not selected_protocol or len(selected_protocol) > 64 or any(
            char.isspace() for char in selected_protocol
        ):
            raise ConnectivityError("protocol is invalid")
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT e.id, e.code, e.region, e.state,
                          e.accepts_new_assignments, e.last_healthy_at,
                          e.max_active_keys,
                          COUNT(a.id) AS active_assignments,
                          l.enabled AS plan_enabled,
                          l.max_active_assignments AS plan_max,
                          (SELECT COUNT(*) FROM endpoint_assignments pa
                           WHERE pa.endpoint_id = e.id
                             AND pa.plan_code = ? AND pa.status = 'active') AS plan_active,
                          (SELECT s.management_latency_ms
                           FROM endpoint_capacity_snapshots s
                           WHERE s.endpoint_id = e.id
                           ORDER BY s.observed_at DESC LIMIT 1) AS management_latency_ms,
                          (SELECT s.observed_at
                           FROM endpoint_capacity_snapshots s
                           WHERE s.endpoint_id = e.id
                           ORDER BY s.observed_at DESC LIMIT 1) AS last_probe_at,
                          (SELECT pp.status
                           FROM endpoint_protocol_profiles pp
                           WHERE pp.endpoint_id = e.id AND pp.protocol = ?
                           LIMIT 1) AS protocol_status
                   FROM vpn_endpoints e
                   LEFT JOIN endpoint_assignments a
                     ON a.endpoint_id = e.id AND a.status = 'active'
                   LEFT JOIN endpoint_plan_limits l
                     ON l.endpoint_id = e.id AND l.plan_code = ?
                   WHERE e.state != 'RETIRED'
                   GROUP BY e.id, e.code, e.region, e.state,
                            e.accepts_new_assignments, e.last_healthy_at,
                            e.max_active_keys, l.enabled, l.max_active_assignments
                   ORDER BY e.code""",
                (plan, selected_protocol, plan),
            ).fetchall()
        max_age_seconds = max(
            30, int(os.environ.get("AURIX_ENDPOINT_HEALTH_MAX_AGE_SECONDS", "900"))
        )
        fresh_after = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
        result = []
        for row in rows:
            item = dict(row)
            try:
                healthy = bool(item.get("last_healthy_at")) and datetime.fromisoformat(
                    str(item["last_healthy_at"])
                ).astimezone(UTC) >= fresh_after
            except (TypeError, ValueError, OverflowError):
                healthy = False
            plan_enabled = item.get("plan_enabled") not in (False, 0)
            at_endpoint_capacity = (
                item.get("max_active_keys") is not None
                and int(item["active_assignments"] or 0) >= int(item["max_active_keys"])
            )
            at_plan_capacity = (
                item.get("plan_max") is not None
                and int(item["plan_active"] or 0) >= int(item["plan_max"])
            )
            item["healthy"] = healthy
            item["protocol"] = selected_protocol
            item["eligible"] = (
                str(item.get("state")) == "ACTIVE"
                and item.get("accepts_new_assignments") not in (False, 0)
                and healthy
                and item.get("protocol_status") == "enabled"
                and plan_enabled
                and not at_endpoint_capacity
                and not at_plan_capacity
            )
            result.append(item)
        return result

    def validate_customer_endpoint(self, endpoint_id: str, plan_code: str) -> dict[str, Any]:
        """Validate one customer-selectable endpoint without exposing secrets."""
        requested = str(endpoint_id or "").strip()
        if not requested or len(requested) > 128:
            raise ConnectivityError("Choose a valid VPN server")
        endpoint = next(
            (item for item in self.list_customer_endpoints(plan_code) if item["id"] == requested),
            None,
        )
        if endpoint is None or not endpoint.get("eligible"):
            raise ConnectivityError("That VPN server is not currently available for this plan")
        return endpoint

    def backfill_free_assignments(self) -> int:
        """Idempotently include pre-registry free/promo keys in capacity accounting."""
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            if connection.__class__.__name__ == "_PostgresConnection":
                exists = connection.execute(
                    "SELECT to_regclass('public.keys') AS table_name"
                ).fetchone()
                if not exists or not exists["table_name"]:
                    return 0
            else:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'keys'"
                ).fetchone()
                if exists is None:
                    return 0
            before = connection.execute(
                "SELECT COUNT(*) AS n FROM endpoint_assignments WHERE free_key_id IS NOT NULL"
            ).fetchone()["n"]
            connection.execute(
                """INSERT INTO endpoint_assignments
                   (id, endpoint_id, subscription_id, free_key_id, plan_code, status,
                    reason, reserved_quota_bytes, assigned_at, released_at)
                   SELECT 'free-' || k.id, k.endpoint_id, NULL, k.id,
                          CASE WHEN g.campaign_code IS NOT NULL THEN 'PROMO:' || g.campaign_code
                               WHEN k.key_type = 'daily_free' THEN 'FREE300MB' ELSE 'FREE3GB' END,
                          CASE WHEN k.status = 'active' THEN 'active' ELSE 'released' END,
                          'legacy-backfill', k.data_limit_bytes, k.created_at,
                          CASE WHEN k.status = 'active' THEN NULL ELSE k.created_at END
                   FROM keys k LEFT JOIN giveaway_claims g ON g.key_id = k.id
                   ON CONFLICT(free_key_id) DO NOTHING"""
            )
            after = connection.execute(
                "SELECT COUNT(*) AS n FROM endpoint_assignments WHERE free_key_id IS NOT NULL"
            ).fetchone()["n"]
        return max(0, int(after) - int(before))

    def endpoint(self, endpoint_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM vpn_endpoints WHERE id = ?", (endpoint_id,)
            ).fetchone()
        if row is None:
            raise ConnectivityError("VPN endpoint does not exist")
        return dict(row)

    def client(self, endpoint_id: str) -> OutlineClient:
        endpoint = self.endpoint(endpoint_id)
        if not endpoint.get("certificate_sha256"):
            raise ConnectivityError("Endpoint certificate fingerprint is unavailable")
        return OutlineClient(
            self._decrypt(endpoint.get("management_url_ciphertext")),
            str(endpoint["certificate_sha256"]),
        )

    @staticmethod
    def usage_for_endpoint(metrics: dict[str, Any] | None, endpoint_id: str) -> dict[str, Any]:
        """Return one endpoint's key map while accepting the legacy one-server shape."""
        if not isinstance(metrics, dict):
            return {}
        scoped = metrics.get("byEndpoint")
        if isinstance(scoped, dict):
            value = scoped.get(endpoint_id, {})
            return value if isinstance(value, dict) else {}
        legacy = metrics.get("bytesTransferredByUserId", {})
        if isinstance(legacy, dict) and legacy:
            return legacy
        return metrics if "byEndpoint" not in metrics and "errors" not in metrics else {}

    def collect_metrics(self) -> dict[str, Any]:
        """Collect collision-safe metrics from every managed, non-retired endpoint.

        A partial endpoint outage is represented in ``errors`` instead of erasing
        healthy observations or falling back to a same-named key on another node.
        """
        by_endpoint: dict[str, dict[str, int]] = {}
        errors: dict[str, str] = {}
        for endpoint in self.list_endpoints():
            endpoint_id = str(endpoint["id"])
            if str(endpoint.get("state")) == "RETIRED":
                continue
            started = time.perf_counter()
            try:
                client = self.client(endpoint_id)
                raw = client.transfer_metrics().get("bytesTransferredByUserId", {})
                if not isinstance(raw, dict):
                    raise ConnectivityError("Outline returned an invalid metrics payload")
                usage = {
                    str(key_id): max(0, int(value or 0))
                    for key_id, value in raw.items()
                }
                by_endpoint[endpoint_id] = usage
                self.record_capacity(
                    endpoint_id,
                    healthy=True,
                    active_key_count=None,
                    observed_transfer_bytes=sum(usage.values()),
                    management_latency_ms=(time.perf_counter() - started) * 1000,
                )
            except Exception as exc:
                errors[endpoint_id] = type(exc).__name__
                self.record_capacity(
                    endpoint_id,
                    healthy=False,
                    active_key_count=None,
                    observed_transfer_bytes=None,
                    management_latency_ms=(time.perf_counter() - started) * 1000,
                    last_error=type(exc).__name__,
                )
        return {"byEndpoint": by_endpoint, "errors": errors}

    def collect_inventory(self) -> dict[str, Any]:
        """Collect access URLs keyed by endpoint, without flattening key IDs."""
        by_endpoint: dict[str, dict[str, str]] = {}
        errors: dict[str, str] = {}
        for endpoint in self.list_endpoints():
            endpoint_id = str(endpoint["id"])
            if str(endpoint.get("state")) == "RETIRED":
                continue
            try:
                keys = self.client(endpoint_id).list_keys().get("accessKeys", [])
                if not isinstance(keys, list):
                    raise ConnectivityError("Outline returned an invalid key inventory")
                by_endpoint[endpoint_id] = {
                    str(item["id"]): str(item["accessUrl"]).replace("\r", "").replace("\n", "").strip()
                    for item in keys
                    if isinstance(item, dict) and item.get("id") and item.get("accessUrl")
                }
            except Exception as exc:
                errors[endpoint_id] = type(exc).__name__
        return {"byEndpoint": by_endpoint, "errors": errors}

    def collect_customer_snapshot(self) -> dict[str, Any]:
        """Fetch usage and key inventory once per endpoint for interactive views."""
        metrics: dict[str, dict[str, int]] = {}
        inventory: dict[str, dict[str, str]] = {}
        errors: dict[str, str] = {}
        for endpoint in self.list_endpoints():
            endpoint_id = str(endpoint["id"])
            if str(endpoint.get("state")) == "RETIRED":
                continue
            try:
                client = self.client(endpoint_id)
                raw_usage = client.transfer_metrics().get("bytesTransferredByUserId", {})
                raw_keys = client.list_keys().get("accessKeys", [])
                if not isinstance(raw_usage, dict) or not isinstance(raw_keys, list):
                    raise ConnectivityError("Outline returned an invalid customer snapshot")
                metrics[endpoint_id] = {
                    str(key_id): max(0, int(value or 0))
                    for key_id, value in raw_usage.items()
                }
                inventory[endpoint_id] = {
                    str(item["id"]): str(item["accessUrl"]).replace("\r", "").replace("\n", "").strip()
                    for item in raw_keys
                    if isinstance(item, dict) and item.get("id") and item.get("accessUrl")
                }
            except Exception as exc:
                errors[endpoint_id] = type(exc).__name__
        return {
            "metrics": {"byEndpoint": metrics, "errors": dict(errors)},
            "inventory": {"byEndpoint": inventory, "errors": dict(errors)},
        }

    def select_endpoint_for_plan(
        self,
        connection: Any,
        plan_code: str,
        preferred_endpoint_id: str | None = None,
        protocol: str | None = "outline",
    ) -> str:
        """Select and lock a capacity-eligible endpoint inside the caller transaction."""
        max_age_seconds = max(
            30, int(os.environ.get("AURIX_ENDPOINT_HEALTH_MAX_AGE_SECONDS", "900"))
        )
        fresh_after = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).isoformat()
        lock = (
            " FOR UPDATE OF e SKIP LOCKED"
            if connection.__class__.__name__ == "_PostgresConnection"
            else ""
        )
        protocol_filter = ""
        normalized_protocol = None
        if protocol is not None:
            normalized_protocol = str(protocol or "").strip().lower()
            if not normalized_protocol or len(normalized_protocol) > 64 or any(
                char.isspace() for char in normalized_protocol
            ):
                raise ConnectivityError("protocol is invalid")
            protocol_filter = """
                 AND EXISTS (
                       SELECT 1 FROM endpoint_protocol_profiles pp
                        WHERE pp.endpoint_id = e.id
                          AND pp.protocol = ? AND pp.status = 'enabled'
                 )"""
        rows = connection.execute(
            """SELECT e.id, e.code, e.max_active_keys,
                      (SELECT COUNT(*) FROM endpoint_assignments a
                        WHERE a.endpoint_id = e.id AND a.status = 'active') AS active_count,
                      l.enabled AS plan_enabled, l.max_active_assignments AS plan_max,
                      (SELECT COUNT(*) FROM endpoint_assignments pa
                        WHERE pa.endpoint_id = e.id AND pa.plan_code = ?
                          AND pa.status = 'active') AS plan_active
               FROM vpn_endpoints e
               LEFT JOIN endpoint_plan_limits l
                 ON l.endpoint_id = e.id AND l.plan_code = ?
               WHERE e.state = 'ACTIVE' AND e.accepts_new_assignments = ?
                 AND e.last_healthy_at IS NOT NULL AND e.last_healthy_at >= ?"""
            + protocol_filter
            + """
                 AND (? IS NULL OR e.id = ?)
               ORDER BY
                 CASE WHEN e.max_active_keys IS NULL THEN 2147483647
                      ELSE e.max_active_keys -
                        (SELECT COUNT(*) FROM endpoint_assignments aa
                          WHERE aa.endpoint_id = e.id AND aa.status = 'active') END DESC,
                 e.code"""
            + lock,
            tuple(
                [plan_code, plan_code, True, fresh_after]
                + ([normalized_protocol] if normalized_protocol else [])
                + [preferred_endpoint_id, preferred_endpoint_id]
            ),
        ).fetchall()
        for row in rows:
            if row["plan_enabled"] is False or row["plan_enabled"] == 0:
                continue
            if row["max_active_keys"] is not None and int(row["active_count"] or 0) >= int(
                row["max_active_keys"]
            ):
                continue
            if row["plan_max"] is not None and int(row["plan_active"] or 0) >= int(
                row["plan_max"]
            ):
                continue
            return str(row["id"])
        raise ConnectivityError("No verified VPN endpoint has capacity for this plan")

    def assign_free_key(
        self,
        connection: Any,
        *,
        endpoint_id: str,
        free_key_id: int,
        plan_code: str,
        quota_bytes: int,
        now: datetime,
    ) -> str:
        assignment_id = uuid.uuid4().hex
        connection.execute(
            """INSERT INTO endpoint_assignments
               (id, endpoint_id, subscription_id, free_key_id, plan_code, status,
                reason, reserved_quota_bytes, assigned_at)
               VALUES (?, ?, NULL, ?, ?, 'active', 'free-entitlement', ?, ?)""",
            (
                assignment_id,
                endpoint_id,
                int(free_key_id),
                plan_code,
                int(quota_bytes),
                now.astimezone(UTC).isoformat(),
            ),
        )
        return assignment_id

    @staticmethod
    def _assignment(row: Any) -> EndpointAssignment:
        return EndpointAssignment(
            id=str(row["id"]),
            endpoint_id=str(row["endpoint_id"]),
            subscription_id=row["subscription_id"],
            free_key_id=row["free_key_id"],
            plan_code=str(row["plan_code"]),
            status=str(row["status"]),
            reserved_quota_bytes=(
                int(row["reserved_quota_bytes"])
                if row["reserved_quota_bytes"] is not None
                else None
            ),
        )

    def assignment_for_subscription(self, subscription_id: str) -> EndpointAssignment | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM endpoint_assignments WHERE subscription_id = ?",
                (subscription_id,),
            ).fetchone()
        return self._assignment(row) if row is not None else None

    def ensure_subscription_assignment(
        self,
        subscription_id: str,
        plan_code: str,
        quota_bytes: int | None,
        *,
        reason: str = "deterministic-allocation",
        preferred_endpoint_id: str | None = None,
        protocol: str | None = "outline",
        now: datetime | None = None,
    ) -> EndpointAssignment:
        timestamp = (now or datetime.now(UTC)).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            existing = connection.execute(
                "SELECT * FROM endpoint_assignments WHERE subscription_id = ?",
                (subscription_id,),
            ).fetchone()
            if existing is not None:
                return self._assignment(existing)
            endpoint_id = self.select_endpoint_for_plan(
                connection,
                plan_code,
                preferred_endpoint_id=preferred_endpoint_id,
                protocol=protocol,
            )
            assignment_id = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO endpoint_assignments
                   (id, endpoint_id, subscription_id, free_key_id, plan_code, status,
                    reason, reserved_quota_bytes, assigned_at)
                   VALUES (?, ?, ?, NULL, ?, 'active', ?, ?, ?)""",
                (
                    assignment_id,
                    endpoint_id,
                    subscription_id,
                    plan_code,
                    reason[:128],
                    quota_bytes,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM endpoint_assignments WHERE id = ?", (assignment_id,)
            ).fetchone()
        return self._assignment(row)

    def transfer_assignment(
        self,
        entitlement_key: str,
        target_endpoint_id: str,
        *,
        reason: str = "operator-drain",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Move one active entitlement reservation without changing its ID.

        A failover/migration must move the durable endpoint reservation along
        with the credential generation.  This keeps capacity accounting and
        customer-facing endpoint state aligned while preserving the
        subscription/free-key identity.
        """
        kind, separator, source_id = str(entitlement_key or "").partition(":")
        if not separator or kind not in {"paid", "free"} or not source_id:
            raise ConnectivityError("entitlement key is invalid")
        target_id = str(target_endpoint_id or "").strip()
        if not target_id or len(target_id) > 128:
            raise ConnectivityError("target endpoint is invalid")
        if kind == "paid":
            assignment_where = "subscription_id = ?"
            assignment_value: Any = source_id
        else:
            try:
                assignment_value = int(source_id)
            except (TypeError, ValueError) as exc:
                raise ConnectivityError("free entitlement key is invalid") from exc
            assignment_where = "free_key_id = ?"
        timestamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        rollback = str(reason or "").strip() == "failover-rollback"
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            lock = " FOR UPDATE" if connection.__class__.__name__ == "_PostgresConnection" else ""
            assignment = connection.execute(
                f"SELECT * FROM endpoint_assignments WHERE {assignment_where} AND status = 'active'{lock}",
                (assignment_value,),
            ).fetchone()
            if assignment is None:
                return {
                    "changed": False,
                    "entitlement_key": entitlement_key,
                    "target_endpoint_id": target_id,
                    "reason": "assignment_missing",
                }
            source_endpoint_id = str(assignment["endpoint_id"])
            if source_endpoint_id == target_id:
                return {
                    "changed": False,
                    "entitlement_key": entitlement_key,
                    "source_endpoint_id": source_endpoint_id,
                    "target_endpoint_id": target_id,
                    "assignment_id": str(assignment["id"]),
                    "reason": "already_on_target",
                }
            target = connection.execute(
                """SELECT e.id, e.state, e.accepts_new_assignments,
                          e.max_active_keys, COUNT(a.id) AS active_count
                     FROM vpn_endpoints e
                     LEFT JOIN endpoint_assignments a
                       ON a.endpoint_id = e.id AND a.status = 'active'
                    WHERE e.id = ?
                    GROUP BY e.id, e.state, e.accepts_new_assignments, e.max_active_keys""",
                (target_id,),
            ).fetchone()
            if target is None or str(target["state"]) not in {"ACTIVE", "DEGRADED", "DRAINING"}:
                raise ConnectivityError("target endpoint is not available")
            if not rollback and target["accepts_new_assignments"] in (False, 0):
                raise ConnectivityError("target endpoint is not accepting assignments")
            if target["max_active_keys"] is not None and int(target["active_count"] or 0) >= int(
                target["max_active_keys"]
            ):
                raise ConnectivityError("target endpoint has no capacity")
            source_protocols = connection.execute(
                """SELECT DISTINCT LOWER(COALESCE(NULLIF(protocol, ''), 'outline')) AS protocol
                     FROM credential_generations
                    WHERE entitlement_key = ? AND endpoint_id = ?
                      AND status IN ('pending', 'active', 'retiring', 'unknown')""",
                (entitlement_key, source_endpoint_id),
            ).fetchall()
            protocols = {
                str(row["protocol"] or "outline").strip().lower()
                for row in source_protocols
                if str(row["protocol"] or "outline").strip()
            } or {"outline"}
            for protocol in sorted(protocols):
                profile = connection.execute(
                    """SELECT 1 FROM endpoint_protocol_profiles
                        WHERE endpoint_id = ? AND protocol = ? AND status = 'enabled'
                        LIMIT 1""",
                    (target_id, protocol),
                ).fetchone()
                if profile is None:
                    raise ConnectivityError(
                        f"target endpoint has no enabled {protocol} protocol profile"
                    )
            plan_limit = connection.execute(
                """SELECT enabled, max_active_assignments
                     FROM endpoint_plan_limits
                    WHERE endpoint_id = ? AND plan_code = ?""",
                (target_id, str(assignment["plan_code"])),
            ).fetchone()
            if plan_limit is not None and not rollback:
                if plan_limit["enabled"] in (False, 0):
                    raise ConnectivityError("target endpoint does not accept this plan")
                if plan_limit["max_active_assignments"] is not None:
                    plan_active = connection.execute(
                        """SELECT COUNT(*) AS n FROM endpoint_assignments
                            WHERE endpoint_id = ? AND plan_code = ? AND status = 'active'""",
                        (target_id, str(assignment["plan_code"])),
                    ).fetchone()
                    if int(plan_active["n"] or 0) >= int(plan_limit["max_active_assignments"]):
                        raise ConnectivityError("target endpoint has no plan capacity")
            connection.execute(
                """UPDATE endpoint_assignments
                      SET endpoint_id = ?, reason = ?
                    WHERE id = ? AND status = 'active'""",
                (target_id, str(reason or "operator-drain")[:128], str(assignment["id"])),
            )
        return {
            "changed": True,
            "entitlement_key": entitlement_key,
            "source_endpoint_id": source_endpoint_id,
            "target_endpoint_id": target_id,
            "assignment_id": str(assignment["id"]),
            "reason": str(reason or "operator-drain")[:128],
            "updated_at": timestamp,
        }

    def attach_job(self, job_id: str, assignment_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE provisioning_jobs SET endpoint_assignment_id = ?
                   WHERE id = ? AND (endpoint_assignment_id IS NULL OR endpoint_assignment_id = ?)""",
                (assignment_id, job_id, assignment_id),
            )

    def record_capacity(
        self,
        endpoint_id: str,
        *,
        healthy: bool,
        active_key_count: int | None,
        observed_transfer_bytes: int | None,
        management_latency_ms: float | None,
        last_error: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = (now or datetime.now(UTC)).isoformat()
        snapshot_id = uuid.uuid4().hex
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """INSERT INTO endpoint_capacity_snapshots
                   (id, endpoint_id, observed_at, healthy, active_key_count,
                    observed_transfer_bytes, management_latency_ms, last_error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id,
                    endpoint_id,
                    timestamp,
                    healthy,
                    active_key_count,
                    observed_transfer_bytes,
                    management_latency_ms,
                    (last_error or "")[:500] or None,
                ),
            )
            connection.execute(
                """UPDATE vpn_endpoints SET
                     last_healthy_at = CASE WHEN ? THEN ? ELSE last_healthy_at END,
                     state = CASE WHEN ? THEN
                               CASE WHEN state = 'DEGRADED' THEN 'ACTIVE' ELSE state END
                              WHEN state = 'ACTIVE' THEN 'DEGRADED' ELSE state END
                   WHERE id = ?""",
                (healthy, timestamp, healthy, endpoint_id),
            )
        return {
            "id": snapshot_id,
            "endpoint_id": endpoint_id,
            "observed_at": timestamp,
            "healthy": healthy,
            "active_key_count": active_key_count,
            "observed_transfer_bytes": observed_transfer_bytes,
            "management_latency_ms": management_latency_ms,
            "last_error": last_error,
        }

    def probe(self, endpoint_id: str) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            client = self.client(endpoint_id)
            info = client.server_info()
            keys = client.list_keys().get("accessKeys", [])
            metrics = client.transfer_metrics().get("bytesTransferredByUserId", {})
            snapshot = self.record_capacity(
                endpoint_id,
                healthy=True,
                active_key_count=len(keys),
                observed_transfer_bytes=sum(int(value or 0) for value in metrics.values()),
                management_latency_ms=(time.perf_counter() - started) * 1000,
            )
            snapshot["outline_version"] = info.get("version")
            return snapshot
        except Exception as exc:
            return self.record_capacity(
                endpoint_id,
                healthy=False,
                active_key_count=None,
                observed_transfer_bytes=None,
                management_latency_ms=(time.perf_counter() - started) * 1000,
                last_error=type(exc).__name__,
            )


class DigitalOceanClient:
    """Small scoped DigitalOcean API client; never logs bearer credentials."""

    BASE_URL = "https://api.digitalocean.com/v2"

    def __init__(self, token: str, timeout_seconds: int = 20):
        if not token:
            raise ValueError("DIGITALOCEAN_API_TOKEN is required")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        payload = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.BASE_URL + path,
            data=payload,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise ConnectivityError(f"DigitalOcean returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ConnectivityError("DigitalOcean request failed") from exc
        return json.loads(raw) if raw else {}

    def create_droplet(self, specification: dict[str, Any]) -> dict[str, Any]:
        result = self._request("POST", "/droplets", specification)
        droplet = result.get("droplet") if isinstance(result, dict) else None
        if not isinstance(droplet, dict) or not droplet.get("id"):
            raise ConnectivityError("DigitalOcean create response lacks a Droplet ID")
        return droplet

    def droplet(self, droplet_id: str | int) -> dict[str, Any]:
        result = self._request("GET", f"/droplets/{droplet_id}")
        droplet = result.get("droplet") if isinstance(result, dict) else None
        if not isinstance(droplet, dict):
            raise ConnectivityError("DigitalOcean response lacks a Droplet")
        return droplet

    def list_by_tag(self, tag: str) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(tag, safe="")
        result = self._request("GET", f"/droplets?tag_name={encoded}&per_page=200")
        droplets = result.get("droplets") if isinstance(result, dict) else None
        if not isinstance(droplets, list):
            raise ConnectivityError("DigitalOcean response lacks Droplets")
        return [item for item in droplets if isinstance(item, dict)]

    def action(self, action_id: str | int) -> dict[str, Any]:
        result = self._request("GET", f"/actions/{action_id}")
        action = result.get("action") if isinstance(result, dict) else None
        if not isinstance(action, dict):
            raise ConnectivityError("DigitalOcean response lacks an action")
        return action

    def destroy_droplet(self, droplet_id: str | int) -> None:
        self._request("DELETE", f"/droplets/{droplet_id}")

    def billing_balance(self) -> dict[str, Any]:
        result = self._request("GET", "/customers/my/balance")
        if not isinstance(result, dict):
            raise ConnectivityError("DigitalOcean response lacks billing data")
        return result


class FleetController:
    """Budget-guarded infrastructure intent creation and provider execution."""

    def __init__(
        self,
        database: Any,
        provider: DigitalOceanClient | None = None,
        registry: EndpointRegistry | None = None,
    ):
        self.database = database
        self.provider = provider
        self.registry = registry

    @staticmethod
    def _mutations_enabled() -> bool:
        return os.environ.get("AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED", "0").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def queue_provision(
        self,
        *,
        region: str,
        size: str,
        image: str,
        requested_by: int,
        now: datetime | None = None,
    ) -> str:
        allowed_regions = {
            item.strip() for item in os.environ.get("AURIX_ALLOWED_REGIONS", "sgp1").split(",")
        }
        allowed_sizes = {
            item.strip()
            for item in os.environ.get("AURIX_ALLOWED_DROPLET_SIZES", "s-1vcpu-1gb").split(",")
        }
        allowed_images = {
            item.strip()
            for item in os.environ.get("AURIX_ALLOWED_DROPLET_IMAGES", "ubuntu-24-04-x64").split(",")
        }
        if region not in allowed_regions or size not in allowed_sizes or image not in allowed_images:
            raise ConnectivityError("Droplet specification is outside the configured allowlist")
        timestamp = (now or datetime.now(UTC)).isoformat()
        fingerprint = hashlib.sha256(f"provision:{region}:{size}:{image}:{timestamp[:13]}".encode()).hexdigest()
        job_id = uuid.uuid4().hex
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            max_total = int(os.environ.get("AURIX_MAX_VPN_NODES", "3"))
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM vpn_endpoints WHERE state != 'RETIRED'"
            ).fetchone()["n"]
            if int(count) >= max_total:
                raise ConnectivityError("Configured VPN node limit has been reached")
            day_start = (now or datetime.now(UTC)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()
            created_today = connection.execute(
                """SELECT COUNT(*) AS n FROM infrastructure_jobs
                   WHERE operation = 'provision' AND created_at >= ?""",
                (day_start,),
            ).fetchone()["n"]
            max_daily = int(os.environ.get("AURIX_MAX_NODE_CREATIONS_PER_DAY", "2"))
            if int(created_today) >= max_daily:
                raise ConnectivityError("Daily VPN node creation limit has been reached")
            latest = connection.execute(
                """SELECT created_at FROM infrastructure_jobs
                   WHERE operation = 'provision' ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
            cooldown_seconds = max(
                0, int(os.environ.get("AURIX_NODE_CREATION_COOLDOWN_SECONDS", "1800"))
            )
            if latest is not None:
                latest_at = datetime.fromisoformat(str(latest["created_at"])).astimezone(UTC)
                if (now or datetime.now(UTC)).astimezone(UTC) < latest_at + timedelta(
                    seconds=cooldown_seconds
                ):
                    raise ConnectivityError("VPN node creation cooldown is still active")
            active = connection.execute(
                """SELECT 1 FROM infrastructure_jobs
                   WHERE operation = 'provision' AND status IN ('pending', 'running')
                   LIMIT 1"""
            ).fetchone()
            if active is not None:
                raise ConnectivityError("Another server provisioning job is already active")
            connection.execute(
                """INSERT INTO infrastructure_jobs
                   (id, operation, status, attempts, next_attempt_at,
                    request_fingerprint, created_at)
                   VALUES (?, 'provision', 'pending', 0, ?, ?, ?)""",
                (job_id, timestamp, fingerprint, timestamp),
            )
            connection.execute(
                """INSERT INTO infrastructure_events
                   (id, infrastructure_job_id, event_type, metadata_json, created_at)
                   VALUES (?, ?, 'provision_requested', ?, ?)""",
                (
                    uuid.uuid4().hex,
                    job_id,
                    json.dumps(
                        {
                            "region": region,
                            "size": size,
                            "image": image,
                            "requested_by": int(requested_by),
                        },
                        sort_keys=True,
                    ),
                    timestamp,
                ),
            )
        return job_id

    def execute_provision(self, job_id: str, specification: dict[str, Any]) -> dict[str, Any]:
        if not self._mutations_enabled():
            raise ConnectivityError("Infrastructure mutations are disabled")
        if self.provider is None:
            raise ConnectivityError("DigitalOcean provider is not configured")
        maximum_budget = os.environ.get("AURIX_MAX_MONTHLY_INFRA_BUDGET_USD", "").strip()
        if maximum_budget:
            try:
                budget = Decimal(maximum_budget)
                estimate = Decimal(
                    os.environ.get("AURIX_DROPLET_MONTHLY_COST_ESTIMATE_USD", "6")
                )
                usage = Decimal(str(self.provider.billing_balance().get("month_to_date_usage", "0")))
            except (InvalidOperation, ValueError, TypeError, AttributeError) as exc:
                raise ConnectivityError("DigitalOcean budget data is invalid") from exc
            if budget <= 0 or estimate < 0 or usage + estimate > budget:
                raise ConnectivityError("Configured monthly infrastructure budget would be exceeded")
        # No permanent secrets are accepted in user data.
        specification = dict(specification)
        user_data = str(specification.get("user_data") or "")
        forbidden = ("DIGITALOCEAN_API_TOKEN", "TELEGRAM_BOT_TOKEN", "SUPABASE_SERVICE_ROLE_KEY")
        if any(name in user_data for name in forbidden):
            raise ConnectivityError("Permanent credentials are forbidden in Droplet user data")
        tags = {str(item) for item in specification.get("tags", [])}
        tags.update({"aurix-vpn-node", "aurix-env-production"})
        specification["tags"] = sorted(tags)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM infrastructure_jobs WHERE id = ? AND status = 'pending'",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ConnectivityError("Provisioning job is not pending")
            connection.execute(
                "UPDATE infrastructure_jobs SET status = 'running', attempts = attempts + 1, locked_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), job_id),
            )
        try:
            droplet = self.provider.create_droplet(specification)
        except Exception as exc:
            timestamp = datetime.now(UTC).isoformat()
            with self.database.connect() as connection:
                connection.execute(
                    """UPDATE infrastructure_jobs SET status = 'failed', locked_at = NULL,
                              last_error = ?, next_attempt_at = ? WHERE id = ?""",
                    (f"{type(exc).__name__}: {str(exc)[:300]}", timestamp, job_id),
                )
                connection.execute(
                    """INSERT INTO infrastructure_events
                       (id, infrastructure_job_id, event_type, metadata_json, created_at)
                       VALUES (?, ?, 'provider_create_failed', '{}', ?)""",
                    (uuid.uuid4().hex, job_id, timestamp),
                )
            raise
        action_ids = droplet.get("action_ids") or []
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE infrastructure_jobs SET provider_resource_id = ?,
                          provider_action_id = ?, status = 'running'
                   WHERE id = ?""",
                (
                    str(droplet["id"]),
                    str(action_ids[0]) if action_ids else None,
                    job_id,
                ),
            )
        return {"job_id": job_id, "droplet_id": str(droplet["id"]), "status": "creating"}

    def reconcile_provision(self, job_id: str) -> dict[str, Any]:
        """Observe asynchronous provider state; never guesses Outline readiness."""
        if self.provider is None:
            raise ConnectivityError("DigitalOcean provider is not configured")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM infrastructure_jobs WHERE id = ? AND operation = 'provision'",
                (job_id,),
            ).fetchone()
        if row is None:
            raise ConnectivityError("Provisioning job does not exist")
        if row["status"] in ("failed", "completed"):
            return {"job_id": job_id, "status": str(row["status"])}
        action_id = row["provider_action_id"]
        if action_id:
            action = self.provider.action(str(action_id))
            action_status = str(action.get("status") or "unknown")
            if action_status == "errored":
                with self.database.connect() as connection:
                    connection.execute(
                        """UPDATE infrastructure_jobs SET status = 'failed', locked_at = NULL,
                                  last_error = 'DigitalOcean create action failed'
                           WHERE id = ?""",
                        (job_id,),
                    )
                return {"job_id": job_id, "status": "failed"}
            if action_status != "completed":
                return {"job_id": job_id, "status": "creating", "provider_status": action_status}
        droplet = self.provider.droplet(str(row["provider_resource_id"]))
        if str(droplet.get("status")) != "active":
            return {
                "job_id": job_id,
                "status": "creating",
                "provider_status": str(droplet.get("status") or "unknown"),
            }
        public_ip = next(
            (
                str(network.get("ip_address"))
                for network in (droplet.get("networks") or {}).get("v4", [])
                if isinstance(network, dict) and network.get("type") == "public"
            ),
            None,
        )
        timestamp = datetime.now(UTC).isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE infrastructure_jobs SET status = 'awaiting_verification',
                          locked_at = NULL WHERE id = ? AND status = 'running'""",
                (job_id,),
            )
            connection.execute(
                """INSERT INTO infrastructure_events
                   (id, infrastructure_job_id, event_type, metadata_json, created_at)
                   VALUES (?, ?, 'droplet_active', ?, ?)""",
                (
                    uuid.uuid4().hex,
                    job_id,
                    json.dumps({"public_ip": public_ip}, sort_keys=True),
                    timestamp,
                ),
            )
        return {"job_id": job_id, "status": "awaiting_verification", "public_ip": public_ip}

    def verify_and_activate(
        self,
        job_id: str,
        *,
        code: str,
        region: str,
        api_url: str,
        certificate_sha256: str,
        max_active_keys: int | None = None,
    ) -> dict[str, Any]:
        """Register an Outline endpoint only after a real management-API probe."""
        if os.environ.get("AURIX_ENDPOINT_ACTIVATION_ENABLED", "0").lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise ConnectivityError("New endpoint activation is disabled")
        if self.registry is None:
            raise ConnectivityError("Endpoint registry is not configured")
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT * FROM infrastructure_jobs
                   WHERE id = ? AND operation = 'provision' AND status = 'awaiting_verification'""",
                (job_id,),
            ).fetchone()
        if row is None or not row["provider_resource_id"]:
            raise ConnectivityError("Provisioning job is not awaiting endpoint verification")
        endpoint_id = f"do-{row['provider_resource_id']}"
        endpoint = self.registry.register_verified_endpoint(
            endpoint_id=endpoint_id,
            code=code,
            provider="digitalocean",
            provider_resource_id=str(row["provider_resource_id"]),
            region=region,
            api_url=api_url,
            certificate_sha256=certificate_sha256,
            max_active_keys=max_active_keys,
        )
        timestamp = datetime.now(UTC).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE infrastructure_jobs SET endpoint_id = ?, status = 'completed',
                          completed_at = ?, locked_at = NULL, last_error = NULL WHERE id = ?""",
                (endpoint_id, timestamp, job_id),
            )
            connection.execute(
                """INSERT INTO infrastructure_events
                   (id, infrastructure_job_id, endpoint_id, event_type, metadata_json, created_at)
                   VALUES (?, ?, ?, 'endpoint_verified', '{}', ?)""",
                (uuid.uuid4().hex, job_id, endpoint_id, timestamp),
            )
        return endpoint
