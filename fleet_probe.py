"""Authenticated, server-side fleet probing and route recommendation.

The control plane owns probe intent and durable observations.  A node agent owns
the network operation itself and submits only a bounded, signed result.  This
module deliberately has no Telegram or Outline-management dependency so it can
be used by the bot, a web ingest process, and a small node-side runner.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from fleet_probe_repository import FleetProbeRepository

UTC = timezone.utc

PROBE_TYPES = frozenset({"icmp", "tcp", "udp", "dns", "https", "download", "node_to_node"})
RESULT_STATUSES = frozenset({"success", "timeout", "refused", "unavailable", "error"})
TARGET_KINDS = frozenset({"public", "control_plane", "server"})
MAX_RESULT_BYTES = 100 * 1024 * 1024
MAX_INSTRUCTION_BYTES = 16 * 1024


class FleetProbeError(RuntimeError):
    """Raised when a probe job or result violates the control-plane contract."""


def _now_text() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise FleetProbeError("timestamp is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FleetProbeError("timestamp is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def sign_probe_result(job_id: str, payload: Mapping[str, Any], secret: str) -> str:
    """Return the HMAC used by a node agent for one result envelope."""
    if not str(secret or ""):
        raise FleetProbeError("probe agent secret is not configured")
    envelope = {"job_id": str(job_id), "payload": dict(payload)}
    return hmac.new(str(secret).encode("utf-8"), _canonical_json(envelope), hashlib.sha256).hexdigest()


def verify_probe_result(
    job_id: str, payload: Mapping[str, Any], signature: str, secret: str
) -> bool:
    expected = sign_probe_result(job_id, payload, secret)
    return hmac.compare_digest(expected, str(signature or ""))


@dataclass(frozen=True)
class ProbeJob:
    job_id: str
    schedule_id: str
    source_server_id: str
    target_id: str
    probe_type: str
    instruction: dict[str, Any]
    nonce: str
    expires_at: str
    status: str


@dataclass(frozen=True)
class RouteRecommendation:
    server_id: str
    label: str
    region: str
    status: str
    score: float | None
    sample_count: int
    last_observed_at: str | None
    reason: str | None


def _bounded_number(value: Any, *, name: str, maximum: float) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise FleetProbeError(f"{name} is invalid") from exc
    if not math.isfinite(parsed) or parsed < 0 or parsed > maximum:
        raise FleetProbeError(f"{name} is outside the allowed range")
    return parsed


def _bounded_int(value: Any, *, name: str, maximum: int) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise FleetProbeError(f"{name} is invalid") from exc
    if parsed < 0 or parsed > maximum:
        raise FleetProbeError(f"{name} is outside the allowed range")
    return parsed


class FleetProbeService:
    """Durable scheduling, authenticated result ingestion, and route scoring."""

    def __init__(
        self,
        database: Any,
        *,
        agent_secrets: Mapping[str, str] | None = None,
        stale_after_seconds: int = 900,
        job_ttl_seconds: int = 180,
    ):
        if stale_after_seconds < 30:
            raise ValueError("stale_after_seconds must be at least 30")
        if not 30 <= job_ttl_seconds <= 3600:
            raise ValueError("job_ttl_seconds must be between 30 and 3600")
        self.database = database
        self.agent_secrets = {str(key): str(value) for key, value in (agent_secrets or {}).items() if value}
        self.stale_after_seconds = int(stale_after_seconds)
        self.job_ttl_seconds = int(job_ttl_seconds)
        self.repository = FleetProbeRepository()

    @staticmethod
    def _require_identifier(value: Any, *, name: str, maximum: int = 128) -> str:
        normalized = str(value or "").strip()
        if not normalized or len(normalized) > maximum:
            raise FleetProbeError(f"{name} is invalid")
        return normalized

    def register_target(
        self,
        *,
        target_id: str,
        label: str,
        target_kind: str,
        host: str,
        port: int | None = None,
        scheme: str | None = None,
        enabled: bool = True,
        now: str | None = None,
    ) -> dict[str, Any]:
        target_id = self._require_identifier(target_id, name="target_id")
        label = self._require_identifier(label, name="label", maximum=256)
        target_kind = self._require_identifier(target_kind, name="target_kind")
        if target_kind not in TARGET_KINDS:
            raise FleetProbeError("target_kind is unsupported")
        host = self._require_identifier(host, name="host", maximum=253)
        normalized_port = None if port is None else int(port)
        if normalized_port is not None and not 1 <= normalized_port <= 65535:
            raise FleetProbeError("port is invalid")
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self.repository.upsert_target(
                connection,
                (target_id, label, target_kind, host, normalized_port, scheme, int(bool(enabled)), timestamp, timestamp),
            )
        return {"target_id": target_id, "label": label, "target_kind": target_kind, "host": host, "port": normalized_port}

    def register_schedule(
        self,
        *,
        schedule_id: str,
        source_server_id: str,
        target_id: str,
        probe_type: str,
        interval_seconds: int,
        timeout_ms: int,
        payload_bytes: int = 0,
        enabled: bool = True,
        next_run_at: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        schedule_id = self._require_identifier(schedule_id, name="schedule_id")
        source_server_id = self._require_identifier(source_server_id, name="source_server_id")
        target_id = self._require_identifier(target_id, name="target_id")
        probe_type = self._require_identifier(probe_type, name="probe_type")
        if probe_type not in PROBE_TYPES:
            raise FleetProbeError("probe_type is unsupported")
        if not 10 <= int(interval_seconds) <= 86400:
            raise FleetProbeError("interval_seconds is outside the allowed range")
        if not 100 <= int(timeout_ms) <= 30000:
            raise FleetProbeError("timeout_ms is outside the allowed range")
        if not 0 <= int(payload_bytes) <= 10 * 1024 * 1024:
            raise FleetProbeError("payload_bytes is outside the allowed range")
        timestamp = str(now or _now_text())
        next_run = str(next_run_at or timestamp)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            if not self.repository.schedule_dependencies(connection, source_server_id, target_id):
                raise FleetProbeError("schedule references an unknown server or target")
            self.repository.upsert_schedule(
                connection,
                (schedule_id, source_server_id, target_id, probe_type, int(interval_seconds), int(timeout_ms),
                 int(payload_bytes), int(bool(enabled)), next_run, timestamp, timestamp),
            )
        return {"schedule_id": schedule_id, "source_server_id": source_server_id, "target_id": target_id,
                "probe_type": probe_type, "next_run_at": next_run}

    def enqueue_due_probes(self, *, now: str | None = None, limit: int = 100) -> int:
        """Turn due schedules into expiring, idempotent jobs."""
        timestamp = str(now or _now_text())
        if not 1 <= int(limit) <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            rows = self.repository.due_schedules(connection, timestamp, int(limit))
            count = 0
            now_dt = _parse_time(timestamp)
            expires = (now_dt + timedelta(seconds=self.job_ttl_seconds)).isoformat()
            for row in rows:
                schedule_id = str(row["schedule_id"])
                job_id = f"probe-{secrets.token_hex(16)}"
                nonce = secrets.token_urlsafe(24)
                instruction = {
                    "version": 1,
                    "job_id": job_id,
                    "nonce": nonce,
                    "source_server_id": str(row["source_server_id"]),
                    "target_id": str(row["target_id"]),
                    "target_kind": str(row["target_kind"]),
                    "target_label": str(row["target_label"]),
                    "host": str(row["host"]),
                    "port": int(row["port"]) if row["port"] is not None else None,
                    "scheme": str(row["scheme"]) if row["scheme"] else None,
                    "probe_type": str(row["probe_type"]),
                    "timeout_ms": int(row["timeout_ms"]),
                    "payload_bytes": int(row["payload_bytes"]),
                    "expires_at": expires,
                }
                encoded = json.dumps(instruction, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
                if len(encoded.encode("utf-8")) > MAX_INSTRUCTION_BYTES:
                    raise FleetProbeError("probe instruction is too large")
                self.repository.insert_job(
                    connection,
                    (job_id, schedule_id, row["source_server_id"], row["target_id"], row["probe_type"],
                     encoded, nonce, expires, timestamp),
                )
                next_run = (now_dt + timedelta(seconds=int(row["interval_seconds"]))).isoformat()
                self.repository.advance_schedule(
                    connection,
                    (next_run, timestamp, timestamp, schedule_id),
                )
                count += 1
        return count

    def claim_jobs(
        self,
        *,
        agent_id: str,
        source_server_id: str | None = None,
        now: str | None = None,
        limit: int = 10,
        lease_seconds: int = 120,
    ) -> list[ProbeJob]:
        """Lease pending jobs to the matching node agent."""
        agent_id = self._require_identifier(agent_id, name="agent_id")
        timestamp = str(now or _now_text())
        if not 1 <= int(limit) <= 100:
            raise ValueError("limit must be between 1 and 100")
        if not 30 <= int(lease_seconds) <= 900:
            raise ValueError("lease_seconds must be between 30 and 900")
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            rows = self.repository.claimable_jobs(
                connection,
                timestamp=timestamp,
                source_server_id=source_server_id,
                limit=int(limit),
            )
            claimed_at = _parse_time(timestamp)
            lease_at = (claimed_at + timedelta(seconds=int(lease_seconds))).isoformat()
            jobs: list[ProbeJob] = []
            for row in rows:
                if source_server_id is None and str(row["source_server_id"]) != agent_id:
                    continue
                updated = self.repository.claim_job(
                    connection,
                    agent_id=agent_id,
                    lease_at=lease_at,
                    job_id=str(row["job_id"]),
                    timestamp=timestamp,
                )
                if not updated:
                    continue
                instruction = json.loads(str(row["instruction_json"]))
                jobs.append(ProbeJob(
                    job_id=str(row["job_id"]), schedule_id=str(row["schedule_id"]),
                    source_server_id=str(row["source_server_id"]), target_id=str(row["target_id"]),
                    probe_type=str(row["probe_type"]), instruction=instruction,
                    nonce=str(row["nonce"]), expires_at=str(row["expires_at"]), status="claimed",
                ))
        return jobs

    def submit_result(
        self,
        *,
        job_id: str,
        agent_id: str,
        payload: Mapping[str, Any],
        signature: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Verify and persist one node result exactly once."""
        job_id = self._require_identifier(job_id, name="job_id")
        agent_id = self._require_identifier(agent_id, name="agent_id")
        if not isinstance(payload, Mapping):
            raise FleetProbeError("probe result must be an object")
        secret = self.agent_secrets.get(agent_id)
        if not secret or not verify_probe_result(job_id, payload, signature, secret):
            raise FleetProbeError("probe result signature is invalid")
        timestamp = str(now or _now_text())
        status = self._require_identifier(payload.get("status"), name="status")
        if status not in RESULT_STATUSES:
            raise FleetProbeError("probe result status is unsupported")
        observed_at = str(payload.get("observed_at") or timestamp)
        observed_time = _parse_time(observed_at)
        now_time = _parse_time(timestamp)
        if observed_time > now_time + timedelta(minutes=5):
            raise FleetProbeError("probe result timestamp is too far in the future")
        latency_ms = _bounded_number(payload.get("latency_ms"), name="latency_ms", maximum=86400000)
        loss = _bounded_number(payload.get("packet_loss_percent"), name="packet_loss_percent", maximum=100)
        bytes_transferred = _bounded_int(payload.get("bytes_transferred"), name="bytes_transferred", maximum=MAX_RESULT_BYTES)
        duration_ms = _bounded_number(payload.get("duration_ms"), name="duration_ms", maximum=86400000)
        error_class = str(payload.get("error_class") or "")[:128] or None
        sanitized_payload = {
            "status": status,
            "latency_ms": latency_ms,
            "packet_loss_percent": loss,
            "bytes_transferred": bytes_transferred,
            "duration_ms": duration_ms,
            "error_class": error_class,
            "observed_at": observed_at,
        }
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            job = self.repository.job(connection, job_id)
            if job is None:
                raise FleetProbeError("probe job does not exist")
            if str(job["source_server_id"]) != agent_id:
                raise FleetProbeError("agent is not assigned to this probe job")
            if _parse_time(str(job["expires_at"])) < now_time:
                self.repository.expire_job(connection, job_id)
                raise FleetProbeError("probe job has expired")
            if str(job["status"]) == "completed":
                existing = self.repository.observation_for_job(connection, job_id)
                return {"accepted": False, "duplicate": True, "observation_id": existing["observation_id"] if existing else None}
            if str(job["claimed_by"] or "") != agent_id:
                raise FleetProbeError("probe job is not leased to this agent")
            observation_id = f"observation-{job_id}"
            self.repository.insert_observation(
                connection,
                (observation_id, job_id, job["source_server_id"], job["target_id"], job["probe_type"],
                 agent_id, status, latency_ms, loss, bytes_transferred, duration_ms, error_class,
                 json.dumps(sanitized_payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
                 str(signature), observed_at, timestamp),
            )
            self.repository.complete_job(connection, job_id, timestamp)
        self.recompute_health(server_id=str(job["source_server_id"]), now=timestamp)
        return {"accepted": True, "duplicate": False, "observation_id": observation_id}

    def expire_jobs(self, *, now: str | None = None) -> int:
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            return self.repository.expire_jobs(connection, timestamp)

    def recompute_health(self, *, server_id: str, now: str | None = None) -> dict[str, Any]:
        """Build a deterministic health projection from recent observations."""
        server_id = self._require_identifier(server_id, name="server_id")
        timestamp = str(now or _now_text())
        cutoff = (_parse_time(timestamp) - timedelta(seconds=self.stale_after_seconds)).isoformat()
        with self.database.connect() as connection:
            samples = self.repository.recent_observations(connection, server_id, cutoff)
            count = len(samples)
            success = [row for row in samples if row["status"] == "success"]
            availability = (len(success) / count * 100) if count else None
            latencies = [float(row["latency_ms"]) for row in success if row["latency_ms"] is not None]
            losses = [float(row["packet_loss_percent"]) for row in samples if row["packet_loss_percent"] is not None]
            rates = [
                float(row["bytes_transferred"]) / (float(row["duration_ms"]) / 1000)
                for row in success
                if row["bytes_transferred"] is not None and row["duration_ms"] and float(row["duration_ms"]) > 0
            ]
            latency_score = max(0.0, min(100.0, 100.0 - statistics.median(latencies) / 10.0)) if latencies else None
            loss_score = max(0.0, min(100.0, 100.0 - statistics.mean(losses))) if losses else None
            # 10 MiB/s is a deliberately conservative normalized reference;
            # this is a ranking signal, not a customer speed guarantee.
            throughput_score = max(0.0, min(100.0, statistics.median(rates) / (10 * 1024 * 1024) * 100)) if rates else None
            components = [
                (availability, 0.40),
                (latency_score, 0.25),
                (loss_score, 0.20),
                (throughput_score, 0.15),
            ]
            weight = sum(item[1] for item in components if item[0] is not None)
            score = sum(float(value) * factor for value, factor in components if value is not None) / weight if weight else None
            if count == 0:
                status = "unknown"
                reason = "no_fresh_observations"
            elif availability is not None and availability < 50:
                status = "unreachable"
                reason = "low_success_ratio"
            elif availability is not None and availability < 80:
                status = "degraded"
                reason = "elevated_failure_ratio"
            else:
                status = "healthy"
                reason = "fresh_probe_evidence"
            last_observed = samples[0]["observed_at"] if samples else None
            freshness = int(max(0, (_parse_time(timestamp) - _parse_time(str(last_observed))).total_seconds())) if last_observed else None
            self.repository.save_health_snapshot(
                connection,
                (server_id, status, score, availability, latency_score, loss_score, throughput_score,
                 count, freshness, last_observed, reason, timestamp),
            )
        return {"server_id": server_id, "status": status, "score": score, "sample_count": count,
                "availability_score": availability, "latency_score": latency_score,
                "loss_score": loss_score, "throughput_score": throughput_score,
                "last_observed_at": last_observed, "reason": reason}

    def recommendations(
        self,
        *,
        region: str | None = None,
        now: str | None = None,
        limit: int = 10,
    ) -> list[RouteRecommendation]:
        timestamp = str(now or _now_text())
        cutoff = (_parse_time(timestamp) - timedelta(seconds=self.stale_after_seconds)).isoformat()
        with self.database.connect() as connection:
            rows = self.repository.recommendation_rows(
                connection, region=region, cutoff=cutoff, limit=int(limit)
            )
        return [RouteRecommendation(
            server_id=str(row["server_id"]), label=str(row["label"]), region=str(row["region"]),
            status=str(row["probe_status"]), score=float(row["score"]) if row["score"] is not None else None,
            sample_count=int(row["sample_count"] or 0),
            last_observed_at=str(row["last_observed_at"]) if row["last_observed_at"] else None,
            reason=str(row["reason"]) if row["reason"] else None,
        ) for row in rows]

    def record_decision(
        self,
        *,
        decision_mode: str,
        selected_server_id: str | None,
        evidence: Mapping[str, Any],
        telegram_id: int | None = None,
        entitlement_ref: str | None = None,
        requested_region: str | None = None,
        score: float | None = None,
        now: str | None = None,
    ) -> str:
        if decision_mode not in {"automatic", "manual", "fallback"}:
            raise FleetProbeError("decision_mode is unsupported")
        if len(json.dumps(dict(evidence), ensure_ascii=True)) > 8192:
            raise FleetProbeError("decision evidence is too large")
        decision_id = f"decision-{secrets.token_hex(16)}"
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self.repository.record_decision(
                connection,
                (decision_id, telegram_id, entitlement_ref, requested_region, selected_server_id,
                 decision_mode, score, json.dumps(dict(evidence), ensure_ascii=True, separators=(",", ":"), sort_keys=True), timestamp),
            )
        return decision_id

    def summary(self, *, now: str | None = None) -> dict[str, Any]:
        recommendations = self.recommendations(now=now, limit=1000)
        return {
            "server_count": len(recommendations),
            "healthy": sum(item.status == "healthy" for item in recommendations),
            "degraded": sum(item.status == "degraded" for item in recommendations),
            "unreachable": sum(item.status == "unreachable" for item in recommendations),
            "unknown": sum(item.status == "unknown" for item in recommendations),
            "routes": [item.__dict__ for item in recommendations],
        }
