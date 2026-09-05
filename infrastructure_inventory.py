"""Provider inventory and orphan-safety handlers."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from infrastructure_support import InfrastructureError, UTC, _enabled


class FleetInventoryMixin:
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
