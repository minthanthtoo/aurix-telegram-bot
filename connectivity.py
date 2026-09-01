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

from outline_adapter import OutlineClient


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

    def configure_bootstrap(
        self,
        api_url: str,
        certificate_sha256: str,
        *,
        code: str = "SGP-01",
        region: str = "sgp1",
        outline_version: str | None = None,
        now: datetime | None = None,
    ) -> None:
        timestamp = (now or datetime.now(UTC)).isoformat()
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
                          public_address = ?, verified_at = COALESCE(verified_at, ?),
                          last_healthy_at = COALESCE(last_healthy_at, ?)
                   WHERE id = ?""",
                (
                    code[:64],
                    region[:32],
                    True,
                    encrypted,
                    certificate_sha256.lower().replace(":", ""),
                    outline_version,
                    public_address,
                    timestamp,
                    timestamp,
                    DEFAULT_ENDPOINT_ID,
                ),
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
        return [dict(row) for row in rows]

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

    def select_endpoint_for_plan(self, connection: Any, plan_code: str) -> str:
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
                 AND e.last_healthy_at IS NOT NULL AND e.last_healthy_at >= ?
               ORDER BY
                 CASE WHEN e.max_active_keys IS NULL THEN 2147483647
                      ELSE e.max_active_keys -
                        (SELECT COUNT(*) FROM endpoint_assignments aa
                          WHERE aa.endpoint_id = e.id AND aa.status = 'active') END DESC,
                 e.code"""
            + lock,
            (plan_code, plan_code, True, fresh_after),
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
            endpoint_id = self.select_endpoint_for_plan(connection, plan_code)
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
