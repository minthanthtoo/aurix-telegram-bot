"""Guarded DigitalOcean fleet intents for a separate operator worker.

The Telegram runtime never imports or executes this module. Infrastructure
mutation requires an explicit worker, a scoped provider token, durable job
state, allowlists, budget limits, and an explicit mutation feature gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fleet_enrollment import (
    EnrollmentError,
    create_pending_enrollment,
    generate_token,
    render_user_data,
    validate_enrollment_key,
)


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

    def delete_droplet(self, droplet_id: str) -> None:
        """Delete one Droplet after the fleet controller's safety gates pass."""
        normalized = str(droplet_id).strip()
        if not normalized:
            raise InfrastructureError("DigitalOcean Droplet ID is required")
        self._request("DELETE", f"/droplets/{urllib.parse.quote(normalized, safe='')}")

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
    """Create and reconcile bounded provider jobs.

    Provider creation and Outline activation remain separate gates.  The
    optional worker-side activation bridge may mark a job complete only after
    an external fleet reconciler has verified the declared node identity.
    """

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

    @staticmethod
    def _provider_ssh_key_ids() -> list[str | int]:
        """Return provider-side SSH key IDs/fingerprints for new Droplets.

        DigitalOcean does not make a newly created Droplet reachable through
        the control plane unless an SSH key is attached at creation time. The
        key identifiers are safe to include in the provider request; private
        key material remains only in the worker environment and is never sent
        to DigitalOcean.
        """
        raw = os.environ.get("AURIX_DIGITALOCEAN_SSH_KEY_IDS", "")
        values = [item.strip() for item in raw.split(",") if item.strip()]
        if not values:
            raise InfrastructureError(
                "AURIX_DIGITALOCEAN_SSH_KEY_IDS is required for provider provisioning"
            )
        if len(values) > 10 or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", value)
            for value in values
        ):
            raise InfrastructureError("AURIX_DIGITALOCEAN_SSH_KEY_IDS is invalid")
        # DigitalOcean's API accepts numeric key IDs as JSON numbers and
        # fingerprints as strings. Preserve fingerprints while avoiding the
        # ambiguous string form for numeric IDs.
        return [int(value) if value.isdigit() else value for value in values]

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

    def _find_created_droplet(self, name: str) -> dict[str, Any] | None:
        """Recover a Droplet created by an ambiguous POST response.

        DigitalOcean does not provide a request-idempotency key for Droplet
        creation. The worker therefore searches for the exact generated name
        and AuriX tag before ever retrying a timed-out create. Zero matches are
        left as a terminal failure; multiple matches are unsafe and fail closed.
        """
        if self.provider is None:
            return None
        listing = getattr(self.provider, "list_droplets", None)
        if not callable(listing):
            return None
        droplets = listing()
        matches = [
            item
            for item in droplets
            if isinstance(item, dict)
            and str(item.get("name") or "") == name
            and "aurix-vpn-node" in {str(tag) for tag in (item.get("tags") or [])}
            and item.get("id")
        ]
        if len(matches) > 1:
            raise InfrastructureError("ambiguous provider create recovery")
        return matches[0] if matches else None

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
        configured_provider_count = len(self._managed_provider_ids())
        return max(database_count, job_count, provider_count, configured_provider_count)

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
                metadata = json.dumps(
                    {
                        "provider_resource_id": provider_id,
                        "status": status,
                        "region": str(region.get("slug") or "")[:32],
                        "size": str(droplet.get("size_slug") or "")[:64],
                        "name": str(droplet.get("name") or "")[:96],
                        "managed_tag": os.environ.get(
                            "AURIX_MANAGED_DROPLET_TAG", "aurix-vpn-node"
                        ).strip(),
                    },
                    sort_keys=True,
                )
                recent = connection.execute(
                    """SELECT 1 FROM infrastructure_events
                       WHERE event_type = 'provider_inventory_observed'
                         AND metadata_json = ? AND created_at >= ? LIMIT 1""",
                    (metadata, (datetime.now(UTC) - timedelta(hours=1)).isoformat()),
                ).fetchone()
                if recent is None:
                    connection.execute(
                        """INSERT INTO infrastructure_events
                           (id, event_type, metadata_json, created_at)
                           VALUES (?, 'provider_inventory_observed', ?, ?)""",
                        (uuid.uuid4().hex, metadata, now_text),
                    )
        return {"managed": len(inventory), "matched": matched, "unmatched": unmatched}

    def provider_orphan_candidates(
        self,
        *,
        inventory: list[dict[str, Any]] | None = None,
        now: datetime | None = None,
        min_age_seconds: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return stale, unregistered managed Droplets without mutating anything.

        A provider resource is a candidate only when it is managed by AuriX,
        absent from both the endpoint registry and every infrastructure job,
        and has appeared in at least two persisted inventory observations over
        the configured minimum age.  This deliberately excludes a freshly
        created node that is still waiting for endpoint verification.
        """
        if self.provider is None:
            raise InfrastructureError("DigitalOcean provider is not configured")
        observed = inventory if inventory is not None else self._provider_inventory()
        current = (now or datetime.now(UTC)).astimezone(UTC)
        try:
            age_seconds = max(
                0,
                int(
                    os.environ.get(
                        "AURIX_ORPHAN_CLEANUP_MIN_AGE_SECONDS", "3600"
                    )
                    if min_age_seconds is None
                    else min_age_seconds
                ),
            )
        except (TypeError, ValueError) as exc:
            raise InfrastructureError("AURIX_ORPHAN_CLEANUP_MIN_AGE_SECONDS is invalid") from exc
        managed_tag = os.environ.get("AURIX_MANAGED_DROPLET_TAG", "aurix-vpn-node").strip()
        explicit_ids = self._managed_provider_ids()
        with self.database.connect() as connection:
            registered_rows = connection.execute(
                "SELECT provider_resource_id FROM outline_servers "
                "WHERE provider_resource_id IS NOT NULL"
            ).fetchall()
            job_rows = connection.execute(
                "SELECT provider_resource_id FROM infrastructure_jobs "
                "WHERE provider_resource_id IS NOT NULL "
                "AND status NOT IN ('failed', 'completed')"
            ).fetchall()
            event_rows = connection.execute(
                """SELECT metadata_json, created_at FROM infrastructure_events
                   WHERE event_type = 'provider_inventory_observed'
                   ORDER BY created_at DESC LIMIT 2000"""
            ).fetchall()
        registered = {str(row["provider_resource_id"]) for row in registered_rows}
        referenced_by_job = {str(row["provider_resource_id"]) for row in job_rows}
        observations: dict[str, list[datetime]] = {}
        for row in event_rows:
            try:
                metadata = json.loads(str(row["metadata_json"]))
                provider_id = str(metadata.get("provider_resource_id") or "")
                observed_at = datetime.fromisoformat(str(row["created_at"])).astimezone(UTC)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if provider_id:
                observations.setdefault(provider_id, []).append(observed_at)
        candidates: list[dict[str, Any]] = []
        for droplet in observed:
            provider_id = str(droplet.get("id") or "").strip()
            if not provider_id or provider_id in registered or provider_id in referenced_by_job:
                continue
            tags = {str(tag) for tag in (droplet.get("tags") or [])}
            if managed_tag not in tags and provider_id not in explicit_ids:
                continue
            seen = sorted(observations.get(provider_id, []))
            if len(seen) < 2:
                continue
            first_seen = seen[0]
            age = max(0, int((current - first_seen).total_seconds()))
            if age < age_seconds:
                continue
            region = droplet.get("region") if isinstance(droplet.get("region"), dict) else {}
            candidates.append(
                {
                    "provider_resource_id": provider_id,
                    "name": str(droplet.get("name") or "")[:96],
                    "status": str(droplet.get("status") or "unknown")[:32],
                    "region": str(region.get("slug") or "")[:32],
                    "size": str(droplet.get("size_slug") or "")[:64],
                    "observation_count": len(seen),
                    "first_seen_at": first_seen.isoformat(),
                    "last_seen_at": seen[-1].isoformat(),
                    "age_seconds": age,
                }
            )
        return candidates

    def cleanup_provider_orphans(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Delete only fully-audited provider orphans under explicit gates.

        Cleanup is opt-in and requires both the normal provider mutation gate
        and an exact operator confirmation phrase.  The candidate is checked
        against the database again immediately before deletion so a concurrent
        registration or provisioning job cannot be deleted accidentally.
        """
        candidates = self.provider_orphan_candidates(now=now)
        result: dict[str, Any] = {
            "status": "disabled",
            "candidates": len(candidates),
            "deleted": 0,
            "failed": 0,
        }
        if not _enabled("AURIX_ORPHAN_CLEANUP_ENABLED"):
            return result
        if not _enabled("AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED"):
            result["status"] = "mutations_disabled"
            return result
        if os.environ.get("AURIX_ORPHAN_CLEANUP_CONFIRMATION", "") != (
            "DELETE-UNREGISTERED-AURIX-NODES"
        ):
            raise InfrastructureError(
                "AURIX_ORPHAN_CLEANUP_CONFIRMATION must exactly equal "
                "DELETE-UNREGISTERED-AURIX-NODES"
            )
        if self.provider is None or not callable(getattr(self.provider, "delete_droplet", None)):
            raise InfrastructureError("DigitalOcean provider does not support Droplet deletion")
        result["status"] = "completed"
        for candidate in candidates:
            provider_id = str(candidate["provider_resource_id"])
            with self.database.connect() as connection:
                protected = connection.execute(
                    """SELECT 1 FROM outline_servers
                       WHERE provider_resource_id = ?
                       UNION ALL
                       SELECT 1 FROM infrastructure_jobs
                       WHERE provider_resource_id = ?
                         AND status NOT IN ('failed', 'completed')
                       LIMIT 1""",
                    (provider_id, provider_id),
                ).fetchone()
            if protected is not None:
                continue
            try:
                self.provider.delete_droplet(provider_id)
            except Exception as exc:
                result["failed"] += 1
                now_text = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
                with self.database.connect() as connection:
                    self.database.begin_write(connection)
                    connection.execute(
                        """INSERT INTO infrastructure_events
                           (id, event_type, metadata_json, created_at)
                           VALUES (?, 'provider_orphan_delete_failed', ?, ?)""",
                        (
                            uuid.uuid4().hex,
                            json.dumps(
                                {
                                    "provider_resource_id": provider_id,
                                    "error_type": type(exc).__name__,
                                },
                                sort_keys=True,
                            ),
                            now_text,
                        ),
                    )
                continue
            result["deleted"] += 1
            now_text = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                connection.execute(
                    """INSERT INTO infrastructure_events
                       (id, event_type, metadata_json, created_at)
                       VALUES (?, 'provider_orphan_deleted', ?, ?)""",
                    (
                        uuid.uuid4().hex,
                        json.dumps(
                            {
                                "provider_resource_id": provider_id,
                                "observation_count": candidate["observation_count"],
                            },
                            sort_keys=True,
                        ),
                        now_text,
                    ),
                )
        return result

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

    def _auto_enrollment_payload(
        self, *, job_id: str, specification: dict[str, Any]
    ) -> tuple[str, str] | None:
        """Build one-time user-data and return ``(token, node_id)`` when enabled."""
        if not _enabled("AURIX_FLEET_AUTO_REGISTRATION_ENABLED"):
            return None
        if not _enabled("AURIX_FLEET_REGISTRATION_ENABLED"):
            raise InfrastructureError(
                "automatic node registration requires AURIX_FLEET_REGISTRATION_ENABLED=1"
            )
        registration_url = os.environ.get("AURIX_FLEET_REGISTRATION_URL", "").strip()
        enrollment_key = os.environ.get("AURIX_FLEET_ENROLLMENT_KEY", "").strip()
        if not registration_url or not enrollment_key:
            raise InfrastructureError(
                "automatic node registration requires AURIX_FLEET_REGISTRATION_URL "
                "and AURIX_FLEET_ENROLLMENT_KEY"
            )
        try:
            validate_enrollment_key(enrollment_key)
        except EnrollmentError as exc:
            raise InfrastructureError("automatic node registration encryption key is invalid") from exc
        source = os.environ.get("AURIX_FLEET_CONTROL_PLANE_SOURCE", "").strip()
        if not source:
            raise InfrastructureError(
                "automatic node registration requires AURIX_FLEET_CONTROL_PLANE_SOURCE"
            )
        token = generate_token()
        # Keep the manifest identifier within its 24-character contract while
        # avoiding collisions from jobs that share a long prefix.
        node_id = "auto-" + hashlib.sha256(str(job_id).strip().encode()).hexdigest()[:18]
        try:
            bootstrap_script = (Path(__file__).resolve().parent / "deploy" / "node_bootstrap.sh").read_bytes()
            rendered = render_user_data(
                bootstrap_script=bootstrap_script,
                registration_url=registration_url,
                token=token,
                job_id=str(job_id),
                node_id=node_id,
                control_plane_source=source,
                api_port=int(os.environ.get("AURIX_SCALE_API_PORT", "61603")),
                keys_port=int(os.environ.get("AURIX_SCALE_KEYS_PORT", "443")),
                ssh_port=int(os.environ.get("AURIX_SCALE_SSH_PORT", "22")),
                swap_mb=int(os.environ.get("AURIX_SCALE_SWAP_MB", "1024")),
                installer_url=os.environ.get("AURIX_OUTLINE_INSTALLER_URL", ""),
                installer_sha256=os.environ.get("AURIX_OUTLINE_INSTALLER_SHA256", ""),
            )
        except (OSError, ValueError, EnrollmentError) as exc:
            raise InfrastructureError(f"automatic node registration payload is invalid: {type(exc).__name__}") from exc
        specification["user_data"] = rendered
        specification["tags"] = [
            "aurix-vpn-node",
            "aurix-awaiting-verification",
            "aurix-auto-enrollment",
        ]
        # Keep the encryption key out of the provider payload.  It is used by
        # the control-plane registration endpoint and worker only.
        return token, node_id

    def execute_provision(self, job_id: str) -> dict[str, Any]:
        if not _enabled("AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED"):
            raise InfrastructureError("Infrastructure mutations are disabled")
        auto_enrollment: tuple[str, str] | None = None
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
            lock_clause = (
                " FOR UPDATE SKIP LOCKED"
                if connection.__class__.__name__ == "_PostgresConnection"
                else ""
            )
            job = connection.execute(
                "SELECT * FROM infrastructure_jobs WHERE id = ? AND status = 'pending'"
                + lock_clause,
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
            specification = {
                "name": f"aurix-vpn-{specification['region']}-{job_id[:8]}",
                "region": specification["region"],
                "size": specification["size"],
                "image": specification["image"],
                "tags": ["aurix-vpn-node", "aurix-awaiting-verification"],
            }
            # Attach the pre-registered automation public key(s) at creation;
            # a password delivered out-of-band cannot support unattended,
            # repeatable bootstrap.  This list contains provider key IDs or
            # fingerprints, never private key material.
            specification["ssh_keys"] = self._provider_ssh_key_ids()
            try:
                auto_enrollment = self._auto_enrollment_payload(
                    job_id=str(job_id), specification=specification
                )
                if auto_enrollment is not None:
                    create_pending_enrollment(
                        self.database,
                        job_id=str(job_id),
                        token=auto_enrollment[0],
                        now=datetime.now(UTC),
                        connection=connection,
                    )
            except EnrollmentError as exc:
                raise InfrastructureError(
                    f"automatic node enrollment could not be prepared: {type(exc).__name__}"
                ) from exc
            connection.execute(
                """UPDATE infrastructure_jobs SET status = 'running', attempts = attempts + 1,
                          locked_at = ? WHERE id = ?""",
                (datetime.now(UTC).isoformat(), job_id),
            )
        try:
            droplet = self.provider.create_droplet(specification)  # type: ignore[union-attr]
        except Exception as exc:
            # A network timeout can happen after DigitalOcean has accepted the
            # POST. Search by the unique generated name/tag before marking the
            # job failed, otherwise an automatic retry could create a second
            # billable node.
            try:
                recovered = self._find_created_droplet(str(specification["name"]))
            except Exception:
                recovered = None
            if recovered is not None:
                actions = recovered.get("action_ids") or []
                recovered_id = str(recovered["id"])
                recovered_action = str(actions[0]) if actions else None
                now_text = datetime.now(UTC).isoformat()
                with self.database.connect() as connection:
                    self.database.begin_write(connection)
                    connection.execute(
                        """UPDATE infrastructure_jobs
                              SET status = 'running', provider_resource_id = ?,
                                  provider_action_id = ?, locked_at = ?, last_error = ?
                            WHERE id = ?""",
                        (
                            recovered_id,
                            recovered_action,
                            now_text,
                            "create response ambiguous; recovered by exact name",
                            job_id,
                        ),
                    )
                    connection.execute(
                        """INSERT INTO infrastructure_events
                           (id, infrastructure_job_id, event_type, metadata_json, created_at)
                           VALUES (?, ?, 'provider_create_recovered', ?, ?)""",
                        (
                            uuid.uuid4().hex,
                            job_id,
                            json.dumps(
                                {"provider_resource_id": recovered_id},
                                sort_keys=True,
                            ),
                            now_text,
                        ),
                    )
                return {
                    "job_id": job_id,
                    "droplet_id": recovered_id,
                    "status": "creating",
                    "recovered": True,
                }
            with self.database.connect() as connection:
                self.database.begin_write(connection)
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
            result = {"job_id": job_id, "status": str(row["status"])}
            if row["status"] == "awaiting_verification":
                with self.database.connect() as connection:
                    event = connection.execute(
                        """SELECT metadata_json FROM infrastructure_events
                           WHERE infrastructure_job_id = ? AND event_type = 'droplet_active'
                           ORDER BY created_at DESC LIMIT 1""",
                        (job_id,),
                    ).fetchone()
                if event is not None:
                    try:
                        metadata = json.loads(str(event["metadata_json"]))
                    except (TypeError, json.JSONDecodeError):
                        metadata = {}
                    result["public_ip"] = metadata.get("public_ip")
                result["provider_resource_id"] = str(row["provider_resource_id"] or "")
            return result
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
        return {
            "job_id": job_id,
            "status": "awaiting_verification",
            "public_ip": public_ip,
            "provider_resource_id": str(row["provider_resource_id"]),
        }

    def mark_provision_activated(self, job_id: str, node_id: str) -> dict[str, Any]:
        """Commit a verified endpoint activation exactly once.

        This method does not inspect or create provider resources.  Callers
        must complete the pinned-SSH/Outline reconciliation first; this is the
        durable state transition that records that verified hand-off.
        """
        normalized_node = str(node_id).strip()
        if not normalized_node:
            raise InfrastructureError("activated node id is required")
        now_text = datetime.now(UTC).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                """SELECT j.status, j.provider_resource_id,
                          s.provider_resource_id AS node_provider_resource_id
                   FROM infrastructure_jobs AS j
                   LEFT JOIN outline_servers AS s ON s.server_id = ?
                   WHERE j.id = ? AND j.operation = 'provision'""",
                (normalized_node, job_id),
            ).fetchone()
            if row is None:
                raise InfrastructureError("provisioning job does not exist")
            status = str(row["status"])
            if status == "completed":
                return {"job_id": job_id, "status": status, "node_id": normalized_node}
            if status != "awaiting_verification":
                raise InfrastructureError(
                    f"provisioning job is not awaiting verification (status={status})"
                )
            if not row["provider_resource_id"] or str(row["provider_resource_id"]) != str(
                row["node_provider_resource_id"] or ""
            ):
                raise InfrastructureError(
                    "activated node is not registered for this provider resource"
                )
            connection.execute(
                """UPDATE infrastructure_jobs
                   SET status = 'completed', completed_at = ?, locked_at = NULL, last_error = NULL
                   WHERE id = ? AND status = 'awaiting_verification'""",
                (now_text, job_id),
            )
            connection.execute(
                """INSERT INTO infrastructure_events
                   (id, infrastructure_job_id, server_id, event_type, metadata_json, created_at)
                   VALUES (?, ?, ?, 'endpoint_activated', ?, ?)""",
                (
                    uuid.uuid4().hex,
                    job_id,
                    normalized_node,
                    json.dumps({"node_id": normalized_node}, sort_keys=True),
                    now_text,
                ),
            )
        return {"job_id": job_id, "status": "completed", "node_id": normalized_node}
