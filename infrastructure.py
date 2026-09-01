"""Guarded DigitalOcean fleet intents for a separate operator worker.

The Telegram runtime never imports or executes this module. Infrastructure
mutation requires an explicit worker, a scoped provider token, durable job
state, allowlists, budget limits, and an explicit mutation feature gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


UTC = timezone.utc


class InfrastructureError(RuntimeError):
    """A provider or fleet safety decision failed."""


def _enabled(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in {"1", "true", "yes", "on"}


class DigitalOceanClient:
    """Minimal DigitalOcean API client that never logs its bearer token."""

    BASE_URL = "https://api.digitalocean.com/v2"

    def __init__(self, token: str, timeout_seconds: int = 20):
        if not token:
            raise ValueError("DIGITALOCEAN_API_TOKEN is required")
        self._token = token
        self.timeout_seconds = max(1, int(timeout_seconds))

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(
            self.BASE_URL + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise InfrastructureError(f"DigitalOcean returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise InfrastructureError("DigitalOcean request failed") from exc
        try:
            return json.loads(raw) if raw else {}
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise InfrastructureError("DigitalOcean returned invalid JSON") from exc

    def create_droplet(self, specification: dict[str, Any]) -> dict[str, Any]:
        payload = self._request("POST", "/droplets", specification)
        droplet = payload.get("droplet") if isinstance(payload, dict) else None
        if not isinstance(droplet, dict) or not droplet.get("id"):
            raise InfrastructureError("DigitalOcean create response lacks a Droplet ID")
        return droplet

    def list_droplets(self, *, tag_name: str | None = None) -> list[dict[str, Any]]:
        """Return all Droplets, following provider pagination safely."""
        droplets: list[dict[str, Any]] = []
        page = 1
        per_page = 100
        while page <= 100:
            query = urllib.parse.urlencode({"page": page, "per_page": per_page})
            if tag_name:
                query += "&" + urllib.parse.urlencode({"tag_name": tag_name})
            payload = self._request("GET", f"/droplets?{query}")
            batch = payload.get("droplets") if isinstance(payload, dict) else None
            if not isinstance(batch, list):
                raise InfrastructureError("DigitalOcean response lacks Droplets")
            droplets.extend(item for item in batch if isinstance(item, dict) and item.get("id"))
            meta = payload.get("meta") if isinstance(payload, dict) else None
            total = meta.get("total") if isinstance(meta, dict) else None
            if not batch or (isinstance(total, int) and len(droplets) >= total) or len(batch) < per_page:
                break
            page += 1
        if page > 100:
            raise InfrastructureError("DigitalOcean pagination exceeded safety limit")
        return droplets

    def droplet(self, droplet_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/droplets/{urllib.parse.quote(droplet_id, safe='')}")
        droplet = payload.get("droplet") if isinstance(payload, dict) else None
        if not isinstance(droplet, dict):
            raise InfrastructureError("DigitalOcean response lacks a Droplet")
        return droplet

    def action(self, action_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/actions/{urllib.parse.quote(action_id, safe='')}")
        action = payload.get("action") if isinstance(payload, dict) else None
        if not isinstance(action, dict):
            raise InfrastructureError("DigitalOcean response lacks an action")
        return action

    def billing_balance(self) -> dict[str, Any]:
        payload = self._request("GET", "/customers/my/balance")
        if not isinstance(payload, dict):
            raise InfrastructureError("DigitalOcean response lacks billing data")
        return payload


class FleetController:
    """Create and reconcile bounded provider jobs; never auto-activate endpoints."""

    def __init__(self, database: Any, provider: DigitalOceanClient | None = None):
        self.database = database
        self.provider = provider

    @staticmethod
    def _allowlist(name: str, default: str) -> set[str]:
        return {item.strip() for item in os.environ.get(name, default).split(",") if item.strip()}

    @staticmethod
    def _managed_provider_ids() -> set[str]:
        raw = os.environ.get("AURIX_MANAGED_DROPLET_IDS", "")
        return {item.strip() for item in raw.split(",") if item.strip()}

    def _provider_inventory(self) -> list[dict[str, Any]]:
        """Return provider nodes explicitly owned by AuriX.

        Tags are preferred, with explicit IDs covering pre-existing nodes while
        an operator is applying tags. A provider inventory failure is allowed to
        abort admission so a hidden node cannot make the budget unsafe.
        """
        if self.provider is None:
            return []
        listing = getattr(self.provider, "list_droplets", None)
        if not callable(listing):
            return []
        droplets = listing()
        managed_ids = self._managed_provider_ids()
        with self.database.connect() as connection:
            configured_ids = connection.execute(
                "SELECT provider_resource_id FROM outline_servers WHERE provider_resource_id IS NOT NULL"
            ).fetchall()
        managed_ids.update(str(row["provider_resource_id"]) for row in configured_ids)
        managed_tag = os.environ.get("AURIX_MANAGED_DROPLET_TAG", "aurix-vpn-node").strip()
        result: list[dict[str, Any]] = []
        for droplet in droplets:
            tags = {str(tag) for tag in (droplet.get("tags") or [])}
            if str(droplet.get("id")) in managed_ids or managed_tag in tags:
                result.append(droplet)
        return result

    def _known_node_count(self, provider_inventory: list[dict[str, Any]] | None = None) -> int:
        with self.database.connect() as connection:
            database_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM outline_servers WHERE enabled = 1"
                ).fetchone()["n"]
            )
            job_count = int(
                connection.execute(
                    """SELECT COUNT(*) AS n FROM infrastructure_jobs
                       WHERE operation = 'provision' AND status IN
                       ('running', 'awaiting_verification', 'completed')"""
                ).fetchone()["n"]
            )
        provider_count = len(provider_inventory or [])
        return max(database_count, job_count, provider_count)

    def reconcile_provider_inventory(self) -> dict[str, int]:
        """Persist a sanitized observation of managed provider Droplets."""
        if self.provider is None:
            raise InfrastructureError("DigitalOcean provider is not configured")
        inventory = self._provider_inventory()
        now_text = datetime.now(UTC).isoformat()
        matched = 0
        unmatched = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            for droplet in inventory:
                provider_id = str(droplet["id"])
                status = str(droplet.get("status") or "unknown")[:32]
                region = droplet.get("region") if isinstance(droplet.get("region"), dict) else {}
                updated = connection.execute(
                    """UPDATE outline_servers
                       SET provider_status = ?, provider_last_seen_at = ?, updated_at = ?
                       WHERE provider_resource_id = ?""",
                    (status, now_text, now_text, provider_id),
                ).rowcount
                if updated:
                    matched += 1
                else:
                    unmatched += 1
                connection.execute(
                    """INSERT INTO infrastructure_events
                       (id, event_type, metadata_json, created_at)
                       VALUES (?, 'provider_inventory_observed', ?, ?)""",
                    (
                        uuid.uuid4().hex,
                        json.dumps(
                            {
                                "provider_resource_id": provider_id,
                                "status": status,
                                "region": str(region.get("slug") or "")[:32],
                                "size": str(droplet.get("size_slug") or "")[:64],
                            },
                            sort_keys=True,
                        ),
                        now_text,
                    ),
                )
        return {"managed": len(inventory), "matched": matched, "unmatched": unmatched}

    def queue_provision(
        self,
        *,
        region: str,
        size: str,
        image: str,
        requested_by: int,
        now: datetime | None = None,
    ) -> str:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if region not in self._allowlist("AURIX_ALLOWED_REGIONS", "sgp1"):
            raise InfrastructureError("Droplet region is outside the configured allowlist")
        if size not in self._allowlist("AURIX_ALLOWED_DROPLET_SIZES", "s-1vcpu-1gb"):
            raise InfrastructureError("Droplet size is outside the configured allowlist")
        if image not in self._allowlist("AURIX_ALLOWED_DROPLET_IMAGES", "ubuntu-24-04-x64"):
            raise InfrastructureError("Droplet image is outside the configured allowlist")
        request = {
            "region": region,
            "size": size,
            "image": image,
            "requested_by": int(requested_by),
        }
        window = current.replace(minute=0, second=0, microsecond=0).isoformat()
        fingerprint = hashlib.sha256(
            json.dumps({**request, "window": window}, sort_keys=True).encode()
        ).hexdigest()
        now_text = current.isoformat()
        # Preserve idempotency during a provider outage: an already-created
        # request must be returned without requiring a fresh inventory call.
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM infrastructure_jobs WHERE request_fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        if existing is not None:
            return str(existing["id"])
        provider_inventory = self._provider_inventory()
        node_count = self._known_node_count(provider_inventory)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            existing = connection.execute(
                "SELECT id FROM infrastructure_jobs WHERE request_fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if existing is not None:
                return str(existing["id"])
            if node_count >= max(1, int(os.environ.get("AURIX_MAX_VPN_NODES", "3"))):
                raise InfrastructureError("Configured VPN node limit has been reached")
            day_start = current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            created_today = int(
                connection.execute(
                    """SELECT COUNT(*) AS n FROM infrastructure_jobs
                       WHERE operation = 'provision' AND created_at >= ?""",
                    (day_start,),
                ).fetchone()["n"]
            )
            if created_today >= max(
                1, int(os.environ.get("AURIX_MAX_NODE_CREATIONS_PER_DAY", "1"))
            ):
                raise InfrastructureError("Daily VPN node creation limit has been reached")
            latest = connection.execute(
                """SELECT created_at FROM infrastructure_jobs
                   WHERE operation = 'provision' ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
            cooldown = max(0, int(os.environ.get("AURIX_NODE_CREATION_COOLDOWN_SECONDS", "86400")))
            if latest is not None:
                latest_at = datetime.fromisoformat(str(latest["created_at"])).astimezone(UTC)
                if current < latest_at + timedelta(seconds=cooldown):
                    raise InfrastructureError("VPN node creation cooldown is still active")
            if connection.execute(
                """SELECT 1 FROM infrastructure_jobs
                   WHERE operation = 'provision' AND status IN ('pending', 'running') LIMIT 1"""
            ).fetchone() is not None:
                raise InfrastructureError("Another server provisioning job is already active")
            job_id = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO infrastructure_jobs
                   (id, operation, status, attempts, next_attempt_at,
                    request_fingerprint, created_at)
                   VALUES (?, 'provision', 'pending', 0, ?, ?, ?)""",
                (job_id, now_text, fingerprint, now_text),
            )
            connection.execute(
                """INSERT INTO infrastructure_events
                   (id, infrastructure_job_id, event_type, metadata_json, created_at)
                   VALUES (?, ?, 'provision_requested', ?, ?)""",
                (uuid.uuid4().hex, job_id, json.dumps(request, sort_keys=True), now_text),
            )
        return job_id

    def _budget_guard(self, *, existing_nodes: int = 0) -> None:
        maximum = os.environ.get("AURIX_MAX_MONTHLY_INFRA_BUDGET_USD", "").strip()
        if not maximum:
            raise InfrastructureError("A monthly infrastructure budget must be configured")
        if self.provider is None:
            raise InfrastructureError("DigitalOcean provider is not configured")
        try:
            budget = Decimal(maximum)
            estimate = Decimal(os.environ.get("AURIX_DROPLET_MONTHLY_COST_ESTIMATE_USD", "6"))
            usage = Decimal(str(self.provider.billing_balance().get("month_to_date_usage", "")))
        except (InvalidOperation, ValueError, TypeError, AttributeError) as exc:
            raise InfrastructureError("DigitalOcean budget data is invalid") from exc
        committed = Decimal(max(0, int(existing_nodes))) * estimate
        if (
            budget <= 0
            or estimate <= 0
            or usage < 0
            or committed + estimate > budget
            or usage + estimate > budget
        ):
            raise InfrastructureError("Configured monthly infrastructure budget would be exceeded")

    def execute_provision(self, job_id: str) -> dict[str, Any]:
        if not _enabled("AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED"):
            raise InfrastructureError("Infrastructure mutations are disabled")
        with self.database.connect() as connection:
            pending = connection.execute(
                "SELECT id FROM infrastructure_jobs WHERE id = ? AND status = 'pending'",
                (job_id,),
            ).fetchone()
        if pending is None:
            raise InfrastructureError("Provisioning job is not pending")
        provider_inventory = self._provider_inventory()
        self._budget_guard(existing_nodes=self._known_node_count(provider_inventory))
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            job = connection.execute(
                "SELECT * FROM infrastructure_jobs WHERE id = ? AND status = 'pending'",
                (job_id,),
            ).fetchone()
            event = connection.execute(
                """SELECT metadata_json FROM infrastructure_events
                   WHERE infrastructure_job_id = ? AND event_type = 'provision_requested'
                   ORDER BY created_at LIMIT 1""",
                (job_id,),
            ).fetchone()
            if job is None or event is None:
                raise InfrastructureError("Provisioning job is not pending")
            specification = json.loads(str(event["metadata_json"]))
            # Bootstrap is deliberately credential-free. An operator installs
            # Outline and registers its secret management URL after creation.
            specification = {
                "name": f"aurix-vpn-{specification['region']}-{job_id[:8]}",
                "region": specification["region"],
                "size": specification["size"],
                "image": specification["image"],
                "tags": ["aurix-vpn-node", "aurix-awaiting-verification"],
            }
            connection.execute(
                """UPDATE infrastructure_jobs SET status = 'running', attempts = attempts + 1,
                          locked_at = ? WHERE id = ?""",
                (datetime.now(UTC).isoformat(), job_id),
            )
        try:
            droplet = self.provider.create_droplet(specification)  # type: ignore[union-attr]
        except Exception as exc:
            with self.database.connect() as connection:
                connection.execute(
                    """UPDATE infrastructure_jobs SET status = 'failed', locked_at = NULL,
                              last_error = ? WHERE id = ?""",
                    (type(exc).__name__, job_id),
                )
            raise
        actions = droplet.get("action_ids") or []
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE infrastructure_jobs SET provider_resource_id = ?,
                          provider_action_id = ? WHERE id = ?""",
                (str(droplet["id"]), str(actions[0]) if actions else None, job_id),
            )
        return {"job_id": job_id, "droplet_id": str(droplet["id"]), "status": "creating"}

    def reconcile_provision(self, job_id: str) -> dict[str, Any]:
        """Observe provider state and stop before secret endpoint activation."""
        if self.provider is None:
            raise InfrastructureError("DigitalOcean provider is not configured")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM infrastructure_jobs WHERE id = ? AND operation = 'provision'",
                (job_id,),
            ).fetchone()
        if row is None:
            raise InfrastructureError("Provisioning job does not exist")
        if row["status"] in ("failed", "awaiting_verification", "completed"):
            return {"job_id": job_id, "status": str(row["status"])}
        if row["provider_action_id"]:
            action = self.provider.action(str(row["provider_action_id"]))
            status = str(action.get("status") or "unknown")
            if status == "errored":
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE infrastructure_jobs SET status = 'failed', last_error = ? WHERE id = ?",
                        ("provider action failed", job_id),
                    )
                return {"job_id": job_id, "status": "failed"}
            if status != "completed":
                return {"job_id": job_id, "status": "creating", "provider_status": status}
        droplet = self.provider.droplet(str(row["provider_resource_id"]))
        if str(droplet.get("status")) != "active":
            return {"job_id": job_id, "status": "creating"}
        public_ip = next(
            (
                str(item.get("ip_address"))
                for item in (droplet.get("networks") or {}).get("v4", [])
                if isinstance(item, dict) and item.get("type") == "public"
            ),
            None,
        )
        now_text = datetime.now(UTC).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE infrastructure_jobs SET status = 'awaiting_verification',
                          locked_at = NULL WHERE id = ? AND status = 'running'""",
                (job_id,),
            )
            connection.execute(
                """INSERT INTO infrastructure_events
                   (id, infrastructure_job_id, event_type, metadata_json, created_at)
                   VALUES (?, ?, 'droplet_active', ?, ?)""",
                (uuid.uuid4().hex, job_id, json.dumps({"public_ip": public_ip}), now_text),
            )
        return {"job_id": job_id, "status": "awaiting_verification", "public_ip": public_ip}
