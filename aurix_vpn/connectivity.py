"""Endpoint registry, deterministic allocation, and guarded fleet operations."""

from __future__ import annotations

import hashlib
import ipaddress
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


class AmbiguousProviderOperation(ConnectivityError):
    """The provider response was lost after a request may have been accepted."""


class ProvisionValidationError(ConnectivityError):
    """A durable infrastructure intent cannot pass deterministic validation."""


@dataclass(frozen=True)
class EndpointAssignment:
    id: str
    endpoint_id: str
    subscription_id: str | None
    free_key_id: int | None
    plan_code: str
    protocol: str
    status: str
    reserved_quota_bytes: int | None


class EndpointRegistry:
    """PostgreSQL/SQLite-compatible endpoint and assignment repository."""

    # These are the minimum commercial-readiness gates for every non-Outline
    # transport.  Operator-supplied requirements may add checks, but cannot
    # remove any of these documented lifecycle and data-plane checks.
    _NON_OUTLINE_PROMOTION_SIGNALS = (
        "management",
        "usage",
        "quota",
        "restart",
        "data_plane",
    )
    _NON_OUTLINE_PROMOTION_CAPABILITIES = (
        "managed_config",
        "manual_export",
        "quota_cap",
        "usage",
        "rotation",
        "management_probe",
        "data_plane_probe",
        "reconcile",
    )
    _NON_OUTLINE_PROMOTION_EVIDENCE = {
        "usage": "sample_count",
        "quota": "quota_enforced",
        "restart": "restart_persisted",
        "data_plane": "data_plane_result",
    }
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

    @classmethod
    def protocol_promotion_requirements(cls, protocol: str) -> dict[str, tuple[str, ...]]:
        """Return immutable minimum promotion gates for one transport.

        Outline is already the default production transport and retains its
        existing caller-selected evidence behavior.  Every other transport
        must prove the full documented lifecycle before it can be enabled.
        """
        normalized = str(protocol or "").strip().lower()
        if normalized == "outline":
            return {"signals": (), "capabilities": ()}
        return {
            "signals": cls._NON_OUTLINE_PROMOTION_SIGNALS,
            "capabilities": cls._NON_OUTLINE_PROMOTION_CAPABILITIES,
        }

    @classmethod
    def _normalize_promotion_requirements(
        cls,
        protocol: str,
        required_signals: tuple[str, ...] | list[str],
        required_capabilities: tuple[str, ...] | list[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if isinstance(required_signals, str) or isinstance(required_capabilities, str):
            raise ConnectivityError("protocol evidence requirements must be a sequence")
        operator_signals = {
            str(value or "").strip().lower()
            for value in required_signals
            if str(value or "").strip()
        }
        operator_capabilities = {
            str(value or "").strip().lower()
            for value in required_capabilities
            if str(value or "").strip()
        }
        minimums = cls.protocol_promotion_requirements(protocol)
        signals = tuple(sorted(operator_signals.union(minimums["signals"])))
        capabilities = tuple(sorted(operator_capabilities.union(minimums["capabilities"])))
        return signals, capabilities

    @classmethod
    def _promotion_evidence_is_credible(
        cls, protocol: str, signal: str, details: dict[str, Any]
    ) -> bool:
        """Require the bounded proof field associated with mandatory gates."""
        if str(protocol or "").strip().lower() == "outline":
            return True
        if signal == "usage":
            sample_count = details.get("sample_count")
            return isinstance(sample_count, int) and not isinstance(sample_count, bool) and sample_count > 0
        if signal in {"quota", "restart"}:
            return details.get(cls._NON_OUTLINE_PROMOTION_EVIDENCE[signal]) is True
        if signal == "data_plane":
            client_path = details.get("client_path")
            if isinstance(client_path, str) and client_path.strip():
                return True
            status_code = details.get("status_code")
            return (
                isinstance(status_code, int)
                and not isinstance(status_code, bool)
                and 200 <= status_code < 400
            )
        return True

    @staticmethod
    def _audit_events_available(connection: Any) -> bool:
        """Return whether this database has the optional commerce audit table."""
        if isinstance(connection, _PostgresConnection):
            row = connection.execute(
                "SELECT to_regclass(?) AS table_name", ("public.audit_events",)
            ).fetchone()
            return bool(row and row["table_name"])
        row = connection.execute(
            """SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'audit_events'"""
        ).fetchone()
        return row is not None

    @classmethod
    def _record_audit_event(
        cls,
        connection: Any,
        *,
        actor_type: str,
        actor_id: str | int | None,
        action: str,
        target_type: str,
        target_id: str,
        metadata: dict[str, Any] | None,
        created_at: str,
    ) -> None:
        """Write a bounded, non-secret audit event when commerce is initialized."""
        if not cls._audit_events_available(connection):
            return
        connection.execute(
            """INSERT INTO audit_events
               (actor_type, actor_id, action, target_type, target_id,
                metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                str(actor_type)[:64],
                None if actor_id is None else str(actor_id)[:128],
                str(action)[:128],
                str(target_type)[:128],
                str(target_id)[:256],
                json.dumps(dict(metadata or {}), sort_keys=True, separators=(",", ":")),
                created_at,
            ),
        )

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
        if profile_status == "enabled" and transport != "outline":
            raise ConnectivityError(
                "non-Outline protocol profiles require promote_protocol_profile"
            )
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
                "SELECT state FROM vpn_endpoints WHERE id = ?", (endpoint,)
            ).fetchone()
            if exists is None:
                raise ConnectivityError("VPN endpoint does not exist")
            if str(exists["state"] or "").upper() == "RETIRED" and profile_status != "retired":
                raise ConnectivityError("retired endpoint cannot accept protocol profiles")
            current = connection.execute(
                "SELECT status FROM endpoint_protocol_profiles WHERE endpoint_id = ? AND protocol = ?",
                (endpoint, transport),
            ).fetchone()
            if current is not None and str(current["status"] or "").lower() == "retired" and profile_status != "retired":
                raise ConnectivityError("retired protocol profile is terminal")
            retired_at = timestamp if profile_status == "retired" else None
            connection.execute(
                """INSERT INTO endpoint_protocol_profiles
                   (profile_id, endpoint_id, protocol, adapter_type, status,
                    capabilities_json, verified_at, last_healthy_at, created_at,
                    retired_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(endpoint_id, protocol) DO UPDATE SET
                     adapter_type = excluded.adapter_type,
                     status = excluded.status,
                     capabilities_json = excluded.capabilities_json,
                     verified_at = COALESCE(excluded.verified_at,
                                            endpoint_protocol_profiles.verified_at),
                     last_healthy_at = COALESCE(excluded.last_healthy_at,
                                                endpoint_protocol_profiles.last_healthy_at),
                     retired_at = CASE WHEN excluded.status = 'retired'
                                       THEN COALESCE(endpoint_protocol_profiles.retired_at,
                                                     excluded.retired_at,
                                                     excluded.last_healthy_at,
                                                     excluded.verified_at,
                                                     excluded.created_at)
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
                    retired_at,
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

    def protocol_profile_status(self, endpoint_id: str, protocol: str) -> dict[str, Any]:
        """Return one non-secret profile for an operator state check."""
        endpoint = str(endpoint_id or "").strip()
        transport = str(protocol or "").strip().lower()
        if not endpoint or len(endpoint) > 128:
            raise ConnectivityError("endpoint ID is invalid")
        if not transport or len(transport) > 64 or any(char.isspace() for char in transport):
            raise ConnectivityError("protocol is invalid")
        profile = next(
            (item for item in self.list_protocol_profiles(endpoint) if item["protocol"] == transport),
            None,
        )
        if profile is None:
            raise ConnectivityError("protocol profile does not exist")
        return profile

    def disable_protocol_profile(
        self,
        endpoint_id: str,
        protocol: str,
        *,
        actor_id: str | int | None = None,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Stop new allocation for a profile without revoking existing credentials."""
        profile = self.protocol_profile_status(endpoint_id, protocol)
        if profile["status"] == "retired":
            raise ConnectivityError("retired protocol profile cannot be disabled")
        timestamp = self._observation_time(now).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            current = connection.execute(
                """SELECT profile_id, endpoint_id, protocol, status
                     FROM endpoint_protocol_profiles
                    WHERE profile_id = ?""",
                (str(profile["profile_id"]),),
            ).fetchone()
            if current is None:
                raise ConnectivityError("protocol profile does not exist")
            current_status = str(current["status"] or "").lower()
            if current_status == "retired":
                raise ConnectivityError("retired protocol profile cannot be disabled")
            connection.execute(
                """UPDATE endpoint_protocol_profiles
                      SET status = 'disabled', retired_at = NULL
                    WHERE profile_id = ?""",
                (str(profile["profile_id"]),),
            )
            self._record_audit_event(
                connection,
                actor_type="admin",
                actor_id=actor_id,
                action="protocol_profile_disabled",
                target_type="endpoint_protocol_profile",
                target_id=str(profile["profile_id"]),
                metadata={"previous_status": current_status},
                created_at=timestamp,
            )
        return self.protocol_profile_status(profile["endpoint_id"], profile["protocol"])

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

    @staticmethod
    def _health_threshold(name: str, default: int) -> int:
        try:
            value = int(os.environ.get(name, str(default)))
        except (TypeError, ValueError) as exc:
            raise ConnectivityError(f"{name} is invalid") from exc
        if not 1 <= value <= 100:
            raise ConnectivityError(f"{name} is invalid")
        return value

    @staticmethod
    def _health_recovery_cooldown(name: str, default: int) -> int:
        try:
            value = int(os.environ.get(name, str(default)))
        except (TypeError, ValueError) as exc:
            raise ConnectivityError(f"{name} is invalid") from exc
        if not 0 <= value <= 86_400:
            raise ConnectivityError(f"{name} is invalid")
        return value

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

    def protocol_profile_promotion_readiness(
        self,
        endpoint_id: str,
        protocol: str,
        *,
        required_signals: tuple[str, ...] | list[str],
        required_capabilities: tuple[str, ...] | list[str] = (),
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Return a non-mutating, redacted promotion decision preview.

        The preview deliberately does not change profile state or contact a
        provider. It lets an operator see whether the explicitly selected
        evidence and capability requirements are satisfied before calling the
        state-changing promotion method.
        """
        endpoint = str(endpoint_id or "").strip()
        transport = str(protocol or "").strip().lower()
        if not endpoint or len(endpoint) > 128:
            raise ConnectivityError("endpoint ID is invalid")
        if not transport or len(transport) > 64 or any(char.isspace() for char in transport):
            raise ConnectivityError("protocol is invalid")
        signals, capabilities = self._normalize_promotion_requirements(
            transport, required_signals, required_capabilities
        )
        if not signals:
            raise ConnectivityError("at least one protocol evidence signal is required")
        if any(len(value) > 64 or any(char.isspace() for char in value) for value in signals):
            raise ConnectivityError("protocol evidence signal is invalid")
        if any(len(value) > 64 or any(char.isspace() for char in value) for value in capabilities):
            raise ConnectivityError("protocol capability is invalid")
        checked_at = self._observation_time(now)
        with self.database.connect() as connection:
            profile = connection.execute(
                """SELECT profile_id, status, capabilities_json
                     FROM endpoint_protocol_profiles
                    WHERE endpoint_id = ? AND protocol = ?""",
                (endpoint, transport),
            ).fetchone()
            base = {
                "endpoint_id": endpoint,
                "protocol": transport,
                "required_signals": list(signals),
                "required_capabilities": list(capabilities),
                "minimum_required_signals": list(
                    self.protocol_promotion_requirements(transport)["signals"]
                ),
                "minimum_required_capabilities": list(
                    self.protocol_promotion_requirements(transport)["capabilities"]
                ),
                "checked_at": checked_at.isoformat(),
                "profile_exists": profile is not None,
                "profile_id": None if profile is None else str(profile["profile_id"]),
                "profile_status": None if profile is None else str(profile["status"] or "").lower(),
                "declared_capabilities": {},
                "fresh_healthy_signals": [],
                "missing_signals": list(signals),
                "missing_evidence": [],
                "missing_capabilities": list(capabilities),
                "latest_healthy_at": None,
                "reasons": [],
                "promotable": False,
            }
            if profile is None:
                base["reasons"] = ["protocol profile does not exist"]
                return base
            try:
                declared = json.loads(str(profile["capabilities_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                declared = {}
            if not isinstance(declared, dict):
                declared = {}
            declared = {str(key): value is True for key, value in declared.items()}
            missing_capabilities = [
                name for name in capabilities if declared.get(name) is not True
            ]
            observations = connection.execute(
                """SELECT signal, details_json, observed_at, expires_at
                     FROM endpoint_protocol_observations
                    WHERE profile_id = ? AND status = 'healthy'""",
                (str(profile["profile_id"]),),
            ).fetchall()
            fresh_signals: set[str] = set()
            incomplete_evidence: set[str] = set()
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
                if observed > checked_at or (expires is not None and expires <= checked_at):
                    continue
                observation_signal = str(observation["signal"]).strip().lower()
                try:
                    details = json.loads(str(observation["details_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    details = {}
                if not isinstance(details, dict):
                    details = {}
                if self._promotion_evidence_is_credible(transport, observation_signal, details):
                    fresh_signals.add(observation_signal)
                    incomplete_evidence.discard(observation_signal)
                elif observation_signal in self._NON_OUTLINE_PROMOTION_EVIDENCE:
                    incomplete_evidence.add(observation_signal)
                if latest_healthy is None or observed > latest_healthy:
                    latest_healthy = observed
            missing_signals = [name for name in signals if name not in fresh_signals]
            missing_evidence = [
                name
                for name in signals
                if name in incomplete_evidence and name not in fresh_signals
            ]
            reasons: list[str] = []
            if str(profile["status"] or "").lower() == "retired":
                reasons.append("retired protocol profile cannot be promoted")
            if missing_capabilities:
                reasons.append(
                    "protocol profile lacks required capabilities: "
                    + ", ".join(missing_capabilities)
                )
            if missing_signals:
                reasons.append(
                    "protocol profile evidence is incomplete: " + ", ".join(missing_signals)
                )
            if missing_evidence:
                reasons.append(
                    "protocol profile evidence details are incomplete: "
                    + ", ".join(missing_evidence)
                )
            base.update(
                {
                    "declared_capabilities": declared,
                    "fresh_healthy_signals": sorted(fresh_signals),
                    "missing_signals": missing_signals,
                    "missing_evidence": missing_evidence,
                    "missing_capabilities": missing_capabilities,
                    "latest_healthy_at": latest_healthy.isoformat() if latest_healthy else None,
                    "reasons": reasons,
                    "promotable": not reasons,
                }
            )
            return base

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
        if not endpoint or len(endpoint) > 128:
            raise ConnectivityError("endpoint ID is invalid")
        if not transport or len(transport) > 64 or any(char.isspace() for char in transport):
            raise ConnectivityError("protocol is invalid")
        signals, capabilities = self._normalize_promotion_requirements(
            transport, required_signals, required_capabilities
        )
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
                """SELECT signal, details_json, observed_at, expires_at
                     FROM endpoint_protocol_observations
                    WHERE profile_id = ? AND status = 'healthy'""",
                (str(profile["profile_id"]),),
            ).fetchall()
            fresh_signals: set[str] = set()
            incomplete_evidence: set[str] = set()
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
                observation_signal = str(observation["signal"]).strip().lower()
                try:
                    details = json.loads(str(observation["details_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    details = {}
                if not isinstance(details, dict):
                    details = {}
                if self._promotion_evidence_is_credible(transport, observation_signal, details):
                    fresh_signals.add(observation_signal)
                    incomplete_evidence.discard(observation_signal)
                elif observation_signal in self._NON_OUTLINE_PROMOTION_EVIDENCE:
                    incomplete_evidence.add(observation_signal)
                if latest_healthy is None or observed > latest_healthy:
                    latest_healthy = observed
            missing_signals = [name for name in signals if name not in fresh_signals]
            missing_evidence = [
                name
                for name in signals
                if name in incomplete_evidence and name not in fresh_signals
            ]
            if missing_signals:
                raise ConnectivityError(
                    "protocol profile evidence is incomplete: "
                    + ", ".join(missing_signals)
                    + (
                        "; evidence details are incomplete: "
                        + ", ".join(missing_evidence)
                        if missing_evidence
                        else ""
                    )
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
            self._record_audit_event(
                connection,
                actor_type="admin",
                actor_id=actor_id,
                action="protocol_profile_promoted",
                target_type="endpoint_protocol_profile",
                target_id=str(profile["profile_id"]),
                metadata={
                    "required_signals": list(signals),
                    "required_capabilities": list(capabilities),
                },
                created_at=timestamp,
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
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT state FROM vpn_endpoints WHERE id = ?", (str(endpoint_id),)
            ).fetchone()
        if existing is not None and str(existing["state"]).upper() in {"DRAINING", "RETIRED"}:
            raise ConnectivityError("endpoint lifecycle does not permit activation")
        fingerprint = certificate_sha256.lower().replace(":", "")
        client = OutlineClient(api_url.rstrip("/"), fingerprint)
        info = client.server_info()
        timestamp = (now or datetime.now(UTC)).isoformat()
        parsed = urllib.parse.urlsplit(api_url)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            lock = " FOR UPDATE" if isinstance(connection, _PostgresConnection) else ""
            current = connection.execute(
                "SELECT state FROM vpn_endpoints WHERE id = ?" + lock, (str(endpoint_id),)
            ).fetchone()
            if current is not None and str(current["state"]).upper() in {"DRAINING", "RETIRED"}:
                raise ConnectivityError("endpoint lifecycle does not permit activation")
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

    def validate_customer_endpoint(
        self, endpoint_id: str, plan_code: str, protocol: str = "outline"
    ) -> dict[str, Any]:
        """Validate one customer-selectable endpoint without exposing secrets."""
        requested = str(endpoint_id or "").strip()
        if not requested or len(requested) > 128:
            raise ConnectivityError("Choose a valid VPN server")
        selected_protocol = str(protocol or "").strip().lower()
        if not selected_protocol or len(selected_protocol) > 64 or any(
            char.isspace() for char in selected_protocol
        ):
            raise ConnectivityError("Choose a valid VPN protocol")
        endpoint = next(
            (
                item
                for item in self.list_customer_endpoints(plan_code, selected_protocol)
                if item["id"] == requested
            ),
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

    @staticmethod
    def _endpoint_lifecycle_counts(connection: Any, endpoint_id: str) -> dict[str, int]:
        """Return durable blockers for a local endpoint retirement decision.

        This deliberately checks both the assignment projection and the
        credential/key projections.  A stale projection must block retirement;
        it must never be silently interpreted as an empty endpoint.
        """
        def count(query: str, values: tuple[Any, ...] = ()) -> int:
            row = connection.execute(query, values).fetchone()
            return int(row["n"] or 0) if row is not None else 0

        return {
            "active_assignments": count(
                """SELECT COUNT(*) AS n FROM endpoint_assignments
                    WHERE endpoint_id = ? AND status = 'active'""",
                (endpoint_id,),
            ),
            "active_free_keys": count(
                """SELECT COUNT(*) AS n FROM keys
                    WHERE endpoint_id = ? AND status IN ('active', 'revoke_failed')""",
                (endpoint_id,),
            ),
            "active_paid_keys": count(
                """SELECT COUNT(*) AS n FROM paid_vpn_keys
                    WHERE endpoint_id = ? AND status IN ('active', 'revoke_failed')""",
                (endpoint_id,),
            ),
            "live_generations": count(
                """SELECT COUNT(*) AS n FROM credential_generations
                    WHERE endpoint_id = ? AND status IN ('pending', 'active', 'retiring', 'unknown')""",
                (endpoint_id,),
            ),
            "unresolved_remote_generations": count(
                """SELECT COUNT(*) AS n FROM credential_generations
                    WHERE endpoint_id = ? AND remote_state != 'revoked_verified'""",
                (endpoint_id,),
            ),
            "pending_free_provisioning": count(
                """SELECT COUNT(*) AS n FROM free_provisioning_jobs
                    WHERE endpoint_id = ? AND status IN ('pending', 'running')""",
                (endpoint_id,),
            ),
            "pending_giveaway_provisioning": count(
                """SELECT COUNT(*) AS n FROM giveaway_provisioning_jobs
                    WHERE endpoint_id = ? AND status IN ('pending', 'running')""",
                (endpoint_id,),
            ),
            "pending_paid_provisioning": count(
                """SELECT COUNT(*) AS n
                     FROM provisioning_jobs p
                     JOIN endpoint_assignments a ON a.id = p.endpoint_assignment_id
                    WHERE a.endpoint_id = ? AND p.status IN ('pending', 'running')""",
                (endpoint_id,),
            ),
            "pending_failover": count(
                """SELECT COUNT(*) AS n FROM failover_decisions
                    WHERE (source_endpoint_id = ? OR target_endpoint_id = ?)
                      AND state IN ('pending', 'creating', 'verified')""",
                (endpoint_id, endpoint_id),
            ),
            "pending_infrastructure": count(
                """SELECT COUNT(*) AS n FROM infrastructure_jobs
                    WHERE endpoint_id = ? AND status IN ('pending', 'running', 'awaiting_verification')""",
                (endpoint_id,),
            ),
        }

    @staticmethod
    def _endpoint_lifecycle_blockers(
        current: str,
        desired: str,
        counts: dict[str, int],
    ) -> list[str]:
        blockers: list[str] = []
        if desired == "RETIRED":
            if current not in {"DRAINING", "RETIRED"}:
                blockers.append("endpoint must be draining first")
            for name, label in (
                ("active_assignments", "active assignments"),
                ("active_free_keys", "active free keys"),
                ("active_paid_keys", "active paid keys"),
                ("live_generations", "live credential generations"),
                ("unresolved_remote_generations", "unresolved remote generations"),
                ("pending_free_provisioning", "pending free provisioning"),
                ("pending_giveaway_provisioning", "pending giveaway provisioning"),
                ("pending_paid_provisioning", "pending paid provisioning"),
                ("pending_failover", "pending failover decisions"),
                ("pending_infrastructure", "pending infrastructure work"),
            ):
                if counts[name]:
                    blockers.append(f"{counts[name]} {label}")
        elif desired in {"ACTIVE", "DRAINING"} and current == "RETIRED":
            blockers.append("retired endpoint is terminal")
        return blockers

    def endpoint_lifecycle_preview(
        self,
        endpoint_id: str,
        requested_state: str,
    ) -> dict[str, Any]:
        """Return a read-only, durable readiness check for a lifecycle change."""
        normalized_id = str(endpoint_id or "").strip()
        desired = str(requested_state or "").strip().upper()
        if not normalized_id or len(normalized_id) > 128:
            raise ConnectivityError("Endpoint identity is invalid")
        if desired not in {"ACTIVE", "DRAINING", "RETIRED"}:
            raise ConnectivityError("Endpoint lifecycle must be active, draining or retired")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id, code, state, accepts_new_assignments, retired_at FROM vpn_endpoints WHERE id = ?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise ConnectivityError("VPN endpoint does not exist")
            counts = self._endpoint_lifecycle_counts(connection, normalized_id)
        current = str(row["state"] or "ACTIVE").upper()
        blockers = self._endpoint_lifecycle_blockers(current, desired, counts)
        return {
            "endpoint_id": normalized_id,
            "endpoint_code": str(row["code"] or normalized_id),
            "current_state": current,
            "requested_state": desired,
            "accepts_new_assignments": bool(row["accepts_new_assignments"]),
            "retired_at": row["retired_at"],
            "counts": counts,
            "blockers": blockers,
            "ready": not blockers,
        }

    def set_endpoint_lifecycle(
        self,
        endpoint_id: str,
        requested_state: str,
        *,
        actor_id: int | None = None,
        reason: str = "operator-lifecycle",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Apply a local lifecycle transition without touching the provider.

        Retirement is intentionally terminal and requires the endpoint to be
        draining with every durable assignment, credential, and queued work
        projection empty. Provider destruction is a separate owner-approved
        operation and is not part of this method.
        """
        normalized_id = str(endpoint_id or "").strip()
        desired = str(requested_state or "").strip().upper()
        if not normalized_id or len(normalized_id) > 128:
            raise ConnectivityError("Endpoint identity is invalid")
        if desired not in {"ACTIVE", "DRAINING", "RETIRED"}:
            raise ConnectivityError("Endpoint lifecycle must be active, draining or retired")
        timestamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        clean_reason = str(reason or "operator-lifecycle").strip()[:256] or "operator-lifecycle"
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            lock = " FOR UPDATE" if isinstance(connection, _PostgresConnection) else ""
            row = connection.execute(
                "SELECT id, code, state, accepts_new_assignments, retired_at FROM vpn_endpoints WHERE id = ?"
                + lock,
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise ConnectivityError("VPN endpoint does not exist")
            current = str(row["state"] or "ACTIVE").upper()
            if current == desired:
                counts = self._endpoint_lifecycle_counts(connection, normalized_id)
                return {
                    "endpoint_id": normalized_id,
                    "endpoint_code": str(row["code"] or normalized_id),
                    "current_state": current,
                    "requested_state": desired,
                    "accepts_new_assignments": bool(row["accepts_new_assignments"]),
                    "retired_at": row["retired_at"],
                    "counts": counts,
                    "blockers": [],
                    "ready": True,
                    "changed": False,
                }
            counts = self._endpoint_lifecycle_counts(connection, normalized_id)
            blockers = self._endpoint_lifecycle_blockers(current, desired, counts)
            if blockers:
                raise ConnectivityError("Endpoint lifecycle change is blocked: " + "; ".join(blockers))
            accepts = desired == "ACTIVE"
            retired_at = timestamp if desired == "RETIRED" else None
            connection.execute(
                """UPDATE vpn_endpoints
                      SET state = ?, accepts_new_assignments = ?,
                          retired_at = CASE WHEN ? = 'RETIRED' THEN ? ELSE retired_at END
                    WHERE id = ?""",
                (desired, accepts, desired, retired_at, normalized_id),
            )
            connection.execute(
                """INSERT INTO infrastructure_events
                   (id, endpoint_id, event_type, metadata_json, created_at)
                   VALUES (?, ?, 'endpoint_lifecycle_changed', ?, ?)""",
                (
                    uuid.uuid4().hex,
                    normalized_id,
                    json.dumps(
                        {
                            "actor_id": actor_id,
                            "previous_state": current,
                            "requested_state": desired,
                            "reason": clean_reason,
                            "counts": counts,
                        },
                        sort_keys=True,
                    ),
                    timestamp,
                ),
            )
            self._record_audit_event(
                connection,
                actor_type="admin",
                actor_id=actor_id,
                action="endpoint_lifecycle_changed",
                target_type="vpn_endpoint",
                target_id=normalized_id,
                metadata={
                    "previous_state": current,
                    "requested_state": desired,
                    "reason": clean_reason,
                    "counts": counts,
                },
                created_at=timestamp,
            )
        return self.endpoint_lifecycle_preview(normalized_id, desired) | {
            "changed": True,
            "changed_at": timestamp,
            "previous_state": current,
            "reason": clean_reason,
        }

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

    @staticmethod
    def _has_enabled_outline_profile(endpoint: dict[str, Any]) -> bool:
        """Return whether the Outline management collector owns this endpoint."""
        profiles = endpoint.get("protocols")
        if not isinstance(profiles, list) or not profiles:
            # Legacy callers may inject endpoint rows without protocol metadata;
            # preserve their Outline behavior until the registry is upgraded.
            return True
        return any(
            str(profile.get("protocol") or "").strip().lower() == "outline"
            and str(profile.get("status") or "").strip().lower() == "enabled"
            for profile in profiles
            if isinstance(profile, dict)
        )

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
            if not self._has_enabled_outline_profile(endpoint):
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

    def persist_usage_snapshot(
        self,
        metrics: dict[str, Any] | None,
        *,
        now: datetime | None = None,
        source: str = "maintenance",
    ) -> int:
        """Persist bounded per-key usage observations for read-only customer views.

        Provider reads belong to the maintenance worker.  Customer requests
        should consume this local snapshot rather than synchronously querying
        every endpoint.  Invalid counters are ignored and an unavailable
        legacy schema is reported as zero writes for compatibility.
        """
        scoped = metrics.get("byEndpoint") if isinstance(metrics, dict) else None
        if not isinstance(scoped, dict):
            return 0
        timestamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        normalized_source = str(source or "maintenance").strip()[:64] or "maintenance"
        observations: list[tuple[str, str, int, str, str]] = []
        endpoint_status: dict[str, tuple[str, str | None]] = {}
        for endpoint_id, values in scoped.items():
            endpoint = str(endpoint_id or "").strip()
            if not endpoint:
                continue
            if not isinstance(values, dict):
                endpoint_status[endpoint] = ("failed", "invalid_payload")
                continue
            endpoint_status[endpoint] = ("healthy", None)
            for external_id, raw_value in values.items():
                if isinstance(raw_value, bool):
                    continue
                try:
                    observed_bytes = int(raw_value)
                except (TypeError, ValueError):
                    continue
                if observed_bytes < 0:
                    continue
                key = str(external_id or "").strip()
                if not key:
                    continue
                observations.append(
                    (endpoint, key, observed_bytes, timestamp, normalized_source)
                )
        errors = metrics.get("errors") if isinstance(metrics, dict) else None
        if isinstance(errors, dict):
            for endpoint_id, error in errors.items():
                endpoint = str(endpoint_id or "").strip()
                if endpoint:
                    error_type = str(error or "provider_error").strip()[:64] or "provider_error"
                    endpoint_status[endpoint] = ("failed", error_type)
        if not observations and not endpoint_status:
            return 0
        with self.database.connect() as connection:
            try:
                connection.execute("SELECT 1 FROM endpoint_usage_snapshots LIMIT 1")
            except Exception:
                return 0
            try:
                connection.execute("SELECT 1 FROM endpoint_usage_snapshot_status LIMIT 1")
                status_table_available = True
            except Exception:
                status_table_available = False
            try:
                valid_endpoints = {
                    str(row["id"])
                    for row in connection.execute("SELECT id FROM vpn_endpoints").fetchall()
                }
            except Exception:
                valid_endpoints = set()
            observations = [
                item for item in observations if item[0] in valid_endpoints
            ]
            endpoint_status = {
                endpoint: value
                for endpoint, value in endpoint_status.items()
                if endpoint in valid_endpoints
            }
            self.database.begin_write(connection)
            for endpoint, external_id, observed_bytes, observed_at, snapshot_source in observations:
                connection.execute(
                    """INSERT INTO endpoint_usage_snapshots
                       (endpoint_id, external_id, protocol, observed_bytes, observed_at, source)
                       VALUES (?, ?, 'outline', ?, ?, ?)
                       ON CONFLICT(endpoint_id, external_id) DO UPDATE SET
                           protocol = excluded.protocol,
                           observed_bytes = excluded.observed_bytes,
                           observed_at = excluded.observed_at,
                           source = excluded.source""",
                    (endpoint, external_id, observed_bytes, observed_at, snapshot_source),
                )
            if status_table_available:
                for endpoint, (status, error_type) in endpoint_status.items():
                    connection.execute(
                        """INSERT INTO endpoint_usage_snapshot_status
                           (endpoint_id, protocol, status, error_type, observed_at, source)
                           VALUES (?, 'outline', ?, ?, ?, ?)
                           ON CONFLICT(endpoint_id, protocol) DO UPDATE SET
                               status = excluded.status,
                               error_type = excluded.error_type,
                               observed_at = excluded.observed_at,
                               source = excluded.source""",
                        (endpoint, status, error_type, timestamp, normalized_source),
                    )
        return len(observations)

    def cached_usage_metrics(self) -> dict[str, Any]:
        """Return the latest maintenance-owned usage snapshot without provider I/O."""
        try:
            with self.database.connect() as connection:
                try:
                    status_rows = connection.execute(
                        """SELECT endpoint_id, protocol, status, error_type, observed_at
                             FROM endpoint_usage_snapshot_status
                            WHERE protocol = 'outline'"""
                    ).fetchall()
                except Exception:
                    status_rows = []
                rows = connection.execute(
                    """SELECT endpoint_id, external_id, observed_bytes, observed_at
                         FROM endpoint_usage_snapshots
                        ORDER BY endpoint_id, external_id"""
                ).fetchall()
        except Exception:
            return {
                "byEndpoint": {},
                "errors": {"snapshot": "unavailable"},
                "source": "maintenance_snapshot",
            }
        by_endpoint: dict[str, dict[str, int]] = {}
        unavailable: dict[str, str] = {}
        latest: str | None = None
        max_age_seconds = max(
            60, int(os.environ.get("AURIX_USAGE_SNAPSHOT_MAX_AGE_SECONDS", "1800"))
        )
        cutoff = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
        for row in status_rows:
            endpoint = str(row["endpoint_id"])
            observed_at = str(row["observed_at"] or "")
            if observed_at and (latest is None or observed_at > latest):
                latest = observed_at
            try:
                observed = datetime.fromisoformat(observed_at).astimezone(UTC)
            except (TypeError, ValueError, OverflowError):
                observed = None
            status = str(row["status"] or "unknown").strip().lower()
            if status != "healthy":
                unavailable[endpoint] = str(row["error_type"] or status or "unavailable")
            elif observed is None or observed < cutoff:
                unavailable[endpoint] = "stale"
        for row in rows:
            endpoint = str(row["endpoint_id"])
            if endpoint in unavailable:
                continue
            by_endpoint.setdefault(endpoint, {})[str(row["external_id"])] = max(
                0, int(row["observed_bytes"] or 0)
            )
            observed_at = str(row["observed_at"] or "")
            if observed_at and (latest is None or observed_at > latest):
                latest = observed_at
        stale = latest is None
        if latest is not None and not status_rows:
            try:
                stale = datetime.fromisoformat(latest).astimezone(UTC) < (
                    datetime.now(UTC) - timedelta(seconds=max_age_seconds)
                )
            except (TypeError, ValueError, OverflowError):
                stale = True
        return {
            "byEndpoint": by_endpoint,
            "errors": {
                **unavailable,
                **({"snapshot": "stale"} if stale and not unavailable else {}),
            },
            "source": "maintenance_snapshot",
            "latest_observed_at": latest,
            "snapshot_max_age_seconds": max_age_seconds,
        }

    def collect_inventory(self) -> dict[str, Any]:
        """Collect access URLs keyed by endpoint, without flattening key IDs."""
        by_endpoint: dict[str, dict[str, str]] = {}
        errors: dict[str, str] = {}
        for endpoint in self.list_endpoints():
            endpoint_id = str(endpoint["id"])
            if str(endpoint.get("state")) == "RETIRED":
                continue
            if not self._has_enabled_outline_profile(endpoint):
                continue
            try:
                keys = self.client(endpoint_id).list_keys().get("accessKeys", [])
                if not isinstance(keys, list):
                    raise ConnectivityError("Outline returned an invalid key inventory")
                by_endpoint[endpoint_id] = {
                    str(item["id"]): str(item.get("accessUrl") or "")
                    .replace("\r", "")
                    .replace("\n", "")
                    .strip()
                    for item in keys
                    if isinstance(item, dict) and item.get("id")
                }
            except Exception as exc:
                errors[endpoint_id] = type(exc).__name__
        return {"byEndpoint": by_endpoint, "errors": errors}

    @staticmethod
    def _table_exists(connection: Any, table: str) -> bool:
        if isinstance(connection, _PostgresConnection):
            row = connection.execute(
                "SELECT to_regclass(?) AS table_name", (f"public.{table}",)
            ).fetchone()
            return bool(row and row["table_name"])
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        return row is not None

    @staticmethod
    def _table_columns(connection: Any, table: str) -> set[str]:
        if isinstance(connection, _PostgresConnection):
            rows = connection.execute(
                """SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = ?""",
                (table,),
            ).fetchall()
            return {str(row["column_name"]) for row in rows}
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"] if "name" in row.keys() else row[1]) for row in rows}

    def _known_protocol_external_ids(
        self, connection: Any
    ) -> dict[tuple[str, str], set[str]]:
        """Return durable provider IDs by endpoint/protocol without exposing them."""
        known: dict[tuple[str, str], set[str]] = {}
        sources = (
            (
                "credential_generations",
                "endpoint_id, protocol, external_id",
                "protocol IS NOT NULL",
            ),
            ("paid_vpn_keys", "endpoint_id, outline_key_id", "1 = 1"),
            ("keys", "endpoint_id, outline_key_id", "1 = 1"),
        )
        for table, columns, predicate in sources:
            if not self._table_exists(connection, table):
                continue
            available = self._table_columns(connection, table)
            if not set(columns.replace(" ", "").split(",")).issubset(available):
                continue
            rows = connection.execute(
                f"SELECT {columns} FROM {table} WHERE {predicate}"
            ).fetchall()
            for row in rows:
                endpoint_id = str(row["endpoint_id"] or "").strip()
                protocol = (
                    str(row["protocol"] or "").strip().lower()
                    if "protocol" in row.keys()
                    else "outline"
                )
                raw_external_id = row["external_id"] if "external_id" in row.keys() else row["outline_key_id"]
                external_id = str(raw_external_id or "").strip()
                if endpoint_id and protocol and external_id:
                    known.setdefault((endpoint_id, protocol), set()).add(external_id)
        return known

    def _known_outline_external_ids(self, connection: Any) -> dict[str, set[str]]:
        """Compatibility view of durable Outline IDs by endpoint."""
        known = self._known_protocol_external_ids(connection)
        return {
            endpoint_id: external_ids
            for (endpoint_id, protocol), external_ids in known.items()
            if protocol == "outline"
        }

    def persist_inventory_snapshot(
        self,
        inventory: dict[str, Any] | None,
        *,
        now: datetime | str | None = None,
        source: str = "maintenance",
    ) -> dict[str, Any]:
        """Persist a secret-safe remote-key inventory for reconciliation.

        Only an encrypted provider ID is stored. Unknown remote keys remain
        classified as ``unmanaged`` for operator review and are never removed.
        A failed endpoint is left untouched so a transient outage cannot turn
        all of its known keys into false absences.
        """
        scoped = inventory.get("byEndpointProtocol") if isinstance(inventory, dict) else None
        if not isinstance(scoped, dict) and isinstance(inventory, dict):
            legacy = inventory.get("byEndpoint")
            if isinstance(legacy, dict):
                scoped = {
                    str(endpoint_id): {"outline": values}
                    for endpoint_id, values in legacy.items()
                }
        errors = inventory.get("errors") if isinstance(inventory, dict) else None
        if not isinstance(scoped, dict):
            return {
                "status": "unavailable",
                "endpoints": 0,
                "remote_keys": 0,
                "managed_present": 0,
                "unmanaged_present": 0,
                "errors": {"inventory": "invalid"},
            }
        timestamp = self._observation_time(now).isoformat()
        source_text = str(source or "maintenance").strip()[:64] or "maintenance"
        normalized_scoped: dict[tuple[str, str], dict[str, Any]] = {}
        result = {
            "status": "healthy",
            "endpoints": 0,
            "remote_keys": 0,
            "managed_present": 0,
            "unmanaged_present": 0,
            "protocols": {},
            "errors": {
                str(endpoint): str(value)[:128]
                for endpoint, value in (errors.items() if isinstance(errors, dict) else ())
            },
        }
        for raw_endpoint, protocol_values in scoped.items():
            endpoint = str(raw_endpoint or "").strip()
            if not endpoint or not isinstance(protocol_values, dict):
                if endpoint:
                    result["errors"][endpoint] = "invalid_payload"
                continue
            for raw_protocol, values in protocol_values.items():
                protocol = str(raw_protocol or "").strip().lower()
                if (
                    not protocol
                    or len(protocol) > 64
                    or any(char.isspace() for char in protocol)
                    or not isinstance(values, dict)
                ):
                    result["errors"][f"{endpoint}:{raw_protocol}"] = "invalid_payload"
                    continue
                normalized_scoped[(endpoint, protocol)] = values
        with self.database.connect() as connection:
            if not self._table_exists(connection, "endpoint_key_inventory"):
                result["status"] = "unavailable"
                result["errors"]["inventory"] = "schema_missing"
                return result
            if "protocol" not in self._table_columns(connection, "endpoint_key_inventory"):
                result["status"] = "unavailable"
                result["errors"]["inventory"] = "protocol_schema_missing"
                return result
            known = self._known_protocol_external_ids(connection)
            existing_rows = connection.execute(
                """SELECT endpoint_id, protocol, external_id_ciphertext, classification
                     FROM endpoint_key_inventory"""
            ).fetchall()
            existing: dict[tuple[str, str, str], Any] = {}
            for row in existing_rows:
                try:
                    external_id = self._decrypt(str(row["external_id_ciphertext"] or ""))
                except ConnectivityError:
                    continue
                existing[
                    (
                        str(row["endpoint_id"]),
                        str(row["protocol"] or "outline").strip().lower(),
                        external_id,
                    )
                ] = row
            self.database.begin_write(connection)
            successful_endpoints: set[str] = set()
            for (endpoint, protocol), values in normalized_scoped.items():
                successful_endpoints.add(endpoint)
                connection.execute(
                    """UPDATE endpoint_key_inventory SET present = 0
                        WHERE endpoint_id = ? AND protocol = ?""",
                    (endpoint, protocol),
                )
                protocol_result = result["protocols"].setdefault(
                    protocol,
                    {"endpoints": 0, "remote_keys": 0, "managed_present": 0, "unmanaged_present": 0},
                )
                protocol_result["endpoints"] += 1
                for raw_external_id in values:
                    external_id = str(raw_external_id or "").strip()
                    if not external_id or len(external_id) > 256:
                        continue
                    classification = (
                        "managed"
                        if external_id in known.get((endpoint, protocol), set())
                        else "unmanaged"
                    )
                    row = existing.get((endpoint, protocol, external_id))
                    if row is not None:
                        ciphertext = str(row["external_id_ciphertext"])
                        connection.execute(
                            """UPDATE endpoint_key_inventory
                                  SET classification = ?, present = 1,
                                      last_seen_at = ?, source = ?
                                WHERE endpoint_id = ? AND protocol = ?
                                  AND external_id_ciphertext = ?""",
                            (classification, timestamp, source_text, endpoint, protocol, ciphertext),
                        )
                    else:
                        ciphertext = self._encrypt(external_id)
                        connection.execute(
                            """INSERT INTO endpoint_key_inventory
                                   (endpoint_id, protocol, external_id_ciphertext, classification,
                                    present, first_seen_at, last_seen_at, source)
                                VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
                            (endpoint, protocol, ciphertext, classification, timestamp, timestamp, source_text),
                        )
                    result["remote_keys"] += 1
                    protocol_result["remote_keys"] += 1
                    if classification == "managed":
                        result["managed_present"] += 1
                        protocol_result["managed_present"] += 1
                    else:
                        result["unmanaged_present"] += 1
                        protocol_result["unmanaged_present"] += 1
            result["endpoints"] = len(successful_endpoints)
            if result["errors"]:
                result["status"] = "degraded"
        return result

    def inventory_reconciliation(
        self, endpoint_id: str | None = None, protocol: str | None = None
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Return redacted present/managed/unmanaged inventory counts."""
        with self.database.connect() as connection:
            if not self._table_exists(connection, "endpoint_key_inventory"):
                empty = {
                    "endpoint_id": str(endpoint_id) if endpoint_id else None,
                    "protocol": str(protocol).lower() if protocol else None,
                    "present_keys": 0,
                    "managed_present": 0,
                    "unmanaged_present": 0,
                    "historical_keys": 0,
                    "latest_observed_at": None,
                    "status": "unavailable",
                }
                return empty if endpoint_id else {"status": "unavailable", "endpoints": []}
            clauses: list[str] = []
            params: list[Any] = []
            if endpoint_id:
                clauses.append("endpoint_id = ?")
                params.append(str(endpoint_id))
            if protocol:
                clauses.append("protocol = ?")
                params.append(str(protocol).strip().lower())
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = connection.execute(
                """SELECT endpoint_id,
                          protocol,
                          SUM(CASE WHEN present = 1 THEN 1 ELSE 0 END) AS present_keys,
                          SUM(CASE WHEN present = 1 AND classification = 'managed' THEN 1 ELSE 0 END) AS managed_present,
                          SUM(CASE WHEN present = 1 AND classification = 'unmanaged' THEN 1 ELSE 0 END) AS unmanaged_present,
                          COUNT(*) AS historical_keys,
                          MAX(last_seen_at) AS latest_observed_at
                     FROM endpoint_key_inventory"""
                + where
                + " GROUP BY endpoint_id, protocol ORDER BY endpoint_id, protocol",
                tuple(params),
            ).fetchall()
        scoped_result = [
            {
                "endpoint_id": str(row["endpoint_id"]),
                "protocol": str(row["protocol"] or "outline"),
                "present_keys": int(row["present_keys"] or 0),
                "managed_present": int(row["managed_present"] or 0),
                "unmanaged_present": int(row["unmanaged_present"] or 0),
                "historical_keys": int(row["historical_keys"] or 0),
                "latest_observed_at": row["latest_observed_at"],
                "status": "healthy",
            }
            for row in rows
        ]
        if protocol:
            return next(
                iter(scoped_result),
                {
                    "endpoint_id": str(endpoint_id) if endpoint_id else None,
                    "protocol": str(protocol).strip().lower(),
                    "present_keys": 0,
                    "managed_present": 0,
                    "unmanaged_present": 0,
                    "historical_keys": 0,
                    "latest_observed_at": None,
                    "status": "unobserved",
                },
            )
        if endpoint_id:
            matching = [item for item in scoped_result if item["endpoint_id"] == str(endpoint_id)]
            aggregate = {
                "endpoint_id": str(endpoint_id),
                "present_keys": sum(item["present_keys"] for item in matching),
                "managed_present": sum(item["managed_present"] for item in matching),
                "unmanaged_present": sum(item["unmanaged_present"] for item in matching),
                "historical_keys": sum(item["historical_keys"] for item in matching),
                "latest_observed_at": max(
                    (item["latest_observed_at"] for item in matching if item["latest_observed_at"]),
                    default=None,
                ),
                "status": "healthy" if matching else "unobserved",
                "protocols": matching,
            }
            return aggregate
        grouped: dict[str, dict[str, Any]] = {}
        for item in scoped_result:
            aggregate = grouped.setdefault(
                item["endpoint_id"],
                {
                    "endpoint_id": item["endpoint_id"],
                    "present_keys": 0,
                    "managed_present": 0,
                    "unmanaged_present": 0,
                    "historical_keys": 0,
                    "latest_observed_at": None,
                    "status": "healthy",
                    "protocols": [],
                },
            )
            for field in ("present_keys", "managed_present", "unmanaged_present", "historical_keys"):
                aggregate[field] += item[field]
            if item["latest_observed_at"] and (
                aggregate["latest_observed_at"] is None
                or item["latest_observed_at"] > aggregate["latest_observed_at"]
            ):
                aggregate["latest_observed_at"] = item["latest_observed_at"]
            aggregate["protocols"].append(item)
        return {"status": "healthy", "endpoints": list(grouped.values())}

    def collect_customer_snapshot(self) -> dict[str, Any]:
        """Fetch usage and key inventory once per endpoint for interactive views."""
        metrics: dict[str, dict[str, int]] = {}
        inventory: dict[str, dict[str, str]] = {}
        errors: dict[str, str] = {}
        for endpoint in self.list_endpoints():
            endpoint_id = str(endpoint["id"])
            if str(endpoint.get("state")) == "RETIRED":
                continue
            if not self._has_enabled_outline_profile(endpoint):
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
                 AND (CAST(? AS TEXT) IS NULL OR e.id = ?)
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
        self._record_audit_event(
            connection,
            actor_type="system",
            actor_id=None,
            action="endpoint_assignment_created",
            target_type="endpoint_assignment",
            target_id=assignment_id,
            metadata={
                "endpoint_id": endpoint_id,
                "entitlement_kind": "free",
                "free_key_id": int(free_key_id),
                "plan_code": plan_code,
                "reason": "free-entitlement",
            },
            created_at=now.astimezone(UTC).isoformat(),
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
            protocol=str(row["protocol"] or "outline").strip().lower(),
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
        normalized_protocol = str(protocol or "outline").strip().lower()
        if (
            not normalized_protocol
            or len(normalized_protocol) > 64
            or any(char.isspace() for char in normalized_protocol)
        ):
            raise ConnectivityError("protocol is invalid")
        timestamp = (now or datetime.now(UTC)).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            if isinstance(connection, _PostgresConnection):
                # Lock the durable entitlement owner before checking for an
                # assignment.  Without this parent-row lock, concurrent
                # retries for the same subscription can both observe no
                # assignment and one loses on the unique subscription index.
                subscription = connection.execute(
                    "SELECT id FROM subscriptions WHERE id = ? FOR UPDATE",
                    (subscription_id,),
                ).fetchone()
                if subscription is None:
                    raise ConnectivityError("subscription does not exist")
            existing = connection.execute(
                "SELECT * FROM endpoint_assignments WHERE subscription_id = ?",
                (subscription_id,),
            ).fetchone()
            if existing is not None:
                existing_protocol = str(existing["protocol"] or "outline").strip().lower()
                if existing_protocol != normalized_protocol:
                    raise ConnectivityError("subscription assignment protocol is immutable")
                return self._assignment(existing)
            endpoint_id = self.select_endpoint_for_plan(
                connection,
                plan_code,
                preferred_endpoint_id=preferred_endpoint_id,
                protocol=normalized_protocol,
            )
            assignment_id = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO endpoint_assignments
                   (id, endpoint_id, subscription_id, free_key_id, plan_code, status,
                    reason, reserved_quota_bytes, assigned_at, protocol)
                   VALUES (?, ?, ?, NULL, ?, 'active', ?, ?, ?, ?)""",
                (
                    assignment_id,
                    endpoint_id,
                    subscription_id,
                    plan_code,
                    reason[:128],
                    quota_bytes,
                    timestamp,
                    normalized_protocol,
                ),
            )
            self._record_audit_event(
                connection,
                actor_type="system",
                actor_id=None,
                action="endpoint_assignment_created",
                target_type="endpoint_assignment",
                target_id=assignment_id,
                metadata={
                    "endpoint_id": endpoint_id,
                    "entitlement_kind": "paid",
                    "plan_code": str(plan_code),
                    "preferred_endpoint_id": preferred_endpoint_id,
                    "protocol": normalized_protocol,
                    "reason": str(reason or "deterministic-allocation")[:128],
                },
                created_at=timestamp,
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
            allowed_target_states = {"ACTIVE", "DRAINING"} if rollback else {"ACTIVE"}
            if target is None or str(target["state"]).upper() not in allowed_target_states:
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
            assignment_protocol = str(assignment["protocol"] or "outline").strip().lower()
            generation_protocols = {
                str(row["protocol"] or "outline").strip().lower()
                for row in source_protocols
            }
            if generation_protocols and generation_protocols != {assignment_protocol}:
                raise ConnectivityError("assignment and generation protocols do not match")
            protocols = {assignment_protocol}
            protocols.update(generation_protocols)
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
            self._record_audit_event(
                connection,
                actor_type="system",
                actor_id=None,
                action="endpoint_assignment_transferred",
                target_type="endpoint_assignment",
                target_id=str(assignment["id"]),
                metadata={
                    "entitlement_key": entitlement_key,
                    "source_endpoint_id": source_endpoint_id,
                    "target_endpoint_id": target_id,
                    "reason": str(reason or "operator-drain")[:128],
                },
                created_at=timestamp,
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
        failure_threshold = self._health_threshold(
            "AURIX_ENDPOINT_DEGRADE_FAILURES", 2
        )
        recovery_threshold = self._health_threshold(
            "AURIX_ENDPOINT_RECOVER_SUCCESSES", 2
        )
        recovery_cooldown_seconds = self._health_recovery_cooldown(
            "AURIX_ENDPOINT_RECOVERY_COOLDOWN_SECONDS", 60
        )
        timestamp = (now or datetime.now(UTC)).isoformat()
        snapshot_id = uuid.uuid4().hex
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            lock = " FOR UPDATE" if isinstance(connection, _PostgresConnection) else ""
            endpoint = connection.execute(
                "SELECT state, health_state_changed_at FROM vpn_endpoints WHERE id = ?" + lock,
                (endpoint_id,),
            ).fetchone()
            if endpoint is None:
                raise ConnectivityError("VPN endpoint does not exist")
            current_state = str(endpoint["state"] or "ACTIVE").upper()
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
            history = connection.execute(
                """SELECT healthy, observed_at FROM endpoint_capacity_snapshots
                    WHERE endpoint_id = ?
                    ORDER BY observed_at DESC, id DESC
                    LIMIT ?""",
                (endpoint_id, max(failure_threshold, recovery_threshold)),
            ).fetchall()
            latest_health = bool(history[0]["healthy"]) if history else bool(healthy)
            health_streak = 0
            for row in history:
                if bool(row["healthy"]) != latest_health:
                    break
                health_streak += 1
            next_state = current_state
            try:
                state_changed_at = self._observation_time(endpoint["health_state_changed_at"])
            except (TypeError, ValueError, OverflowError):
                state_changed_at = self._observation_time(timestamp)
            recovery_cooldown_ready = True
            if current_state == "DEGRADED" and latest_health and health_streak >= recovery_threshold:
                recovery_cooldown_ready = self._observation_time(timestamp) >= (
                    state_changed_at + timedelta(seconds=recovery_cooldown_seconds)
                )
            if current_state not in {"DRAINING", "RETIRED"}:
                if (
                    not latest_health
                    and health_streak >= failure_threshold
                    and current_state == "ACTIVE"
                ):
                    next_state = "DEGRADED"
                elif (
                    latest_health
                    and health_streak >= recovery_threshold
                    and recovery_cooldown_ready
                    and current_state == "DEGRADED"
                ):
                    next_state = "ACTIVE"
            if next_state != current_state:
                state_changed_at = self._observation_time(timestamp)
            connection.execute(
                """UPDATE vpn_endpoints SET
                     last_healthy_at = CASE WHEN ? THEN ? ELSE last_healthy_at END,
                     state = ?, health_state_changed_at = ?
                   WHERE id = ?""",
                (healthy, timestamp, next_state, state_changed_at.isoformat(), endpoint_id),
            )
            transitioned = next_state != current_state
            if transitioned:
                self._record_audit_event(
                    connection,
                    actor_type="system",
                    actor_id=None,
                    action="endpoint_health_state_changed",
                    target_type="vpn_endpoint",
                    target_id=str(endpoint_id),
                    metadata={
                        "previous_state": current_state,
                        "next_state": next_state,
                        "healthy": latest_health,
                        "health_streak": health_streak,
                        "failure_threshold": failure_threshold,
                        "recovery_threshold": recovery_threshold,
                        "recovery_cooldown_seconds": recovery_cooldown_seconds,
                        "recovery_cooldown_ready": recovery_cooldown_ready,
                    },
                    created_at=timestamp,
                )
        return {
            "id": snapshot_id,
            "endpoint_id": endpoint_id,
            "observed_at": timestamp,
            "healthy": healthy,
            "state": next_state,
            "health_streak": health_streak,
            "recovery_cooldown_seconds": recovery_cooldown_seconds,
            "recovery_cooldown_ready": recovery_cooldown_ready,
            "transitioned": transitioned,
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
            raise AmbiguousProviderOperation("DigitalOcean request outcome is uncertain") from exc
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

    def list_firewalls_by_tag(self, tag: str) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(tag, safe="")
        result = self._request("GET", f"/firewalls?tag_name={encoded}&per_page=200")
        firewalls = result.get("firewalls") if isinstance(result, dict) else None
        if not isinstance(firewalls, list):
            raise ConnectivityError("DigitalOcean response lacks firewalls")
        return [item for item in firewalls if isinstance(item, dict)]

    def create_firewall(self, specification: dict[str, Any]) -> dict[str, Any]:
        result = self._request("POST", "/firewalls", specification)
        firewall = result.get("firewall") if isinstance(result, dict) else None
        if not isinstance(firewall, dict) or not firewall.get("id"):
            raise ConnectivityError("DigitalOcean create response lacks a firewall ID")
        return firewall

    def update_firewall(
        self, firewall_id: str, specification: dict[str, Any]
    ) -> dict[str, Any]:
        result = self._request(
            "PUT",
            f"/firewalls/{urllib.parse.quote(str(firewall_id), safe='')}",
            specification,
        )
        firewall = result.get("firewall") if isinstance(result, dict) else None
        if not isinstance(firewall, dict) or not firewall.get("id"):
            raise ConnectivityError("DigitalOcean update response lacks a firewall ID")
        return firewall

    def firewall(self, firewall_id: str) -> dict[str, Any]:
        result = self._request("GET", f"/firewalls/{urllib.parse.quote(str(firewall_id), safe='')}")
        firewall = result.get("firewall") if isinstance(result, dict) else None
        if not isinstance(firewall, dict):
            raise ConnectivityError("DigitalOcean response lacks a firewall")
        return firewall

    def validate_droplet_specification(self, specification: dict[str, Any]) -> None:
        """Validate a placement against the provider's current catalog."""
        region = str(specification.get("region") or "").strip()
        size = str(specification.get("size") or "").strip()
        image = str(specification.get("image") or "").strip()
        if not region or not size or not image:
            raise ConnectivityError("DigitalOcean placement specification is incomplete")
        regions = self._request("GET", "/regions?per_page=200")
        sizes = self._request("GET", "/sizes?per_page=200")
        images = self._request("GET", "/images?type=distribution&per_page=200")
        region_rows = regions.get("regions") if isinstance(regions, dict) else None
        size_rows = sizes.get("sizes") if isinstance(sizes, dict) else None
        image_rows = images.get("images") if isinstance(images, dict) else None
        if not isinstance(region_rows, list) or not isinstance(size_rows, list) or not isinstance(image_rows, list):
            raise ConnectivityError("DigitalOcean placement catalog is invalid")
        region_row = next(
            (item for item in region_rows if isinstance(item, dict) and item.get("slug") == region),
            None,
        )
        if region_row is None or region_row.get("available") is False:
            raise ConnectivityError("DigitalOcean region is unavailable")
        size_row = next(
            (item for item in size_rows if isinstance(item, dict) and item.get("slug") == size),
            None,
        )
        if size_row is None or size_row.get("available") is False:
            raise ConnectivityError("DigitalOcean size is unavailable")
        size_regions = size_row.get("regions")
        if isinstance(size_regions, list) and region not in {str(item) for item in size_regions}:
            raise ConnectivityError("DigitalOcean size is unavailable in the requested region")
        image_row = next(
            (
                item
                for item in image_rows
                if isinstance(item, dict)
                and (item.get("slug") == image or str(item.get("id")) == image)
            ),
            None,
        )
        if image_row is None or image_row.get("deprecated") is True:
            raise ConnectivityError("DigitalOcean image is unavailable")

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

    @staticmethod
    def _setting_int(name: str, default: int, *, minimum: int = 0) -> int:
        try:
            value = int(os.environ.get(name, str(default)))
        except (TypeError, ValueError) as exc:
            raise ConnectivityError(f"{name} is invalid") from exc
        if value < minimum:
            raise ConnectivityError(f"{name} is invalid")
        return value

    @staticmethod
    def _setting_bool(name: str, default: bool = False) -> bool:
        value = os.environ.get(name, "1" if default else "0").strip().lower()
        return value in {"1", "true", "yes", "on"}

    @staticmethod
    def _fresh_endpoint_health(value: Any, now: datetime) -> bool:
        try:
            observed_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=UTC)
            max_age = max(
                30,
                FleetController._setting_int(
                    "AURIX_ENDPOINT_HEALTH_MAX_AGE_SECONDS", 900, minimum=0
                ),
            )
            return observed_at.astimezone(UTC) >= now - timedelta(seconds=max_age)
        except (TypeError, ValueError, OverflowError):
            return False

    @staticmethod
    def _active_provision_intents(connection: Any) -> list[tuple[str, str | None]]:
        """Return active provision intents and their durable requested regions."""
        rows = connection.execute(
            """SELECT id, status FROM infrastructure_jobs
                WHERE operation = 'provision'
                  AND status IN ('pending', 'running', 'awaiting_verification')"""
        ).fetchall()
        intents: list[tuple[str, str | None]] = []
        for row in rows:
            event = connection.execute(
                """SELECT metadata_json FROM infrastructure_events
                    WHERE infrastructure_job_id = ? AND event_type = 'provision_requested'
                    ORDER BY created_at ASC LIMIT 1""",
                (str(row["id"]),),
            ).fetchone()
            region: str | None = None
            if event is not None:
                try:
                    metadata = json.loads(str(event["metadata_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                if isinstance(metadata, dict) and str(metadata.get("region") or "").strip():
                    region = str(metadata["region"]).strip()
            intents.append((str(row["id"]), region))
        return intents

    @staticmethod
    def _validate_provision_specification(specification: dict[str, Any]) -> dict[str, str]:
        region = str(specification.get("region") or "").strip()
        size = str(specification.get("size") or "").strip()
        image = str(specification.get("image") or "").strip()
        allowed_regions = {
            item.strip()
            for item in os.environ.get("AURIX_ALLOWED_REGIONS", "sgp1").split(",")
            if item.strip()
        }
        allowed_sizes = {
            item.strip()
            for item in os.environ.get("AURIX_ALLOWED_DROPLET_SIZES", "s-1vcpu-1gb").split(",")
            if item.strip()
        }
        allowed_images = {
            item.strip()
            for item in os.environ.get("AURIX_ALLOWED_DROPLET_IMAGES", "ubuntu-24-04-x64").split(",")
            if item.strip()
        }
        if (
            not region
            or not size
            or not image
            or region not in allowed_regions
            or size not in allowed_sizes
            or image not in allowed_images
        ):
            raise ProvisionValidationError("Droplet specification is outside the configured allowlist")
        return {"region": region, "size": size, "image": image}

    @staticmethod
    def _firewall_policy_from_environment() -> dict[str, Any] | None:
        encoded = os.environ.get("AURIX_DIGITALOCEAN_FIREWALL_POLICY_JSON", "").strip()
        if not encoded:
            return None
        try:
            value = json.loads(encoded)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProvisionValidationError("DigitalOcean firewall policy is invalid JSON") from exc
        if not isinstance(value, dict):
            raise ProvisionValidationError("DigitalOcean firewall policy must be an object")
        return value

    @staticmethod
    def _firewall_ports(value: Any, *, protocol: str) -> str:
        ports = str(value or "").strip().lower()
        if protocol == "icmp":
            if ports not in {"", "all"}:
                raise ProvisionValidationError("ICMP firewall ports are invalid")
            return "all"
        if not ports or len(ports) > 128:
            raise ProvisionValidationError("firewall ports are invalid")
        if ports == "all":
            return ports
        for item in ports.split(","):
            bounds = item.strip().split("-")
            if len(bounds) not in {1, 2}:
                raise ProvisionValidationError("firewall ports are invalid")
            try:
                start = int(bounds[0])
                end = int(bounds[-1])
            except (TypeError, ValueError) as exc:
                raise ProvisionValidationError("firewall ports are invalid") from exc
            if not 1 <= start <= end <= 65_535:
                raise ProvisionValidationError("firewall ports are invalid")
        return ports

    @staticmethod
    def _firewall_rule(value: Any, *, direction: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ProvisionValidationError("firewall rule is invalid")
        protocol = str(value.get("protocol") or "").strip().lower()
        if protocol not in {"tcp", "udp", "icmp"}:
            raise ProvisionValidationError("firewall rule protocol is invalid")
        address_key = "sources_addresses" if direction == "inbound" else "destinations_addresses"
        raw_addresses = value.get(address_key)
        if not isinstance(raw_addresses, list) or not 1 <= len(raw_addresses) <= 64:
            raise ProvisionValidationError("firewall rule addresses are invalid")
        addresses: list[str] = []
        for raw_address in raw_addresses:
            address = str(raw_address or "").strip()
            try:
                network = ipaddress.ip_network(address, strict=False)
            except ValueError as exc:
                raise ProvisionValidationError("firewall rule address is invalid") from exc
            addresses.append(str(network))
        ports = FleetController._firewall_ports(value.get("ports"), protocol=protocol)
        if direction == "inbound" and protocol == "tcp" and FleetController._ports_include(
            ports, 22
        ) and any(address in {"0.0.0.0/0", "::/0"} for address in addresses):
            raise ProvisionValidationError("firewall SSH access must not be public")
        return {
            "protocol": protocol,
            "ports": ports,
            address_key: addresses,
        }

    @staticmethod
    def _ports_include(ports: str, target: int) -> bool:
        if ports == "all":
            return True
        for item in ports.split(","):
            bounds = item.strip().split("-")
            try:
                start = int(bounds[0])
                end = int(bounds[-1])
            except (TypeError, ValueError):
                return False
            if start <= target <= end:
                return True
        return False

    @classmethod
    def _validate_firewall_policy(
        cls, specification: dict[str, Any], job_id: str
    ) -> dict[str, Any]:
        name = str(specification.get("name") or f"aurix-vpn-{str(job_id)[:12]}-firewall").strip()
        if not name or len(name) > 128:
            raise ProvisionValidationError("firewall name is invalid")
        inbound = specification.get("inbound_rules")
        outbound = specification.get("outbound_rules")
        if not isinstance(inbound, list) or not 1 <= len(inbound) <= 32:
            raise ProvisionValidationError("inbound firewall rules are invalid")
        if not isinstance(outbound, list) or not 1 <= len(outbound) <= 32:
            raise ProvisionValidationError("outbound firewall rules are invalid")
        if "droplet_ids" in specification:
            raise ProvisionValidationError("firewall policy must target stable tags")
        tags = specification.get("tags") or []
        if not isinstance(tags, list) or len(tags) > 32:
            raise ProvisionValidationError("firewall tags are invalid")
        normalized_tags: list[str] = []
        for raw_tag in tags + [
            "aurix-vpn-node",
            "aurix-env-production",
            f"aurix-provision-job-{job_id}",
        ]:
            tag = str(raw_tag or "").strip()
            if not tag or len(tag) > 128:
                raise ProvisionValidationError("firewall tag is invalid")
            if tag not in normalized_tags:
                normalized_tags.append(tag)
        return {
            "name": name,
            "inbound_rules": [cls._firewall_rule(item, direction="inbound") for item in inbound],
            "outbound_rules": [cls._firewall_rule(item, direction="outbound") for item in outbound],
            "tags": normalized_tags,
        }

    def _durable_firewall_policy(self, job_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT metadata_json FROM infrastructure_events
                     WHERE infrastructure_job_id = ? AND event_type = 'firewall_policy_requested'
                     ORDER BY created_at ASC LIMIT 1""",
                (str(job_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(str(row["metadata_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProvisionValidationError("durable firewall policy is invalid") from exc
        if not isinstance(value, dict):
            raise ProvisionValidationError("durable firewall policy is invalid")
        return self._validate_firewall_policy(value, job_id)

    @staticmethod
    def _canonical_firewall_value(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): FleetController._canonical_firewall_value(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, list):
            normalized = [FleetController._canonical_firewall_value(item) for item in value]
            return sorted(
                normalized,
                key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
            )
        return value

    @classmethod
    def _firewall_matches_policy(
        cls, policy: dict[str, Any], observed: dict[str, Any]
    ) -> bool:
        if observed.get("droplet_ids") not in (None, []):
            return False
        fields = ("name", "inbound_rules", "outbound_rules", "tags")
        return all(
            cls._canonical_firewall_value(observed.get(field))
            == cls._canonical_firewall_value(policy[field])
            for field in fields
        )

    def apply_firewall(self, job_id: str, specification: dict[str, Any]) -> dict[str, Any]:
        """Apply one stable-tag firewall and reconcile ambiguous creation safely."""
        if not self._mutations_enabled():
            raise ConnectivityError("Infrastructure mutations are disabled")
        if self.provider is None:
            raise ConnectivityError("DigitalOcean provider is not configured")
        policy = self._validate_firewall_policy(specification, job_id)
        list_firewalls = getattr(self.provider, "list_firewalls_by_tag", None)
        create_firewall = getattr(self.provider, "create_firewall", None)
        update_firewall = getattr(self.provider, "update_firewall", None)
        read_firewall = getattr(self.provider, "firewall", None)
        if (
            not callable(list_firewalls)
            or not callable(create_firewall)
            or not callable(update_firewall)
            or not callable(read_firewall)
        ):
            raise ConnectivityError("provider firewall controls are not configured")
        tag = f"aurix-provision-job-{job_id}"
        candidates = list_firewalls(tag)
        if not isinstance(candidates, list):
            raise ConnectivityError("provider firewall reconciliation returned an invalid response")
        if len(candidates) > 1:
            raise ConnectivityError("firewall tag matches multiple provider resources")
        reconciled = bool(candidates)
        if candidates:
            firewall = candidates[0]
        else:
            try:
                firewall = create_firewall(policy)
            except AmbiguousProviderOperation:
                candidates = list_firewalls(tag)
                if not isinstance(candidates, list) or len(candidates) != 1:
                    raise
                firewall = candidates[0]
                reconciled = True
        if not isinstance(firewall, dict) or not firewall.get("id"):
            raise ConnectivityError("provider firewall response lacks a resource ID")
        firewall_id = str(firewall["id"])
        observed = read_firewall(firewall_id)
        if not isinstance(observed, dict):
            raise ConnectivityError("provider firewall read-back is invalid")
        observed_tags = {str(item) for item in observed.get("tags") or []}
        if tag not in observed_tags:
            raise ConnectivityError("provider firewall read-back lacks the stable job tag")
        if not self._firewall_matches_policy(policy, observed):
            try:
                updated = update_firewall(firewall_id, policy)
            except AmbiguousProviderOperation:
                observed = read_firewall(firewall_id)
                if not isinstance(observed, dict) or not self._firewall_matches_policy(
                    policy, observed
                ):
                    raise
            else:
                if not isinstance(updated, dict) or str(updated.get("id")) != firewall_id:
                    raise ConnectivityError("provider firewall update returned the wrong resource")
                observed = read_firewall(firewall_id)
                if not isinstance(observed, dict) or not self._firewall_matches_policy(
                    policy, observed
                ):
                    raise ConnectivityError("provider firewall read-back does not match policy")
        timestamp = datetime.now(UTC).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            existing = connection.execute(
                """SELECT 1 FROM infrastructure_events
                     WHERE infrastructure_job_id = ? AND event_type = 'firewall_applied'
                     LIMIT 1""",
                (str(job_id),),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO infrastructure_events
                       (id, infrastructure_job_id, event_type, metadata_json, created_at)
                       VALUES (?, ?, 'firewall_applied', ?, ?)""",
                    (
                        uuid.uuid4().hex,
                        str(job_id),
                        json.dumps(
                            {"firewall_id": firewall_id, "reconciled": reconciled},
                            sort_keys=True,
                        ),
                        timestamp,
                    ),
                )
        return {"job_id": str(job_id), "firewall_id": firewall_id, "status": "applied"}

    def scale_out_recommendation(
        self,
        *,
        plan_code: str | None = None,
        protocol: str = "outline",
        region: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return a read-only, recommendation-only scale-out decision.

        This method never queues work and never contacts DigitalOcean. It
        combines fresh endpoint readiness with durable node/job guard state so
        an operator can review a recommendation without treating it as an
        authorization to create infrastructure.
        """
        if self.registry is None:
            return {"status": "unavailable", "reason": "endpoint registry is not configured"}
        current = (now or datetime.now(UTC)).astimezone(UTC)
        selected_protocol = str(protocol or "").strip().lower()
        if not selected_protocol or any(char.isspace() for char in selected_protocol):
            raise ConnectivityError("protocol is invalid")
        selected_plan = str(plan_code or "").strip() or None
        allowed_regions = {
            item.strip()
            for item in os.environ.get("AURIX_ALLOWED_REGIONS", "sgp1").split(",")
            if item.strip()
        }
        selected_region = str(region or os.environ.get("AURIX_SCALE_REGION", "")).strip()
        if not selected_region:
            selected_region = sorted(allowed_regions)[0] if allowed_regions else ""
        warm_buffer = self._setting_int(
            "AURIX_SCALE_WARM_BUFFER_ASSIGNMENTS", 0, minimum=0
        )
        max_total = self._setting_int("AURIX_MAX_VPN_NODES", 3, minimum=1)
        max_region = self._setting_int(
            "AURIX_MAX_VPN_NODES_PER_REGION", max_total, minimum=1
        )
        max_daily = self._setting_int(
            "AURIX_MAX_NODE_CREATIONS_PER_DAY", 2, minimum=1
        )
        cooldown_seconds = self._setting_int(
            "AURIX_NODE_CREATION_COOLDOWN_SECONDS", 1800, minimum=0
        )

        endpoints = self.registry.list_endpoints()
        plan_directory: dict[str, dict[str, Any]] = {}
        if selected_plan:
            plan_directory = {
                str(item["id"]): item
                for item in self.registry.list_customer_endpoints(
                    selected_plan, protocol=selected_protocol
                )
            }
        ready_endpoints = 0
        eligible_endpoints = 0
        finite_headroom = 0
        unknown_capacity = 0
        for endpoint in endpoints:
            if selected_region and str(endpoint.get("region") or "") != selected_region:
                continue
            endpoint_id = str(endpoint.get("id") or "")
            profiles = endpoint.get("protocols") or []
            profile_enabled = any(
                isinstance(profile, dict)
                and str(profile.get("protocol") or "").lower() == selected_protocol
                and str(profile.get("status") or "").lower() == "enabled"
                for profile in profiles
            )
            if (
                str(endpoint.get("state") or "").upper() != "ACTIVE"
                or endpoint.get("accepts_new_assignments") in (False, 0)
                or not self._fresh_endpoint_health(endpoint.get("last_healthy_at"), current)
                or not profile_enabled
            ):
                continue
            ready_endpoints += 1
            plan_item = plan_directory.get(endpoint_id)
            plan_eligible = True
            if selected_plan:
                plan_eligible = bool(plan_item)
                if plan_item:
                    plan_eligible = plan_item.get("plan_enabled") not in (False, 0)
                    plan_max = plan_item.get("plan_max")
                    if plan_max is not None:
                        plan_eligible = plan_eligible and int(
                            plan_item.get("plan_active") or 0
                        ) < int(plan_max)
            if plan_eligible:
                eligible_endpoints += 1
            capacity = endpoint.get("max_active_keys")
            if capacity is None:
                unknown_capacity += 1
            else:
                finite_headroom += max(
                    0,
                    int(capacity) - int(endpoint.get("active_assignments") or 0),
                )

        triggers: list[str] = []
        if selected_plan and eligible_endpoints == 0:
            triggers.append("no_eligible_endpoint_for_plan")
        if warm_buffer > 0 and unknown_capacity == 0 and finite_headroom < warm_buffer:
            triggers.append("healthy_ready_capacity_below_warm_buffer")
        recommended = bool(triggers)

        with self.database.connect() as connection:
            endpoint_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM vpn_endpoints WHERE state != 'RETIRED'"
                ).fetchone()["n"]
                or 0
            )
            regional_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM vpn_endpoints WHERE state != 'RETIRED' AND region = ?",
                    (selected_region,),
                ).fetchone()["n"]
                or 0
            )
            active_intents = self._active_provision_intents(connection)
            active_total = len(active_intents)
            active_region = sum(1 for _job, intent_region in active_intents if intent_region == selected_region)
            unknown_region_intent = any(intent_region is None for _job, intent_region in active_intents)
            day_start = current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            created_today = int(
                connection.execute(
                    """SELECT COUNT(*) AS n FROM infrastructure_jobs
                        WHERE operation = 'provision' AND created_at >= ?""",
                    (day_start,),
                ).fetchone()["n"]
                or 0
            )
            latest = connection.execute(
                """SELECT created_at FROM infrastructure_jobs
                    WHERE operation = 'provision' ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
        cooldown_clear = True
        if latest is not None:
            try:
                latest_at = datetime.fromisoformat(str(latest["created_at"])).astimezone(UTC)
                cooldown_clear = current >= latest_at + timedelta(seconds=cooldown_seconds)
            except (TypeError, ValueError, OverflowError):
                cooldown_clear = False
        guards = {
            "region_allowlist": bool(selected_region and selected_region in allowed_regions),
            "node_cap": endpoint_count + active_total < max_total,
            "region_node_cap": regional_count + active_region < max_region,
            "daily_creation_cap": created_today < max_daily,
            "cooldown": cooldown_clear,
            "active_scale_intent": not unknown_region_intent and active_region == 0,
        }
        blocked_by = [name for name, passed in guards.items() if not passed]
        return {
            "status": "recommendation" if recommended else "steady",
            "recommended": recommended,
            "triggers": triggers,
            "trigger": triggers[0] if triggers else None,
            "plan_code": selected_plan,
            "protocol": selected_protocol,
            "region": selected_region,
            "mode": "recommendation-only",
            "capacity": {
                "ready_endpoints": ready_endpoints,
                "eligible_endpoints": eligible_endpoints,
                "finite_headroom_assignments": finite_headroom,
                "unknown_capacity_endpoints": unknown_capacity,
                "warm_buffer_assignments": warm_buffer,
            },
            "guards": guards,
            "blocked_by": blocked_by,
            "ready_to_queue": recommended and not blocked_by,
            "automatic_scale_enabled": self._setting_bool(
                "AURIX_AUTOMATIC_SCALE_ENABLED", False
            ),
            "owner_confirmation_required": True,
            "provider_mutations_enabled": self._mutations_enabled(),
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
        region = str(region or "").strip()
        size = str(size or "").strip()
        image = str(image or "").strip()
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
        current = (now or datetime.now(UTC)).astimezone(UTC)
        timestamp = current.isoformat()
        fingerprint = hashlib.sha256(f"provision:{region}:{size}:{image}:{timestamp[:13]}".encode()).hexdigest()
        job_id = uuid.uuid4().hex
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            if isinstance(connection, _PostgresConnection):
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('aurix:infrastructure:provision'))"
                ).fetchone()
            existing_fingerprint = connection.execute(
                "SELECT id FROM infrastructure_jobs WHERE request_fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if existing_fingerprint is not None:
                return str(existing_fingerprint["id"])
            max_total = self._setting_int("AURIX_MAX_VPN_NODES", 3, minimum=1)
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM vpn_endpoints WHERE state != 'RETIRED'"
            ).fetchone()["n"]
            active_intents = self._active_provision_intents(connection)
            if int(count) + len(active_intents) >= max_total:
                raise ConnectivityError("Configured VPN node limit has been reached")
            max_region = self._setting_int("AURIX_MAX_VPN_NODES_PER_REGION", max_total, minimum=1)
            region_count = connection.execute(
                "SELECT COUNT(*) AS n FROM vpn_endpoints WHERE state != 'RETIRED' AND region = ?",
                (region,),
            ).fetchone()["n"]
            active_region = sum(1 for _job, intent_region in active_intents if intent_region == region)
            if int(region_count) + active_region >= max_region:
                raise ConnectivityError("Configured VPN node limit for this region has been reached")
            if any(intent_region is None for _job, intent_region in active_intents):
                raise ConnectivityError("An infrastructure intent has no durable region")
            if active_region:
                raise ConnectivityError("Another server provisioning job is already active in this region")
            day_start = current.replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()
            created_today = connection.execute(
                """SELECT COUNT(*) AS n FROM infrastructure_jobs
                   WHERE operation = 'provision' AND created_at >= ?""",
                (day_start,),
            ).fetchone()["n"]
            max_daily = self._setting_int("AURIX_MAX_NODE_CREATIONS_PER_DAY", 2, minimum=1)
            if int(created_today) >= max_daily:
                raise ConnectivityError("Daily VPN node creation limit has been reached")
            latest = connection.execute(
                """SELECT created_at FROM infrastructure_jobs
                   WHERE operation = 'provision' ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
            cooldown_seconds = self._setting_int(
                "AURIX_NODE_CREATION_COOLDOWN_SECONDS", 1800, minimum=0
            )
            if latest is not None:
                latest_at = datetime.fromisoformat(str(latest["created_at"])).astimezone(UTC)
                if current < latest_at + timedelta(
                    seconds=cooldown_seconds
                ):
                    raise ConnectivityError("VPN node creation cooldown is still active")
            inserted = connection.execute(
                """INSERT INTO infrastructure_jobs
                   (id, operation, status, attempts, next_attempt_at,
                    request_fingerprint, created_at)
                   VALUES (?, 'provision', 'pending', 0, ?, ?, ?)
                   ON CONFLICT(request_fingerprint) DO NOTHING""",
                (job_id, timestamp, fingerprint, timestamp),
            )
            if int(getattr(inserted, "rowcount", 0) or 0) != 1:
                existing = connection.execute(
                    "SELECT id FROM infrastructure_jobs WHERE request_fingerprint = ?",
                    (fingerprint,),
                ).fetchone()
                if existing is None:
                    raise ConnectivityError("Provisioning intent could not be made idempotent")
                return str(existing["id"])
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
            EndpointRegistry._record_audit_event(
                connection,
                actor_type="admin",
                actor_id=requested_by,
                action="infrastructure_provision_requested",
                target_type="infrastructure_job",
                target_id=job_id,
                metadata={"region": region, "size": size, "image": image},
                created_at=timestamp,
            )
        return job_id

    def execute_provision(self, job_id: str, specification: dict[str, Any]) -> dict[str, Any]:
        if not self._mutations_enabled():
            raise ConnectivityError("Infrastructure mutations are disabled")
        if self.provider is None:
            raise ConnectivityError("DigitalOcean provider is not configured")
        try:
            durable_specification = self._provision_specification(job_id)
        except ProvisionValidationError as exc:
            self._record_provision_validation_failure(job_id, exc)
            raise
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
        tags.update(
            {
                "aurix-vpn-node",
                "aurix-env-production",
                f"aurix-provision-job-{job_id}",
            }
        )
        specification["tags"] = sorted(tags)
        for field in ("region", "size", "image"):
            supplied = str(specification.get(field) or "").strip()
            durable = durable_specification[field]
            if supplied and supplied != durable:
                error = ProvisionValidationError(
                    "Provisioning specification does not match durable intent"
                )
                self._record_provision_validation_failure(job_id, error)
                raise error
            specification[field] = durable
        try:
            placement = self._validate_provision_specification(specification)
        except ProvisionValidationError as exc:
            self._record_provision_validation_failure(job_id, exc)
            raise
        validator = getattr(self.provider, "validate_droplet_specification", None)
        if callable(validator):
            validator(placement)
        try:
            firewall_policy = self._durable_firewall_policy(job_id)
        except ProvisionValidationError as exc:
            self._record_provision_validation_failure(job_id, exc)
            raise
        retry_requested = False
        with self.database.connect() as connection:
            pending = connection.execute(
                """SELECT id FROM infrastructure_jobs
                     WHERE id = ? AND operation = 'provision' AND status = 'pending'""",
                (job_id,),
            ).fetchone()
            if pending is None:
                raise ConnectivityError("Provisioning job is not pending")
            retry_requested = connection.execute(
                """SELECT 1 FROM infrastructure_events
                     WHERE infrastructure_job_id = ?
                       AND event_type = 'provision_retry_requested'
                     LIMIT 1""",
                (job_id,),
            ).fetchone() is not None
        if firewall_policy is None and not retry_requested:
            try:
                configured_policy = self._firewall_policy_from_environment()
                if configured_policy is not None:
                    firewall_policy = self._validate_firewall_policy(configured_policy, job_id)
            except ProvisionValidationError as exc:
                self._record_provision_validation_failure(job_id, exc)
                raise
        if retry_requested:
            list_by_tag = getattr(self.provider, "list_by_tag", None)
            if not callable(list_by_tag):
                raise ConnectivityError("provider cannot reconcile a retried infrastructure intent")
            candidates = list_by_tag(f"aurix-provision-job-{job_id}")
            if not isinstance(candidates, list):
                raise ConnectivityError("provider tag reconciliation returned an invalid response")
            if len(candidates) > 1:
                raise ConnectivityError("retried infrastructure intent matches multiple provider resources")
            if len(candidates) == 1:
                candidate = candidates[0]
                if not isinstance(candidate, dict) or not candidate.get("id"):
                    raise ConnectivityError("provider recovery candidate has no resource ID")
                action_ids = candidate.get("action_ids") or []
                with self.database.connect() as connection:
                    self.database.begin_write(connection)
                    updated = connection.execute(
                        """UPDATE infrastructure_jobs
                              SET status = 'running', attempts = attempts + 1,
                                  provider_resource_id = ?, provider_action_id = ?, locked_at = NULL
                            WHERE id = ? AND status = 'pending'""",
                        (
                            str(candidate["id"]),
                            str(action_ids[0]) if action_ids else None,
                            job_id,
                        ),
                    )
                    if getattr(updated, "rowcount", 1) != 1:
                        raise ConnectivityError("Provisioning job is no longer pending")
                    connection.execute(
                        """INSERT INTO infrastructure_events
                           (id, infrastructure_job_id, event_type, metadata_json, created_at)
                           VALUES (?, ?, 'provider_retry_recovered', '{}', ?)""",
                        (uuid.uuid4().hex, job_id, datetime.now(UTC).isoformat()),
                    )
                return self.reconcile_provision(job_id)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                "SELECT * FROM infrastructure_jobs WHERE id = ? AND status = 'pending'",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ConnectivityError("Provisioning job is not pending")
            updated = connection.execute(
                """UPDATE infrastructure_jobs
                      SET status = 'running', attempts = attempts + 1, locked_at = ?
                    WHERE id = ? AND status = 'pending'""",
                (datetime.now(UTC).isoformat(), job_id),
            )
            if getattr(updated, "rowcount", 1) != 1:
                raise ConnectivityError("Provisioning job is no longer pending")
            if firewall_policy is not None:
                existing_policy = connection.execute(
                    """SELECT 1 FROM infrastructure_events
                         WHERE infrastructure_job_id = ? AND event_type = 'firewall_policy_requested'
                         LIMIT 1""",
                    (job_id,),
                ).fetchone()
                if existing_policy is None:
                    connection.execute(
                        """INSERT INTO infrastructure_events
                           (id, infrastructure_job_id, event_type, metadata_json, created_at)
                           VALUES (?, ?, 'firewall_policy_requested', ?, ?)""",
                        (
                            uuid.uuid4().hex,
                            job_id,
                            json.dumps(firewall_policy, sort_keys=True),
                            datetime.now(UTC).isoformat(),
                        ),
                    )
        try:
            droplet = self.provider.create_droplet(specification)
        except AmbiguousProviderOperation as exc:
            timestamp = datetime.now(UTC).isoformat()
            with self.database.connect() as connection:
                connection.execute(
                    """UPDATE infrastructure_jobs SET status = 'running', locked_at = NULL,
                              last_error = ?, next_attempt_at = ? WHERE id = ?""",
                    (
                        "AmbiguousProviderOperation: provider create outcome is uncertain",
                        timestamp,
                        job_id,
                    ),
                )
                connection.execute(
                    """INSERT INTO infrastructure_events
                       (id, infrastructure_job_id, event_type, metadata_json, created_at)
                       VALUES (?, ?, 'provider_create_ambiguous', '{}', ?)""",
                    (uuid.uuid4().hex, job_id, timestamp),
                )
            raise exc
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
                          provider_action_id = ?, status = 'running', locked_at = NULL
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
        if row["status"] == "awaiting_verification":
            # Verification is an explicit operator step after bootstrap. Do
            # not keep polling or append duplicate events while it is pending.
            return {"job_id": job_id, "status": "awaiting_verification"}
        resource_id = row["provider_resource_id"]
        action_id = row["provider_action_id"]
        if not resource_id:
            list_by_tag = getattr(self.provider, "list_by_tag", None)
            if not callable(list_by_tag):
                raise ConnectivityError("provider cannot reconcile an unrecorded resource")
            candidates = list_by_tag(f"aurix-provision-job-{job_id}")
            if len(candidates) != 1:
                if len(candidates) > 1:
                    timestamp = datetime.now(UTC).isoformat()
                    with self.database.connect() as connection:
                        connection.execute(
                            """UPDATE infrastructure_jobs
                                  SET status = 'failed', locked_at = NULL,
                                      last_error = 'ambiguous provider resources'
                                WHERE id = ? AND status = 'running'""",
                            (job_id,),
                        )
                        connection.execute(
                            """INSERT INTO infrastructure_events
                               (id, infrastructure_job_id, event_type, metadata_json, created_at)
                               VALUES (?, ?, 'provider_reconcile_ambiguous', ?, ?)""",
                            (
                                uuid.uuid4().hex,
                                job_id,
                                json.dumps({"candidate_count": len(candidates)}, sort_keys=True),
                                timestamp,
                            ),
                        )
                    return {"job_id": job_id, "status": "failed"}
                return {"job_id": job_id, "status": "creating", "provider_status": "not_found"}
            candidate_id = candidates[0].get("id") if isinstance(candidates[0], dict) else None
            if not candidate_id:
                raise ConnectivityError("provider recovery candidate has no resource ID")
            candidate_actions = candidates[0].get("action_ids") or []
            resource_id = str(candidate_id)
            action_id = str(candidate_actions[0]) if candidate_actions else None
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                connection.execute(
                    """UPDATE infrastructure_jobs
                          SET provider_resource_id = ?, provider_action_id = ?
                        WHERE id = ? AND status = 'running'
                          AND provider_resource_id IS NULL""",
                    (resource_id, action_id, job_id),
                )
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
        droplet = self.provider.droplet(str(resource_id))
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
        try:
            firewall_policy = self._durable_firewall_policy(job_id)
            if firewall_policy is not None:
                self.apply_firewall(job_id, firewall_policy)
        except ProvisionValidationError as exc:
            self._record_provision_validation_failure(job_id, exc)
            raise
        timestamp = datetime.now(UTC).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            updated = connection.execute(
                """UPDATE infrastructure_jobs SET status = 'awaiting_verification',
                          locked_at = NULL WHERE id = ? AND status = 'running'""",
                (job_id,),
            )
            if getattr(updated, "rowcount", 1) == 1:
                connection.execute(
                    """INSERT INTO infrastructure_events
                       (id, infrastructure_job_id, event_type, metadata_json, created_at)
                       VALUES (?, ?, 'droplet_active', ?, ?)""",
                    (
                        uuid.uuid4().hex,
                        job_id,
                        (
                            "{}"
                            if not public_ip
                            else json.dumps({"public_ip": public_ip}, sort_keys=True)
                        ),
                        timestamp,
                    ),
                )
        return {"job_id": job_id, "status": "awaiting_verification", "public_ip": public_ip}

    def _provision_specification(self, job_id: str) -> dict[str, Any]:
        """Rebuild a provider request from the durable, redacted intent event."""
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT metadata_json FROM infrastructure_events
                     WHERE infrastructure_job_id = ? AND event_type = 'provision_requested'
                     ORDER BY created_at ASC LIMIT 1""",
                (job_id,),
            ).fetchone()
        if row is None:
            raise ProvisionValidationError("Provisioning intent has no durable specification")
        try:
            metadata = json.loads(str(row["metadata_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProvisionValidationError("Provisioning intent specification is invalid") from exc
        if not isinstance(metadata, dict):
            raise ProvisionValidationError("Provisioning intent specification is invalid")
        required = {"region", "size", "image"}
        if any(not str(metadata.get(key) or "").strip() for key in required):
            raise ProvisionValidationError("Provisioning intent specification is incomplete")
        return {
            "name": f"aurix-vpn-{str(job_id)[:12]}",
            "region": str(metadata["region"]).strip(),
            "size": str(metadata["size"]).strip(),
            "image": str(metadata["image"]).strip(),
            "tags": ["aurix-vpn-node", "aurix-env-production"],
        }

    def _record_provision_validation_failure(
        self, job_id: str, error: ProvisionValidationError
    ) -> None:
        timestamp = datetime.now(UTC).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            updated = connection.execute(
                """UPDATE infrastructure_jobs SET status = 'failed', locked_at = NULL,
                          last_error = ?, next_attempt_at = ?
                       WHERE id = ? AND operation = 'provision'
                         AND status IN ('pending', 'running')""",
                (
                    f"{type(error).__name__}: {str(error)[:300]}",
                    timestamp,
                    str(job_id),
                ),
            )
            if int(getattr(updated, "rowcount", 0) or 0) != 1:
                return
            connection.execute(
                """INSERT INTO infrastructure_events
                   (id, infrastructure_job_id, event_type, metadata_json, created_at)
                   VALUES (?, ?, 'provision_validation_failed', '{}', ?)""",
                (uuid.uuid4().hex, str(job_id), timestamp),
            )

    def process_infrastructure_once(self, now: datetime | None = None) -> dict[str, Any] | None:
        """Process one durable provider intent from a dedicated worker boundary.

        A pending intent is submitted only through ``execute_provision`` and a
        submitted intent is only observed through ``reconcile_provision``.
        Endpoint activation remains a separate, explicit operator action.
        """
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT id, status FROM infrastructure_jobs
                     WHERE operation = 'provision'
                       AND ((status = 'pending' AND next_attempt_at <= ?)
                            OR status IN ('running', 'awaiting_verification'))
                     ORDER BY CASE status WHEN 'running' THEN 0
                                          WHEN 'awaiting_verification' THEN 1
                                          ELSE 2 END,
                              created_at ASC
                     LIMIT 1""",
                (current.isoformat(),),
            ).fetchone()
        if row is None:
            return None
        job_id = str(row["id"])
        if str(row["status"]) == "pending":
            try:
                specification = self._provision_specification(job_id)
                return self.execute_provision(job_id, specification)
            except ProvisionValidationError as exc:
                self._record_provision_validation_failure(job_id, exc)
                raise
        return self.reconcile_provision(job_id)

    def verify_and_activate(
        self,
        job_id: str,
        *,
        code: str,
        region: str,
        api_url: str,
        certificate_sha256: str,
        max_active_keys: int | None = None,
        actor_id: int | None = None,
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
        requested = self._provision_specification(job_id)
        if str(region or "").strip().lower() != requested["region"]:
            raise ConnectivityError("Endpoint region does not match the durable provisioning intent")
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
            EndpointRegistry._record_audit_event(
                connection,
                actor_type="admin" if actor_id is not None else "system",
                actor_id=actor_id,
                action="infrastructure_endpoint_verified",
                target_type="vpn_endpoint",
                target_id=endpoint_id,
                metadata={
                    "job_id": str(job_id),
                    "region": str(region).strip(),
                    "provider_resource_id": str(row["provider_resource_id"]),
                },
                created_at=timestamp,
            )
        return endpoint
