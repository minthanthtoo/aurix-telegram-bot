"""Provider provisioning and activation handlers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta
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
from infrastructure_support import InfrastructureError, UTC, _enabled


class FleetProvisioningMixin:
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
            existing = self.provisioning_repository.job_by_fingerprint(connection, fingerprint)
        if existing is not None:
            return str(existing["id"])
        provider_inventory = self._provider_inventory()
        node_count = self._known_node_count(provider_inventory)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            existing = self.provisioning_repository.job_by_fingerprint(connection, fingerprint)
            if existing is not None:
                return str(existing["id"])
            if node_count >= max(1, int(os.environ.get("AURIX_MAX_VPN_NODES", "3"))):
                raise InfrastructureError("Configured VPN node limit has been reached")
            day_start = current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            created_today = self.provisioning_repository.created_today(connection, day_start)
            if created_today >= max(
                1, int(os.environ.get("AURIX_MAX_NODE_CREATIONS_PER_DAY", "1"))
            ):
                raise InfrastructureError("Daily VPN node creation limit has been reached")
            latest = self.provisioning_repository.latest_provision(connection)
            cooldown = max(0, int(os.environ.get("AURIX_NODE_CREATION_COOLDOWN_SECONDS", "86400")))
            if latest is not None:
                latest_at = datetime.fromisoformat(str(latest["created_at"])).astimezone(UTC)
                if current < latest_at + timedelta(seconds=cooldown):
                    raise InfrastructureError("VPN node creation cooldown is still active")
            if self.provisioning_repository.active_provision(connection) is not None:
                raise InfrastructureError("Another server provisioning job is already active")
            job_id = uuid.uuid4().hex
            self.provisioning_repository.insert_job(
                connection, job_id, fingerprint, now_text
            )
            self.provisioning_repository.insert_event(
                connection,
                event_id=uuid.uuid4().hex,
                job_id=job_id,
                event_type="provision_requested",
                metadata=request,
                now_text=now_text,
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
            pending = self.provisioning_repository.pending_job(connection, job_id)
        if pending is None:
            raise InfrastructureError("Provisioning job is not pending")
        provider_inventory = self._provider_inventory()
        self._budget_guard(existing_nodes=self._known_node_count(provider_inventory))
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            job = self.provisioning_repository.pending_job(connection, job_id, lock=True)
            event = self.provisioning_repository.provision_event(
                connection, job_id, "provision_requested"
            )
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
            self.provisioning_repository.mark_running(
                connection, job_id, datetime.now(UTC).isoformat()
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
                    self.provisioning_repository.recover_created(
                        connection, job_id, recovered_id, recovered_action, now_text
                    )
                    self.provisioning_repository.insert_event(
                        connection,
                        event_id=uuid.uuid4().hex,
                        job_id=job_id,
                        event_type="provider_create_recovered",
                        metadata={"provider_resource_id": recovered_id},
                        now_text=now_text,
                    )
                return {
                    "job_id": job_id,
                    "droplet_id": recovered_id,
                    "status": "creating",
                    "recovered": True,
                }
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                self.provisioning_repository.mark_failed(
                    connection, job_id, type(exc).__name__
                )
            raise
        actions = droplet.get("action_ids") or []
        with self.database.connect() as connection:
            self.provisioning_repository.set_provider_ids(
                connection, job_id, str(droplet["id"]), str(actions[0]) if actions else None
            )
        return {"job_id": job_id, "droplet_id": str(droplet["id"]), "status": "creating"}

    def reconcile_provision(self, job_id: str) -> dict[str, Any]:
        """Observe provider state and stop before secret endpoint activation."""
        if self.provider is None:
            raise InfrastructureError("DigitalOcean provider is not configured")
        with self.database.connect() as connection:
            row = self.provisioning_repository.job(connection, job_id)
        if row is None:
            raise InfrastructureError("Provisioning job does not exist")
        if row["status"] in ("failed", "awaiting_verification", "completed"):
            result = {"job_id": job_id, "status": str(row["status"])}
            if row["status"] == "awaiting_verification":
                with self.database.connect() as connection:
                    event = self.provisioning_repository.latest_event(
                        connection, job_id, "droplet_active"
                    )
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
                    self.provisioning_repository.mark_action_failed(connection, job_id)
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
            self.provisioning_repository.mark_awaiting_verification(connection, job_id)
            self.provisioning_repository.insert_event(
                connection,
                event_id=uuid.uuid4().hex,
                job_id=job_id,
                event_type="droplet_active",
                metadata={"public_ip": public_ip},
                now_text=now_text,
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
            row = self.provisioning_repository.activated_job(
                connection, job_id, normalized_node
            )
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
            self.provisioning_repository.mark_completed(connection, job_id, now_text)
            self.provisioning_repository.insert_event(
                connection,
                event_id=uuid.uuid4().hex,
                job_id=job_id,
                server_id=normalized_node,
                event_type="endpoint_activated",
                metadata={"node_id": normalized_node},
                now_text=now_text,
            )
        return {"job_id": job_id, "status": "completed", "node_id": normalized_node}
