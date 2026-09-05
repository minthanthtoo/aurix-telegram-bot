"""Paid-order, wallet, provisioning, and notification application service."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from commerce_models import (
    UTC,
    ApprovalResult,
    CommerceError,
    OrderResult,
    Plan,
    _new_id,
    _human_bytes,
    _normalize_reference,
    _now_text,
)
from commerce_repositories import _PostgresConnection
from ports import OutlineGateway, ReceiptStorageGateway
from repositories import RepositoryDatabase
from commerce_worker import CommerceWorkerMixin
from connectivity_registry import ConnectivityRegistry
from receipt_rules import evaluate_receipt_candidate, load_recipient_profiles
from receipt_fingerprint import (
    NEAR_DUPLICATE_DISTANCE,
    fingerprint_distance,
    receipt_perceptual_hash,
)
from supabase_storage import NullReceiptStorage

LOCAL_PAYMENT_METHODS = frozenset({"kbzpay", "wavepay", "ayapay", "uabpay", "cbpay"})


class CommerceService(CommerceWorkerMixin):
    """Commerce state machine and idempotent Outline job processor."""

    def __init__(
        self,
        database: RepositoryDatabase,
        outline: OutlineGateway,
        access_url_key: bytes | str,
        allow_legacy_text_approval: bool = False,
        receipt_storage: ReceiptStorageGateway | None = None,
        receipt_storage_required: bool = False,
    ):
        self.database = database
        self.outline = outline
        # Kept only for controlled migration tests. Public deployments must
        # require verified screenshot evidence or a wallet reservation.
        self.allow_legacy_text_approval = bool(allow_legacy_text_approval)
        self.receipt_storage = receipt_storage or NullReceiptStorage()
        self.receipt_storage_required = bool(receipt_storage_required)
        self.receipt_recipient_profiles = load_recipient_profiles()
        self._server_metrics_cache: dict[str, dict[str, Any]] = {}
        try:
            self.access_url_cipher = Fernet(access_url_key)
        except (TypeError, ValueError) as exc:
            raise ValueError("AURIX_ACCESS_URL_KEY must be a Fernet key") from exc

    def _encrypt_access_url(self, access_url: str) -> str:
        return self.access_url_cipher.encrypt(access_url.encode()).decode()

    def _decrypt_access_url(self, encrypted: str | None) -> str | None:
        if not encrypted:
            return None
        try:
            return self.access_url_cipher.decrypt(encrypted.encode()).decode()
        except (InvalidToken, UnicodeDecodeError, ValueError):
            return None

    @staticmethod
    def _receipt_storage_extension(mime_type: str) -> str:
        normalized = str(mime_type or "").lower().split(";", 1)[0].strip()
        return {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "image/gif": "gif",
        }.get(normalized, "bin")

    @staticmethod
    def _receipt_storage_path(order_id: str, evidence_id: str, mime_type: str) -> str:
        # Order/evidence IDs are generated UUIDs. Keep this defensive because
        # old/imported order IDs may contain unexpected characters.
        safe_order = re.sub(r"[^A-Za-z0-9_-]+", "-", str(order_id)).strip("-_")[:96]
        safe_evidence = re.sub(r"[^A-Za-z0-9_-]+", "-", str(evidence_id)).strip("-_")[:96]
        extension = CommerceService._receipt_storage_extension(mime_type)
        return f"orders/{safe_order or 'unknown'}/{safe_evidence or _new_id()}.{extension}"

    def _storage_is_configured(self) -> bool:
        return bool(getattr(self.receipt_storage, "configured", False))

    def _storage_bucket(self) -> str | None:
        bucket = getattr(self.receipt_storage, "bucket", None)
        return str(bucket) if bucket else None

    @staticmethod
    def _lock_order(connection: Any, order_id: str) -> None:
        """Serialize aggregate mutations on PostgreSQL as well as SQLite.

        SQLite already serializes writers. PostgreSQL needs an explicit row
        lock because payment, receipt, approval and refund requests can arrive
        concurrently from Telegram retries or two administrators.
        """
        if isinstance(connection, _PostgresConnection):
            connection.execute(
                "SELECT id FROM orders WHERE id = ? FOR UPDATE", (order_id,)
            ).fetchone()

    @staticmethod
    def _assert_no_active_promo(connection: Any, telegram_id: int) -> None:
        if not isinstance(connection, _PostgresConnection):
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'giveaway_claims'"
            ).fetchone()
            if table is None:
                return
        now_text = _now_text()
        winner = connection.execute(
            """SELECT 1
               FROM giveaway_claims g
               JOIN giveaway_campaigns c ON c.code = g.campaign_code
               JOIN keys k ON k.id = g.key_id
               WHERE g.telegram_id = ?
                 AND c.active = 1
                 AND (c.starts_at IS NULL OR c.starts_at <= ?)
                 AND (c.ends_at IS NULL OR c.ends_at > ?)
                 AND k.status IN ('active', 'revoke_failed')
                 AND k.expires_at > ?
                 AND k.quota_reason IS NULL
               LIMIT 1""",
            (telegram_id, now_text, now_text, now_text),
        ).fetchone()
        if winner is not None:
            raise CommerceError(
                "Your promo VPN gift is currently active. Normal plans return automatically "
                "when the gift or promo season ends."
            )

    def initialize(self) -> None:
        self.database.initialize()

    def queue_infrastructure_provision(
        self,
        requested_by: int,
        now: datetime | None = None,
        *,
        snapshot: dict[str, Any] | None = None,
    ) -> str:
        """Queue an owner-approved, allowlisted node request for the worker.

        This method records intent only. The separate infrastructure worker
        still enforces the provider token, budget, node limit and mutation gate.
        """
        if os.environ.get("AURIX_INFRASTRUCTURE_QUEUE_ENABLED", "0").strip().lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise CommerceError("Infrastructure provisioning is not enabled for this deployment.")
        current = now or datetime.now(UTC)
        capacity = snapshot if snapshot is not None else self.capacity_snapshot(current)
        advice = capacity.get("scale_advice") or {}
        if str(advice.get("status") or "") not in {"prepare", "urgent"}:
            raise CommerceError("A scale-out intent is allowed only in Prepare or Urgent posture.")
        if not bool(advice.get("observation_ready")):
            required = int(advice.get("required_observations") or 2)
            observed = int(advice.get("consecutive_observations") or 0)
            raise CommerceError(
                f"Scale-out evidence is not ready: collect {required} separate observations "
                f"({observed}/{required} recorded)."
            )
        from infrastructure import FleetController

        controller = FleetController(self.database)
        return controller.queue_provision(
            region=os.environ.get("AURIX_SCALE_REGION", "sgp1").strip(),
            size=os.environ.get("AURIX_SCALE_DROPLET_SIZE", "s-1vcpu-1gb").strip(),
            image=os.environ.get("AURIX_SCALE_DROPLET_IMAGE", "ubuntu-24-04-x64").strip(),
            requested_by=int(requested_by),
            now=current,
        )

    def auto_queue_scale_out(
        self,
        *,
        snapshot: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Optionally turn sustained scale evidence into one durable intent.

        This method only writes an idempotent local infrastructure job. The
        separate provider worker still requires its own token, budget, and
        ``AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED`` gate before any VM change.
        The default is disabled, preserving the owner-button workflow.
        """
        enabled = os.environ.get("AURIX_INFRASTRUCTURE_AUTO_QUEUE_ENABLED", "0").strip().lower()
        if enabled not in {"1", "true", "yes", "on"}:
            return {"status": "disabled"}
        if os.environ.get("AURIX_INFRASTRUCTURE_QUEUE_ENABLED", "0").strip().lower() not in {
            "1", "true", "yes", "on",
        }:
            return {"status": "queue_disabled"}
        current = (now or datetime.now(UTC)).astimezone(UTC)
        try:
            job_id = self.queue_infrastructure_provision(
                int(os.environ.get("AURIX_SYSTEM_ACTOR_ID", "0")),
                current,
                snapshot=snapshot,
            )
        except (CommerceError, ValueError) as exc:
            # “Not ready” is an expected state on most maintenance passes and
            # should not mark the whole heartbeat failed.
            return {"status": "blocked", "reason": type(exc).__name__}
        return {"status": "queued", "job_id": job_id}

    def register_outline_servers(
        self,
        labels: dict[str, str] | None = None,
        *,
        provider_resource_ids: dict[str, str] | None = None,
        endpoint_metadata: dict[str, dict[str, str]] | None = None,
    ) -> None:
        """Persist non-secret metadata for every environment-configured server."""
        labels = labels or {}
        provider_resource_ids = provider_resource_ids or {}
        endpoint_metadata = endpoint_metadata or {}
        server_ids = (
            self.outline.server_ids()
            if callable(getattr(self.outline, "server_ids", None))
            else ("default",)
        )
        now_text = _now_text()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            configured_resources = [
                str(provider_resource_ids[server_id])
                for server_id in server_ids
                if provider_resource_ids.get(server_id)
            ]
            if len(configured_resources) != len(set(configured_resources)):
                raise CommerceError(
                    "Each Outline server must use a different provider resource ID"
                )
            for server_id in server_ids:
                provider_resource_id = provider_resource_ids.get(server_id)
                if provider_resource_id:
                    existing = connection.execute(
                        "SELECT server_id FROM outline_servers WHERE provider_resource_id = ?",
                        (provider_resource_id,),
                    ).fetchone()
                    if existing is not None and str(existing["server_id"]) != str(server_id):
                        raise CommerceError(
                            f"Provider resource {provider_resource_id} is already registered as "
                            f"server {existing['server_id']!r}; keep that stable server ID in "
                            "OUTLINE_SERVERS_JSON"
                        )
                connection.execute(
                    """INSERT INTO outline_servers
                       (server_id, label, provider_resource_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(server_id) DO UPDATE SET
                         label = excluded.label,
                         provider_resource_id = COALESCE(excluded.provider_resource_id,
                                                         outline_servers.provider_resource_id),
                         updated_at = excluded.updated_at""",
                    (
                        server_id,
                        labels.get(server_id, server_id),
                        provider_resource_id,
                        now_text,
                        now_text,
                    ),
                )
                metadata = endpoint_metadata.get(server_id) or {}
                state_row = connection.execute(
                    "SELECT lifecycle_state, health_status FROM outline_servers WHERE server_id = ?",
                    (server_id,),
                ).fetchone()
                ConnectivityRegistry.sync_outline_endpoint(
                    connection,
                    server_id=str(server_id),
                    label=str(labels.get(server_id, server_id)),
                    provider=str(metadata.get("provider") or "manual"),
                    region=str(metadata.get("region") or "unknown"),
                    lifecycle_state=str(state_row["lifecycle_state"] if state_row else "active"),
                    health_status=str(state_row["health_status"] if state_row else "unknown"),
                    now_text=now_text,
                )
            placeholders = ",".join("?" for _ in server_ids)
            connection.execute(
                f"""UPDATE outline_servers
                       SET enabled = 0,
                           lifecycle_state = 'retired',
                           lifecycle_reason = COALESCE(lifecycle_reason, 'removed from OUTLINE_SERVERS_JSON'),
                           lifecycle_changed_at = CASE
                               WHEN lifecycle_state != 'retired' OR enabled = 1 THEN ?
                               ELSE lifecycle_changed_at
                           END,
                           updated_at = ?
                     WHERE server_id NOT IN ({placeholders})""",
                (now_text, now_text, *server_ids),
            )
            default_server_id = getattr(self.outline, "default_server_id", server_ids[0])
            connection.execute(
                "UPDATE subscriptions SET server_id = ? WHERE server_id IS NULL",
                (default_server_id,),
            )
            connection.execute(
                "UPDATE paid_vpn_keys SET server_id = ? WHERE server_id IS NULL",
                (default_server_id,),
            )
            if self._table_exists(connection, "keys"):
                connection.execute(
                    "UPDATE keys SET server_id = ? WHERE server_id IS NULL",
                    (default_server_id,),
                )
                if "primary" not in server_ids:
                    connection.execute(
                        "UPDATE keys SET server_id = ? WHERE server_id = 'primary'",
                        (default_server_id,),
                    )
            connection.execute(
                """UPDATE orders SET server_id = ?, capacity_reserved_until = COALESCE(capacity_reserved_until, ?)
                   WHERE server_id IS NULL AND status IN ('awaiting_payment', 'payment_submitted')""",
                (default_server_id, _now_text(datetime.now(UTC) + timedelta(hours=24))),
            )
            ConnectivityRegistry.rebuild_from_legacy(
                connection,
                now_text=now_text,
                encrypt_access_url=self._normalize_legacy_access_url,
            )

    def _normalize_legacy_access_url(self, value: str) -> str | None:
        """Return a registry-safe ciphertext for a legacy access URL.

        Current rows already contain ciphertext. A legacy plaintext URL is
        encrypted exactly once. Anything else is retained only when this
        service can decrypt it with the active key; unknown values are omitted
        rather than copied as potentially sensitive plaintext.
        """
        raw = str(value or "")
        if not raw:
            return None
        if raw.startswith("ss://"):
            return self._encrypt_access_url(raw)
        if self._decrypt_access_url(raw) is not None:
            return raw
        return None

    def _outline_client(self, server_id: str | None = None) -> Any:
        getter = getattr(self.outline, "client", None)
        return getter(server_id) if callable(getter) else self.outline

    def set_server_lifecycle(
        self,
        server_id: str,
        lifecycle_state: str,
        actor_id: int,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Change an endpoint's admission lifecycle without touching its VM.

        ``draining`` stops new assignments while existing credentials continue
        to work. ``retired`` is only accepted after local entitlements,
        reservations, pending intents, and the last authoritative remote
        inventory are empty. Provider deletion remains a separate, explicit
        operation outside this method.
        """
        state = str(lifecycle_state or "").strip().lower()
        if state not in {"active", "draining", "retired"}:
            raise CommerceError("Endpoint lifecycle must be active, draining or retired")
        server = str(server_id or "").strip()
        if not server:
            raise CommerceError("Endpoint identity is required")
        now_text = _now_text()
        clean_reason = str(reason or "").strip()[:512] or None
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                "SELECT * FROM outline_servers WHERE server_id = ?",
                (server,),
            ).fetchone()
            if row is None:
                raise CommerceError("Outline server is not configured")
            previous = str(row.get("lifecycle_state") if hasattr(row, "get") else row["lifecycle_state"] or "active")
            if previous == state:
                return {
                    "server_id": server,
                    "lifecycle_state": state,
                    "previous_state": previous,
                    "changed": False,
                }
            if state == "retired":
                blockers: list[str] = []
                active_free = int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM keys WHERE server_id = ? AND status IN ('active', 'revoke_failed')",
                        (server,),
                    ).fetchone()["n"]
                ) if self._table_exists(connection, "keys") else 0
                active_paid = int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM paid_vpn_keys WHERE server_id = ? AND status IN ('active', 'revoke_failed')",
                        (server,),
                    ).fetchone()["n"]
                )
                pending_orders = int(
                    connection.execute(
                        """SELECT COUNT(*) AS n FROM orders
                           WHERE server_id = ? AND status IN ('awaiting_payment', 'payment_submitted')""",
                        (server,),
                    ).fetchone()["n"]
                )
                pending_subscriptions = int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM subscriptions WHERE server_id = ? AND status IN ('pending', 'active')",
                        (server,),
                    ).fetchone()["n"]
                )
                pending_intents = 0
                if self._table_exists(connection, "free_provisioning_intents"):
                    pending_intents += int(
                        connection.execute(
                            """SELECT COUNT(*) AS n FROM free_provisioning_intents
                               WHERE server_id = ? AND status IN ('pending', 'running')""",
                            (server,),
                        ).fetchone()["n"]
                    )
                if active_free:
                    blockers.append(f"{active_free} active free/promo key(s)")
                if active_paid:
                    blockers.append(f"{active_paid} active paid key(s)")
                if pending_orders:
                    blockers.append(f"{pending_orders} open order(s)")
                if pending_subscriptions:
                    blockers.append(f"{pending_subscriptions} active/pending subscription(s)")
                if pending_intents:
                    blockers.append(f"{pending_intents} pending provisioning intent(s)")
                remote_count = row["remote_key_count"]
                orphan_count = int(row["remote_orphan_key_count"] or 0)
                if remote_count is None:
                    blockers.append("remote inventory has not been reconciled")
                elif int(remote_count or 0) != 0:
                    blockers.append(f"remote inventory still has {int(remote_count)} key(s)")
                if orphan_count:
                    blockers.append(f"{orphan_count} unreviewed remote key(s)")
                if blockers:
                    raise CommerceError("Endpoint cannot be retired yet: " + "; ".join(blockers))
            enabled = 1 if state in {"active", "draining"} else 0
            connection.execute(
                """UPDATE outline_servers
                      SET enabled = ?, lifecycle_state = ?, lifecycle_reason = ?,
                          lifecycle_changed_at = ?, updated_at = ?
                    WHERE server_id = ?""",
                (enabled, state, clean_reason, now_text, now_text, server),
            )
            ConnectivityRegistry.sync_outline_health(
                connection,
                server_id=server,
                lifecycle_state=state,
                health_status=str(row["health_status"] or "unknown"),
                now_text=now_text,
            )
            self._audit(
                connection,
                "server_lifecycle_changed",
                "outline_server",
                server,
                "owner",
                str(actor_id),
                {
                    "previous_state": previous,
                    "lifecycle_state": state,
                    "reason": clean_reason,
                },
            )
        return {
            "server_id": server,
            "lifecycle_state": state,
            "previous_state": previous,
            "changed": True,
            "changed_at": now_text,
            "reason": clean_reason,
        }

    def server_drain_readiness(self, server_id: str) -> dict[str, Any]:
        """Return a non-mutating, safe drain/retirement checklist."""
        server = str(server_id or "").strip()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT lifecycle_state, enabled, remote_key_count, remote_orphan_key_count, last_synced_at FROM outline_servers WHERE server_id = ?",
                (server,),
            ).fetchone()
            if row is None:
                raise CommerceError("Outline server is not configured")
            active_free = int(connection.execute(
                "SELECT COUNT(*) AS n FROM keys WHERE server_id = ? AND status IN ('active', 'revoke_failed')",
                (server,),
            ).fetchone()["n"]) if self._table_exists(connection, "keys") else 0
            active_paid = int(connection.execute(
                "SELECT COUNT(*) AS n FROM paid_vpn_keys WHERE server_id = ? AND status IN ('active', 'revoke_failed')",
                (server,),
            ).fetchone()["n"])
            open_orders = int(connection.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE server_id = ? AND status IN ('awaiting_payment', 'payment_submitted')",
                (server,),
            ).fetchone()["n"])
            pending_intents = int(connection.execute(
                "SELECT COUNT(*) AS n FROM free_provisioning_intents WHERE server_id = ? AND status IN ('pending', 'running')",
                (server,),
            ).fetchone()["n"]) if self._table_exists(connection, "free_provisioning_intents") else 0
        blockers = []
        if active_free:
            blockers.append("active_free_keys")
        if active_paid:
            blockers.append("active_paid_keys")
        if open_orders:
            blockers.append("open_orders")
        if pending_intents:
            blockers.append("pending_provisioning")
        if row["remote_key_count"] is None:
            blockers.append("inventory_not_reconciled")
        elif int(row["remote_key_count"] or 0):
            blockers.append("remote_keys_present")
        if int(row["remote_orphan_key_count"] or 0):
            blockers.append("unreviewed_remote_keys")
        return {
            "server_id": server,
            "lifecycle_state": str(row["lifecycle_state"] or "active"),
            "enabled": bool(row["enabled"]),
            "active_free_keys": active_free,
            "active_paid_keys": active_paid,
            "open_orders": open_orders,
            "pending_provisioning": pending_intents,
            "remote_key_count": row["remote_key_count"],
            "remote_orphan_key_count": int(row["remote_orphan_key_count"] or 0),
            "last_synced_at": row["last_synced_at"],
            "ready_to_retire": not blockers,
            "blockers": blockers,
        }

    @staticmethod
    def _table_exists(connection: Any, name: str) -> bool:
        if connection.__class__.__name__ == "_PostgresConnection":
            row = connection.execute("SELECT to_regclass(?) AS table_name", (f"public.{name}",)).fetchone()
            return bool(row and row["table_name"])
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone() is not None

    @staticmethod
    def _metric_bytes(value: Any) -> int | None:
        if isinstance(value, dict):
            value = value.get("bytes", value.get("data"))
            if isinstance(value, dict):
                value = value.get("bytes")
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _health_threshold(name: str, default: int) -> int:
        """Read a bounded hysteresis threshold from the deployment environment."""
        try:
            return max(1, min(10, int(os.environ.get(name, str(default)))))
        except (TypeError, ValueError):
            return default

    def _record_endpoint_health(
        self,
        connection: Any,
        server_id: str,
        observed_at: str,
        *,
        observed_status: str,
        latency_ms: float | None,
        remote_key_count: int | None = None,
        error_type: str | None = None,
    ) -> dict[str, Any]:
        """Persist one health probe and apply conservative state hysteresis.

        A single failed management call blocks new admission immediately by
        moving a healthy node to ``degraded``. Repeated failures make the
        state ``unreachable``; recovery needs independent successful probes.
        The unique timestamp makes repeated maintenance/UI calls idempotent.
        """
        if observed_status not in {"healthy", "unreachable"}:
            raise CommerceError("Invalid endpoint health observation")
        lock_clause = " FOR UPDATE" if connection.__class__.__name__ == "_PostgresConnection" else ""
        current = connection.execute(
            """SELECT health_status, health_success_streak,
                      health_failure_streak, health_state_changed_at, last_error
                 FROM outline_servers WHERE server_id = ?""" + lock_clause,
            (server_id,),
        ).fetchone()
        if current is None:
            raise CommerceError(f"Unknown Outline server: {server_id}")
        previous = str(current["health_status"] or "unknown")
        if self._table_exists(connection, "endpoint_health_observations"):
            duplicate = connection.execute(
                """SELECT state_after FROM endpoint_health_observations
                   WHERE server_id = ? AND probe_type = 'management_inventory'
                     AND observed_at = ?""",
                (server_id, observed_at),
            ).fetchone()
            if duplicate is not None:
                return {
                    "state": str(duplicate["state_after"]),
                    "success_streak": int(current["health_success_streak"] or 0),
                    "failure_streak": int(current["health_failure_streak"] or 0),
                    "duplicate": True,
                }
        success_streak = int(current["health_success_streak"] or 0)
        failure_streak = int(current["health_failure_streak"] or 0)
        recovery_threshold = self._health_threshold(
            "AURIX_ENDPOINT_RECOVERY_THRESHOLD", 2
        )
        failure_threshold = self._health_threshold(
            "AURIX_ENDPOINT_FAILURE_THRESHOLD", 3
        )
        if observed_status == "healthy":
            success_streak += 1
            failure_streak = 0
            state_after = (
                "healthy"
                if previous in {"unknown", "healthy"}
                or success_streak >= recovery_threshold
                else previous
            )
        else:
            failure_streak += 1
            success_streak = 0
            state_after = (
                "unreachable"
                if failure_streak >= failure_threshold
                else "degraded"
            )
        changed_at = (
            observed_at
            if state_after != previous
            else current["health_state_changed_at"]
        )
        last_error = (
            None
            if observed_status == "healthy" and state_after == "healthy"
            else (str(error_type or current["last_error"] or "")[:128] or None)
        )
        connection.execute(
            """UPDATE outline_servers
                  SET health_status = ?, health_success_streak = ?,
                      health_failure_streak = ?, health_state_changed_at = ?,
                      health_last_latency_ms = ?, last_error = ?,
                      last_synced_at = ?, updated_at = ?
                WHERE server_id = ?""",
            (
                state_after,
                success_streak,
                failure_streak,
                changed_at,
                latency_ms,
                last_error,
                observed_at,
                observed_at,
                server_id,
            ),
        )
        if self._table_exists(connection, "endpoint_health_observations"):
            connection.execute(
                """INSERT INTO endpoint_health_observations
                   (id, server_id, probe_type, observed_at, observed_status,
                    state_before, state_after, latency_ms, remote_key_count,
                    error_type, created_at)
                   VALUES (?, ?, 'management_inventory', ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(server_id, probe_type, observed_at) DO NOTHING""",
                (
                    _new_id(),
                    server_id,
                    observed_at,
                    observed_status,
                    previous,
                    state_after,
                    latency_ms,
                    remote_key_count,
                    str(error_type or "")[:128] or None,
                    observed_at,
                ),
            )
        return {
            "state": state_after,
            "success_streak": success_streak,
            "failure_streak": failure_streak,
            "duplicate": False,
        }

    def endpoint_health_history(
        self,
        server_id: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return safe, recent health evidence without endpoint credentials."""
        try:
            page_limit = max(1, min(200, int(limit)))
        except (TypeError, ValueError):
            page_limit = 20
        with self.database.connect() as connection:
            if not self._table_exists(connection, "endpoint_health_observations"):
                return []
            rows = connection.execute(
                """SELECT observed_at, observed_status, state_before, state_after,
                          latency_ms, remote_key_count, error_type
                     FROM endpoint_health_observations
                    WHERE server_id = ?
                    ORDER BY observed_at DESC LIMIT ?""",
                (server_id, page_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def connectivity_snapshot(self) -> list[dict[str, Any]]:
        """Return provider/region/transport dimensions without secrets."""
        with self.database.connect() as connection:
            return ConnectivityRegistry.endpoint_snapshot(connection)

    def endpoint_migration_jobs(
        self, *, limit: int = 50, include_completed: bool = False
    ) -> list[dict[str, Any]]:
        """Return safe endpoint-migration operations for the admin console.

        This is intentionally an operational view: it contains no management
        URLs, certificates, access URLs, or secret ciphertext.  Completed rows
        are omitted by default so the panel stays focused on work that still
        needs observation or retry.
        """
        try:
            page_limit = max(1, min(200, int(limit)))
        except (TypeError, ValueError):
            page_limit = 50
        statuses = (
            "('pending', 'creating', 'source_delete_pending', 'failed', 'cancelled')"
            if not include_completed
            else "('pending', 'creating', 'source_delete_pending', 'failed', 'cancelled', 'completed')"
        )
        with self.database.connect() as connection:
            if not self._table_exists(connection, "connectivity_migration_jobs"):
                return []
            rows = connection.execute(
                f"""SELECT id AS job_id, profile_kind, telegram_id,
                                  source_server_id, target_server_id,
                                  source_external_id, target_external_id,
                                  status AS job_status, attempts,
                                  source_used_bytes, quota_bytes,
                                  next_attempt_at, last_error,
                                  requested_by, created_at, updated_at,
                                  completed_at
                             FROM connectivity_migration_jobs
                            WHERE status IN {statuses}
                            ORDER BY CASE status
                                       WHEN 'failed' THEN 0
                                       WHEN 'source_delete_pending' THEN 1
                                       WHEN 'creating' THEN 2
                                       WHEN 'pending' THEN 3
                                       ELSE 4
                                     END,
                                     created_at DESC
                            LIMIT ?""",
                (page_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def queue_endpoint_migration(
        self,
        source_server_id: str,
        outline_key_id: str,
        target_server_id: str,
        requested_by: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Queue an idempotent credential move to a healthy endpoint.

        This records intent only.  The maintenance worker performs the remote
        create/cutover/delete sequence and never calls a provider VM API.
        Quota and expiry are copied from the authoritative local entitlement;
        the worker subtracts fresh source usage before creating the target.
        """
        source = str(source_server_id or "").strip()
        external_id = str(outline_key_id or "").strip()
        target = str(target_server_id or "").strip()
        if not source or not external_id or not target or source == target:
            raise CommerceError("Source, target, and Outline key identity are required")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = _now_text(current)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            if not ConnectivityRegistry.available(connection):
                raise CommerceError("Connectivity registry is not initialized")
            row = connection.execute(
                """SELECT c.credential_id, c.profile_id, c.status AS credential_status,
                          c.external_id, p.profile_kind, p.telegram_id, p.subscription_id,
                          se.outline_server_id AS source_server, se.endpoint_id AS source_endpoint,
                          te.outline_server_id AS target_server, te.endpoint_id AS target_endpoint,
                          te.status AS target_status, te.accepts_new_keys
                     FROM connectivity_credentials c
                     JOIN connectivity_profiles p ON p.profile_id = c.profile_id
                     JOIN connectivity_endpoints se ON se.endpoint_id = c.endpoint_id
                     JOIN connectivity_endpoints te ON te.outline_server_id = ?
                    WHERE se.outline_server_id = ? AND c.external_id = ?
                    LIMIT 1""",
                (target, source, external_id),
            ).fetchone()
            if row is None:
                raise CommerceError("Active credential is not registered on the source endpoint")
            if str(row["credential_status"]) != "active":
                raise CommerceError("Only an active credential can be migrated")
            if str(row["target_status"]) != "active" or not int(row["accepts_new_keys"] or 0):
                raise CommerceError("Target endpoint is not healthy and accepting new keys")
            kind = str(row["profile_kind"])
            entitlement = None
            if kind == "paid":
                entitlement = connection.execute(
                    """SELECT k.outline_key_id, k.status, k.quota_bytes,
                              s.expires_at, s.status AS subscription_status
                         FROM paid_vpn_keys k JOIN subscriptions s ON s.id = k.subscription_id
                        WHERE k.subscription_id = ? AND k.server_id = ? AND k.outline_key_id = ?""",
                    (str(row["subscription_id"]), source, external_id),
                ).fetchone()
                if entitlement is None or str(entitlement["status"]) != "active" or str(entitlement["subscription_status"]) != "active":
                    raise CommerceError("Paid entitlement is not active on the source endpoint")
            else:
                entitlement = connection.execute(
                    """SELECT outline_key_id, status, data_limit_bytes AS quota_bytes, expires_at
                         FROM keys
                        WHERE server_id = ? AND outline_key_id = ?""",
                    (source, external_id),
                ).fetchone() if self._table_exists(connection, "keys") else None
                if entitlement is None or str(entitlement["status"]) not in {"active", "revoke_failed"}:
                    raise CommerceError("Free/trial/promo entitlement is not active on the source endpoint")
            try:
                expires_at = datetime.fromisoformat(str(entitlement["expires_at"])).astimezone(UTC)
            except (TypeError, ValueError) as exc:
                raise CommerceError("Entitlement expiry is invalid") from exc
            if expires_at <= current:
                raise CommerceError("Expired credentials cannot be migrated")
            quota_bytes = int(entitlement["quota_bytes"] or 0)
            if quota_bytes <= 0:
                raise CommerceError("Entitlement quota is invalid")
            existing = connection.execute(
                """SELECT id, status, target_server_id FROM connectivity_migration_jobs
                    WHERE credential_id = ? AND target_endpoint_id = ?
                    ORDER BY created_at DESC LIMIT 1""",
                (str(row["credential_id"]), str(row["target_endpoint"])),
            ).fetchone()
            if existing is not None and str(existing["status"]) in {
                "pending", "creating", "source_delete_pending"
            }:
                return {
                    "job_id": str(existing["id"]),
                    "status": str(existing["status"]),
                    "source_server_id": source,
                    "target_server_id": target,
                    "idempotent": True,
                }
            job_id = _new_id()
            target_external_id = f"aurix-mig-{job_id[:24]}"
            target_name = f"AuriX-MIG-{str(row['telegram_id'])}-{target[:16]}-{job_id[:8]}"
            connection.execute(
                """INSERT INTO connectivity_migration_jobs
                   (id, profile_id, credential_id, source_endpoint_id, target_endpoint_id,
                    source_server_id, target_server_id, source_external_id, target_external_id,
                    target_name, profile_kind, telegram_id, quota_bytes, expires_at,
                    status, attempts, next_attempt_at, requested_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)""",
                (
                    job_id,
                    str(row["profile_id"]),
                    str(row["credential_id"]),
                    str(row["source_endpoint"]),
                    str(row["target_endpoint"]),
                    source,
                    target,
                    external_id,
                    target_external_id,
                    target_name[:128],
                    kind,
                    int(row["telegram_id"]),
                    quota_bytes,
                    expires_at.isoformat(),
                    now_text,
                    int(requested_by),
                    now_text,
                    now_text,
                ),
            )
            self._audit(
                connection,
                "endpoint_migration_queued",
                "connectivity_migration",
                job_id,
                "owner",
                str(requested_by),
                {
                    "source_server_id": source,
                    "target_server_id": target,
                    "source_external_id": external_id,
                    "profile_kind": kind,
                    "expires_at": expires_at.isoformat(),
                },
            )
        return {
            "job_id": job_id,
            "status": "pending",
            "source_server_id": source,
            "target_server_id": target,
            "profile_kind": kind,
            "expires_at": expires_at.isoformat(),
            "idempotent": False,
        }

    def migratable_credentials(self, source_server_id: str) -> list[dict[str, Any]]:
        """List active managed credentials on a source endpoint, sans secrets."""
        source = str(source_server_id or "").strip()
        if not source:
            raise CommerceError("Source endpoint is required")
        with self.database.connect() as connection:
            if not ConnectivityRegistry.available(connection):
                return []
            rows = connection.execute(
                """SELECT c.credential_id, c.external_id, p.profile_kind,
                          p.telegram_id, c.created_at, c.status
                     FROM connectivity_credentials c
                     JOIN connectivity_profiles p ON p.profile_id = c.profile_id
                     JOIN connectivity_endpoints e ON e.endpoint_id = c.endpoint_id
                    WHERE e.outline_server_id = ? AND c.status = 'active'
                    ORDER BY c.created_at, c.external_id""",
                (source,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _managed_repair_required_observations() -> int:
        """Require independent missing-key observations before recreating access."""
        try:
            return max(
                2,
                min(10, int(os.environ.get("AURIX_KEY_REPAIR_REQUIRED_OBSERVATIONS", "2"))),
            )
        except (TypeError, ValueError):
            return 2

    @staticmethod
    def _managed_repair_observation_interval_seconds() -> int:
        """Keep repeated UI refreshes from fabricating independent observations."""
        try:
            return max(
                0,
                min(
                    86_400,
                    int(os.environ.get("AURIX_KEY_REPAIR_OBSERVATION_INTERVAL_SECONDS", "60")),
                ),
            )
        except (TypeError, ValueError):
            return 60

    @staticmethod
    def _usage_snapshot_interval_seconds() -> int:
        """Throttle durable usage samples without reducing live enforcement."""
        try:
            return max(
                60,
                min(
                    86_400,
                    int(os.environ.get("AURIX_USAGE_SNAPSHOT_INTERVAL_SECONDS", "300")),
                ),
            )
        except (TypeError, ValueError):
            return 300

    @staticmethod
    def _managed_repair_cached_usage_max_age_seconds() -> int:
        """Bound how long a cached usage sample may authorize a repair.

        A missing Outline key can no longer be queried for its historical
        transfer count.  A recent, persisted sample is safe to reuse because
        it is still a lower-bound observation from the same external key; an
        old sample is escalated instead of silently restoring quota.
        """
        try:
            return max(
                0,
                min(
                    86_400,
                    int(os.environ.get("AURIX_KEY_REPAIR_CACHED_USAGE_MAX_AGE_SECONDS", "900")),
                ),
            )
        except (TypeError, ValueError):
            return 900

    @classmethod
    def _managed_repair_cached_usage_is_recent(cls, row: Any, observed_at: Any) -> bool:
        """Return whether the entitlement has a bounded, trustworthy sample."""
        raw_observed = row.get("last_usage_observed_at") if hasattr(row, "get") else None
        if not raw_observed:
            return False
        try:
            usage_at = (
                raw_observed
                if isinstance(raw_observed, datetime)
                else datetime.fromisoformat(str(raw_observed))
            ).astimezone(UTC)
            current = (
                observed_at
                if isinstance(observed_at, datetime)
                else datetime.fromisoformat(str(observed_at))
            ).astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            return False
        age = (current - usage_at).total_seconds()
        return 0 <= age <= cls._managed_repair_cached_usage_max_age_seconds()

    @staticmethod
    def _managed_repair_allow_unknown_usage() -> bool:
        """Return whether an operator explicitly permits a full-quota repair.

        A missing remote key has no trustworthy usage value after an out-of-band
        deletion.  The safe default is to escalate for review rather than reset
        the entitlement to its original quota.  A controlled owner-only repair
        can opt in through the deployment environment when the operator has
        independently verified that no traffic was served.
        """
        return os.environ.get("AURIX_KEY_REPAIR_ALLOW_UNKNOWN_USAGE", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @staticmethod
    def _managed_repair_key_name(row: Any, previous_name: str | None = None) -> str:
        """Keep a stable, human-readable name when a key is recreated."""
        if previous_name and str(previous_name).strip():
            return str(previous_name).strip()[:128]
        raw_identity = str(row.get("username") if hasattr(row, "get") else row["username"] or "")
        raw_identity = raw_identity.lstrip("@") or str(row["telegram_id"])
        identity = re.sub(r"[^A-Za-z0-9_-]+", "-", raw_identity).strip("-_")[:48]
        identity = identity or str(row["telegram_id"])
        kind = str(row["kind"] or "free").lower()
        if kind == "paid":
            plan_name = str(row.get("plan_name") or row.get("plan_code") or "paid")
            match = re.search(r"\b(\d+)\s*GB\b", plan_name, re.IGNORECASE)
            tier = f"PAID{match.group(1)}GB" if match else str(row.get("plan_code") or "PAID")
        elif row.get("campaign_code"):
            tier = f"PROMO-{row['campaign_code']}"
        elif str(row.get("key_type") or "") == "monthly_trial":
            tier = "FREE3GB"
        else:
            tier = "FREE300MB"
        duration = f"{int(row.get('duration_days') or (30 if tier == 'FREE3GB' else 1))}day"
        created_at = str(row.get("created_at") or "")
        try:
            started = datetime.fromisoformat(created_at).astimezone(UTC)
            timestamp = started.strftime("%Y%m%d%H%M")
        except (TypeError, ValueError, OverflowError):
            timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M")
        return f"{identity}-{tier}-{duration}-{timestamp}"[:128]

    def _record_usage_snapshots(
        self,
        connection: Any,
        *,
        server_id: str,
        observed_at: str,
        managed_rows: list[dict[str, Any]],
        by_key: dict[str, Any],
    ) -> int:
        """Persist bounded, non-secret transfer evidence for managed keys."""
        if not self._table_exists(connection, "usage_snapshots") or not isinstance(by_key, dict):
            return 0
        try:
            current = datetime.fromisoformat(str(observed_at)).astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            return 0
        cutoff = (current - timedelta(seconds=self._usage_snapshot_interval_seconds())).isoformat()
        recent_rows = connection.execute(
            """SELECT entitlement_kind, local_key_ref, MAX(observed_at) AS observed_at
                 FROM usage_snapshots
                WHERE server_id = ? AND observed_at >= ?
                GROUP BY entitlement_kind, local_key_ref""",
            (str(server_id), cutoff),
        ).fetchall()
        recent = {
            (str(row["entitlement_kind"]), str(row["local_key_ref"]))
            for row in recent_rows
        }
        recorded = 0
        for item in managed_rows:
            external_id = str(item.get("source_external_id") or "").strip()
            if not external_id or external_id not in by_key:
                continue
            try:
                used = self._metric_bytes(by_key.get(external_id))
                quota = int(item.get("quota_bytes") or 0)
                telegram_id = int(item["telegram_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if used is None or quota <= 0:
                continue
            kind = str(item.get("kind") or "free").lower()
            local_ref = str(item.get("local_key_ref") or "").strip()
            if kind not in {"free", "paid", "trial", "promo"} or not local_ref:
                continue
            if (kind, local_ref) in recent:
                continue
            connection.execute(
                """INSERT INTO usage_snapshots
                   (id, telegram_id, entitlement_kind, local_key_ref, server_id,
                    outline_key_id, observed_at, used_bytes, quota_bytes, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'outline_metrics')
                   ON CONFLICT(server_id, entitlement_kind, local_key_ref, observed_at)
                   DO NOTHING""",
                (
                    _new_id(), telegram_id, kind, local_ref, str(server_id),
                    external_id, str(observed_at), int(used), quota,
                ),
            )
            recent.add((kind, local_ref))
            recorded += 1
        return recorded

    def _managed_repair_rows(self, connection: Any, server_id: str) -> list[dict[str, Any]]:
        """Return active managed entitlements that may need remote repair."""
        rows: list[dict[str, Any]] = []
        paid_rows = connection.execute(
            """SELECT 'paid' AS kind, CAST(k.id AS TEXT) AS local_key_ref,
                      k.id AS local_id, k.telegram_id, k.outline_key_id AS source_external_id,
                      k.quota_bytes, k.last_usage_bytes, k.last_usage_observed_at,
                      k.created_at, s.expires_at,
                      s.plan_name, s.plan_code, s.duration_days, u.username
                 FROM paid_vpn_keys k
                 JOIN subscriptions s ON s.id = k.subscription_id
                 JOIN users u ON u.telegram_id = k.telegram_id
                WHERE k.server_id = ? AND k.status = 'active' AND s.status = 'active'
                  AND k.quota_bytes IS NOT NULL AND COALESCE(k.quota_reason, '') = ''""",
            (server_id,),
        ).fetchall()
        rows.extend(dict(row) for row in paid_rows)
        if self._table_exists(connection, "keys"):
            free_rows = connection.execute(
                """SELECT 'free' AS kind, CAST(k.id AS TEXT) AS local_key_ref,
                          k.id AS local_id, k.telegram_id, k.outline_key_id AS source_external_id,
                          k.data_limit_bytes AS quota_bytes, k.last_usage_bytes,
                          k.last_usage_observed_at,
                          k.created_at, k.expires_at, k.key_type, u.username,
                          g.campaign_code,
                          COALESCE(c.duration_days,
                                   CASE WHEN k.key_type = 'monthly_trial' THEN 30 ELSE 1 END)
                              AS duration_days
                     FROM keys k
                     JOIN users u ON u.telegram_id = k.telegram_id
                     LEFT JOIN giveaway_claims g ON g.key_id = k.id
                     LEFT JOIN giveaway_campaigns c ON c.code = g.campaign_code
                    WHERE k.server_id = ? AND k.status = 'active'
                      AND COALESCE(k.quota_reason, '') = ''
                      AND k.data_limit_bytes IS NOT NULL""",
                (server_id,),
            ).fetchall()
            rows.extend(dict(row) for row in free_rows)
        # A missing key cannot be queried for historical transfer usage.  If
        # the durable telemetry ledger has a newer sample than the live key
        # row, use that same-key lower bound for repair decisions.  This keeps
        # a recent observation useful across a restart or an inventory race,
        # while the bounded freshness check below still escalates old data.
        snapshot_by_ref: dict[tuple[str, str], dict[str, Any]] = {}
        if self._table_exists(connection, "usage_snapshots"):
            snapshot_rows = connection.execute(
                """SELECT entitlement_kind, local_key_ref, used_bytes, observed_at
                     FROM usage_snapshots
                    WHERE server_id = ?
                    ORDER BY observed_at DESC""",
                (server_id,),
            ).fetchall()
            for snapshot in snapshot_rows:
                key = (str(snapshot["entitlement_kind"]), str(snapshot["local_key_ref"]))
                snapshot_by_ref.setdefault(key, dict(snapshot))
        for row in rows:
            row["source_external_id"] = str(row.get("source_external_id") or "").strip()
            snapshot = snapshot_by_ref.get((str(row["kind"]), str(row["local_key_ref"])))
            if snapshot:
                live_observed = row.get("last_usage_observed_at")
                use_snapshot = not live_observed
                if not use_snapshot:
                    try:
                        use_snapshot = datetime.fromisoformat(
                            str(snapshot["observed_at"])
                        ).astimezone(UTC) > datetime.fromisoformat(
                            str(live_observed)
                        ).astimezone(UTC)
                    except (TypeError, ValueError, OverflowError):
                        use_snapshot = False
                if use_snapshot:
                    row["last_usage_bytes"] = snapshot["used_bytes"]
                    row["last_usage_observed_at"] = snapshot["observed_at"]
            row["key_name"] = self._managed_repair_key_name(row)
        return [row for row in rows if row["source_external_id"]]

    def _enqueue_managed_key_repair(
        self,
        connection: Any,
        row: dict[str, Any],
        *,
        observed_at: str,
        missing_observation_count: int,
        previous_name: str | None,
        usage_bytes: int | None,
    ) -> str:
        """Create/update one durable repair decision without calling Outline."""
        local_ref = str(row["local_key_ref"])
        server_id = str(row["server_id"])
        kind = str(row["kind"])
        quota = int(row["quota_bytes"] or 0)
        name = self._managed_repair_key_name(row, previous_name)
        existing = connection.execute(
            """SELECT * FROM managed_key_repair_jobs
               WHERE server_id = ? AND kind = ? AND local_key_ref = ?""",
            (server_id, kind, local_ref),
        ).fetchone()
        repair_id = str(existing["id"]) if existing is not None else _new_id()
        allow_unknown = self._managed_repair_allow_unknown_usage()
        effective_usage = usage_bytes
        usage_is_fresh = usage_bytes is not None
        if effective_usage is None:
            try:
                effective_usage = int(row.get("last_usage_bytes"))
            except (TypeError, ValueError):
                effective_usage = None
            usage_is_fresh = False
        if not usage_is_fresh and self._managed_repair_cached_usage_is_recent(row, observed_at):
            usage_is_fresh = effective_usage is not None
        if not usage_is_fresh and os.environ.get(
            "AURIX_KEY_REPAIR_ALLOW_STALE_USAGE", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}:
            usage_is_fresh = effective_usage is not None
        if effective_usage is not None:
            effective_usage = max(0, effective_usage)
        if not usage_is_fresh and not allow_unknown:
            status = "manual"
            error = "usage_observation_required"
        elif effective_usage is not None and effective_usage >= quota:
            status = "manual"
            error = "quota_already_exhausted"
        else:
            status = "pending"
            error = None
        if existing is None:
            connection.execute(
                """INSERT INTO managed_key_repair_jobs
                   (id, kind, server_id, telegram_id, local_key_ref,
                    source_external_id, target_external_id, key_name, quota_bytes,
                    used_bytes, expires_at, status, attempts, next_attempt_at,
                    last_error, observed_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
                (
                    repair_id, kind, server_id, int(row["telegram_id"]), local_ref,
                    str(row["source_external_id"]), str(row["source_external_id"]), name,
                    quota, effective_usage, str(row["expires_at"]), status,
                    observed_at, error, observed_at, observed_at,
                ),
            )
            job_state = "created"
        else:
            current_status = str(existing["status"] or "")
            # A completed/cancelled job belongs to an earlier disappearance.
            # Re-open it only after a new two-observation episode, preserving
            # the same durable row and audit history without duplicate jobs.
            reopen = current_status in {"done", "cancelled"} or str(
                existing["source_external_id"]
            ) != str(row["source_external_id"])
            if reopen:
                connection.execute(
                    """UPDATE managed_key_repair_jobs
                       SET source_external_id = ?, target_external_id = ?, key_name = ?,
                           quota_bytes = ?, used_bytes = ?, expires_at = ?, status = ?,
                           attempts = 0, next_attempt_at = ?, locked_at = NULL,
                           last_error = ?, observed_at = ?, completed_at = NULL
                         WHERE id = ?""",
                    (
                        str(row["source_external_id"]), str(row["source_external_id"]), name,
                        quota, effective_usage, str(row["expires_at"]), status, observed_at,
                        error, observed_at, existing["id"],
                    ),
                )
                job_state = "reopened"
            else:
                job_state = current_status or "existing"
        if job_state in {"created", "reopened"}:
            self._audit(
                connection,
                "managed_key_missing",
                "managed_key",
                f"{server_id}:{kind}:{local_ref}",
                "system",
                None,
                {
                    "source_external_id": str(row["source_external_id"]),
                    "missing_observation_count": int(missing_observation_count),
                    "repair_status": status,
                    "used_bytes": effective_usage,
                    "quota_bytes": quota,
                    "job_state": job_state,
                },
            )
            # A missing managed key is an operational incident, not a silent
            # customer-facing outage. Queue one durable, preference-aware
            # staff alert for this repair episode; the notification dedupe
            # key prevents repeated inventory polls from spamming staff.
            usage_text = "unknown (fresh Outline telemetry unavailable)"
            if effective_usage is not None:
                usage_text = f"{_human_bytes(effective_usage)} observed"
            self._queue_staff_notification(
                connection,
                "key_repairs",
                repair_id,
                "🧩 MANAGED KEY MISSING\n\n"
                f"Repair: #{repair_id[:8]}\n"
                f"Customer: tg:{int(row['telegram_id'])}\n"
                f"Endpoint: {server_id}\n"
                f"Old key: {str(row['source_external_id'])[:32]}\n"
                f"Usage: {usage_text}\n"
                f"Decision: {status.replace('_', ' ')}\n\n"
                "Open Key Repairs to review. AuriX will not recreate this key or reset quota without the required owner decision.",
                observed_at,
            )
        # Let the caller distinguish a newly opened repair episode from a
        # repeated observation of the same queued/manual decision.  This keeps
        # operator counters and audit volume stable during frequent refreshes.
        return status if job_state in {"created", "reopened"} else f"existing_{status}"

    def refresh_server_inventory(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Reconcile remote inventory and telemetry without storing access URLs."""
        observed_at = _now_text(now)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT server_id FROM outline_servers WHERE enabled = 1 ORDER BY server_id"
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            server_id = str(row["server_id"])
            client = self._outline_client(server_id)
            probe_started = time.perf_counter()
            try:
                info = client.server_info()
                inventory = client.list_keys()
                keys = inventory.get("accessKeys", []) if isinstance(inventory, dict) else []
                remote_items = [
                    item
                    for item in (keys if isinstance(keys, list) else [])
                    if isinstance(item, dict) and str(item.get("id") or "").strip()
                ]
                transfer = client.transfer_metrics()
                by_key = transfer.get("bytesTransferredByUserId", {}) if isinstance(transfer, dict) else {}
                total_transfer = 0
                for value in by_key.values() if isinstance(by_key, dict) else ():
                    try:
                        total_transfer += max(0, int(value or 0))
                    except (TypeError, ValueError):
                        continue
                self._server_metrics_cache[server_id] = (
                    dict(by_key) if isinstance(by_key, dict) else {}
                )
                current_bandwidth = peak_bandwidth = None
                experimental = 0
                experimental_method = getattr(client, "experimental_metrics", None)
                if callable(experimental_method):
                    try:
                        detailed = experimental_method("30d")
                        server_metrics = detailed.get("server", {}) if isinstance(detailed, dict) else {}
                        bandwidth = server_metrics.get("bandwidth", {}) if isinstance(server_metrics, dict) else {}
                        current_bandwidth = self._metric_bytes(bandwidth.get("current"))
                        peak_bandwidth = self._metric_bytes(bandwidth.get("peak"))
                        experimental = 1
                    except Exception:
                        pass
                with self.database.connect() as connection:
                    self.database.begin_write(connection)
                    orphan_count = 0
                    managed_missing_count = 0
                    repair_jobs_queued = 0
                    repair_manual_count = 0
                    usage_snapshots_recorded = 0
                    if self._table_exists(connection, "outline_remote_keys"):
                        managed_rows = self._managed_repair_rows(connection, server_id)
                        for managed_row in managed_rows:
                            managed_row["server_id"] = server_id
                        usage_snapshots_recorded = self._record_usage_snapshots(
                            connection,
                            server_id=server_id,
                            observed_at=observed_at,
                            managed_rows=managed_rows,
                            by_key=by_key,
                        )
                        ledger_rows = {
                            str(item["outline_key_id"]): dict(item)
                            for item in connection.execute(
                                """SELECT * FROM outline_remote_keys
                                   WHERE server_id = ?""",
                                (server_id,),
                            ).fetchall()
                        }
                        managed_ids = {
                            str(item["outline_key_id"])
                            for item in connection.execute(
                                """SELECT outline_key_id FROM paid_vpn_keys
                                   WHERE server_id = ? AND outline_key_id IS NOT NULL""",
                                (server_id,),
                            ).fetchall()
                        }
                        if self._table_exists(connection, "keys"):
                            managed_ids.update(
                                str(item["outline_key_id"])
                                for item in connection.execute(
                                    """SELECT outline_key_id FROM keys
                                       WHERE server_id = ? AND outline_key_id IS NOT NULL""",
                                    (server_id,),
                                ).fetchall()
                            )
                        # A successful inventory containing zero keys is authoritative:
                        # previously observed records become missing, but remain in the
                        # audit ledger for later reconciliation.
                        connection.execute(
                            """UPDATE outline_remote_keys
                               SET status = 'missing'
                               WHERE server_id = ? AND status = 'present'""",
                            (server_id,),
                        )
                        for item in remote_items:
                            outline_key_id = str(item["id"]).strip()
                            remote_name = str(item.get("name") or "").strip()[:256] or None
                            usage_bytes = self._metric_bytes(by_key.get(outline_key_id)) if isinstance(by_key, dict) else None
                            connection.execute(
                                """INSERT INTO outline_remote_keys
                                   (server_id, outline_key_id, remote_name, managed, status,
                                    first_seen_at, last_seen_at, last_usage_bytes,
                                    missing_observation_count, missing_since_at, last_missing_at)
                                   VALUES (?, ?, ?, ?, 'present', ?, ?, ?, 0, NULL, NULL)
                                   ON CONFLICT(server_id, outline_key_id) DO UPDATE SET
                                     remote_name = excluded.remote_name,
                                     managed = excluded.managed,
                                     status = 'present',
                                     last_seen_at = excluded.last_seen_at,
                                     last_usage_bytes = COALESCE(
                                         excluded.last_usage_bytes,
                                         outline_remote_keys.last_usage_bytes
                                     ),
                                     missing_observation_count = 0,
                                     missing_since_at = NULL,
                                     last_missing_at = NULL""",
                                (
                                    server_id,
                                    outline_key_id,
                                    remote_name,
                                    1 if outline_key_id in managed_ids else 0,
                                    observed_at,
                                    observed_at,
                                    usage_bytes,
                                ),
                            )
                        # Persist only metrics that explicitly contain the
                        # external id.  A missing map entry is ambiguous (it
                        # may mean zero traffic or a deleted key), so it must
                        # never refresh the cached-usage freshness window.
                        if isinstance(by_key, dict):
                            for managed_row in managed_rows:
                                external_id = str(managed_row["source_external_id"])
                                if external_id not in by_key:
                                    continue
                                usage_bytes = self._metric_bytes(by_key.get(external_id))
                                if usage_bytes is None:
                                    continue
                                if str(managed_row["kind"]) == "paid":
                                    connection.execute(
                                        """UPDATE paid_vpn_keys
                                              SET last_usage_bytes = ?, last_usage_observed_at = ?
                                            WHERE id = ? AND server_id = ?""",
                                        (
                                            usage_bytes,
                                            observed_at,
                                            managed_row["local_id"],
                                            server_id,
                                        ),
                                    )
                                elif self._table_exists(connection, "keys"):
                                    connection.execute(
                                        """UPDATE keys
                                              SET last_usage_bytes = ?, last_usage_observed_at = ?
                                            WHERE id = ? AND server_id = ?""",
                                        (
                                            usage_bytes,
                                            observed_at,
                                            managed_row["local_id"],
                                            server_id,
                                        ),
                                    )
                        remote_ids = {
                            str(item["id"]).strip()
                            for item in remote_items
                            if str(item.get("id") or "").strip()
                        }
                        required_observations = self._managed_repair_required_observations()
                        interval_seconds = self._managed_repair_observation_interval_seconds()
                        current_observed_dt = datetime.fromisoformat(observed_at).astimezone(UTC)
                        for managed_row in managed_rows:
                            source_id = str(managed_row["source_external_id"])
                            if source_id in remote_ids:
                                continue
                            managed_missing_count += 1
                            previous = ledger_rows.get(source_id)
                            previous_status = str(previous.get("status") or "") if previous else ""
                            previous_count = int(
                                previous.get("missing_observation_count") or 0
                            ) if previous else 0
                            previous_last_missing = str(previous.get("last_missing_at") or "") if previous else ""
                            count = 1
                            missing_since = observed_at
                            should_increment = True
                            if previous_status == "missing":
                                count = max(1, previous_count)
                                missing_since = str(previous.get("missing_since_at") or observed_at)
                                if previous_last_missing:
                                    try:
                                        elapsed = current_observed_dt - datetime.fromisoformat(
                                            previous_last_missing
                                        ).astimezone(UTC)
                                        should_increment = elapsed.total_seconds() >= interval_seconds
                                    except (TypeError, ValueError, OverflowError):
                                        should_increment = True
                                if should_increment:
                                    count += 1
                            connection.execute(
                                """INSERT INTO outline_remote_keys
                                   (server_id, outline_key_id, remote_name, managed, status,
                                    first_seen_at, last_seen_at, last_usage_bytes,
                                    missing_observation_count, missing_since_at, last_missing_at)
                                   VALUES (?, ?, NULL, 1, 'missing', ?, ?, ?, ?, ?, ?)
                                   ON CONFLICT(server_id, outline_key_id) DO UPDATE SET
                                     managed = 1,
                                     status = 'missing',
                                     last_usage_bytes = COALESCE(
                                         excluded.last_usage_bytes,
                                         outline_remote_keys.last_usage_bytes
                                     ),
                                     missing_observation_count = excluded.missing_observation_count,
                                     missing_since_at = excluded.missing_since_at,
                                     last_missing_at = excluded.last_missing_at""",
                                (
                                    server_id,
                                    source_id,
                                    observed_at,
                                    observed_at,
                                    self._metric_bytes(by_key.get(source_id))
                                    if isinstance(by_key, dict)
                                    else managed_row.get("last_usage_bytes"),
                                    count,
                                    missing_since,
                                    observed_at,
                                ),
                            )
                            if count >= required_observations and (
                                previous_status != "missing"
                                or should_increment
                                or previous_count < required_observations
                            ):
                                repair_status = self._enqueue_managed_key_repair(
                                    connection,
                                    managed_row,
                                    observed_at=observed_at,
                                    missing_observation_count=count,
                                    previous_name=(
                                        str(previous.get("remote_name") or "") if previous else None
                                    ),
                                    usage_bytes=(
                                        self._metric_bytes(by_key.get(source_id))
                                        if isinstance(by_key, dict)
                                        else None
                                    ),
                                )
                                if repair_status == "pending":
                                    repair_jobs_queued += 1
                                elif repair_status == "manual":
                                    repair_manual_count += 1
                        orphan_count = int(
                            connection.execute(
                                """SELECT COUNT(*) AS n FROM outline_remote_keys
                                   WHERE server_id = ? AND status = 'present' AND managed = 0
                                     AND COALESCE(
                                           (SELECT review_state
                                              FROM outline_remote_key_reviews r
                                             WHERE r.server_id = outline_remote_keys.server_id
                                               AND r.outline_key_id = outline_remote_keys.outline_key_id),
                                           'unreviewed'
                                         ) = 'unreviewed'""",
                                (server_id,),
                            ).fetchone()["n"]
                        )
                    latency_ms = round((time.perf_counter() - probe_started) * 1000, 3)
                    health = self._record_endpoint_health(
                        connection,
                        server_id,
                        observed_at,
                        observed_status="healthy",
                        latency_ms=latency_ms,
                        remote_key_count=len(remote_items),
                    )
                    connection.execute(
                        """UPDATE outline_servers SET remote_key_count = ?, remote_transfer_bytes = ?,
                                  current_bandwidth_bytes = ?, peak_bandwidth_bytes = ?,
                                  telemetry_experimental = ?, remote_orphan_key_count = ?
                               WHERE server_id = ?""",
                        (
                            len(remote_items),
                            total_transfer,
                            current_bandwidth,
                            peak_bandwidth,
                            experimental,
                            orphan_count,
                            server_id,
                        ),
                    )
                    lifecycle_row = connection.execute(
                        "SELECT lifecycle_state FROM outline_servers WHERE server_id = ?",
                        (server_id,),
                    ).fetchone()
                    ConnectivityRegistry.sync_outline_health(
                        connection,
                        server_id=server_id,
                        lifecycle_state=str(lifecycle_row["lifecycle_state"] if lifecycle_row else "active"),
                        health_status=str(health["state"]),
                        now_text=observed_at,
                    )
                results.append(
                    {
                        "server_id": server_id,
                        "status": health["state"],
                        "observed_status": "healthy",
                        "latency_ms": latency_ms,
                        "health_success_streak": health["success_streak"],
                        "version": info.get("version"),
                        "remote_key_count": len(remote_items),
                        "remote_orphan_key_count": orphan_count,
                        "managed_missing_key_count": managed_missing_count,
                        "repair_jobs_queued": repair_jobs_queued,
                        "repair_manual_count": repair_manual_count,
                        "usage_snapshots_recorded": usage_snapshots_recorded,
                    }
                )
            except Exception as exc:
                with self.database.connect() as connection:
                    self.database.begin_write(connection)
                    latency_ms = round((time.perf_counter() - probe_started) * 1000, 3)
                    health = self._record_endpoint_health(
                        connection,
                        server_id,
                        observed_at,
                        observed_status="unreachable",
                        latency_ms=latency_ms,
                        error_type=type(exc).__name__,
                    )
                    lifecycle_row = connection.execute(
                        "SELECT lifecycle_state FROM outline_servers WHERE server_id = ?",
                        (server_id,),
                    ).fetchone()
                    ConnectivityRegistry.sync_outline_health(
                        connection,
                        server_id=server_id,
                        lifecycle_state=str(lifecycle_row["lifecycle_state"] if lifecycle_row else "active"),
                        health_status=str(health["state"]),
                        now_text=observed_at,
                    )
                results.append(
                    {
                        "server_id": server_id,
                        "status": health["state"],
                        "observed_status": "unreachable",
                        "latency_ms": latency_ms,
                        "health_failure_streak": health["failure_streak"],
                        "managed_missing_key_count": 0,
                        "repair_jobs_queued": 0,
                        "repair_manual_count": 0,
                    }
                )
        return results

    def remote_key_inventory(
        self,
        server_id: str,
        *,
        status: str = "present",
        managed: bool | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return the safe, operator-facing remote-key audit ledger.

        Only Outline's key id, display name, management classification,
        lifecycle status and observed telemetry are returned. Access URLs are
        never read by this method, so the admin inventory panel cannot
        accidentally disclose a usable VPN credential.
        """
        normalized_status = str(status or "present").strip().lower()
        if normalized_status not in {"present", "missing", "all"}:
            raise CommerceError("Remote inventory status must be present, missing or all")
        try:
            page_limit = max(1, min(500, int(limit)))
        except (TypeError, ValueError):
            page_limit = 100
        with self.database.connect() as connection:
            if not self._table_exists(connection, "outline_remote_keys"):
                return []
            # Validate the server id so a stale button is distinguishable from
            # a server which genuinely has no audit rows.
            if connection.execute(
                "SELECT 1 FROM outline_servers WHERE server_id = ?", (str(server_id),)
            ).fetchone() is None:
                raise CommerceError("Outline server is unavailable")
            clauses = ["outline_remote_keys.server_id = ?"]
            params: list[Any] = [str(server_id)]
            if normalized_status != "all":
                clauses.append("outline_remote_keys.status = ?")
                params.append(normalized_status)
            if managed is not None:
                clauses.append("outline_remote_keys.managed = ?")
                params.append(1 if managed else 0)
            params.append(page_limit)
            rows = connection.execute(
                """SELECT outline_remote_keys.server_id, outline_remote_keys.outline_key_id,
                          outline_remote_keys.remote_name, outline_remote_keys.managed,
                          outline_remote_keys.status, outline_remote_keys.first_seen_at,
                          outline_remote_keys.last_seen_at, outline_remote_keys.last_usage_bytes,
                          COALESCE(r.review_state,
                                   CASE WHEN outline_remote_keys.managed = 1
                                        THEN 'managed' ELSE 'unreviewed' END) AS review_state,
                          r.reviewed_by, r.reviewed_at, r.review_note
                     FROM outline_remote_keys
                     LEFT JOIN outline_remote_key_reviews r
                       ON r.server_id = outline_remote_keys.server_id
                      AND r.outline_key_id = outline_remote_keys.outline_key_id
                    WHERE """
                + " AND ".join(clauses)
                + """ ORDER BY CASE WHEN outline_remote_keys.status = 'present' THEN 0 ELSE 1 END,
                                  outline_remote_keys.last_seen_at DESC,
                                  outline_remote_keys.outline_key_id
                    LIMIT ?""",
                tuple(params),
            ).fetchall()
            return [dict(row) for row in rows]

    def review_remote_key(
        self,
        server_id: str,
        outline_key_id: str,
        review_state: str,
        reviewer_id: int,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Record an explicit owner review of an untracked remote key.

        ``accepted_external`` means the key was inspected and intentionally
        remains outside AuriX lifecycle management. It does not make the key
        saleable or hide it from remote capacity counts. Resetting to
        ``unreviewed`` re-opens the migration blocker. No remote key is
        deleted or adopted by this operation.
        """
        normalized_state = str(review_state or "").strip().lower()
        if normalized_state not in {"unreviewed", "accepted_external"}:
            raise CommerceError("Remote key review state is invalid")
        server = str(server_id or "").strip()
        key_id = str(outline_key_id or "").strip()
        if not server or not key_id:
            raise CommerceError("Remote key identity is required")
        now_text = _now_text()
        clean_note = str(note or "").strip()[:512] or None
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                """SELECT managed, status FROM outline_remote_keys
                   WHERE server_id = ? AND outline_key_id = ?""",
                (server, key_id),
            ).fetchone()
            if row is None:
                raise CommerceError("Remote key is not present in the audit inventory")
            if int(row["managed"] or 0) == 1:
                raise CommerceError("Managed AuriX keys do not need orphan review")
            connection.execute(
                """INSERT INTO outline_remote_key_reviews
                   (server_id, outline_key_id, review_state, reviewed_by, reviewed_at, review_note)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(server_id, outline_key_id) DO UPDATE SET
                     review_state = excluded.review_state,
                     reviewed_by = excluded.reviewed_by,
                     reviewed_at = excluded.reviewed_at,
                     review_note = excluded.review_note""",
                (server, key_id, normalized_state, int(reviewer_id), now_text, clean_note),
            )
            connection.execute(
                """UPDATE outline_servers
                      SET remote_orphan_key_count = (
                          SELECT COUNT(*)
                            FROM outline_remote_keys remote
                           WHERE remote.server_id = outline_servers.server_id
                             AND remote.status = 'present'
                             AND remote.managed = 0
                             AND COALESCE(
                                   (SELECT review_state
                                      FROM outline_remote_key_reviews review
                                     WHERE review.server_id = remote.server_id
                                       AND review.outline_key_id = remote.outline_key_id),
                                   'unreviewed'
                                 ) = 'unreviewed'
                      ),
                          updated_at = ?
                    WHERE server_id = ?""",
                (now_text, server),
            )
            self._audit(
                connection,
                "remote_key_reviewed",
                "outline_remote_key",
                f"{server}:{key_id}",
                "staff",
                str(reviewer_id),
                {
                    "review_state": normalized_state,
                    "remote_status": str(row["status"] or ""),
                    "note": clean_note,
                },
            )
        return {
            "server_id": server,
            "outline_key_id": key_id,
            "review_state": normalized_state,
            "reviewed_by": int(reviewer_id),
            "reviewed_at": now_text,
            "review_note": clean_note,
        }

    def managed_key_repair_jobs(
        self, *, status: str = "open", limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return safe operator-facing managed-key repair decisions.

        Secrets are deliberately excluded. ``open`` includes pending, failed,
        and manual decisions; completed history remains available through the
        audit ledger and an explicit ``status='all'`` query.
        """
        normalized = str(status or "open").strip().lower()
        if normalized not in {"open", "pending", "failed", "manual", "done", "cancelled", "all"}:
            raise CommerceError("Repair status must be open, pending, failed, manual, done, cancelled or all")
        try:
            page_limit = max(1, min(500, int(limit)))
        except (TypeError, ValueError):
            page_limit = 100
        with self.database.connect() as connection:
            if not self._table_exists(connection, "managed_key_repair_jobs"):
                return []
            clauses: list[str] = []
            params: list[Any] = []
            if normalized == "open":
                clauses.append("status IN ('pending', 'running', 'failed', 'manual')")
            elif normalized != "all":
                clauses.append("status = ?")
                params.append(normalized)
            params.append(page_limit)
            rows = connection.execute(
                """SELECT id, kind, server_id, telegram_id, local_key_ref,
                          source_external_id, target_external_id, key_name,
                          quota_bytes, used_bytes, expires_at, status, attempts,
                          next_attempt_at, locked_at, last_error, observed_at,
                          created_at, completed_at
                     FROM managed_key_repair_jobs"""
                + (" WHERE " + " AND ".join(clauses) if clauses else "")
                + " ORDER BY CASE status WHEN 'manual' THEN 0 WHEN 'failed' THEN 1 "
                  "WHEN 'pending' THEN 2 WHEN 'running' THEN 3 ELSE 4 END, created_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def approve_managed_key_repair(
        self,
        repair_id: str,
        owner_id: int,
        *,
        allow_unknown_usage: bool = False,
    ) -> dict[str, Any]:
        """Owner-approve a manual repair after an explicit quota decision.

        When Outline no longer exposes the old key's traffic, approving a
        full-quota replacement is intentionally a separate, auditable action.
        The normal automatic worker never takes this path.
        """
        repair = str(repair_id or "").strip()
        if not repair:
            raise CommerceError("Repair identity is required")
        now_text = _now_text()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                "SELECT * FROM managed_key_repair_jobs WHERE id = ?",
                (repair,),
            ).fetchone()
            if row is None:
                raise CommerceError("Managed-key repair was not found")
            if str(row["status"]) not in {"manual", "failed"}:
                raise CommerceError("This repair is not awaiting owner review")
            reason = str(row["last_error"] or "")
            if reason == "quota_already_exhausted":
                raise CommerceError("This key has exhausted its quota and cannot be restored")
            if reason == "usage_observation_required" and not allow_unknown_usage:
                raise CommerceError(
                    "Explicitly confirm full-quota restoration when Outline usage is unavailable"
                )
            override = reason == "usage_observation_required" and allow_unknown_usage
            updated = connection.execute(
                """UPDATE managed_key_repair_jobs
                      SET status = 'pending', attempts = 0, next_attempt_at = ?,
                          locked_at = NULL, used_bytes = ?,
                          last_error = ?, completed_at = NULL
                    WHERE id = ? AND status IN ('manual', 'failed')""",
                (
                    now_text,
                    0 if override else row["used_bytes"],
                    "owner_approved_unknown_usage" if override else None,
                    repair,
                ),
            )
            if int(getattr(updated, "rowcount", 0) or 0) != 1:
                raise CommerceError("Managed-key repair changed before owner approval")
            self._audit(
                connection,
                "managed_key_repair_approved",
                "managed_key_repair",
                repair,
                "owner",
                str(owner_id),
                {
                    "allow_unknown_usage": bool(allow_unknown_usage),
                    "usage_override": bool(override),
                    "previous_status": str(row["status"]),
                    "previous_error": reason,
                },
            )
        return {"repair_id": repair, "status": "pending", "usage_override": override}

    def configure_server_capacity(
        self,
        server_id: str,
        admin_id: int,
        *,
        max_keys: int | None,
        reserved_keys: int = 2,
        monthly_traffic_bytes: int | None = None,
    ) -> None:
        if max_keys is not None and int(max_keys) <= 0:
            raise CommerceError("Maximum keys must be positive")
        if int(reserved_keys) < 0:
            raise CommerceError("Reserved headroom cannot be negative")
        if monthly_traffic_bytes is not None and int(monthly_traffic_bytes) <= 0:
            raise CommerceError("Traffic budget must be positive")
        now_text = _now_text()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            updated = connection.execute(
                """UPDATE outline_servers SET max_keys = ?, reserved_keys = ?,
                          monthly_traffic_bytes = ?, updated_at = ? WHERE server_id = ?""",
                (max_keys, reserved_keys, monthly_traffic_bytes, now_text, server_id),
            )
            if getattr(updated, "rowcount", 1) == 0:
                raise CommerceError("Outline server is not configured in the environment")
            self._validate_server_allocation_capacity(connection, server_id)
            self._audit(
                connection, "server_capacity_changed", "outline_server", server_id,
                "admin", str(admin_id),
                {"max_keys": max_keys, "reserved_keys": reserved_keys,
                 "monthly_traffic_bytes": monthly_traffic_bytes},
            )

    def configure_plan_allocation(
        self, server_id: str, plan_code: str, slot_limit: int, admin_id: int
    ) -> None:
        if int(slot_limit) < 0:
            raise CommerceError("Plan slots cannot be negative")
        now_text = _now_text()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            if connection.execute(
                "SELECT 1 FROM outline_servers WHERE server_id = ? AND enabled = 1", (server_id,)
            ).fetchone() is None:
                raise CommerceError("Outline server is unavailable")
            if connection.execute("SELECT 1 FROM plans WHERE code = ?", (plan_code,)).fetchone() is None:
                raise CommerceError("Unknown plan")
            connection.execute(
                """INSERT INTO server_plan_allocations
                   (server_id, plan_code, slot_limit, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(server_id, plan_code) DO UPDATE SET
                     slot_limit = excluded.slot_limit, updated_at = excluded.updated_at""",
                (server_id, plan_code, int(slot_limit), now_text),
            )
            self._validate_server_allocation_capacity(connection, server_id)
            self._audit(
                connection, "plan_capacity_changed", "outline_server", server_id,
                "admin", str(admin_id), {"plan_code": plan_code, "slot_limit": int(slot_limit)},
            )

    def configure_tier_allocation(
        self, server_id: str, tier_code: str, slot_limit: int, admin_id: int
    ) -> None:
        """Allocate free/promo issuance slots without moving existing keys."""
        normalized = str(tier_code).upper()
        if normalized not in {"FREE300MB", "FREE3GB", "PROMO"}:
            raise CommerceError("Unknown free or promotional tier")
        if int(slot_limit) < 0:
            raise CommerceError("Tier slots cannot be negative")
        now_text = _now_text()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            if connection.execute(
                "SELECT 1 FROM outline_servers WHERE server_id = ? AND enabled = 1", (server_id,)
            ).fetchone() is None:
                raise CommerceError("Outline server is unavailable")
            connection.execute(
                """INSERT INTO server_tier_allocations
                   (server_id, tier_code, slot_limit, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(server_id, tier_code) DO UPDATE SET
                     slot_limit = excluded.slot_limit, updated_at = excluded.updated_at""",
                (server_id, normalized, int(slot_limit), now_text),
            )
            self._validate_server_allocation_capacity(connection, server_id)
            self._audit(
                connection,
                "tier_capacity_changed",
                "outline_server",
                server_id,
                "admin",
                str(admin_id),
                {"tier_code": normalized, "slot_limit": int(slot_limit)},
            )

    @staticmethod
    def _validate_server_allocation_capacity(connection: Any, server_id: str) -> None:
        """Reject a new over-allocation when strict fleet policy is enabled.

        Existing deployments can remain in compatibility mode while their
        manifest is migrated. Once strict validation is enabled, Telegram
        capacity controls and declarative reconciliation share the same
        invariant: the sum of all plan/tier slots cannot exceed saleable key
        headroom. The transaction rolls back on failure, so an admin cannot
        leave a partially updated policy behind.
        """
        if os.environ.get("AURIX_FLEET_STRICT_ALLOCATION_VALIDATION", "").strip().lower() not in {
            "1", "true", "yes", "on",
        }:
            return
        server = connection.execute(
            "SELECT max_keys, reserved_keys FROM outline_servers WHERE server_id = ?",
            (server_id,),
        ).fetchone()
        if server is None or server["max_keys"] is None:
            return
        saleable = max(0, int(server["max_keys"]) - int(server["reserved_keys"] or 0))
        plan_total = int(
            connection.execute(
                "SELECT COALESCE(SUM(slot_limit), 0) AS n FROM server_plan_allocations WHERE server_id = ?",
                (server_id,),
            ).fetchone()["n"]
            or 0
        )
        tier_total = int(
            connection.execute(
                "SELECT COALESCE(SUM(slot_limit), 0) AS n FROM server_tier_allocations WHERE server_id = ?",
                (server_id,),
            ).fetchone()["n"]
            or 0
        )
        total = plan_total + tier_total
        if total > saleable:
            raise CommerceError(
                f"server {server_id} allocates {total} slots but only {saleable} remain after reserved headroom"
            )

    def apply_server_policy(
        self,
        server_id: str,
        admin_id: int,
        *,
        max_keys: int | None,
        reserved_keys: int,
        monthly_traffic_bytes: int | None,
        plan_slots: dict[str, int],
        tier_slots: dict[str, int],
    ) -> None:
        """Apply one complete server policy atomically.

        Fleet reconciliation can migrate a legacy over-allocated database to a
        strict manifest only if capacity and all allocations are changed in one
        transaction. Calling the individual Telegram controls sequentially
        would reject the transient state before the later reductions arrive.
        """
        if max_keys is not None and int(max_keys) <= 0:
            raise CommerceError("Maximum keys must be positive")
        if int(reserved_keys) < 0:
            raise CommerceError("Reserved headroom cannot be negative")
        if monthly_traffic_bytes is not None and int(monthly_traffic_bytes) <= 0:
            raise CommerceError("Traffic budget must be positive")
        normalized_plans = {str(code): int(limit) for code, limit in plan_slots.items()}
        normalized_tiers = {str(code).upper(): int(limit) for code, limit in tier_slots.items()}
        if any(limit < 0 for limit in normalized_plans.values()):
            raise CommerceError("Plan slots cannot be negative")
        if any(limit < 0 for limit in normalized_tiers.values()):
            raise CommerceError("Tier slots cannot be negative")
        if any(code not in {str(plan.code) for plan in self.plans()} for code in normalized_plans):
            raise CommerceError("Unknown plan")
        if any(code not in {"FREE300MB", "FREE3GB", "PROMO"} for code in normalized_tiers):
            raise CommerceError("Unknown free or promotional tier")
        now_text = _now_text()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            server = connection.execute(
                "SELECT max_keys, reserved_keys, monthly_traffic_bytes FROM outline_servers "
                "WHERE server_id = ? AND enabled = 1",
                (server_id,),
            ).fetchone()
            if server is None:
                raise CommerceError("Outline server is unavailable")
            existing_plans = {
                str(row["plan_code"]): int(row["slot_limit"] or 0)
                for row in connection.execute(
                    "SELECT plan_code, slot_limit FROM server_plan_allocations WHERE server_id = ?",
                    (server_id,),
                ).fetchall()
                if int(row["slot_limit"] or 0) > 0
            }
            existing_tiers = {
                str(row["tier_code"]): int(row["slot_limit"] or 0)
                for row in connection.execute(
                    "SELECT tier_code, slot_limit FROM server_tier_allocations WHERE server_id = ?",
                    (server_id,),
                ).fetchall()
                if int(row["slot_limit"] or 0) > 0
            }
            desired_plans = {
                code: limit for code, limit in normalized_plans.items() if limit > 0
            }
            desired_tiers = {
                code: limit for code, limit in normalized_tiers.items() if limit > 0
            }
            changed = (
                server["max_keys"] != max_keys
                or int(server["reserved_keys"] or 0) != int(reserved_keys)
                or server["monthly_traffic_bytes"] != monthly_traffic_bytes
                or existing_plans != desired_plans
                or existing_tiers != desired_tiers
            )
            if not changed:
                return
            connection.execute(
                """UPDATE outline_servers SET max_keys = ?, reserved_keys = ?,
                          monthly_traffic_bytes = ?, updated_at = ? WHERE server_id = ?""",
                (max_keys, reserved_keys, monthly_traffic_bytes, now_text, server_id),
            )
            # Clear stale rows first; no intermediate strict check is performed
            # until every manifest value is present in this transaction.
            connection.execute(
                "UPDATE server_plan_allocations SET slot_limit = 0, updated_at = ? WHERE server_id = ?",
                (now_text, server_id),
            )
            connection.execute(
                "UPDATE server_tier_allocations SET slot_limit = 0, updated_at = ? WHERE server_id = ?",
                (now_text, server_id),
            )
            for plan_code, slot_limit in desired_plans.items():
                connection.execute(
                    """INSERT INTO server_plan_allocations
                       (server_id, plan_code, slot_limit, updated_at) VALUES (?, ?, ?, ?)
                       ON CONFLICT(server_id, plan_code) DO UPDATE SET
                         slot_limit = excluded.slot_limit, updated_at = excluded.updated_at""",
                    (server_id, plan_code, slot_limit, now_text),
                )
            for tier_code, slot_limit in desired_tiers.items():
                connection.execute(
                    """INSERT INTO server_tier_allocations
                       (server_id, tier_code, slot_limit, updated_at) VALUES (?, ?, ?, ?)
                       ON CONFLICT(server_id, tier_code) DO UPDATE SET
                         slot_limit = excluded.slot_limit, updated_at = excluded.updated_at""",
                    (server_id, tier_code, slot_limit, now_text),
                )
            self._validate_server_allocation_capacity(connection, server_id)
            self._audit(
                connection,
                "server_policy_reconciled",
                "outline_server",
                server_id,
                "admin",
                str(admin_id),
                {
                    "max_keys": max_keys,
                    "reserved_keys": int(reserved_keys),
                    "monthly_traffic_bytes": monthly_traffic_bytes,
                    "plan_slots": desired_plans,
                    "tier_slots": desired_tiers,
                },
            )

    def _select_server_for_plan(self, connection: Any, plan_code: str, now_text: str) -> str:
        plan = connection.execute(
            "SELECT quota_bytes FROM plans WHERE code = ? AND active = 1", (plan_code,)
        ).fetchone()
        if plan is None:
            raise CommerceError("Unknown or inactive plan")
        requested_quota = int(plan["quota_bytes"] or 0)
        has_plan_allocations = int(
            connection.execute(
                "SELECT COUNT(*) AS n FROM server_plan_allocations WHERE plan_code = ?",
                (plan_code,),
            ).fetchone()["n"]
        ) > 0
        health_max_age = max(
            30, int(os.environ.get("AURIX_SERVER_HEALTH_MAX_AGE_SECONDS", "900"))
        )
        selection_time = datetime.fromisoformat(now_text).astimezone(UTC)
        fresh_after = _now_text(selection_time - timedelta(seconds=health_max_age))
        servers = connection.execute(
            """SELECT * FROM outline_servers
               WHERE enabled = 1 AND lifecycle_state = 'active'
                 AND health_status = 'healthy'
                 AND last_synced_at IS NOT NULL AND last_synced_at >= ?
               ORDER BY server_id""",
            (fresh_after,),
        ).fetchall()
        candidates: list[tuple[float, str]] = []
        for server in servers:
            server_id = str(server["server_id"])
            allocation = connection.execute(
                "SELECT slot_limit FROM server_plan_allocations WHERE server_id = ? AND plan_code = ?",
                (server_id, plan_code),
            ).fetchone()
            if has_plan_allocations and allocation is None:
                continue
            allocated_count = connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM subscriptions WHERE server_id = ? AND plan_code = ?
                        AND status IN ('pending', 'active')) +
                     (SELECT COUNT(*) FROM orders WHERE server_id = ? AND plan_code = ?
                        AND (status = 'payment_submitted' OR
                             (status = 'awaiting_payment' AND capacity_reserved_until > ?))) AS n""",
                (server_id, plan_code, server_id, plan_code, now_text),
            ).fetchone()["n"]
            if allocation is not None and int(allocated_count) >= int(allocation["slot_limit"]):
                continue
            remote_keys = int(server["remote_key_count"] or 0)
            reservations = connection.execute(
                """SELECT COUNT(*) AS n FROM orders WHERE server_id = ?
                   AND (status = 'payment_submitted' OR
                        (status = 'awaiting_payment' AND capacity_reserved_until > ?))""",
                (server_id, now_text),
            ).fetchone()["n"]
            pending_keys = connection.execute(
                "SELECT COUNT(*) AS n FROM subscriptions WHERE server_id = ? AND status = 'pending'",
                (server_id,),
            ).fetchone()["n"]
            max_keys = server["max_keys"]
            usable = None if max_keys is None else max(0, int(max_keys) - int(server["reserved_keys"] or 0))
            if usable is not None and remote_keys + int(reservations) + int(pending_keys) >= usable:
                continue
            traffic_budget = server["monthly_traffic_bytes"]
            if traffic_budget is not None:
                committed = connection.execute(
                    """SELECT
                       COALESCE((SELECT SUM(COALESCE(quota_bytes, 0)) FROM subscriptions
                         WHERE server_id = ? AND status IN ('pending', 'active')), 0) +
                       COALESCE((SELECT SUM(COALESCE(quota_bytes_snapshot, 0)) FROM orders
                         WHERE server_id = ? AND (status = 'payment_submitted' OR
                           (status = 'awaiting_payment' AND capacity_reserved_until > ?))), 0) AS n""",
                    (server_id, server_id, now_text),
                ).fetchone()["n"]
                if int(committed or 0) + requested_quota > int(traffic_budget):
                    continue
            denominator = int(allocation["slot_limit"]) if allocation is not None and int(allocation["slot_limit"]) else (usable or 1)
            candidates.append((int(allocated_count) / max(1, denominator), server_id))
        if not candidates:
            raise CommerceError("This plan is temporarily full. Please check again later.")
        return min(candidates)[1]

    def plans(self) -> list[Plan]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT code, name, price_minor, currency, quota_bytes, duration_days
                   FROM plans WHERE active = 1 ORDER BY price_minor"""
            ).fetchall()
        return [Plan(**dict(row)) for row in rows]

    def create_wallet_topup(
        self,
        telegram_id: int,
        first_name: str,
        amount_minor: int,
        now: datetime | None = None,
        username: str | None = None,
    ) -> OrderResult:
        """Create a receipt-backed deposit that credits wallet balance only."""
        try:
            amount_minor = int(amount_minor)
        except (TypeError, ValueError) as exc:
            raise CommerceError("Top-up amount must be a whole number of MMK") from exc
        if not 1_000 <= amount_minor <= 1_000_000:
            raise CommerceError("Wallet top-up must be between 1,000 and 1,000,000 MMK")
        plan = Plan("wallet_topup", "Wallet Top-up", amount_minor, "MMK", None, 1)
        order_id = _new_id()
        created_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._ensure_user(connection, telegram_id, first_name, username)
            if isinstance(connection, _PostgresConnection):
                connection.execute(
                    "SELECT telegram_id FROM users WHERE telegram_id = ? FOR UPDATE",
                    (telegram_id,),
                ).fetchone()
            existing = connection.execute(
                """SELECT * FROM orders
                   WHERE telegram_id = ?
                     AND status IN ('awaiting_payment', 'payment_submitted')
                     AND COALESCE(refund_status, 'none') != 'refunded'
                   ORDER BY created_at LIMIT 1""",
                (telegram_id,),
            ).fetchone()
            if existing is not None:
                existing_plan = Plan(
                    str(existing["plan_code"]),
                    str(existing["plan_name"] or existing["plan_code"]),
                    int(existing["amount_minor"]),
                    str(existing["currency"]),
                    existing["quota_bytes_snapshot"],
                    int(existing["duration_days_snapshot"] or 1),
                )
                return OrderResult(
                    str(existing["id"]),
                    existing_plan,
                    str(existing["status"]),
                    False,
                    str(existing["plan_code"]) != "wallet_topup"
                    or int(existing["amount_minor"]) != amount_minor,
                )
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, plan_name,
                    quota_bytes_snapshot, duration_days_snapshot, status, created_at)
                   VALUES (?, ?, 'wallet_topup', ?, 'MMK', 'Wallet Top-up',
                           NULL, 1, 'awaiting_payment', ?)""",
                (order_id, telegram_id, amount_minor, created_at),
            )
            self._audit(
                connection,
                "wallet_topup_created",
                "order",
                order_id,
                "customer",
                str(telegram_id),
                {"amount_minor": amount_minor, "currency": "MMK"},
            )
            self._queue_staff_notification(
                connection,
                "order_created",
                order_id,
                "💰 WALLET TOP-UP\n\n"
                f"Order: #{order_id[:8]}\nCustomer: tg:{telegram_id}\n"
                f"Amount: {amount_minor:,} MMK\n\nStatus: waiting for receipt",
                created_at,
            )
        return OrderResult(order_id, plan, "awaiting_payment")

    @staticmethod
    def _receipt_timestamp(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=6, minutes=30)))
        return parsed.astimezone(UTC)

    def _receipt_risk_flags(
        self, extraction: dict[str, Any], order: Any, submitted_at: datetime
    ) -> list[str]:
        selected_provider = str(order["payment_method"] or "")
        if not selected_provider:
            try:
                selected_provider = str(order["provider"] or "")
            except (KeyError, IndexError):
                selected_provider = ""
        evaluated = evaluate_receipt_candidate(
            extraction,
            selected_provider=selected_provider,
            expected_amount_minor=int(order["amount_minor"]),
            expected_currency=str(order["currency"]),
            submitted_at=submitted_at,
            recipient_profiles=self.receipt_recipient_profiles,
        )
        extraction["automation_decision"] = evaluated["automation_decision"]
        extraction["rule_checks"] = evaluated["rule_checks"]
        return list(evaluated["flags"])

    def get_plan(self, code: str) -> Plan:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT code, name, price_minor, currency, quota_bytes, duration_days
                   FROM plans WHERE code = ? AND active = 1""",
                (code,),
            ).fetchone()
        if row is None:
            raise CommerceError("Unknown or inactive plan")
        return Plan(**dict(row))

    def plan_availability(self, now: datetime | None = None) -> dict[str, dict[str, Any]]:
        """Return admission availability from configured per-server allocations."""
        now_text = _now_text(now)
        result: dict[str, dict[str, Any]] = {}
        health_max_age = max(
            30, int(os.environ.get("AURIX_SERVER_HEALTH_MAX_AGE_SECONDS", "900"))
        )
        fresh_after = _now_text((now or datetime.now(UTC)) - timedelta(seconds=health_max_age))
        with self.database.connect() as connection:
            plans = connection.execute("SELECT code FROM plans WHERE active = 1").fetchall()
            server_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM outline_servers WHERE enabled = 1"
                ).fetchone()["n"]
            )
            for plan in plans:
                code = str(plan["code"])
                allocations = connection.execute(
                    """SELECT a.server_id, a.slot_limit FROM server_plan_allocations a
                       JOIN outline_servers s ON s.server_id = a.server_id
                       WHERE a.plan_code = ? AND s.enabled = 1
                         AND s.health_status = 'healthy'
                         AND s.last_synced_at IS NOT NULL AND s.last_synced_at >= ?""",
                    (code, fresh_after),
                ).fetchall()
                if not server_count:
                    result[code] = {"available": True, "remaining_slots": None, "managed": False}
                    continue
                if not allocations:
                    # Total server limits still protect admission; plan-specific
                    # allocation remains optional until the owner configures it.
                    try:
                        self._select_server_for_plan(connection, code, now_text)
                        result[code] = {"available": True, "remaining_slots": None, "managed": False}
                    except CommerceError:
                        result[code] = {"available": False, "remaining_slots": 0, "managed": False}
                    continue
                remaining = 0
                for allocation in allocations:
                    used = connection.execute(
                        """SELECT
                           (SELECT COUNT(*) FROM subscriptions WHERE server_id = ? AND plan_code = ?
                              AND status IN ('pending', 'active')) +
                           (SELECT COUNT(*) FROM orders WHERE server_id = ? AND plan_code = ?
                              AND (status = 'payment_submitted' OR
                                   (status = 'awaiting_payment' AND capacity_reserved_until > ?))) AS n""",
                        (allocation["server_id"], code, allocation["server_id"], code, now_text),
                    ).fetchone()["n"]
                    remaining += max(0, int(allocation["slot_limit"]) - int(used))
                result[code] = {"available": remaining > 0, "remaining_slots": remaining, "managed": True}
        return result

    @staticmethod
    def _ensure_user(
        connection: sqlite3.Connection,
        telegram_id: int,
        first_name: str,
        username: str | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO users (telegram_id, first_name, username, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                   first_name = excluded.first_name,
                   username = COALESCE(excluded.username, users.username)""",
            (
                telegram_id,
                first_name[:128],
                (username or "").lstrip("@")[:64] or None,
                _now_text(),
            ),
        )

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        action: str,
        target_type: str,
        target_id: str,
        actor_type: str,
        actor_id: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO audit_events
               (actor_type, actor_id, action, target_type, target_id, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                actor_type,
                actor_id,
                action,
                target_type,
                target_id,
                json.dumps(metadata or {}, sort_keys=True),
                _now_text(),
            ),
        )

    @staticmethod
    def _queue_staff_notification(
        connection: Any,
        event_type: str,
        entity_id: str,
        text: str,
        created_at: str,
    ) -> None:
        """Durably fan out one deduplicated operational alert per opted-in staff member."""
        try:
            rows = connection.execute(
                """SELECT s.telegram_id
                   FROM staff_accounts s
                   LEFT JOIN staff_notification_preferences p
                     ON p.telegram_id = s.telegram_id AND p.event_type = ?
                   WHERE s.status = 'active' AND COALESCE(p.enabled, 1) = 1""",
                (event_type,),
            ).fetchall()
        except Exception:
            # Some isolated commerce test/migration stores do not include the
            # free-access staff component. Runtime uses the shared database.
            return
        for row in rows:
            staff_id = int(row["telegram_id"])
            connection.execute(
                """INSERT INTO notifications
                   (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                   ON CONFLICT(dedupe_key) DO NOTHING""",
                (
                    _new_id(),
                    f"staff:{event_type}:{entity_id}:{staff_id}",
                    staff_id,
                    f"staff_{event_type}",
                    text,
                    created_at,
                    created_at,
                ),
            )

    @staticmethod
    def _queue_receipt_extraction(connection: Any, evidence_id: str, created_at: str) -> None:
        connection.execute(
            """INSERT INTO receipt_extraction_jobs
               (id, evidence_id, status, next_attempt_at, created_at)
               VALUES (?, ?, 'pending', ?, ?)
               ON CONFLICT(evidence_id) DO NOTHING""",
            (_new_id(), evidence_id, created_at, created_at),
        )

    def create_order(
        self,
        telegram_id: int,
        first_name: str,
        plan_code: str,
        now: datetime | None = None,
        username: str | None = None,
    ) -> OrderResult:
        plan = self.get_plan(plan_code)
        order_id = _new_id()
        created_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._ensure_user(connection, telegram_id, first_name, username)
            if isinstance(connection, _PostgresConnection):
                connection.execute(
                    "SELECT telegram_id FROM users WHERE telegram_id = ? FOR UPDATE",
                    (telegram_id,),
                ).fetchone()
            self._assert_no_active_promo(connection, telegram_id)
            existing = connection.execute(
                """SELECT * FROM orders
                   WHERE telegram_id = ?
                     AND status IN ('awaiting_payment', 'payment_submitted')
                     AND COALESCE(refund_status, 'none') != 'refunded'
                   ORDER BY created_at LIMIT 1""",
                (telegram_id,),
            ).fetchone()
            if existing is not None:
                existing_plan = Plan(
                    code=str(existing["plan_code"]),
                    name=str(existing["plan_name"] or plan.name),
                    price_minor=int(existing["amount_minor"]),
                    currency=str(existing["currency"]),
                    quota_bytes=(
                        existing["quota_bytes_snapshot"]
                        if existing["quota_bytes_snapshot"] is not None
                        else plan.quota_bytes
                    ),
                    duration_days=int(existing["duration_days_snapshot"] or plan.duration_days),
                )
                return OrderResult(
                    str(existing["id"]),
                    existing_plan,
                    str(existing["status"]),
                    False,
                    existing_plan.code != plan.code,
                )
            registered = connection.execute(
                "SELECT COUNT(*) AS n FROM outline_servers WHERE enabled = 1"
            ).fetchone()["n"]
            server_id = (
                self._select_server_for_plan(connection, plan.code, created_at)
                if int(registered)
                else None
            )
            reserved_until = (
                ((now or datetime.now(UTC)).astimezone(UTC) + timedelta(hours=24)).isoformat()
                if server_id
                else None
            )
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, plan_name,
                    quota_bytes_snapshot, duration_days_snapshot, status, created_at,
                    server_id, capacity_reserved_until)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_payment', ?, ?, ?)""",
                (
                    order_id,
                    telegram_id,
                    plan.code,
                    plan.price_minor,
                    plan.currency,
                    plan.name,
                    plan.quota_bytes,
                    plan.duration_days,
                    created_at,
                    server_id,
                    reserved_until,
                ),
            )
            self._audit(
                connection,
                "order_created",
                "order",
                order_id,
                "customer",
                str(telegram_id),
                {"plan_code": plan.code, "amount_minor": plan.price_minor, "server_id": server_id},
            )
            self._queue_staff_notification(
                connection,
                "order_created",
                order_id,
                "🛒 NEW ORDER\n\n"
                f"Order: #{order_id[:8]}\n"
                f"Customer: tg:{telegram_id}\n"
                f"Plan: {plan.name}\n"
                f"Amount: {plan.price_minor:,} {plan.currency}\n\n"
                "Status: waiting for payment",
                created_at,
            )
        return OrderResult(order_id, plan, "awaiting_payment")

    def replace_open_order(
        self,
        telegram_id: int,
        first_name: str,
        plan_code: str,
        now: datetime | None = None,
        username: str | None = None,
        expected_order_id: str | None = None,
    ) -> OrderResult:
        """Replace an untouched open order with a different plan."""
        plan = self.get_plan(plan_code)
        created_at = _now_text(now)
        new_order_id = _new_id()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._ensure_user(connection, telegram_id, first_name, username)
            if isinstance(connection, _PostgresConnection):
                connection.execute(
                    "SELECT telegram_id FROM users WHERE telegram_id = ? FOR UPDATE",
                    (telegram_id,),
                ).fetchone()
            self._assert_no_active_promo(connection, telegram_id)
            existing = connection.execute(
                """SELECT * FROM orders
                   WHERE telegram_id = ? AND status IN ('awaiting_payment', 'payment_submitted')
                     AND COALESCE(refund_status, 'none') != 'refunded'
                   ORDER BY created_at LIMIT 1""",
                (telegram_id,),
            ).fetchone()
            if existing is None:
                raise CommerceError("No open order is available to replace")
            self._lock_order(connection, str(existing["id"]))
            if expected_order_id and str(existing["id"]) != str(expected_order_id):
                raise CommerceError("The open order changed; refresh and try again")
            if existing["plan_code"] == plan.code:
                return OrderResult(str(existing["id"]), plan, str(existing["status"]), False)
            evidence_count = connection.execute(
                "SELECT COUNT(*) AS n FROM payment_evidence WHERE order_id = ?",
                (existing["id"],),
            ).fetchone()["n"]
            payment_count = connection.execute(
                "SELECT COUNT(*) AS n FROM payments WHERE order_id = ?",
                (existing["id"],),
            ).fetchone()["n"]
            if evidence_count or payment_count:
                raise CommerceError(
                    "This order has payment activity and cannot be replaced; ask staff to review it"
                )
            connection.execute(
                "UPDATE orders SET status = 'cancelled', rejected_at = ? WHERE id = ?",
                (created_at, existing["id"]),
            )
            registered = connection.execute(
                "SELECT COUNT(*) AS n FROM outline_servers WHERE enabled = 1"
            ).fetchone()["n"]
            server_id = (
                self._select_server_for_plan(connection, plan.code, created_at)
                if int(registered)
                else None
            )
            reserved_until = (
                ((now or datetime.now(UTC)).astimezone(UTC) + timedelta(hours=24)).isoformat()
                if server_id
                else None
            )
            self._audit(
                connection,
                "order_replaced",
                "order",
                str(existing["id"]),
                "customer",
                str(telegram_id),
                {"new_order_id": new_order_id, "plan_code": plan.code},
            )
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, plan_name,
                    quota_bytes_snapshot, duration_days_snapshot, status, created_at,
                    server_id, capacity_reserved_until)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_payment', ?, ?, ?)""",
                (
                    new_order_id,
                    telegram_id,
                    plan.code,
                    plan.price_minor,
                    plan.currency,
                    plan.name,
                    plan.quota_bytes,
                    plan.duration_days,
                    created_at,
                    server_id,
                    reserved_until,
                ),
            )
            self._audit(
                connection,
                "order_created",
                "order",
                new_order_id,
                "customer",
                str(telegram_id),
                {"plan_code": plan.code, "replaces_order_id": str(existing["id"])},
            )
        return OrderResult(new_order_id, plan, "awaiting_payment")

    def cancel_order(self, telegram_id: int, order_id: str, now: datetime | None = None) -> str:
        """Cancel an empty customer order, or release a wallet reservation."""
        cancelled_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute(
                "SELECT * FROM orders WHERE id = ? AND telegram_id = ?",
                (order_id, telegram_id),
            ).fetchone()
            if order is None:
                raise CommerceError("Order not found")
            if order["status"] in ("cancelled", "rejected"):
                return "already_cancelled"
            if order["status"] == "approved":
                raise CommerceError("An approved order cannot be cancelled")
            evidence_count = connection.execute(
                "SELECT COUNT(*) AS n FROM payment_evidence WHERE order_id = ?",
                (order_id,),
            ).fetchone()["n"]
            payment_count = connection.execute(
                "SELECT COUNT(*) AS n FROM payments WHERE order_id = ?",
                (order_id,),
            ).fetchone()["n"]
            if evidence_count or payment_count:
                raise CommerceError(
                    "This order has payment activity; ask staff to reject or refund it"
                )
            connection.execute(
                "UPDATE orders SET status = 'cancelled', rejected_at = ? WHERE id = ?",
                (cancelled_at, order_id),
            )
            self._audit(
                connection,
                "order_cancelled",
                "order",
                order_id,
                "customer",
                str(telegram_id),
            )
        return "cancelled"

    def expire_open_orders(
        self,
        now: datetime | None = None,
        awaiting_ttl: timedelta = timedelta(hours=24),
    ) -> int:
        """Close only untouched unpaid orders past the customer-facing TTL."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        cutoff = _now_text(current - awaiting_ttl)
        closed = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            rows = connection.execute(
                """SELECT o.id FROM orders o
                   WHERE o.status = 'awaiting_payment' AND o.created_at <= ?
                     AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.id)
                     AND NOT EXISTS (SELECT 1 FROM payment_evidence e WHERE e.order_id = o.id)""",
                (cutoff,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE orders SET status = 'cancelled', rejected_at = ? WHERE id = ?",
                    (_now_text(current), row["id"]),
                )
                self._audit(
                    connection,
                    "order_expired",
                    "order",
                    str(row["id"]),
                    "system",
                    None,
                )
                closed += 1
        return closed

    def release_expired_wallet_reservations(
        self,
        now: datetime | None = None,
        reservation_ttl: timedelta = timedelta(hours=24),
    ) -> int:
        """Release wallet holds that were never approved by staff."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        cutoff = _now_text(current - reservation_ttl)
        released = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            rows = connection.execute(
                """SELECT r.order_id, r.telegram_id, r.amount_minor, r.currency
                   FROM wallet_reservations r JOIN orders o ON o.id = r.order_id
                   WHERE r.status = 'reserved' AND r.created_at <= ?
                     AND o.status = 'payment_submitted'""",
                (cutoff,),
            ).fetchall()
            for row in rows:
                idem = f"release:{row['order_id']}"
                if (
                    connection.execute(
                        "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (idem,)
                    ).fetchone()
                    is None
                ):
                    connection.execute(
                        "UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ? WHERE telegram_id = ?",
                        (row["amount_minor"], _now_text(current), row["telegram_id"]),
                    )
                    connection.execute(
                        """INSERT INTO wallet_ledger
                           (id, telegram_id, kind, amount_minor, currency, reference_type,
                            reference_id, idempotency_key, created_at)
                           VALUES (?, ?, 'release', ?, ?, 'order', ?, ?, ?)""",
                        (
                            _new_id(),
                            row["telegram_id"],
                            row["amount_minor"],
                            row["currency"],
                            row["order_id"],
                            idem,
                            _now_text(current),
                        ),
                    )
                connection.execute(
                    "UPDATE wallet_reservations SET status = 'released', updated_at = ? WHERE order_id = ?",
                    (_now_text(current), row["order_id"]),
                )
                connection.execute(
                    "UPDATE payments SET status = 'rejected' WHERE order_id = ? AND provider = 'wallet' AND status = 'submitted'",
                    (row["order_id"],),
                )
                connection.execute(
                    "UPDATE orders SET status = 'cancelled', rejected_at = ? WHERE id = ? AND status = 'payment_submitted'",
                    (_now_text(current), row["order_id"]),
                )
                connection.execute(
                    """INSERT INTO notifications
                       (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
                       VALUES (?, ?, ?, 'wallet_reservation_expired', ?, 'pending', ?, ?)
                       ON CONFLICT(dedupe_key) DO NOTHING""",
                    (
                        _new_id(),
                        f"wallet-reservation-expired:{row['order_id']}",
                        row["telegram_id"],
                        "Your wallet payment hold expired before approval; the funds were returned to your wallet.",
                        _now_text(current),
                        _now_text(current),
                    ),
                )
                self._audit(
                    connection,
                    "wallet_reservation_expired",
                    "order",
                    row["order_id"],
                    "system",
                    None,
                )
                released += 1
        return released

    @staticmethod
    def _order_stage(order: dict[str, Any]) -> str:
        """Derive one customer-facing stage from order/payment/evidence state."""
        status = str(order.get("status") or "")
        subscription = str(order.get("subscription_status") or "")
        payment = str(order.get("payment_status") or "")
        receipt = str(order.get("receipt_status") or "")
        reservation = str(order.get("wallet_reservation_status") or "")
        if str(order.get("refund_status") or "none") == "refunded" or payment == "refunded":
            return "refunded"
        provision = str(order.get("provisioning_status") or "")
        revoke = str(order.get("revocation_status") or "")
        if revoke in ("pending", "running"):
            return "revocation_pending"
        if revoke == "failed":
            return "revocation_failed"
        if status == "approved":
            if provision == "failed":
                return "activation_failed"
            if subscription == "active":
                return "fulfilled"
            if subscription == "pending":
                return "activation_pending"
            return "approved"
        if status in ("rejected", "cancelled"):
            return status
        if receipt == "verified" or payment == "verified":
            return "payment_verified"
        if reservation == "reserved":
            return "wallet_reserved"
        if receipt == "pending" or payment == "submitted":
            return "review_pending"
        return "awaiting_payment"

    def list_user_orders(self, telegram_id: int, limit: int = 10) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT o.id, o.plan_code, o.plan_name, o.amount_minor, o.currency,
                          o.status, o.refund_status, o.created_at,
                          (SELECT p.status FROM payments p WHERE p.order_id = o.id
                           ORDER BY p.submitted_at DESC LIMIT 1) AS payment_status,
                          (SELECT e.review_status FROM payment_evidence e WHERE e.order_id = o.id
                           ORDER BY e.submitted_at DESC LIMIT 1) AS receipt_status,
                          (SELECT s.status FROM subscriptions s WHERE s.order_id = o.id
                           LIMIT 1) AS subscription_status,
                          (SELECT s.expires_at FROM subscriptions s WHERE s.order_id = o.id
                           LIMIT 1) AS expires_at,
                          (SELECT j.status FROM provisioning_jobs j JOIN subscriptions s
                           ON s.id = j.subscription_id WHERE s.order_id = o.id
                           AND j.operation = 'provision' LIMIT 1) AS provisioning_status,
                          (SELECT j.status FROM provisioning_jobs j JOIN subscriptions s
                           ON s.id = j.subscription_id WHERE s.order_id = o.id
                           AND j.operation = 'revoke' LIMIT 1) AS revocation_status,
                          (SELECT r.status FROM wallet_reservations r WHERE r.order_id = o.id
                           LIMIT 1) AS wallet_reservation_status
                   FROM orders o WHERE o.telegram_id = ?
                   ORDER BY o.created_at DESC LIMIT ?""",
                (telegram_id, max(1, min(limit, 50))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["stage"] = self._order_stage(item)
            result.append(item)
        return result

    def reconcile_duplicate_open_orders(self) -> dict[str, int]:
        """Cancel only empty historical duplicates, preserving review evidence."""
        cancelled = 0
        manual_conflicts = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            users = connection.execute(
                """SELECT telegram_id FROM orders
                   WHERE status IN ('awaiting_payment', 'payment_submitted')
                   GROUP BY telegram_id HAVING COUNT(*) > 1"""
            ).fetchall()
            for user in users:
                rows = connection.execute(
                    """SELECT o.id, o.created_at,
                              (SELECT COUNT(*) FROM payments p WHERE p.order_id = o.id) AS payments,
                              (SELECT COUNT(*) FROM payment_evidence e WHERE e.order_id = o.id) AS evidence
                       FROM orders o WHERE o.telegram_id = ?
                         AND o.status IN ('awaiting_payment', 'payment_submitted')
                       ORDER BY o.created_at""",
                    (user["telegram_id"],),
                ).fetchall()
                protected = [row for row in rows if int(row["payments"]) or int(row["evidence"])]
                keeper_id = (protected[0] if protected else rows[0])["id"]
                if len(protected) > 1:
                    manual_conflicts += 1
                for row in rows:
                    if row["id"] == keeper_id:
                        continue
                    if int(row["payments"]) or int(row["evidence"]):
                        continue
                    connection.execute(
                        "UPDATE orders SET status = 'cancelled' WHERE id = ?",
                        (row["id"],),
                    )
                    self._audit(
                        connection,
                        "duplicate_empty_order_cancelled",
                        "order",
                        str(row["id"]),
                        "system",
                        None,
                        {"kept_order_id": str(keeper_id)},
                    )
                    cancelled += 1
        return {"cancelled": cancelled, "manual_conflicts": manual_conflicts}

    def order_detail(
        self, order_id: str, requester_id: int, is_admin: bool = False
    ) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT o.*,
                          (SELECT p.status FROM payments p WHERE p.order_id = o.id
                           ORDER BY p.submitted_at DESC LIMIT 1) AS payment_status,
                          (SELECT p.provider FROM payments p WHERE p.order_id = o.id
                           ORDER BY p.submitted_at DESC LIMIT 1) AS payment_provider,
                          (SELECT e.review_status FROM payment_evidence e WHERE e.order_id = o.id
                           ORDER BY e.submitted_at DESC LIMIT 1) AS receipt_status,
                          (SELECT e.id FROM payment_evidence e WHERE e.order_id = o.id
                           ORDER BY e.submitted_at DESC LIMIT 1) AS evidence_id,
                          (SELECT s.status FROM subscriptions s WHERE s.order_id = o.id
                           LIMIT 1) AS subscription_status,
                          (SELECT s.expires_at FROM subscriptions s WHERE s.order_id = o.id
                           LIMIT 1) AS expires_at,
                          (SELECT j.status FROM provisioning_jobs j JOIN subscriptions s
                           ON s.id = j.subscription_id
                           WHERE s.order_id = o.id AND j.operation = 'provision'
                           LIMIT 1) AS provisioning_status,
                          (SELECT j.status FROM provisioning_jobs j JOIN subscriptions s
                           ON s.id = j.subscription_id
                           WHERE s.order_id = o.id AND j.operation = 'revoke'
                           LIMIT 1) AS revocation_status,
                          (SELECT r.status FROM wallet_reservations r WHERE r.order_id = o.id
                           LIMIT 1) AS wallet_reservation_status
                   FROM orders o WHERE o.id = ?""",
                (order_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if not is_admin and int(result["telegram_id"]) != int(requester_id):
            return None
        result["stage"] = self._order_stage(result)
        return result

    def choose_payment_method(
        self, telegram_id: int, order_id: str, payment_method: str
    ) -> dict[str, Any]:
        """Attach an explicit local transfer rail to an open customer order."""
        method = str(payment_method).strip().lower()
        if method not in LOCAL_PAYMENT_METHODS:
            raise CommerceError("That payment method is unavailable")
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if order is None or int(order["telegram_id"]) != int(telegram_id):
                raise CommerceError("Order not found")
            if order["status"] != "awaiting_payment":
                raise CommerceError("The payment method can no longer be changed")
            evidence = connection.execute(
                "SELECT 1 FROM payment_evidence WHERE order_id = ? LIMIT 1", (order_id,)
            ).fetchone()
            if evidence is not None:
                raise CommerceError("A receipt is already attached to this order")
            connection.execute(
                "UPDATE orders SET payment_method = ? WHERE id = ?", (method, order_id)
            )
            self._audit(
                connection,
                "payment_method_selected",
                "order",
                order_id,
                "customer",
                str(telegram_id),
                {"payment_method": method},
            )
        result = self.order_detail(order_id, telegram_id)
        if result is None:  # pragma: no cover - transaction just verified ownership
            raise CommerceError("Order not found")
        return result

    def submit_payment(
        self,
        telegram_id: int,
        order_id: str,
        provider: str,
        provider_reference: str,
        now: datetime | None = None,
    ) -> str:
        # Provider identity is canonicalized alongside the reference so the
        # database uniqueness constraint, legacy rows, and application checks
        # all agree on values such as ``Manual`` vs `` manual ``.
        provider = _normalize_reference(provider)[:64]
        provider_reference = provider_reference.strip()[:128]
        normalized_reference = _normalize_reference(provider_reference)
        if not provider or not provider_reference:
            raise CommerceError("Payment provider and reference are required")
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if order is None or order["telegram_id"] != telegram_id:
                raise CommerceError("Order not found")
            if str(order["plan_code"]) != "wallet_topup":
                self._assert_no_active_promo(connection, telegram_id)
            if order["status"] == "approved":
                raise CommerceError("Order is already approved")
            if order["status"] not in ("awaiting_payment", "payment_submitted"):
                raise CommerceError("Order is not open for payment")
            existing = connection.execute(
                """SELECT provider, provider_reference FROM payments WHERE order_id = ?
                   ORDER BY submitted_at DESC LIMIT 1""",
                (order_id,),
            ).fetchone()
            if existing is not None:
                if (
                    _normalize_reference(existing["provider"]) == _normalize_reference(provider)
                    and _normalize_reference(existing["provider_reference"]) == normalized_reference
                ):
                    return "already_submitted"
                raise CommerceError("A payment reference is already attached to this order")
            try:
                connection.execute(
                    """INSERT INTO payments
                       (id, order_id, provider, provider_reference, normalized_reference, status, submitted_at)
                       VALUES (?, ?, ?, ?, ?, 'submitted', ?)""",
                    (
                        _new_id(),
                        order_id,
                        provider,
                        provider_reference,
                        normalized_reference,
                        _now_text(now),
                    ),
                )
            except Exception as exc:
                if self.database.is_integrity_error(exc):
                    raise CommerceError("Payment reference has already been submitted") from exc
                raise
            connection.execute(
                "UPDATE orders SET status = 'payment_submitted' WHERE id = ?",
                (order_id,),
            )
            self._audit(
                connection,
                "payment_submitted",
                "order",
                order_id,
                "customer",
                str(telegram_id),
                {"provider": provider},
            )
        return "submitted"

    def pending_order_for_user(self, telegram_id: int) -> dict[str, Any] | None:
        """Return the oldest open order so a receipt can be sent without text."""
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT * FROM orders
                   WHERE telegram_id = ? AND status IN ('awaiting_payment', 'payment_submitted')
                     AND COALESCE(refund_status, 'none') != 'refunded'
                   ORDER BY created_at LIMIT 1""",
                (telegram_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def open_order_ids_for_user(self, telegram_id: int, limit: int = 20) -> list[str]:
        """Return open order IDs for uncaptioned receipt routing.

        A customer may intentionally purchase multiple keys.  The Telegram
        transport uses this list to refuse ambiguous uncaptioned screenshots;
        an order-specific Upload Receipt button or an explicit caption then
        selects the intended order.
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT id FROM orders
                   WHERE telegram_id = ?
                     AND status IN ('awaiting_payment', 'payment_submitted')
                     AND COALESCE(refund_status, 'none') != 'refunded'
                   ORDER BY created_at LIMIT ?""",
                (telegram_id, max(1, min(int(limit), 100))),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def receipt_duplicate_status(
        self,
        telegram_id: int,
        order_id: str,
        image_bytes: bytes,
        file_unique_id: str | None = None,
        provider: str | None = None,
    ) -> str:
        """Check immutable and near-duplicate identity before vision work.

        Exact SHA-256/Telegram identity remains a hard duplicate check. A
        perceptual match is a conservative cross-order fraud signal for
        resized/re-encoded screenshots; a match is held out of model spend and
        sent to manual review, never treated as proof of a payment.
        """
        digest = hashlib.sha256(image_bytes).hexdigest()
        phash = receipt_perceptual_hash(image_bytes)
        expected_provider = str(provider or "").strip().lower()
        with self.database.connect() as connection:
            order = connection.execute(
                "SELECT telegram_id, payment_method FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if order is None or int(order["telegram_id"]) != int(telegram_id):
                raise CommerceError("Order not found")
            if not expected_provider:
                expected_provider = str(order["payment_method"] or "").strip().lower()
            rows = connection.execute(
                """SELECT order_id, provider, image_phash FROM payment_evidence
                   WHERE image_sha256 = ? OR
                         (? IS NOT NULL AND telegram_file_unique_id = ?)""",
                (digest, file_unique_id, file_unique_id),
            ).fetchall()
        if any(str(row["order_id"]) != str(order_id) for row in rows):
            return "different_order"
        if rows:
            return "same_order"
        if phash:
            with self.database.connect() as connection:
                prior_rows = connection.execute(
                    """SELECT order_id, provider, image_phash FROM payment_evidence
                       WHERE image_phash IS NOT NULL"""
                ).fetchall()
            for row in prior_rows:
                if expected_provider and str(row["provider"] or "").strip().lower() != expected_provider:
                    continue
                distance = fingerprint_distance(phash, str(row["image_phash"] or ""))
                if distance is not None and distance <= NEAR_DUPLICATE_DISTANCE:
                    return "possible_duplicate"
        return "new"

    def submit_receipt(
        self,
        telegram_id: int,
        order_id: str,
        provider: str,
        file_id: str,
        file_unique_id: str | None,
        image_bytes: bytes,
        mime_type: str,
        extraction: dict[str, Any] | None = None,
        now: datetime | None = None,
        telegram_media_type: str = "photo",
        queue_extraction: bool = False,
    ) -> dict[str, Any]:
        """Persist receipt metadata and upload the raw image out-of-band.

        The database transaction creates an upload-pending evidence row, then
        the object is uploaded without holding a database connection open. A
        second short transaction marks the object stored and moves the order to
        payment review. This keeps network latency out of the database lock and
        makes a lost response safely retryable using the same immutable path.
        """
        if not isinstance(file_id, str) or not file_id.strip():
            raise CommerceError("Receipt file id is missing")
        if not image_bytes or len(image_bytes) > 20 * 1024 * 1024:
            raise CommerceError("Receipt image is empty or too large")
        if telegram_media_type not in ("photo", "document"):
            raise CommerceError("Receipt media type is invalid")
        digest = hashlib.sha256(image_bytes).hexdigest()
        phash = receipt_perceptual_hash(image_bytes)
        extraction = extraction if isinstance(extraction, dict) else None
        tx_id = extraction.get("transaction_id") if extraction else None
        # The customer-selected method is authoritative workflow state. Model
        # output may describe a different provider, but must never overwrite it.
        provider_name = _normalize_reference(provider)[:64]
        tx_candidate = str(tx_id).strip()[:128] if tx_id else ""
        status = "parsed" if tx_id else "needs_review"
        submitted_at = _now_text(now)
        storage_configured = self._storage_is_configured()
        if self.receipt_storage_required and not storage_configured:
            raise CommerceError("Receipt storage is not configured")
        storage_status = "pending" if storage_configured else "not_configured"
        storage_bucket = self._storage_bucket() if storage_configured else None
        evidence_id: str
        storage_path: str | None = None
        is_new = False
        near_duplicate = False
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if order is None or order["telegram_id"] != telegram_id:
                raise CommerceError("Order not found")
            if str(order["plan_code"]) != "wallet_topup":
                self._assert_no_active_promo(connection, telegram_id)
            if order["status"] == "approved":
                raise CommerceError("Order is already approved")
            if order["status"] not in ("awaiting_payment", "payment_submitted"):
                raise CommerceError("Order is not open for a receipt")
            duplicate = connection.execute(
                """SELECT order_id FROM payment_evidence
                   WHERE (image_sha256 = ? OR
                          (? IS NOT NULL AND telegram_file_unique_id = ?))
                     AND order_id != ? LIMIT 1""",
                (digest, file_unique_id, file_unique_id, order_id),
            ).fetchone()
            if duplicate is not None:
                raise CommerceError("This receipt was already submitted for another order")
            if phash:
                prior_hashes = connection.execute(
                    """SELECT order_id, provider, image_phash FROM payment_evidence
                       WHERE image_phash IS NOT NULL AND order_id != ?""",
                    (order_id,),
                ).fetchall()
                for prior in prior_hashes:
                    if str(prior["provider"] or "").strip().lower() != provider_name:
                        continue
                    distance = fingerprint_distance(phash, str(prior["image_phash"] or ""))
                    if distance is not None and distance <= NEAR_DUPLICATE_DISTANCE:
                        # Do not hard-reject a perceptual match: two receipts
                        # from the same provider can share a template. Preserve
                        # the upload, stop automatic extraction, and surface a
                        # strong manual-review flag instead.
                        near_duplicate = True
                        status = "needs_review"
                        flagged = dict(extraction or {})
                        flagged["flags"] = sorted(
                            set(flagged.get("flags") or [])
                            | {"duplicate_image_candidate"}
                        )
                        extraction = flagged
                        break
            existing = connection.execute(
                """SELECT id, extraction_json, extraction_status, review_status,
                          storage_bucket, storage_path, storage_status
                   FROM payment_evidence
                   WHERE order_id = ? AND image_sha256 = ?""",
                (order_id, digest),
            ).fetchone()
            if existing is not None:
                parsed = json.loads(existing["extraction_json"] or "{}")
                result = parsed if isinstance(parsed, dict) else {}
                result["evidence_id"] = existing["id"]
                result["extraction_status"] = existing["extraction_status"]
                result["review_status"] = existing["review_status"]
                result["image_sha256"] = digest
                result["storage_status"] = existing["storage_status"] or "not_configured"
                result["storage_path"] = existing["storage_path"]
                storage_ready = (
                    result["storage_status"] == "stored"
                    if storage_configured
                    else result["storage_status"] in ("stored", "not_configured")
                )
                if storage_ready:
                    # A prior process may have committed the evidence row but
                    # lost the response before moving the order state. Repair
                    # that narrow inconsistency on an idempotent retry.
                    if order["status"] == "awaiting_payment":
                        connection.execute(
                            "UPDATE orders SET status = 'payment_submitted' WHERE id = ?",
                            (order_id,),
                        )
                        self._audit(
                            connection,
                            "receipt_state_recovered",
                            "order",
                            order_id,
                            "customer",
                            str(telegram_id),
                            {"evidence_id": existing["id"]},
                        )
                    return result
                evidence_id = str(existing["id"])
                storage_path = str(existing["storage_path"] or "") or self._receipt_storage_path(
                    order_id, evidence_id, mime_type
                )
                connection.execute(
                    """UPDATE payment_evidence
                       SET storage_bucket = ?, storage_path = ?, storage_status = 'pending',
                           storage_error = NULL
                       WHERE id = ?""",
                    (storage_bucket, storage_path, evidence_id),
                )
            else:
                latest = connection.execute(
                    """SELECT review_status FROM payment_evidence
                       WHERE order_id = ? ORDER BY submitted_at DESC LIMIT 1""",
                    (order_id,),
                ).fetchone()
                if latest is not None and str(latest["review_status"] or "pending") != "rejected":
                    raise CommerceError(
                        "A receipt is already awaiting review; wait for staff feedback"
                    )
                payment_rows = connection.execute(
                    "SELECT provider, status FROM payments WHERE order_id = ?",
                    (order_id,),
                ).fetchall()
                if any(str(item["provider"] or "").lower() == "wallet" for item in payment_rows):
                    raise CommerceError(
                        "This order already uses wallet payment; receipt payment cannot be combined"
                    )
                if any(
                    str(item["status"] or "") in ("submitted", "verified", "refunded")
                    for item in payment_rows
                ):
                    raise CommerceError("A payment is already attached to this order")
                # Keep model output as evidence only. Detect a repeated
                # candidate across screenshots without creating an
                # authoritative payment row.
                if tx_candidate:
                    prior_evidence = connection.execute(
                        "SELECT provider, extraction_json FROM payment_evidence WHERE order_id != ?",
                        (order_id,),
                    ).fetchall()
                    for prior in prior_evidence:
                        try:
                            prior_extraction = json.loads(prior["extraction_json"] or "{}")
                        except json.JSONDecodeError:
                            prior_extraction = {}
                        prior_tx = (
                            prior_extraction.get("transaction_id")
                            if isinstance(prior_extraction, dict)
                            else None
                        )
                        if _normalize_reference(str(prior["provider"])) == _normalize_reference(
                            provider_name or "manual"
                        ) and _normalize_reference(str(prior_tx or "")) == _normalize_reference(
                            tx_candidate
                        ):
                            status = "needs_review"
                            flagged = dict(extraction or {})
                            flagged["flags"] = sorted(
                                set(flagged.get("flags") or [])
                                | {"duplicate_transaction_candidate"}
                            )
                            extraction = flagged
                            break
                evidence_id = _new_id()
                storage_path = (
                    self._receipt_storage_path(order_id, evidence_id, mime_type)
                    if storage_configured
                    else None
                )
                connection.execute(
                    """INSERT INTO payment_evidence
                       (id, order_id, telegram_id, provider, telegram_file_id,
                        telegram_file_unique_id, telegram_media_type, image_sha256,
                        image_phash, mime_type, byte_size, storage_bucket, storage_path,
                        storage_status, extraction_json, extraction_status,
                        submitted_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        evidence_id,
                        order_id,
                        telegram_id,
                        provider_name,
                        file_id[:256],
                        file_unique_id[:256] if file_unique_id else None,
                        telegram_media_type,
                        digest,
                        phash,
                        mime_type[:64],
                        len(image_bytes),
                        storage_bucket,
                        storage_path,
                        storage_status,
                        json.dumps(extraction or {}, sort_keys=True),
                        status,
                        submitted_at,
                    ),
                )
                is_new = True

        if storage_configured:
            assert storage_path is not None
            try:
                uploaded_path = self.receipt_storage.upload(storage_path, image_bytes, mime_type)
                uploaded_path = str(uploaded_path or "").strip()
                if not uploaded_path:
                    raise RuntimeError("Receipt storage returned an empty object path")
            except Exception as exc:
                # Preserve the row so a retry can reuse the same object path.
                try:
                    with self.database.connect() as connection:
                        connection.execute(
                            """UPDATE payment_evidence
                               SET storage_status = 'failed', storage_error = ?
                               WHERE id = ?""",
                            (type(exc).__name__[:128], evidence_id),
                        )
                except Exception:
                    pass
                raise CommerceError("Receipt image could not be saved. Please try again.") from exc
            try:
                storage_path = uploaded_path
                with self.database.connect() as connection:
                    self.database.begin_write(connection)
                    connection.execute(
                        """UPDATE payment_evidence
                           SET storage_bucket = ?, storage_path = ?, storage_status = 'stored',
                               storage_error = NULL, stored_at = ?
                           WHERE id = ?""",
                        (storage_bucket, str(uploaded_path), submitted_at, evidence_id),
                    )
                    connection.execute(
                        "UPDATE orders SET status = 'payment_submitted' WHERE id = ?",
                        (order_id,),
                    )
                    self._audit(
                        connection,
                        "receipt_submitted" if is_new else "receipt_storage_recovered",
                        "order",
                        order_id,
                        "customer",
                        str(telegram_id),
                        {"evidence_id": evidence_id, "extraction_status": status},
                    )
                    self._queue_staff_notification(
                        connection,
                        "receipt_submitted",
                        evidence_id,
                        "🧾 RECEIPT AWAITING REVIEW\n\n"
                        f"Order: #{order_id[:8]}\n"
                        f"Evidence: {evidence_id[:10]}\n"
                        f"Customer: tg:{telegram_id}\n"
                        f"Method: {provider_name.upper()}\n"
                        f"AI extraction: {status.replace('_', ' ')}\n\n"
                        "Action: open the receipt and confirm it against the receiving account.",
                        submitted_at,
                    )
                    if queue_extraction and extraction is None and not near_duplicate:
                        self._queue_receipt_extraction(connection, evidence_id, submitted_at)
            except Exception:
                # Do not leave a billable orphan if the final metadata commit
                # fails. Deletion is best-effort and the row remains retryable.
                try:
                    self.receipt_storage.delete(storage_path)
                except Exception:
                    pass
                raise
        else:
            # A receipt is a payment submission even when OCR/LLM extraction
            # failed. Approval still requires a human verification decision.
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                connection.execute(
                    "UPDATE orders SET status = 'payment_submitted' WHERE id = ?",
                    (order_id,),
                )
                if is_new:
                    self._audit(
                        connection,
                        "receipt_submitted",
                        "order",
                        order_id,
                        "customer",
                        str(telegram_id),
                        {"evidence_id": evidence_id, "extraction_status": status},
                    )
                self._queue_staff_notification(
                    connection,
                    "receipt_submitted",
                    evidence_id,
                    "🧾 RECEIPT AWAITING REVIEW\n\n"
                    f"Order: #{order_id[:8]}\n"
                    f"Evidence: {evidence_id[:10]}\n"
                    f"Customer: tg:{telegram_id}\n"
                    f"Method: {provider_name.upper()}\n"
                    f"AI extraction: {status.replace('_', ' ')}\n\n"
                    "Action: open the receipt and confirm it against the receiving account.",
                    submitted_at,
                )
                if queue_extraction and extraction is None:
                    self._queue_receipt_extraction(connection, evidence_id, submitted_at)
        result = dict(extraction or {})
        result["evidence_id"] = evidence_id
        result["image_sha256"] = digest
        result["extraction_status"] = status
        result["storage_status"] = "stored" if storage_configured else "not_configured"
        result["storage_path"] = storage_path
        return result

    def claim_receipt_extraction_job(self, now: datetime | None = None) -> dict[str, Any] | None:
        """Claim one durable assisted-extraction job for the maintenance worker."""
        current = now or datetime.now(UTC)
        current_text = _now_text(current)
        stale_before = _now_text(current - timedelta(minutes=10))
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE receipt_extraction_jobs
                   SET status = 'pending', locked_at = NULL
                   WHERE status = 'running' AND locked_at < ?""",
                (stale_before,),
            )
            lock_clause = (
                " FOR UPDATE SKIP LOCKED" if isinstance(connection, _PostgresConnection) else ""
            )
            row = connection.execute(
                """SELECT j.id AS job_id, j.attempts, e.id AS evidence_id,
                          e.provider, e.telegram_file_id, e.mime_type,
                          e.storage_path, e.storage_status
                   FROM receipt_extraction_jobs j
                   JOIN payment_evidence e ON e.id = j.evidence_id
                   WHERE j.status = 'pending' AND j.next_attempt_at <= ?
                     AND e.review_status = 'pending'
                   ORDER BY j.created_at LIMIT 1"""
                + lock_clause,
                (current_text,),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """UPDATE receipt_extraction_jobs
                   SET status = 'running', attempts = attempts + 1, locked_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (current_text, row["job_id"]),
            )
            if getattr(updated, "rowcount", 1) == 0:
                return None
            result = dict(row)
            result["attempts"] = int(row["attempts"]) + 1
            return result

    def finish_receipt_extraction(
        self,
        job_id: str,
        evidence_id: str,
        extraction: dict[str, Any],
        diagnostics: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        """Commit untrusted extraction metadata without changing payment approval state."""
        if not isinstance(extraction, dict):
            raise CommerceError("Receipt extraction result is invalid")
        completed = now or datetime.now(UTC)
        completed_at = _now_text(completed)
        result = dict(extraction)
        tx_candidate = str(result.get("transaction_id") or "").strip()[:128]
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            evidence = connection.execute(
                """SELECT e.provider, e.order_id, e.submitted_at,
                          o.amount_minor, o.currency, o.payment_method
                   FROM payment_evidence e
                   JOIN orders o ON o.id = e.order_id
                   WHERE e.id = ? AND e.review_status = 'pending'""",
                (evidence_id,),
            ).fetchone()
            if evidence is None:
                connection.execute(
                    """UPDATE receipt_extraction_jobs
                       SET status = 'done', locked_at = NULL, completed_at = ? WHERE id = ?""",
                    (completed_at, job_id),
                )
                return
            if tx_candidate:
                prior_rows = connection.execute(
                    """SELECT provider, extraction_json FROM payment_evidence
                       WHERE id != ? AND order_id != ?""",
                    (evidence_id, evidence["order_id"]),
                ).fetchall()
                for prior in prior_rows:
                    try:
                        prior_result = json.loads(prior["extraction_json"] or "{}")
                    except json.JSONDecodeError:
                        prior_result = {}
                    prior_tx = prior_result.get("transaction_id") if isinstance(prior_result, dict) else None
                    if (
                        _normalize_reference(str(prior["provider"] or ""))
                        == _normalize_reference(str(result.get("provider") or evidence["provider"]))
                        and _normalize_reference(str(prior_tx or ""))
                        == _normalize_reference(tx_candidate)
                    ):
                        result["flags"] = sorted(
                            set(result.get("flags") or []) | {"duplicate_transaction_candidate"}
                        )
                        break
            try:
                submitted = datetime.fromisoformat(str(evidence["submitted_at"]))
            except (TypeError, ValueError):
                submitted = completed
            if submitted.tzinfo is None:
                submitted = submitted.replace(tzinfo=UTC)
            result["flags"] = self._receipt_risk_flags(result, evidence, submitted)
            # Preserve the existing storage enum: `parsed` means all triage
            # checks passed, while every mismatch/ambiguity stays reviewable.
            # The detailed three-way decision lives in extraction_json.
            status = (
                "parsed"
                if result.get("automation_decision") == "candidate_pass"
                else "needs_review"
            )
            connection.execute(
                """UPDATE payment_evidence
                   SET extraction_json = ?, extraction_status = ? WHERE id = ?""",
                (json.dumps(result, sort_keys=True), status, evidence_id),
            )
            connection.execute(
                """UPDATE receipt_extraction_jobs
                   SET status = 'done', locked_at = NULL, last_error = NULL, completed_at = ?
                   WHERE id = ?""",
                (completed_at, job_id),
            )
            self._audit(
                connection,
                "receipt_assisted_extraction_completed",
                "payment_evidence",
                evidence_id,
                "system",
                None,
                {
                    "extraction_status": status,
                    "selected_model": str((diagnostics or {}).get("selected_model") or "")[:128],
                },
            )

    def fail_receipt_extraction_job(
        self, job_id: str, error: Exception, now: datetime | None = None
    ) -> None:
        """Retry bounded provider failures; manual review remains available throughout."""
        current = now or datetime.now(UTC)
        next_attempt = _now_text(current + timedelta(minutes=5))
        safe_error = f"{type(error).__name__}: {str(error)[:240]}"
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE receipt_extraction_jobs
                   SET status = CASE WHEN attempts >= 3 THEN 'failed' ELSE 'pending' END,
                       next_attempt_at = ?, locked_at = NULL, last_error = ?
                   WHERE id = ?""",
                (next_attempt, safe_error, job_id),
            )

    def list_pending_receipts(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT e.id, e.order_id, e.telegram_id, e.provider, e.image_sha256,
                          e.byte_size, e.storage_bucket, e.storage_path, e.storage_status,
                          e.extraction_json, e.extraction_status, e.submitted_at,
                          o.plan_code, o.amount_minor, o.currency
                   FROM payment_evidence e JOIN orders o ON o.id = e.order_id
                   WHERE e.review_status = 'pending'
                     AND e.storage_status IN ('stored', 'not_configured')
                   ORDER BY e.submitted_at LIMIT ?""",
                (max(1, min(limit, 100)),),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            try:
                item["extraction"] = json.loads(item.pop("extraction_json") or "{}")
            except json.JSONDecodeError:
                item["extraction"] = {}
            results.append(item)
        return results

    def get_receipt(self, evidence_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT e.*, o.plan_code, o.amount_minor, o.currency, o.status AS order_status
                   FROM payment_evidence e JOIN orders o ON o.id = e.order_id
                   WHERE e.id = ?""",
                (evidence_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["extraction"] = json.loads(result.pop("extraction_json") or "{}")
        except json.JSONDecodeError:
            result["extraction"] = {}
        return result

    def verify_receipt(
        self,
        evidence_id: str,
        admin_id: int,
        provider_reference: str,
        verified_amount_minor: int,
        currency: str = "MMK",
        now: datetime | None = None,
    ) -> str:
        """Record a human verification against the receiving account.

        LLM extraction is deliberately excluded from this trust boundary.  The
        reviewer must supply the transaction ID and amount observed in the
        actual receiving account before an order can be approved.
        """
        provider_reference = provider_reference.strip()[:128]
        currency = currency.strip().upper()[:16]
        try:
            verified_amount_minor = int(verified_amount_minor)
        except (TypeError, ValueError) as exc:
            raise CommerceError("Verified payment amount must be an integer") from exc
        if not provider_reference:
            raise CommerceError("Verified transaction ID is required")
        if verified_amount_minor <= 0:
            raise CommerceError("Verified payment amount must be positive")
        reviewed_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            evidence = connection.execute(
                """SELECT e.*, o.amount_minor, o.currency, o.plan_code,
                          o.status AS order_status
                   FROM payment_evidence e JOIN orders o ON o.id = e.order_id
                   WHERE e.id = ?""",
                (evidence_id,),
            ).fetchone()
            if evidence is None:
                raise CommerceError("Receipt evidence not found")
            self._lock_order(connection, str(evidence["order_id"]))
            if evidence["order_status"] == "approved":
                return evidence["order_id"]
            if self.receipt_storage_required and str(evidence["storage_status"] or "") != "stored":
                raise CommerceError("Receipt image must be stored before verification")
            if evidence["review_status"] == "verified":
                if (
                    str(evidence["verified_provider_reference"] or "").strip().casefold()
                    == provider_reference.casefold()
                    and int(evidence["verified_amount_minor"] or 0) == verified_amount_minor
                    and str(evidence["verified_currency"] or "").upper() == currency
                ):
                    return evidence["order_id"]
                raise CommerceError("Receipt verification is already recorded")
            if evidence["review_status"] == "rejected":
                raise CommerceError("Receipt was rejected; submit a new screenshot")
            if evidence["order_status"] not in ("awaiting_payment", "payment_submitted"):
                raise CommerceError("Order is not open for receipt verification")
            if currency != str(evidence["currency"]).upper():
                raise CommerceError("Verified payment currency does not match the order")
            if verified_amount_minor < int(evidence["amount_minor"]):
                raise CommerceError("Verified payment amount is below the order total")
            if (
                str(evidence["plan_code"]) == "wallet_topup"
                and verified_amount_minor != int(evidence["amount_minor"])
            ):
                raise CommerceError("Wallet top-up receipt amount must match exactly")
            payment = connection.execute(
                """SELECT id FROM payments
                   WHERE order_id = ? AND status IN ('submitted', 'verified')
                   ORDER BY submitted_at DESC LIMIT 1""",
                (evidence["order_id"],),
            ).fetchone()
            provider = _normalize_reference(str(evidence["provider"] or "manual"))[:64]
            normalized_provider = _normalize_reference(provider)
            normalized_reference = _normalize_reference(provider_reference)
            conflicts = connection.execute(
                """SELECT order_id, provider, normalized_reference FROM payments
                   WHERE status IN ('submitted', 'verified')
                     AND normalized_reference = ? AND order_id != ?""",
                (normalized_reference, evidence["order_id"]),
            ).fetchall()
            if any(
                _normalize_reference(str(item["provider"])) == normalized_provider
                for item in conflicts
            ):
                raise CommerceError(
                    "This transaction ID has already been submitted for another order"
                )
            try:
                if payment is None:
                    payment_id = _new_id()
                    connection.execute(
                        """INSERT INTO payments
                           (id, order_id, provider, provider_reference, normalized_reference,
                            status, submitted_at, verified_at)
                           VALUES (?, ?, ?, ?, ?, 'verified', ?, ?)""",
                        (
                            payment_id,
                            evidence["order_id"],
                            provider,
                            provider_reference,
                            normalized_reference,
                            reviewed_at,
                            reviewed_at,
                        ),
                    )
                else:
                    payment_id = payment["id"]
                    connection.execute(
                        """UPDATE payments
                           SET provider = ?, provider_reference = ?, normalized_reference = ?,
                               status = 'verified', verified_at = ?
                           WHERE id = ?""",
                        (
                            provider,
                            provider_reference,
                            normalized_reference,
                            reviewed_at,
                            payment_id,
                        ),
                    )
            except Exception as exc:
                if self.database.is_integrity_error(exc):
                    raise CommerceError("This transaction ID has already been verified") from exc
                raise
            connection.execute(
                """UPDATE payment_evidence
                   SET reviewer_id = ?, review_notes = 'verified against receiving account',
                       review_status = 'verified', verified_provider_reference = ?,
                       verified_amount_minor = ?, verified_currency = ?, reviewed_at = ?
                   WHERE id = ?""",
                (
                    admin_id,
                    provider_reference,
                    verified_amount_minor,
                    currency,
                    reviewed_at,
                    evidence_id,
                ),
            )
            connection.execute(
                "UPDATE orders SET status = 'payment_submitted' WHERE id = ?",
                (evidence["order_id"],),
            )
            self._audit(
                connection,
                "receipt_verified",
                "payment_evidence",
                evidence_id,
                "admin",
                str(admin_id),
                {"amount_minor": verified_amount_minor, "currency": currency},
            )
        return str(evidence["order_id"])

    def reject_receipt(
        self,
        evidence_id: str,
        admin_id: int,
        notes: str = "rejected by admin",
        now: datetime | None = None,
    ) -> str:
        reviewed_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            evidence = connection.execute(
                """SELECT e.id, e.order_id, e.review_status, e.telegram_id, e.provider,
                          o.plan_name, o.plan_code
                   FROM payment_evidence e JOIN orders o ON o.id = e.order_id
                   WHERE e.id = ?""",
                (evidence_id,),
            ).fetchone()
            if evidence is None:
                raise CommerceError("Receipt evidence not found")
            self._lock_order(connection, str(evidence["order_id"]))
            if evidence["review_status"] == "verified":
                raise CommerceError("Verified receipt cannot be rejected")
            if evidence["review_status"] == "rejected":
                return str(evidence["order_id"])
            connection.execute(
                """UPDATE payment_evidence SET reviewer_id = ?, review_notes = ?,
                           review_status = 'rejected', reviewed_at = ? WHERE id = ?""",
                (admin_id, (notes or "rejected by admin")[:500], reviewed_at, evidence_id),
            )
            connection.execute(
                "UPDATE payments SET status = 'rejected' WHERE order_id = ? AND status = 'submitted'",
                (evidence["order_id"],),
            )
            connection.execute(
                """INSERT INTO notifications
                   (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
                   VALUES (?, ?, ?, 'receipt_rejected', ?, 'pending', ?, ?)
                   ON CONFLICT(dedupe_key) DO NOTHING""",
                (
                    _new_id(),
                    f"receipt-rejected:{evidence_id}",
                    evidence["telegram_id"],
                    "❌ Your receipt was not accepted.\n\n"
                    f"Order: #{str(evidence['order_id'])[:8]}\n"
                    f"Reason: {(notes or 'Please submit a clearer screenshot.')[:240]}\n\n"
                    "Your order remains available for a replacement receipt.",
                    reviewed_at,
                    reviewed_at,
                ),
            )
            self._audit(
                connection,
                "receipt_rejected",
                "payment_evidence",
                evidence_id,
                "admin",
                str(admin_id),
                {"notes": (notes or "")[:500]},
            )
            self._queue_staff_notification(
                connection,
                "rejected",
                evidence_id,
                "❌ RECEIPT REJECTED\n\n"
                f"Order: #{str(evidence['order_id'])[:8]}\n"
                f"Evidence: {evidence_id[:10]}\n"
                f"Customer: tg:{evidence['telegram_id']}\n"
                f"Method: {str(evidence['provider'] or 'manual').upper()}\n"
                f"Reviewed by: tg:{admin_id}\n\n"
                f"Reason: {(notes or 'rejected by admin')[:240]}",
                reviewed_at,
            )
        return str(evidence["order_id"])

    def wallet_balance(self, telegram_id: int, currency: str = "MMK") -> int:
        now_text = _now_text()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            user = connection.execute(
                "SELECT telegram_id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            if user is None:
                self._ensure_user(connection, telegram_id, "")
            existing_wallet = connection.execute(
                "SELECT currency FROM wallets WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
            if (
                existing_wallet is not None
                and str(existing_wallet["currency"]).upper() != currency.upper()
            ):
                raise CommerceError("A user wallet has one supported currency")
            connection.execute(
                """INSERT INTO wallets (telegram_id, currency, balance_minor, created_at, updated_at)
                   VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
                (telegram_id, currency, now_text, now_text),
            )
            row = connection.execute(
                "SELECT balance_minor FROM wallets WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        return int(row["balance_minor"] if row else 0)

    def wallet_history(
        self, telegram_id: int, limit: int = 20, currency: str = "MMK"
    ) -> list[dict[str, Any]]:
        """Return immutable wallet events for the owner, newest first."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT kind, amount_minor, currency, reference_type, reference_id,
                          created_at
                   FROM wallet_ledger
                   WHERE telegram_id = ? AND currency = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (telegram_id, currency.upper(), max(1, min(limit, 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def consistency_report(
        self,
        now: datetime | None = None,
        review_sla: timedelta = timedelta(hours=24),
    ) -> dict[str, int]:
        """Read-only invariant scan for admin operations and deployment checks."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        review_cutoff = _now_text(current - review_sla)
        with self.database.connect() as connection:
            duplicate_open = connection.execute(
                """SELECT COUNT(*) AS n FROM (
                     SELECT telegram_id FROM orders
                     WHERE status IN ('awaiting_payment', 'payment_submitted')
                     GROUP BY telegram_id HAVING COUNT(*) > 1)"""
            ).fetchone()["n"]
            duplicate_payment_references = connection.execute(
                """SELECT COUNT(*) AS n FROM (
                     SELECT lower(provider), normalized_reference
                     FROM payments
                     WHERE normalized_reference <> ''
                     GROUP BY lower(provider), normalized_reference
                     HAVING COUNT(*) > 1)"""
            ).fetchone()["n"]
            approved_missing_subscription = connection.execute(
                """SELECT COUNT(*) AS n FROM orders o
                   LEFT JOIN subscriptions s ON s.order_id = o.id
                   WHERE o.status = 'approved' AND o.plan_code != 'wallet_topup'
                     AND s.id IS NULL"""
            ).fetchone()["n"]
            approved_missing_job = connection.execute(
                """SELECT COUNT(*) AS n FROM subscriptions s
                   JOIN orders o ON o.id = s.order_id
                   WHERE o.status = 'approved'
                     AND NOT EXISTS (SELECT 1 FROM provisioning_jobs j
                                     WHERE j.subscription_id = s.id AND j.operation = 'provision')"""
            ).fetchone()["n"]
            pending_reviews = connection.execute(
                "SELECT COUNT(*) AS n FROM payment_evidence WHERE review_status = 'pending'"
            ).fetchone()["n"]
            stale_reviews = connection.execute(
                """SELECT COUNT(*) AS n FROM payment_evidence
                   WHERE review_status = 'pending' AND submitted_at <= ?""",
                (review_cutoff,),
            ).fetchone()["n"]
            pending_receipt_uploads = connection.execute(
                "SELECT COUNT(*) AS n FROM payment_evidence WHERE storage_status = 'pending'"
            ).fetchone()["n"]
            failed_receipt_uploads = connection.execute(
                "SELECT COUNT(*) AS n FROM payment_evidence WHERE storage_status = 'failed'"
            ).fetchone()["n"]
            failed_jobs = connection.execute(
                "SELECT COUNT(*) AS n FROM provisioning_jobs WHERE status = 'failed'"
            ).fetchone()["n"]
            pending_revocations = connection.execute(
                "SELECT COUNT(*) AS n FROM provisioning_jobs WHERE operation = 'revoke' AND status IN ('pending', 'running')"
            ).fetchone()["n"]
            failed_revocations = connection.execute(
                "SELECT COUNT(*) AS n FROM provisioning_jobs WHERE operation = 'revoke' AND status = 'failed'"
            ).fetchone()["n"]
            failed_activations = connection.execute(
                "SELECT COUNT(*) AS n FROM provisioning_jobs WHERE operation = 'provision' AND status = 'failed'"
            ).fetchone()["n"]
            dead_notifications = connection.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE dead_lettered_at IS NOT NULL"
            ).fetchone()["n"]
            managed_key_repairs_pending = 0
            managed_key_repairs_failed = 0
            managed_key_repairs_manual = 0
            managed_key_missing = 0
            if self._table_exists(connection, "managed_key_repair_jobs"):
                managed_key_repairs_pending = connection.execute(
                    "SELECT COUNT(*) AS n FROM managed_key_repair_jobs WHERE status IN ('pending', 'running')"
                ).fetchone()["n"]
                managed_key_repairs_failed = connection.execute(
                    "SELECT COUNT(*) AS n FROM managed_key_repair_jobs WHERE status = 'failed'"
                ).fetchone()["n"]
                managed_key_repairs_manual = connection.execute(
                    "SELECT COUNT(*) AS n FROM managed_key_repair_jobs WHERE status = 'manual'"
                ).fetchone()["n"]
            if self._table_exists(connection, "outline_remote_keys"):
                managed_key_missing = connection.execute(
                    """SELECT COUNT(*) AS n FROM outline_remote_keys
                        WHERE managed = 1 AND status = 'missing'"""
                ).fetchone()["n"]
            wallet_mismatches = 0
            wallets = connection.execute(
                "SELECT telegram_id, currency, balance_minor FROM wallets"
            ).fetchall()
            for wallet in wallets:
                ledger = connection.execute(
                    """SELECT COALESCE(SUM(CASE WHEN kind IN ('credit', 'release', 'reversal')
                                                THEN amount_minor
                                                WHEN kind = 'reserve' THEN -amount_minor
                                                ELSE 0 END), 0) AS projected
                       FROM wallet_ledger WHERE telegram_id = ? AND currency = ?""",
                    (wallet["telegram_id"], wallet["currency"]),
                ).fetchone()
                if int(ledger["projected"] or 0) != int(wallet["balance_minor"]):
                    wallet_mismatches += 1
        return {
            "duplicate_open_orders": int(duplicate_open),
            "duplicate_payment_references": int(duplicate_payment_references),
            "approved_missing_subscription": int(approved_missing_subscription),
            "approved_missing_provision_job": int(approved_missing_job),
            "pending_receipts": int(pending_reviews),
            "stale_receipts": int(stale_reviews),
            "pending_receipt_uploads": int(pending_receipt_uploads),
            "failed_receipt_uploads": int(failed_receipt_uploads),
            "failed_jobs": int(failed_jobs),
            "pending_revocations": int(pending_revocations),
            "failed_revocations": int(failed_revocations),
            "failed_activations": int(failed_activations),
            "dead_notifications": int(dead_notifications),
            "managed_key_missing": int(managed_key_missing),
            "managed_key_repairs_pending": int(managed_key_repairs_pending),
            "managed_key_repairs_failed": int(managed_key_repairs_failed),
            "managed_key_repairs_manual": int(managed_key_repairs_manual),
            "wallet_balance_mismatches": int(wallet_mismatches),
        }

    def credit_wallet(
        self,
        telegram_id: int,
        amount_minor: int,
        reference_id: str,
        admin_id: int,
        currency: str = "MMK",
    ) -> str:
        if amount_minor <= 0:
            raise CommerceError("Wallet credit must be positive")
        now_text = _now_text()
        idem = f"credit:{reference_id}"
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._ensure_user(connection, telegram_id, "")
            connection.execute(
                """INSERT INTO wallets (telegram_id, currency, balance_minor, created_at, updated_at)
                   VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
                (telegram_id, currency, now_text, now_text),
            )
            existing = connection.execute(
                "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (idem,)
            ).fetchone()
            if existing is not None:
                return "already_credited"
            connection.execute(
                "UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ? WHERE telegram_id = ?",
                (amount_minor, now_text, telegram_id),
            )
            connection.execute(
                """INSERT INTO wallet_ledger
                   (id, telegram_id, kind, amount_minor, currency, reference_type,
                    reference_id, idempotency_key, created_at)
                   VALUES (?, ?, 'credit', ?, ?, 'payment', ?, ?, ?)""",
                (_new_id(), telegram_id, amount_minor, currency, reference_id, idem, now_text),
            )
            self._audit(
                connection,
                "wallet_credited",
                "user",
                str(telegram_id),
                "admin",
                str(admin_id),
                {"amount_minor": amount_minor, "reference_id": reference_id},
            )
        return "credited"

    def pay_order_with_wallet(
        self, telegram_id: int, order_id: str, now: datetime | None = None
    ) -> str:
        """Reserve wallet funds and submit an idempotent wallet payment."""
        now_text = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if order is None or order["telegram_id"] != telegram_id:
                raise CommerceError("Order not found")
            if str(order["plan_code"]) == "wallet_topup":
                raise CommerceError("A wallet cannot be topped up from the same wallet")
            self._assert_no_active_promo(connection, telegram_id)
            if order["status"] == "approved":
                return "already_approved"
            if order["status"] not in ("awaiting_payment", "payment_submitted"):
                raise CommerceError("Order is not open for wallet payment")
            evidence = connection.execute(
                "SELECT review_status FROM payment_evidence WHERE order_id = ? ORDER BY submitted_at DESC LIMIT 1",
                (order_id,),
            ).fetchone()
            if evidence is not None and str(evidence["review_status"] or "pending") != "rejected":
                raise CommerceError(
                    "This order already has a receipt; wallet payment cannot be combined"
                )
            payments = connection.execute(
                "SELECT provider, status FROM payments WHERE order_id = ?",
                (order_id,),
            ).fetchall()
            if any(
                str(item["provider"] or "").lower() != "wallet"
                and str(item["status"] or "") in ("submitted", "verified")
                for item in payments
            ):
                raise CommerceError("A receipt payment is already attached to this order")
            self._ensure_user(connection, telegram_id, "")
            connection.execute(
                """INSERT INTO wallets (telegram_id, currency, balance_minor, created_at, updated_at)
                   VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
                (telegram_id, order["currency"], now_text, now_text),
            )
            idem = f"reserve:{order_id}"
            existing = connection.execute(
                "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (idem,)
            ).fetchone()
            if existing is not None:
                return "already_reserved"
            updated = connection.execute(
                """UPDATE wallets SET balance_minor = balance_minor - ?, updated_at = ?
                   WHERE telegram_id = ? AND balance_minor >= ?""",
                (order["amount_minor"], now_text, telegram_id, order["amount_minor"]),
            )
            if getattr(updated, "rowcount", 1) == 0:
                raise CommerceError("Insufficient wallet balance")
            connection.execute(
                """INSERT INTO wallet_ledger
                   (id, telegram_id, kind, amount_minor, currency, reference_type,
                    reference_id, idempotency_key, created_at)
                   VALUES (?, ?, 'reserve', ?, ?, 'order', ?, ?, ?)""",
                (
                    _new_id(),
                    telegram_id,
                    order["amount_minor"],
                    order["currency"],
                    order_id,
                    idem,
                    now_text,
                ),
            )
            connection.execute(
                """INSERT INTO wallet_reservations
                   (id, telegram_id, order_id, amount_minor, currency, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?)""",
                (
                    _new_id(),
                    telegram_id,
                    order_id,
                    order["amount_minor"],
                    order["currency"],
                    now_text,
                    now_text,
                ),
            )
            connection.execute(
                """INSERT INTO payments
                   (id, order_id, provider, provider_reference, normalized_reference, status, submitted_at)
                   VALUES (?, ?, 'wallet', ?, ?, 'submitted', ?)""",
                (
                    _new_id(),
                    order_id,
                    f"wallet:{order_id}",
                    _normalize_reference(f"wallet:{order_id}"),
                    now_text,
                ),
            )
            connection.execute(
                "UPDATE orders SET status = 'payment_submitted', payment_method = 'wallet' WHERE id = ?",
                (order_id,),
            )
            self._audit(
                connection,
                "wallet_reserved",
                "order",
                order_id,
                "customer",
                str(telegram_id),
                {"amount_minor": order["amount_minor"]},
            )
        return "reserved"

    def list_pending_orders(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT o.id, o.telegram_id, o.plan_code, o.amount_minor, o.currency,
                          o.status, o.created_at,
                          (SELECT p.provider FROM payments p WHERE p.order_id = o.id
                           ORDER BY p.submitted_at DESC LIMIT 1) AS provider,
                          (SELECT p.provider_reference FROM payments p WHERE p.order_id = o.id
                           ORDER BY p.submitted_at DESC LIMIT 1) AS provider_reference,
                          (SELECT e.review_status FROM payment_evidence e WHERE e.order_id = o.id
                           ORDER BY e.submitted_at DESC LIMIT 1) AS receipt_status,
                          (SELECT r.status FROM wallet_reservations r WHERE r.order_id = o.id
                           LIMIT 1) AS wallet_reservation_status
                   FROM orders o
                   WHERE o.status IN ('awaiting_payment', 'payment_submitted')
                     AND COALESCE(o.refund_status, 'none') != 'refunded'
                   ORDER BY o.created_at LIMIT ?""",
                (max(1, min(limit, 100)),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["stage"] = self._order_stage(item)
            result.append(item)
        return result

    def approve_order(
        self,
        order_id: str,
        admin_id: int,
        now: datetime | None = None,
    ) -> ApprovalResult:
        starts_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if order is None:
                raise CommerceError("Order not found")
            is_wallet_topup = str(order["plan_code"]) == "wallet_topup"
            if not is_wallet_topup:
                self._assert_no_active_promo(connection, int(order["telegram_id"]))
            if order["status"] == "approved":
                if is_wallet_topup:
                    return ApprovalResult(
                        order_id, f"wallet:{order['telegram_id']}", "already_credited"
                    )
                subscription = connection.execute(
                    "SELECT id FROM subscriptions WHERE order_id = ?", (order_id,)
                ).fetchone()
                if subscription is None:
                    raise CommerceError("Approved order has no subscription record")
                return ApprovalResult(order_id, subscription["id"], "already_approved")
            if order["status"] != "payment_submitted":
                raise CommerceError("Order has no submitted payment for review")
            evidence = connection.execute(
                """SELECT review_status, verified_amount_minor, verified_currency,
                          storage_status
                   FROM payment_evidence WHERE order_id = ?
                   ORDER BY submitted_at DESC LIMIT 1""",
                (order_id,),
            ).fetchone()
            wallet_reservation = connection.execute(
                """SELECT id, amount_minor, currency, status
                   FROM wallet_reservations WHERE order_id = ? LIMIT 1""",
                (order_id,),
            ).fetchone()
            if evidence is not None and evidence["review_status"] != "verified":
                if wallet_reservation is None or wallet_reservation["status"] != "reserved":
                    raise CommerceError(
                        "Receipt must be verified against the receiving account first"
                    )
            if (
                evidence is not None
                and self.receipt_storage_required
                and str(evidence["storage_status"] or "") != "stored"
            ):
                raise CommerceError("Receipt image must be stored before approval")
            payment = connection.execute(
                """SELECT id, provider, status FROM payments WHERE order_id = ?
                   AND status IN ('submitted', 'verified')
                   ORDER BY submitted_at DESC LIMIT 1""",
                (order_id,),
            ).fetchone()
            if payment is None:
                raise CommerceError("Payment record is missing")
            wallet_payment = str(payment["provider"] or "") == "wallet"
            if evidence is not None and payment["status"] != "verified":
                raise CommerceError("Receipt payment has not been verified")
            if wallet_payment and (
                wallet_reservation is None
                or wallet_reservation["status"] != "reserved"
                or int(wallet_reservation["amount_minor"]) < int(order["amount_minor"])
                or str(wallet_reservation["currency"]).upper() != str(order["currency"]).upper()
            ):
                raise CommerceError("Wallet reservation is missing or no longer valid")
            if evidence is None and not wallet_payment and not self.allow_legacy_text_approval:
                raise CommerceError("Verified receipt evidence is required before approval")
            if evidence is not None and wallet_payment:
                raise CommerceError("An order cannot combine a wallet payment and receipt evidence")
            if is_wallet_topup:
                if wallet_payment:
                    raise CommerceError("A wallet cannot be topped up from the same wallet")
                if evidence is None or evidence["review_status"] != "verified":
                    raise CommerceError("Verified receipt evidence is required for a wallet top-up")
                if int(evidence["verified_amount_minor"] or 0) != int(order["amount_minor"]):
                    raise CommerceError("Wallet top-up receipt amount must match exactly")
                if str(evidence["verified_currency"] or "").upper() != str(
                    order["currency"]
                ).upper():
                    raise CommerceError("Wallet top-up receipt currency does not match")
                payment_id = str(payment["id"])
                credit_idem = f"credit:{payment_id}"
                connection.execute(
                    """INSERT INTO wallets
                       (telegram_id, currency, balance_minor, created_at, updated_at)
                       VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
                    (order["telegram_id"], order["currency"], starts_at, starts_at),
                )
                if connection.execute(
                    "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (credit_idem,)
                ).fetchone() is None:
                    connection.execute(
                        """UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ?
                           WHERE telegram_id = ?""",
                        (order["amount_minor"], starts_at, order["telegram_id"]),
                    )
                    connection.execute(
                        """INSERT INTO wallet_ledger
                           (id, telegram_id, kind, amount_minor, currency, reference_type,
                            reference_id, idempotency_key, created_at)
                           VALUES (?, ?, 'credit', ?, ?, 'payment', ?, ?, ?)""",
                        (
                            _new_id(),
                            order["telegram_id"],
                            order["amount_minor"],
                            order["currency"],
                            payment_id,
                            credit_idem,
                            starts_at,
                        ),
                    )
                connection.execute(
                    "UPDATE orders SET status = 'approved', approved_at = ? WHERE id = ?",
                    (starts_at, order_id),
                )
                connection.execute(
                    """INSERT INTO notifications
                       (id, dedupe_key, telegram_id, kind, text, status,
                        next_attempt_at, created_at)
                       VALUES (?, ?, ?, 'wallet_topup_approved', ?, 'pending', ?, ?)
                       ON CONFLICT(dedupe_key) DO NOTHING""",
                    (
                        _new_id(),
                        f"wallet-topup-approved:{order_id}",
                        order["telegram_id"],
                        f"✅ Wallet top-up approved: {int(order['amount_minor']):,} "
                        f"{order['currency']}.",
                        starts_at,
                        starts_at,
                    ),
                )
                self._audit(
                    connection,
                    "wallet_topup_approved",
                    "order",
                    order_id,
                    "admin",
                    str(admin_id),
                    {"amount_minor": int(order["amount_minor"]), "payment_id": payment_id},
                )
                return ApprovalResult(
                    order_id, f"wallet:{order['telegram_id']}", "wallet_credited"
                )
            plan = connection.execute(
                "SELECT duration_days, quota_bytes, name FROM plans WHERE code = ?",
                (order["plan_code"],),
            ).fetchone()
            if plan is None:
                raise CommerceError("Plan record is missing")
            duration_days = int(order["duration_days_snapshot"] or plan["duration_days"])
            plan_name = str(order["plan_name"] or plan["name"])
            quota_bytes = (
                order["quota_bytes_snapshot"]
                if order["quota_bytes_snapshot"] is not None
                else plan["quota_bytes"]
            )
            # Each approved paid order represents an independent entitlement
            # and may provision its own key. A customer can therefore buy
            # multiple plans/devices at once; renewal is not serialized behind
            # an existing subscription.
            effective_start = datetime.fromisoformat(starts_at)
            expires_at = (effective_start + timedelta(days=duration_days)).isoformat()
            subscription_id = _new_id()
            if payment["status"] == "submitted":
                connection.execute(
                    """UPDATE payments SET status = 'verified', verified_at = ?
                       WHERE id = ?""",
                    (starts_at, payment["id"]),
                )
            connection.execute(
                """UPDATE orders SET status = 'approved', approved_at = ? WHERE id = ?""",
                (starts_at, order_id),
            )
            connection.execute(
                """INSERT INTO subscriptions
                   (id, order_id, telegram_id, plan_code, starts_at, expires_at,
                    plan_name, quota_bytes, duration_days, status, server_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    subscription_id,
                    order_id,
                    order["telegram_id"],
                    order["plan_code"],
                    effective_start.isoformat(),
                    expires_at,
                    plan_name,
                    quota_bytes,
                    duration_days,
                    order["server_id"] if "server_id" in order.keys() else None,
                ),
            )
            # Record money movement as immutable ledger events. External
            # deposits are credited and immediately reserved/captured. Wallet
            # payments already have a reservation; approval only captures it.
            wallet_now = starts_at
            connection.execute(
                """INSERT INTO wallets (telegram_id, currency, balance_minor, created_at, updated_at)
                   VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
                (order["telegram_id"], order["currency"], wallet_now, wallet_now),
            )
            payment_id = payment["id"]
            credit_amount = int(order["amount_minor"])
            if evidence is not None:
                if str(evidence["verified_currency"]).upper() != str(order["currency"]).upper():
                    raise CommerceError("Verified receipt currency does not match the order")
                credit_amount = int(evidence["verified_amount_minor"])
            if not wallet_payment:
                credit_idem = f"credit:{payment_id}"
                credit_exists = connection.execute(
                    "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (credit_idem,)
                ).fetchone()
                if credit_exists is None:
                    connection.execute(
                        "UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ? WHERE telegram_id = ?",
                        (credit_amount, wallet_now, order["telegram_id"]),
                    )
                    connection.execute(
                        """INSERT INTO wallet_ledger
                           (id, telegram_id, kind, amount_minor, currency, reference_type,
                            reference_id, idempotency_key, created_at)
                           VALUES (?, ?, 'credit', ?, ?, 'payment', ?, ?, ?)""",
                        (
                            _new_id(),
                            order["telegram_id"],
                            credit_amount,
                            order["currency"],
                            payment_id,
                            credit_idem,
                            wallet_now,
                        ),
                    )
                reserve_idem = f"reserve:{order_id}"
                reserve_exists = connection.execute(
                    "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (reserve_idem,)
                ).fetchone()
                if reserve_exists is None:
                    updated = connection.execute(
                        "UPDATE wallets SET balance_minor = balance_minor - ?, updated_at = ? WHERE telegram_id = ? AND balance_minor >= ?",
                        (
                            order["amount_minor"],
                            wallet_now,
                            order["telegram_id"],
                            order["amount_minor"],
                        ),
                    )
                    if getattr(updated, "rowcount", 1) == 0:
                        raise CommerceError(
                            "Verified payment credit is insufficient for this order"
                        )
                    connection.execute(
                        """INSERT INTO wallet_ledger
                           (id, telegram_id, kind, amount_minor, currency, reference_type,
                            reference_id, idempotency_key, created_at)
                           VALUES (?, ?, 'reserve', ?, ?, 'order', ?, ?, ?)""",
                        (
                            _new_id(),
                            order["telegram_id"],
                            order["amount_minor"],
                            order["currency"],
                            order_id,
                            reserve_idem,
                            wallet_now,
                        ),
                    )
                    connection.execute(
                        """INSERT INTO wallet_reservations
                           (id, telegram_id, order_id, amount_minor, currency, status, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?)""",
                        (
                            _new_id(),
                            order["telegram_id"],
                            order_id,
                            order["amount_minor"],
                            order["currency"],
                            wallet_now,
                            wallet_now,
                        ),
                    )
            capture_idem = f"capture:{order_id}"
            if (
                connection.execute(
                    "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (capture_idem,)
                ).fetchone()
                is None
            ):
                # Capture is a state transition; the reserve already reduced
                # available balance, so capture must not deduct again.
                connection.execute(
                    """INSERT INTO wallet_ledger
                       (id, telegram_id, kind, amount_minor, currency, reference_type,
                        reference_id, idempotency_key, created_at)
                       VALUES (?, ?, 'capture', ?, ?, 'order', ?, ?, ?)""",
                    (
                        _new_id(),
                        order["telegram_id"],
                        order["amount_minor"],
                        order["currency"],
                        order_id,
                        capture_idem,
                        wallet_now,
                    ),
                )
            connection.execute(
                """INSERT INTO wallet_reservations
                   (id, telegram_id, order_id, amount_minor, currency, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'captured', ?, ?)
                   ON CONFLICT(order_id) DO UPDATE SET status = 'captured', updated_at = excluded.updated_at""",
                (
                    _new_id(),
                    order["telegram_id"],
                    order_id,
                    order["amount_minor"],
                    order["currency"],
                    wallet_now,
                    wallet_now,
                ),
            )
            connection.execute(
                """INSERT INTO provisioning_jobs
                   (id, subscription_id, operation, status, next_attempt_at, created_at)
                   VALUES (?, ?, 'provision', 'pending', ?, ?)""",
                (
                    _new_id(),
                    subscription_id,
                    effective_start.isoformat(),
                    effective_start.isoformat(),
                ),
            )
            self._audit(
                connection,
                "order_approved",
                "order",
                order_id,
                "admin",
                str(admin_id),
                {"subscription_id": subscription_id},
            )
        return ApprovalResult(order_id, subscription_id, "approved")

    def reject_order(self, order_id: str, admin_id: int, now: datetime | None = None) -> str:
        rejected_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute(
                """SELECT status, telegram_id, plan_name, plan_code, amount_minor, currency
                   FROM orders WHERE id = ?""",
                (order_id,),
            ).fetchone()
            if order is None:
                raise CommerceError("Order not found")
            if order["status"] == "rejected":
                return "already_rejected"
            if order["status"] == "approved":
                raise CommerceError("Approved order cannot be rejected here")
            verified_payment = connection.execute(
                "SELECT id FROM payments WHERE order_id = ? AND status = 'verified' LIMIT 1",
                (order_id,),
            ).fetchone()
            verified_receipt = connection.execute(
                "SELECT id FROM payment_evidence WHERE order_id = ? AND review_status = 'verified' LIMIT 1",
                (order_id,),
            ).fetchone()
            if verified_payment is not None or verified_receipt is not None:
                raise CommerceError("Verified payment must be refunded instead of rejected")
            connection.execute(
                "UPDATE orders SET status = 'rejected', rejected_at = ? WHERE id = ?",
                (rejected_at, order_id),
            )
            connection.execute(
                "UPDATE payments SET status = 'rejected' WHERE order_id = ? AND status = 'submitted'",
                (order_id,),
            )
            wallet_payment = connection.execute(
                "SELECT provider FROM payments WHERE order_id = ? ORDER BY submitted_at DESC LIMIT 1",
                (order_id,),
            ).fetchone()
            if wallet_payment is not None and wallet_payment["provider"] == "wallet":
                amount_row = connection.execute(
                    "SELECT amount_minor, currency, telegram_id FROM orders WHERE id = ?",
                    (order_id,),
                ).fetchone()
                release_idem = f"release:{order_id}"
                if (
                    amount_row is not None
                    and connection.execute(
                        "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (release_idem,)
                    ).fetchone()
                    is None
                ):
                    connection.execute(
                        "UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ? WHERE telegram_id = ?",
                        (amount_row["amount_minor"], rejected_at, amount_row["telegram_id"]),
                    )
                    connection.execute(
                        """INSERT INTO wallet_ledger
                           (id, telegram_id, kind, amount_minor, currency, reference_type,
                            reference_id, idempotency_key, created_at)
                           VALUES (?, ?, 'release', ?, ?, 'order', ?, ?, ?)""",
                        (
                            _new_id(),
                            amount_row["telegram_id"],
                            amount_row["amount_minor"],
                            amount_row["currency"],
                            order_id,
                            release_idem,
                            rejected_at,
                        ),
                    )
                    connection.execute(
                        "UPDATE wallet_reservations SET status = 'released', updated_at = ? WHERE order_id = ?",
                        (rejected_at, order_id),
                    )
            connection.execute(
                """UPDATE payment_evidence
                   SET reviewer_id = ?, review_notes = 'rejected by admin',
                       review_status = 'rejected', reviewed_at = ?
                   WHERE order_id = ? AND reviewer_id IS NULL""",
                (admin_id, rejected_at, order_id),
            )
            connection.execute(
                """INSERT INTO notifications
                   (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
                   VALUES (?, ?, ?, 'payment_rejected', ?, 'pending', ?, ?)
                   ON CONFLICT(dedupe_key) DO NOTHING""",
                (
                    _new_id(),
                    f"payment-rejected:{order_id}",
                    order["telegram_id"],
                    "Your AuriX payment/order was rejected. Contact support if you need a review.",
                    rejected_at,
                    rejected_at,
                ),
            )
            self._audit(
                connection,
                "order_rejected",
                "order",
                order_id,
                "admin",
                str(admin_id),
            )
            self._queue_staff_notification(
                connection,
                "rejected",
                f"order-{order_id}",
                "❌ ORDER REJECTED\n\n"
                f"Order: #{order_id[:8]}\n"
                f"Customer: tg:{order['telegram_id']}\n"
                f"Plan: {order['plan_name'] or order['plan_code']}\n"
                f"Amount: {int(order['amount_minor']):,} {order['currency']}\n"
                f"Reviewed by: tg:{admin_id}",
                rejected_at,
            )
        return "rejected"

    def refund_order(
        self,
        order_id: str,
        admin_id: int,
        reason: str = "refunded by admin",
        now: datetime | None = None,
    ) -> str:
        """Record an idempotent wallet compensation and close paid access.

        This does not claim that an external bank transfer was reversed.  It
        credits the customer's AuriX wallet as a compensating ledger event;
        the operator remains responsible for any off-platform payout.
        """
        refunded_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if order is None:
                raise CommerceError("Order not found")
            if str(order["plan_code"]) == "wallet_topup":
                raise CommerceError(
                    "Wallet top-up refunds require manual off-platform reconciliation"
                )
            if str(order["refund_status"] or "none") == "refunded":
                return "already_refunded"
            payment = connection.execute(
                """SELECT id, provider, status FROM payments
                   WHERE order_id = ? AND status IN ('verified', 'submitted')
                   ORDER BY submitted_at DESC LIMIT 1""",
                (order_id,),
            ).fetchone()
            if payment is None or payment["status"] != "verified":
                raise CommerceError("Only a verified payment can be refunded")
            verified_evidence = connection.execute(
                """SELECT verified_amount_minor, verified_currency FROM payment_evidence
                   WHERE order_id = ? AND review_status = 'verified'
                   ORDER BY reviewed_at DESC LIMIT 1""",
                (order_id,),
            ).fetchone()
            amount = int(order["amount_minor"])
            if str(payment["provider"] or "").lower() != "wallet" and verified_evidence is not None:
                amount = max(amount, int(verified_evidence["verified_amount_minor"] or amount))
            currency = str(order["currency"]).upper()
            now_text = refunded_at
            connection.execute(
                """INSERT INTO wallets (telegram_id, currency, balance_minor, created_at, updated_at)
                   VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
                (order["telegram_id"], currency, now_text, now_text),
            )
            wallet = connection.execute(
                "SELECT currency FROM wallets WHERE telegram_id = ?",
                (order["telegram_id"],),
            ).fetchone()
            if wallet is None or str(wallet["currency"]).upper() != currency:
                raise CommerceError("Wallet currency does not match the order")
            idem = f"reversal:{order_id}"
            existing_reversal = connection.execute(
                "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (idem,)
            ).fetchone()
            if existing_reversal is None:
                connection.execute(
                    "UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ? WHERE telegram_id = ?",
                    (amount, now_text, order["telegram_id"]),
                )
                connection.execute(
                    """INSERT INTO wallet_ledger
                       (id, telegram_id, kind, amount_minor, currency, reference_type,
                        reference_id, idempotency_key, metadata_json, created_at)
                       VALUES (?, ?, 'reversal', ?, ?, 'order', ?, ?, ?, ?)""",
                    (
                        _new_id(),
                        order["telegram_id"],
                        amount,
                        currency,
                        order_id,
                        idem,
                        json.dumps(
                            {"reason": (reason or "")[:500], "admin_id": admin_id}, sort_keys=True
                        ),
                        now_text,
                    ),
                )
            connection.execute(
                "UPDATE payments SET status = 'refunded' WHERE order_id = ? AND status IN ('verified', 'submitted')",
                (order_id,),
            )
            final_order_status = str(order["status"])
            if final_order_status != "approved":
                final_order_status = "rejected"
            connection.execute(
                "UPDATE orders SET status = ?, refund_status = 'refunded', rejected_at = COALESCE(rejected_at, ?) WHERE id = ?",
                (final_order_status, now_text, order_id),
            )
            subscription = connection.execute(
                "SELECT id, status FROM subscriptions WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if subscription is not None:
                if subscription["status"] == "active":
                    connection.execute(
                        "UPDATE subscriptions SET status = 'revoked' WHERE id = ?",
                        (subscription["id"],),
                    )
                elif subscription["status"] == "pending":
                    connection.execute(
                        "UPDATE subscriptions SET status = 'cancelled' WHERE id = ?",
                        (subscription["id"],),
                    )
                connection.execute(
                    """INSERT INTO provisioning_jobs
                       (id, subscription_id, operation, status, next_attempt_at, created_at)
                       VALUES (?, ?, 'revoke', 'pending', ?, ?)
                       ON CONFLICT(subscription_id, operation) DO NOTHING""",
                    (_new_id(), subscription["id"], now_text, now_text),
                )
            connection.execute(
                """INSERT INTO notifications
                   (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
                   VALUES (?, ?, ?, 'payment_refunded', ?, 'pending', ?, ?)
                   ON CONFLICT(dedupe_key) DO NOTHING""",
                (
                    _new_id(),
                    f"payment-refund-recorded:{order_id}",
                    order["telegram_id"],
                    f"Your AuriX order was refunded with a {amount:,} {currency} wallet credit. Reason: {(reason or 'admin refund')[:300]}",
                    now_text,
                    now_text,
                ),
            )
            self._audit(
                connection,
                "order_refunded",
                "order",
                order_id,
                "admin",
                str(admin_id),
                {"amount_minor": amount, "currency": currency, "reason": (reason or "")[:500]},
            )
        return "refunded"

    def user_usage(self, telegram_id: int, usage_by_key: dict[str, Any]) -> list[dict[str, Any]]:
        """Return paid key usage belonging to one Telegram user."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT s.plan_code, s.plan_name, s.status AS subscription_status, s.expires_at, s.starts_at,
                          k.server_id, os.label AS server_label, os.health_status AS server_health_status,
                          k.outline_key_id, k.quota_bytes, k.status,
                          k.last_usage_bytes, k.quota_reason, k.created_at,
                          (SELECT r.status FROM managed_key_repair_jobs r
                           WHERE r.kind = 'paid' AND r.server_id = k.server_id
                             AND r.local_key_ref = CAST(k.id AS TEXT)
                           ORDER BY r.created_at DESC LIMIT 1) AS repair_status,
                          (SELECT r.last_error FROM managed_key_repair_jobs r
                           WHERE r.kind = 'paid' AND r.server_id = k.server_id
                             AND r.local_key_ref = CAST(k.id AS TEXT)
                           ORDER BY r.created_at DESC LIMIT 1) AS repair_reason,
                          (SELECT j.status FROM provisioning_jobs j WHERE j.subscription_id = s.id
                           AND j.operation = 'revoke' LIMIT 1) AS revocation_status
                   FROM subscriptions s
                   JOIN paid_vpn_keys k ON k.subscription_id = s.id
                   LEFT JOIN outline_servers os ON os.server_id = k.server_id
                   WHERE s.telegram_id = ?
                     AND (k.status IN ('active', 'revoke_failed') OR k.quota_reason = 'quota')
                   ORDER BY k.created_at DESC LIMIT 10""",
                (telegram_id,),
            ).fetchall()
        result = []
        for row in rows:
            server_id = str(
                row["server_id"] or getattr(self.outline, "default_server_id", "default")
            )
            key_id = str(row["outline_key_id"])
            scoped = usage_by_key.get("byServer") if isinstance(usage_by_key, dict) else None
            if isinstance(scoped, dict):
                server_usage = scoped.get(server_id)
                server_usage = server_usage if isinstance(server_usage, dict) else {}
                server_observed = server_id in scoped
            else:
                server_usage = usage_by_key if isinstance(usage_by_key, dict) else {}
                server_observed = True
            observed = server_observed and key_id in server_usage
            raw_used = server_usage.get(key_id, row["last_usage_bytes"] or 0)
            try:
                used = max(0, int(raw_used or 0))
            except (TypeError, ValueError):
                used = max(0, int(row["last_usage_bytes"] or 0))
                observed = False
            quota = int(row["quota_bytes"] or 0)
            if quota <= 0:
                continue
            result.append(
                {
                    "outline_key_id": key_id,
                    "server_id": server_id,
                    "server_label": row["server_label"],
                    "server_health_status": row["server_health_status"],
                    "tier": row["plan_name"] or row["plan_code"],
                    "used_bytes": used,
                    "quota_bytes": quota,
                    "remaining_bytes": max(0, quota - used),
                    "usage_observed": observed,
                    "expires_at": row["expires_at"],
                    "status": "quota exhausted"
                    if row["quota_reason"] == "quota"
                    else (
                        "revocation failed"
                        if row["revocation_status"] == "failed"
                        else (
                            "revocation pending"
                            if row["subscription_status"] != "active" and row["status"] == "active"
                            else (
                                "revocation pending"
                                if row["status"] == "revoke_failed"
                                else row["status"]
                            )
                        )
                    ),
                    "repair_status": row["repair_status"],
                    "repair_reason": row["repair_reason"],
                    "created_at": row["created_at"],
                }
            )
        return result

    def user_migrated_usage(self, telegram_id: int) -> int:
        """Return usage already consumed by this user before key turnover.

        Endpoint migration replaces the local credential row with the target
        key so the new key can be delivered normally.  The migration ledger is
        the durable link to the source key's measured traffic; include only
        migrations that reached cutover/source-delete phase.  A job that failed
        before cutover still has the original key and must not be counted a
        second time.
        """
        with self.database.connect() as connection:
            if not self._table_exists(connection, "connectivity_migration_jobs"):
                return 0
            row = connection.execute(
                """SELECT COALESCE(SUM(source_used_bytes), 0) AS used
                     FROM connectivity_migration_jobs
                    WHERE telegram_id = ?
                      AND status IN ('source_delete_pending', 'completed')
                      AND source_used_bytes IS NOT NULL""",
                (int(telegram_id),),
            ).fetchone()
        try:
            return max(0, int(row["used"] if row is not None else 0))
        except (KeyError, TypeError, ValueError):
            return 0

    def prune_usage_snapshots(
        self,
        now: datetime | None = None,
        *,
        retention_days: int | None = None,
    ) -> int:
        """Bound telemetry storage while keeping a useful audit window."""
        if not self._usage_snapshot_table_available():
            return 0
        if retention_days is None:
            try:
                retention_days = int(os.environ.get("AURIX_USAGE_SNAPSHOT_RETENTION_DAYS", "90"))
            except (TypeError, ValueError):
                retention_days = 90
        retention_days = max(7, min(3_650, int(retention_days)))
        current = (now or datetime.now(UTC)).astimezone(UTC)
        cutoff = (current - timedelta(days=retention_days)).isoformat()
        with self.database.connect() as connection:
            deleted = connection.execute(
                "DELETE FROM usage_snapshots WHERE observed_at < ?",
                (cutoff,),
            )
        return max(0, int(getattr(deleted, "rowcount", 0) or 0))

    def _usage_snapshot_table_available(self) -> bool:
        with self.database.connect() as connection:
            return self._table_exists(connection, "usage_snapshots")

    def user_vpns(self, telegram_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """Return all of a user's paid entitlements without exposing secrets.

        A customer may own multiple active keys (for devices or parallel
        plans). Access URLs are decrypted only for active, non-expired keys.
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT s.id AS subscription_id, s.plan_code, s.plan_name, s.status,
                          s.expires_at, s.starts_at,
                          COALESCE(k.server_id, s.server_id) AS server_id,
                          os.label AS server_label, os.health_status AS server_health_status,
                          os.last_synced_at AS server_last_synced_at,
                          k.outline_key_id, k.access_url,
                          COALESCE(k.quota_bytes, s.quota_bytes) AS quota_bytes,
                          k.status AS key_status, k.created_at, k.quota_reason,
                          (SELECT r.status FROM managed_key_repair_jobs r
                           WHERE r.kind = 'paid'
                             AND r.server_id = COALESCE(k.server_id, s.server_id)
                             AND r.local_key_ref = CAST(k.id AS TEXT)
                           ORDER BY r.created_at DESC LIMIT 1) AS repair_status,
                          (SELECT r.last_error FROM managed_key_repair_jobs r
                           WHERE r.kind = 'paid'
                             AND r.server_id = COALESCE(k.server_id, s.server_id)
                             AND r.local_key_ref = CAST(k.id AS TEXT)
                           ORDER BY r.created_at DESC LIMIT 1) AS repair_reason
                   FROM subscriptions s
                   LEFT JOIN paid_vpn_keys k ON k.subscription_id = s.id
                   LEFT JOIN outline_servers os
                     ON os.server_id = COALESCE(k.server_id, s.server_id)
                   WHERE s.telegram_id = ? AND s.status IN ('pending', 'active', 'expired', 'revoked')
                   ORDER BY CASE s.status WHEN 'active' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                            s.starts_at DESC LIMIT ?""",
                (telegram_id, max(1, min(int(limit), 100))),
            ).fetchall()
        now_text = _now_text()
        results = []
        for row in rows:
            result = dict(row)
            result["access_url"] = self._decrypt_access_url(result.get("access_url"))
            if (
                result.get("status") != "active"
                or result.get("key_status") != "active"
                or str(result.get("expires_at") or "") <= now_text
            ):
                result["access_url"] = None
            results.append(result)
        return results

    def user_vpn(self, telegram_id: int) -> dict[str, Any] | None:
        """Backward-compatible latest/most relevant paid entitlement view."""
        subscriptions = self.user_vpns(telegram_id, limit=1)
        return subscriptions[0] if subscriptions else None

    def user_vpn_detail(
        self, telegram_id: int, subscription_id: str
    ) -> dict[str, Any] | None:
        """Return one customer-owned paid entitlement for a focused key view."""
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT s.id AS subscription_id, s.plan_code, s.plan_name, s.status,
                          s.expires_at, s.starts_at,
                          COALESCE(k.server_id, s.server_id) AS server_id,
                          os.label AS server_label, os.health_status AS server_health_status,
                          os.last_synced_at AS server_last_synced_at,
                          k.outline_key_id, k.access_url,
                          COALESCE(k.quota_bytes, s.quota_bytes) AS quota_bytes,
                          k.status AS key_status, k.created_at, k.quota_reason,
                          k.last_usage_bytes, k.last_usage_observed_at,
                          (SELECT r.status FROM managed_key_repair_jobs r
                           WHERE r.kind = 'paid'
                             AND r.server_id = COALESCE(k.server_id, s.server_id)
                             AND r.local_key_ref = CAST(k.id AS TEXT)
                           ORDER BY r.created_at DESC LIMIT 1) AS repair_status,
                          (SELECT r.last_error FROM managed_key_repair_jobs r
                           WHERE r.kind = 'paid'
                             AND r.server_id = COALESCE(k.server_id, s.server_id)
                             AND r.local_key_ref = CAST(k.id AS TEXT)
                           ORDER BY r.created_at DESC LIMIT 1) AS repair_reason
                   FROM subscriptions s
                   LEFT JOIN paid_vpn_keys k ON k.subscription_id = s.id
                   LEFT JOIN outline_servers os
                     ON os.server_id = COALESCE(k.server_id, s.server_id)
                   WHERE s.telegram_id = ? AND s.id = ?""",
                (telegram_id, subscription_id),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["access_url"] = self._decrypt_access_url(result.get("access_url"))
        if (
            result.get("status") != "active"
            or result.get("key_status") != "active"
            or str(result.get("expires_at") or "") <= _now_text()
        ):
            result["access_url"] = None
        return result

    def receipt_policy(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM receipt_verification_policy WHERE id = 1"
            ).fetchone()
        if row is None:
            return {"mode": "manual", "version": 0, "updated_at": None}
        return dict(row)

    def set_receipt_mode(
        self,
        mode: str,
        admin_id: int,
        *,
        expected_version: int | None = None,
        reason: str = "changed from Telegram admin panel",
    ) -> dict[str, Any]:
        normalized = str(mode).strip().lower()
        if normalized not in {"manual", "assisted"}:
            raise CommerceError(
                "Automatic approval requires an authoritative payment verifier; choose manual or assisted"
            )
        now_text = _now_text()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            current = connection.execute(
                "SELECT mode, version FROM receipt_verification_policy WHERE id = 1"
            ).fetchone()
            if current is None:
                raise CommerceError("Receipt verification policy is unavailable")
            if expected_version is not None and int(current["version"]) != int(expected_version):
                raise CommerceError("Receipt mode changed while you were reviewing it; refresh first")
            old_mode = str(current["mode"])
            connection.execute(
                """UPDATE receipt_verification_policy
                   SET mode = ?, version = version + 1, updated_by = ?,
                       updated_at = ?, change_reason = ? WHERE id = 1""",
                (normalized, int(admin_id), now_text, str(reason)[:500]),
            )
            self._audit(
                connection,
                "receipt_mode_changed",
                "receipt_policy",
                "1",
                "admin",
                str(admin_id),
                {"old_mode": old_mode, "new_mode": normalized},
            )
        return self.receipt_policy()

    def start_receipt_diagnostic(self, admin_id: int) -> str:
        run_id = _new_id()
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO receipt_diagnostic_runs
                   (id, admin_id, status, result_json, started_at)
                   VALUES (?, ?, 'running', '{}', ?)""",
                (run_id, int(admin_id), _now_text()),
            )
        return run_id

    def finish_receipt_diagnostic(
        self, run_id: str, admin_id: int, status: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        normalized = "passed" if status == "passed" else "failed"
        safe_result = json.loads(json.dumps(result, default=str))
        encoded = json.dumps(safe_result, sort_keys=True)
        if len(encoded) > 20_000:
            safe_result["raw_response"] = str(safe_result.get("raw_response") or "")[:4000]
            safe_result["truncated"] = True
            encoded = json.dumps(safe_result, sort_keys=True)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            updated = connection.execute(
                """UPDATE receipt_diagnostic_runs
                   SET status = ?, result_json = ?, completed_at = ?
                   WHERE id = ? AND admin_id = ? AND status = 'running'""",
                (normalized, encoded, _now_text(), str(run_id), int(admin_id)),
            )
            if int(getattr(updated, "rowcount", 0) or 0) != 1:
                raise CommerceError("Receipt diagnostic run is no longer active")
            self._audit(
                connection,
                f"receipt_diagnostic_{normalized}",
                "receipt_diagnostic",
                str(run_id),
                "admin",
                str(admin_id),
                {"summary": str(safe_result.get("summary") or "")[:300]},
            )
        return self.last_receipt_diagnostic()

    def last_receipt_diagnostic(self) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT * FROM receipt_diagnostic_runs
                   WHERE status IN ('passed', 'failed')
                   ORDER BY started_at DESC LIMIT 1"""
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["result"] = json.loads(result.pop("result_json") or "{}")
        except json.JSONDecodeError:
            result["result"] = {}
        return result

    def receipt_system_snapshot(self) -> dict[str, Any]:
        policy = self.receipt_policy()
        with self.database.connect() as connection:
            pending = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM payment_evidence WHERE review_status = 'pending'"
                ).fetchone()["count"]
            )
            failed_uploads = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM payment_evidence WHERE storage_status = 'failed'"
                ).fetchone()["count"]
            )
        return {
            "policy": policy,
            "pending_receipts": pending,
            "failed_uploads": failed_uploads,
            "last_diagnostic": self.last_receipt_diagnostic(),
            "storage_configured": self._storage_is_configured(),
            "storage_bucket": self._storage_bucket(),
        }
