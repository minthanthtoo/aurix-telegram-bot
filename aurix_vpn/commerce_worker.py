"""Durable provisioning, revocation, quota, and notification worker boundary."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from typing import Any

from .commerce_models import (
    JOB_RETRY_DELAY,
    NOTIFICATION_RETRY_DELAY,
    QUOTA_WARNING_THRESHOLDS,
    UTC,
    CommerceError,
    _human_bytes,
    _new_id,
    _now_text,
    _paid_outline_key_name,
)
from .commerce_repositories import _PostgresConnection
from .connectivity_adapters import ConnectivityAdapterRegistry
from .connectivity import ConnectivityError
from .identity import IdentityError, IdentityService
from .route_failover import FailoverError


class CommerceWorkerMixin:
    """Reliable-worker operations sharing the service transaction boundary."""

    @staticmethod
    def _usage_map(metrics: dict[str, Any] | None, endpoint_id: str) -> dict[str, Any]:
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
    def _endpoint_observed(metrics: dict[str, Any] | None, endpoint_id: str) -> bool:
        if not isinstance(metrics, dict):
            return False
        scoped = metrics.get("byEndpoint")
        if isinstance(scoped, dict):
            return endpoint_id in scoped
        # An empty/error payload is a failed observation, not zero usage.
        if not metrics or "errors" in metrics:
            return False
        return True

    def _route_for_endpoint(self, endpoint_id: str) -> dict[str, Any]:
        """Return the protocol-neutral route identity for a legacy endpoint."""
        return {
            "route_id": f"outline:{str(endpoint_id)}",
            "endpoint_id": str(endpoint_id),
            "protocol": "outline",
        }

    def _adapter_for_route(self, route: dict[str, Any], client: Any | None = None) -> Any:
        normalized = dict(route)
        protocol = str(normalized.get("protocol") or "").strip().lower()
        endpoint_id = str(normalized.get("endpoint_id") or normalized.get("route_id") or "").strip()
        if not protocol or not endpoint_id:
            raise CommerceError("managed route requires protocol and endpoint_id")
        normalized.setdefault("route_id", f"{protocol}:{endpoint_id}")
        registry = getattr(self, "adapter_registry", None) or ConnectivityAdapterRegistry()
        gateway = client
        if gateway is None:
            connectivity = getattr(self, "connectivity", None)
            gateway = connectivity.client(endpoint_id) if connectivity is not None else self.outline
        return registry.for_route(normalized, gateway)

    def _adapter_for_endpoint(self, endpoint_id: str, client: Any | None = None) -> Any:
        return self._adapter_for_route(self._route_for_endpoint(str(endpoint_id)), client)

    def reconcile_managed_route(
        self, route: dict[str, Any], client: Any | None = None, *, now: datetime | str | None = None
    ) -> dict[str, Any]:
        """Rehydrate durable protocol credentials after a node restart.

        The caller supplies the verified route metadata (including protocol
        parameters such as Xray REALITY public values).  Only active or
        retiring generations in the durable identity store are candidates for
        recreation; unknown provider users are reported and left untouched.
        """
        normalized = dict(route)
        protocol = str(normalized.get("protocol") or "").strip().lower()
        endpoint_id = str(normalized.get("endpoint_id") or "").strip()
        if not protocol or not endpoint_id:
            raise CommerceError("managed route requires protocol and endpoint_id")
        normalized.setdefault("route_id", f"{protocol}:{endpoint_id}")
        identity = getattr(self, "identity", None)
        if identity is None:
            raise CommerceError("identity service is required for managed reconciliation")
        decrypt = getattr(self, "_decrypt_access_url", None)
        expected: list[dict[str, Any]] = []
        skipped = 0
        recovery_denied = 0
        for generation in identity.generations_for_accounting():
            if str(generation.get("endpoint_id")) != endpoint_id:
                continue
            if str(generation.get("protocol") or "outline").lower() != protocol:
                continue
            if str(generation.get("status") or "") not in {"active", "retiring"}:
                skipped += 1
                continue
            if str(generation.get("remote_state") or "unknown") != "observed":
                # Unknown or delete-requested ownership is not evidence that
                # this worker may recreate a remote credential after restart.
                skipped += 1
                continue
            authorize = getattr(identity, "recovery_authorization", None)
            if callable(authorize):
                authorization = authorize(
                    str(generation["entitlement_key"]),
                    str(generation["generation_id"]),
                    now=now,
                )
                if not authorization.get("authorized"):
                    recovery_denied += 1
                    continue
                recovery_quota = int(authorization.get("remaining_bytes") or 0)
                if recovery_quota <= 0:
                    recovery_denied += 1
                    continue
            encrypted = generation.get("access_url_ciphertext")
            access_url = decrypt(encrypted) if callable(decrypt) else str(encrypted or "")
            if not access_url:
                skipped += 1
                continue
            expected.append(
                {
                    "protocol": protocol,
                    "route_id": normalized["route_id"],
                    "endpoint_id": endpoint_id,
                    "external_id": str(generation["external_id"]),
                    "access_url": access_url,
                    "name": f"AuriX {protocol} {generation['entitlement_key']}",
                }
            )
            if callable(authorize):
                expected[-1]["recovery_quota_bytes"] = recovery_quota
        adapter = self._adapter_for_route(normalized, client)
        reconcile = getattr(adapter, "reconcile_credentials", None)
        if not callable(reconcile):
            return {
                "status": "unsupported",
                "protocol": protocol,
                "route_id": normalized["route_id"],
                "expected_credentials": len(expected),
                "skipped": skipped,
            }
        result = reconcile(normalized, expected)
        return {
            **result,
            "status": "healthy",
            "protocol": protocol,
            "route_id": normalized["route_id"],
            "skipped": skipped + int(result.get("skipped", 0) or 0),
            "recovery_denied": recovery_denied,
        }

    def reconcile_managed_routes(self, routes: list[dict[str, Any]]) -> dict[str, Any]:
        """Reconcile verified routes without failing the remaining fleet."""
        results: list[dict[str, Any]] = []
        for route in routes:
            route_id = str(route.get("route_id") or route.get("endpoint_id") or "")
            try:
                results.append(self.reconcile_managed_route(route))
            except Exception as exc:
                results.append(
                    {
                        "status": "failed",
                        "route_id": route_id,
                        "error": type(exc).__name__,
                    }
                )
        return {
            "routes": len(results),
            "healthy": sum(1 for item in results if item.get("status") == "healthy"),
            "unsupported": sum(1 for item in results if item.get("status") == "unsupported"),
            "failed": sum(1 for item in results if item.get("status") == "failed"),
            "results": results,
        }

    def run_failover_once(
        self,
        *,
        route_provider: Any | None = None,
        adapter_provider: Any | None = None,
        require_data_plane_probe: bool = False,
        now: datetime | str | None = None,
    ) -> dict[str, Any] | None:
        """Execute one durable failover decision through verified adapters.

        The default provider is intentionally limited to the existing endpoint
        registry and Outline gateway. Xray/Hysteria2 callers must inject their
        verified route metadata and node-agent adapter, and should require a
        data-plane probe before enabling automatic failover.
        """
        from .failover_worker import RouteFailoverExecutor

        identity = getattr(self, "identity", None)
        failover = getattr(self, "failover", None)
        if identity is None or failover is None:
            raise CommerceError("identity and failover services are required")
        connectivity = getattr(self, "connectivity", None)

        if route_provider is None:
            if connectivity is None:
                raise CommerceError("connectivity registry is required for failover")
            route_provider = lambda endpoint_id: connectivity.endpoint(str(endpoint_id))
        if adapter_provider is None:
            def default_adapter(route: dict[str, Any]) -> Any:
                endpoint_id = str(route.get("endpoint_id") or "")
                gateway = connectivity.client(endpoint_id) if connectivity is not None else self.outline
                return self._adapter_for_route(route, gateway)

            adapter_provider = default_adapter
        assignment_transfer = getattr(connectivity, "transfer_assignment", None)
        assignment_transfer_callback = None
        if callable(assignment_transfer):
            assignment_transfer_callback = (
                lambda entitlement_key, target_endpoint_id, reason: assignment_transfer(
                    entitlement_key, target_endpoint_id, reason=reason
                )
            )
        executor = RouteFailoverExecutor(
            self.database,
            identity=identity,
            failover=failover,
            route_provider=route_provider,
            adapter_provider=adapter_provider,
            assignment_transfer=assignment_transfer_callback,
            access_url_encryptor=self._encrypt_access_url,
            require_data_plane_probe=require_data_plane_probe,
        )
        return executor.run_once(now=now)

    @staticmethod
    def _grant_from_key(key: dict[str, Any], route: dict[str, Any], *, ownership: str) -> dict[str, Any]:
        return {
            "protocol": str(route.get("protocol") or "outline"),
            "route_id": str(route.get("route_id") or ""),
            "endpoint_id": str(route.get("endpoint_id") or ""),
            "external_id": str(key.get("id") or ""),
            "access_url": str(key.get("accessUrl") or ""),
            "name": str(key.get("name") or ""),
            "ownership": ownership,
            "created": False,
        }

    def _ensure_paid_generation(
        self, key: Any, subscription: Any, now: datetime, grant: dict[str, Any] | None = None
    ) -> str:
        """Project one paid key into generation-level aggregate accounting."""
        identity = getattr(self, "identity", None)
        if identity is None:
            return ""
        endpoint_id = str(key["endpoint_id"] or "legacy-default")
        external_id = str(key["outline_key_id"])
        key_id = key["id"] if "id" in key.keys() else None
        ownership = str(
            (grant or {}).get("ownership")
            or (key["remote_ownership"] if "remote_ownership" in key.keys() else "unknown")
        )
        remote_state = "unknown" if ownership in {"unknown", "uncertain"} else "observed"
        usage_baseline_provenance = "new" if ownership == "owned" else "unknown"
        generation_id = identity.ensure_generation_for_credential(
            f"paid:{subscription['id']}",
            endpoint_id,
            credential_id=f"paid-key:{key_id or external_id}",
            external_id=external_id,
            protocol="outline",
            access_url_ciphertext=str(key["access_url"]),
            status="active" if str(key["status"]) == "active" else "unknown",
            remote_state=remote_state,
            intent_key=(grant or {}).get("intent_key"),
            usage_baseline_provenance=usage_baseline_provenance,
            now=_now_text(now),
        )
        if str(key["status"]) == "active" and str(subscription["status"]) in {"active", "pending"}:
            identity.ensure_generation_lease(
                f"paid:{subscription['id']}",
                generation_id,
                endpoint_id,
                int(key["quota_bytes"] or 0),
                str(subscription["expires_at"]),
                now=_now_text(now),
            )
        return generation_id

    def _generation_id_for_paid_key(self, key: Any) -> str:
        identity = getattr(self, "identity", None)
        if identity is None:
            return ""
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT generation_id FROM credential_generations
                    WHERE entitlement_key = ? AND endpoint_id = ? AND external_id = ?
                    ORDER BY generation_no DESC LIMIT 1""",
                (f"paid:{key['subscription_id']}", str(key["endpoint_id"] or "legacy-default"), str(key["outline_key_id"])),
            ).fetchone()
        return str(row["generation_id"]) if row is not None else ""

    def _record_generation_usage(
        self, metrics: dict[str, Any] | None, now: datetime
    ) -> list[dict[str, Any]]:
        """Account every remotely usable generation, including failover keys."""
        identity = getattr(self, "identity", None)
        if identity is None:
            return []
        try:
            generations = identity.generations_for_accounting()
        except Exception:
            return []
        exhausted: list[dict[str, Any]] = []
        for generation in generations:
            endpoint_id = str(generation["endpoint_id"])
            external_id = str(generation["external_id"])
            if not self._endpoint_observed(metrics, endpoint_id):
                # Missing endpoint metrics are not zero and must not reset or
                # release an entitlement.
                continue
            by_key = self._usage_map(metrics, endpoint_id)
            raw = by_key.get(external_id)
            if raw is None:
                continue
            try:
                observed = max(0, int(raw or 0))
            except (TypeError, ValueError):
                continue
            try:
                result = identity.record_usage(
                    str(generation["entitlement_key"]),
                    str(generation["generation_id"]),
                    observed,
                    endpoint_id=endpoint_id,
                    source_external_id=external_id,
                    observed_at=_now_text(now),
                    counter_mode=(
                        "rolling_window"
                        if str(generation.get("protocol") or "outline").lower() == "outline"
                        else "reset_on_decrease"
                    ),
                )
            except IdentityError as exc:
                # One legacy/ambiguous generation must not prevent accounting
                # for other endpoints. It remains visible for reconciliation.
                print(f"generation usage deferred: {type(exc).__name__}", file=sys.stderr)
                continue
            if result.get("exhausted"):
                exhausted.append({**generation, **result})
        return exhausted

    def _queue_aggregate_revoke_jobs(self, exhausted: list[dict[str, Any]], now: datetime) -> int:
        """Queue legacy subscription revocation once aggregate usage is exhausted."""
        queued = 0
        for generation in exhausted:
            source_type, _, source_id = str(generation["entitlement_key"]).partition(":")
            if source_type != "paid" or not source_id:
                continue
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                result = connection.execute(
                    """INSERT INTO provisioning_jobs
                       (id, subscription_id, operation, status, next_attempt_at, created_at)
                       VALUES (?, ?, 'revoke', 'pending', ?, ?)
                       ON CONFLICT(subscription_id, operation) DO NOTHING""",
                    (_new_id(), source_id, _now_text(now), _now_text(now)),
                )
                queued += int(getattr(result, "rowcount", 0) or 0) == 1
        return queued

    def _claim_job(self, operation: str, now: datetime) -> dict[str, Any] | None:
        now_text = _now_text(now)
        stale_before = _now_text(now - timedelta(minutes=5))
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE provisioning_jobs SET status = 'pending', locked_at = NULL
                   WHERE status = 'running' AND locked_at < ?""",
                (stale_before,),
            )
            lock_clause = (
                " FOR UPDATE SKIP LOCKED" if isinstance(connection, _PostgresConnection) else ""
            )
            row = connection.execute(
                """SELECT * FROM provisioning_jobs
                   WHERE operation = ? AND status = 'pending' AND next_attempt_at <= ?
                   ORDER BY created_at LIMIT 1"""
                + lock_clause,
                (operation, now_text),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """UPDATE provisioning_jobs
                   SET status = 'running', attempts = attempts + 1, locked_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (now_text, row["id"]),
            )
            result = dict(row)
            result["attempts"] = row["attempts"] + 1
            return result

    def _job_done(self, job_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE provisioning_jobs SET status = 'done', locked_at = NULL, last_error = NULL
                   WHERE id = ?""",
                (job_id,),
            )

    def _job_failed(self, job_id: str, error: Exception, now: datetime) -> None:
        safe_error = f"{type(error).__name__}: {str(error)[:500]}"
        next_attempt = _now_text(now + JOB_RETRY_DELAY)
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE provisioning_jobs
                   SET status = CASE WHEN attempts >= 8 THEN 'failed' ELSE 'pending' END,
                       next_attempt_at = ?, locked_at = NULL, last_error = ?
                   WHERE id = ?""",
                (next_attempt, safe_error, job_id),
            )

    def failed_jobs(
        self, limit: int = 20, include_nonterminal: bool = False
    ) -> list[dict[str, Any]]:
        """Return worker operations needing attention.

        The default remains terminal-only for API compatibility; operators can
        request pending/running retries so a silent revoke failure is visible
        before the eighth attempt.
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT j.id AS job_id, j.operation, j.attempts, j.last_error,
                          j.next_attempt_at, j.status AS job_status, s.order_id, s.telegram_id,
                          s.plan_code, s.status AS subscription_status
                   FROM provisioning_jobs j
                   JOIN subscriptions s ON s.id = j.subscription_id
                   WHERE j.status = 'failed' OR (? = 1 AND j.status IN ('pending', 'running'))
                   ORDER BY j.created_at LIMIT ?""",
                (1 if include_nonterminal else 0, max(1, min(limit, 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def retry_job(self, job_id: str, admin_id: int, now: datetime | None = None) -> str:
        """Requeue one exact failed job (avoids ambiguous order-level retries)."""
        current = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                "SELECT id, operation, subscription_id FROM provisioning_jobs WHERE id = ? AND status = 'failed'",
                (job_id,),
            ).fetchone()
            if row is None:
                raise CommerceError("No terminal worker failure exists for that job")
            connection.execute(
                """UPDATE provisioning_jobs SET status = 'pending', attempts = 0,
                          next_attempt_at = ?, locked_at = NULL, last_error = NULL
                   WHERE id = ? AND status = 'failed'""",
                (current, job_id),
            )
            self._audit(
                connection,
                "job_retried",
                "provisioning_job",
                job_id,
                "admin",
                str(admin_id),
                {"operation": row["operation"]},
            )
        return str(row["operation"])

    def retry_failed_job(
        self,
        order_id: str,
        admin_id: int,
        now: datetime | None = None,
        operation: str | None = None,
    ) -> str:
        """Requeue one terminal job after an operator has reviewed its error."""
        current = _now_text(now)
        if operation not in (None, "provision", "revoke"):
            raise CommerceError("Unknown worker operation")
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                """SELECT j.id, j.operation, s.id AS subscription_id
                   FROM provisioning_jobs j JOIN subscriptions s
                     ON s.id = j.subscription_id
                   WHERE s.order_id = ? AND j.status = 'failed'
                     AND (? IS NULL OR j.operation = ?)
                   ORDER BY j.created_at DESC LIMIT 1""",
                (order_id, operation, operation),
            ).fetchone()
            if row is None:
                raise CommerceError("No terminal worker failure exists for this order")
            connection.execute(
                """UPDATE provisioning_jobs
                   SET status = 'pending', attempts = 0, next_attempt_at = ?,
                       locked_at = NULL, last_error = NULL
                   WHERE id = ? AND status = 'failed'""",
                (current, row["id"]),
            )
            self._audit(
                connection,
                "job_retried",
                "subscription",
                str(row["subscription_id"]),
                "admin",
                str(admin_id),
                {"operation": row["operation"], "order_id": order_id},
            )
        return str(row["operation"])

    def _find_key(self, name: str, outline: Any | None = None) -> dict[str, Any] | None:
        gateway = outline or self.outline
        result = gateway.list_keys()
        if isinstance(result, dict):
            keys = result.get("accessKeys", [])
        else:
            keys = result if isinstance(result, list) else []
        if not isinstance(keys, list):
            raise CommerceError("Outline key inventory has an invalid shape")
        matches = [key for key in keys if isinstance(key, dict) and key.get("name") == name]
        if len(matches) > 1:
            raise CommerceError("Outline has multiple keys for one subscription")
        return matches[0] if matches else None

    def _revoke_legacy_free_keys(
        self, telegram_id: int, keep_key_id: str, username: str | None = None
    ) -> None:
        """Remove this account's tracked free/trial keys on their assigned endpoints."""
        with self.database.connect() as connection:
            if connection.__class__.__name__ == "_PostgresConnection":
                exists = connection.execute(
                    "SELECT to_regclass('public.keys') AS table_name"
                ).fetchone()
                if not exists or not exists["table_name"]:
                    return
            elif connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'keys'"
            ).fetchone() is None:
                return
            rows = connection.execute(
                """SELECT id, telegram_id, outline_key_id, endpoint_id,
                          data_limit_bytes, expires_at
                   FROM keys WHERE telegram_id = ? AND status IN ('active', 'revoke_failed')""",
                (telegram_id,),
            ).fetchall()
        for row in rows:
            timestamp = _now_text()
            try:
                endpoint_id = str(row["endpoint_id"])
                gateway = self.outline
                if self.connectivity is not None:
                    gateway = self.connectivity.client(endpoint_id)
                adapter = self._adapter_for_endpoint(endpoint_id, gateway)
                adapter.revoke_auth(
                    {
                        "protocol": "outline",
                        "route_id": f"outline:{endpoint_id}",
                        "endpoint_id": endpoint_id,
                        "external_id": str(row["outline_key_id"]),
                        # Free-key legacy rows do not store access URLs.  The
                        # adapter only needs it for contract validation, while
                        # the management API deletion itself uses the ID.
                        "access_url": "ss://legacy-redacted",
                    }
                )
                getter = getattr(gateway, "get_key", None)
                verified = False
                if callable(getter):
                    if getter(str(row["outline_key_id"])) is not None:
                        raise CommerceError("free credential still exists after delete")
                    verified = True
                with self.database.connect() as connection:
                    self.database.begin_write(connection)
                    connection.execute("UPDATE keys SET status = 'revoked' WHERE id = ?", (row["id"],))
                    connection.execute(
                        """UPDATE endpoint_assignments SET status = 'released', released_at = ?
                           WHERE free_key_id = ? AND status = 'active'""",
                        (timestamp, row["id"]),
                    )
                    connection.execute(
                        """INSERT INTO key_termination_events
                           (key_id, telegram_id, outline_key_id, reason, quota_bytes,
                            expires_at, detected_at, remote_state, delete_attempts,
                            deletion_verified_at)
                           VALUES (?, ?, ?, 'paid_upgrade_cleanup', ?, ?, ?, ?, 1, ?)
                           ON CONFLICT(key_id, reason) DO UPDATE SET
                             remote_state = excluded.remote_state,
                             delete_attempts = key_termination_events.delete_attempts + 1,
                             deletion_verified_at = excluded.deletion_verified_at""",
                        (
                            row["id"], row["telegram_id"], row["outline_key_id"],
                            row["data_limit_bytes"], row["expires_at"], timestamp,
                            "deleted_verified" if verified else "delete_accepted",
                            timestamp if verified else None,
                        ),
                    )
                identity = getattr(self, "identity", None)
                if identity is not None:
                    try:
                        with self.database.connect() as connection:
                            generation = None
                            if IdentityService._table_exists(connection, "credential_generations"):
                                generation = connection.execute(
                                    """SELECT generation_id FROM credential_generations
                                        WHERE entitlement_key = ? AND endpoint_id = ?
                                          AND external_id = ?
                                        ORDER BY generation_no DESC LIMIT 1""",
                                    (
                                        f"free:{row['id']}",
                                        endpoint_id,
                                        str(row["outline_key_id"]),
                                    ),
                                ).fetchone()
                        if generation is not None:
                            identity.mark_remote_revoked(
                                str(generation["generation_id"]), verified=verified, now=timestamp
                            )
                    except Exception as identity_exc:
                        print(
                            f"free entitlement cleanup projection deferred: {type(identity_exc).__name__}",
                            file=sys.stderr,
                        )
            except Exception as exc:
                with self.database.connect() as connection:
                    connection.execute(
                        """INSERT INTO key_termination_events
                           (key_id, telegram_id, outline_key_id, reason, quota_bytes,
                            expires_at, detected_at, remote_state, delete_attempts, last_error)
                           VALUES (?, ?, ?, 'paid_upgrade_cleanup', ?, ?, ?, 'retrying', 1, ?)
                           ON CONFLICT(key_id, reason) DO UPDATE SET
                             remote_state = 'retrying',
                             delete_attempts = key_termination_events.delete_attempts + 1,
                             last_error = excluded.last_error""",
                        (
                            row["id"], row["telegram_id"], row["outline_key_id"],
                            row["data_limit_bytes"], row["expires_at"], timestamp,
                            type(exc).__name__[:128],
                        ),
                    )

    def _provision(self, job: dict[str, Any], now: datetime) -> None:
        with self.database.connect() as connection:
            subscription = connection.execute(
                """SELECT s.*, p.quota_bytes AS catalog_quota_bytes,
                          p.name AS catalog_plan_name, u.username
                   FROM subscriptions s JOIN plans p ON p.code = s.plan_code
                   JOIN users u ON u.telegram_id = s.telegram_id
                   WHERE s.id = ?""",
                (job["subscription_id"],),
            ).fetchone()
            existing = connection.execute(
                "SELECT * FROM paid_vpn_keys WHERE subscription_id = ?",
                (job["subscription_id"],),
            ).fetchone()
        if subscription is None:
            self._job_done(job["id"])
            return
        desired_quota = (
            subscription["quota_bytes"]
            if subscription["quota_bytes"] is not None
            else subscription["catalog_quota_bytes"]
        )
        desired_plan_name = subscription["plan_name"] or subscription["catalog_plan_name"]
        outline = self.outline
        endpoint_id = "legacy-default"
        connectivity = getattr(self, "connectivity", None)
        if connectivity is not None:
            assignment = connectivity.ensure_subscription_assignment(
                str(subscription["id"]),
                str(subscription["plan_code"]),
                int(desired_quota) if desired_quota is not None else None,
                preferred_endpoint_id=subscription["preferred_endpoint_id"],
                now=now,
            )
            connectivity.attach_job(str(job["id"]), assignment.id)
            endpoint_id = assignment.endpoint_id
            outline = connectivity.client(endpoint_id)
        current_dt = (now or datetime.now(UTC)).astimezone(UTC)
        starts_dt = datetime.fromisoformat(subscription["starts_at"])
        expires_dt = datetime.fromisoformat(subscription["expires_at"])
        if current_dt < starts_dt:
            with self.database.connect() as connection:
                connection.execute(
                    """UPDATE provisioning_jobs SET status = 'pending', next_attempt_at = ?, locked_at = NULL
                       WHERE id = ?""",
                    (subscription["starts_at"], job["id"]),
                )
            return
        if subscription["status"] not in ("pending", "active"):
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                connection.execute(
                    "UPDATE subscriptions SET status = 'expired' WHERE id = ? AND status = 'pending'",
                    (subscription["id"],),
                )
                connection.execute(
                    "UPDATE provisioning_jobs SET status = 'done', locked_at = NULL, last_error = 'expired before provision' WHERE id = ?",
                    (job["id"],),
                )
            return
        # Pending entitlements have no expiry clock yet.  Their planned
        # boundary is only a scheduling hint; paid time starts at successful
        # activation below.  Already-active legacy rows retain their stored
        # expiry and are still protected from late provisioning retries.
        if subscription["status"] == "active" and current_dt >= expires_dt:
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                connection.execute(
                    "UPDATE subscriptions SET status = 'expired' WHERE id = ? AND status = 'active'",
                    (subscription["id"],),
                )
                connection.execute(
                    "UPDATE provisioning_jobs SET status = 'done', locked_at = NULL, last_error = 'expired before provision' WHERE id = ?",
                    (job["id"],),
                )
            return
        if existing is not None:
            self._ensure_paid_generation(existing, subscription, now)
            self._job_done(job["id"])
            return
        key_name = _paid_outline_key_name(subscription)
        route = self._route_for_endpoint(endpoint_id)
        adapter = self._adapter_for_endpoint(endpoint_id, outline)
        key = None
        deterministic_id = f"aurix-{subscription['id']}"
        getter = getattr(outline, "get_key", None)
        if callable(getter):
            try:
                key = getter(deterministic_id)
            except Exception:
                key = None
        if key is None:
            key = self._find_key(key_name, outline)
        if key is None:
            legacy_key = self._find_key(f"aurix-sub-{subscription['id']}", outline)
            if legacy_key is not None:
                key = legacy_key
                rename = getattr(outline, "rename_key", None)
                if callable(rename):
                    rename(str(key["id"]), key_name)
        if key is None:
            # The adapter owns deterministic create/read-back semantics.  A
            # recovered timeout is returned as ownership=uncertain and is
            # therefore never cleanup-deleted if the local write later fails.
            grant = adapter.provision(
                route,
                {
                    "external_id": deterministic_id,
                    "name": key_name,
                    "quota_bytes": desired_quota,
                },
            )
            grant["intent_key"] = f"paid-provision:{job['id']}"
            key = {"id": grant["external_id"], "accessUrl": grant["access_url"], "name": key_name}
        else:
            grant = self._grant_from_key(key, route, ownership="preexisting")
            grant["intent_key"] = f"paid-provision:{job['id']}"
            if desired_quota is not None:
                adapter.apply_quota_cap(grant, int(desired_quota))
        if not isinstance(key, dict) or not key.get("id") or not key.get("accessUrl"):
            raise CommerceError("Outline key response lacks id or accessUrl")
        ownership = str(grant.get("ownership") or "unknown")
        remote_state = "unknown" if ownership in {"unknown", "uncertain"} else "observed"
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE provisioning_jobs
                      SET remote_credential_id = ?, remote_ownership = ?,
                          remote_state = ?
                    WHERE id = ?""",
                (str(key["id"]), ownership, remote_state, job["id"]),
            )
        try:
            created_at = _now_text(now)
            activated_at = current_dt.isoformat()
            activated_expires_at = (
                current_dt + timedelta(days=int(subscription["duration_days"] or 0))
            ).isoformat()
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                connection.execute(
                    """INSERT INTO paid_vpn_keys
                       (id, subscription_id, telegram_id, outline_key_id, access_url,
                        quota_bytes, status, created_at, endpoint_id,
                        remote_ownership, remote_state)
                       VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
                    (
                        _new_id(),
                        subscription["id"],
                        subscription["telegram_id"],
                        str(key["id"]),
                        self._encrypt_access_url(str(key["accessUrl"])),
                        desired_quota,
                        created_at,
                        endpoint_id,
                        ownership,
                        remote_state,
                    ),
                )
                connection.execute(
                    """UPDATE subscriptions
                       SET status = 'active', activated_at = ?, starts_at = ?, expires_at = ?
                       WHERE id = ?""",
                    (activated_at, activated_at, activated_expires_at, subscription["id"]),
                )
                connection.execute(
                    """INSERT INTO notifications
                       (id, dedupe_key, telegram_id, kind, text, access_url_ciphertext,
                        status, next_attempt_at, created_at)
                       VALUES (?, ?, ?, 'vpn_ready', ?, ?, 'pending', ?, ?)
                       ON CONFLICT(dedupe_key) DO NOTHING""",
                    (
                        _new_id(),
                        f"vpn-ready:{subscription['id']}",
                        subscription["telegram_id"],
                        f"Your {desired_plan_name} AuriX VPN is ready.\n\nExpires: {activated_expires_at}",
                        self._encrypt_access_url(str(key["accessUrl"])),
                        created_at,
                        created_at,
                    ),
                )
                self._audit(
                    connection,
                    "key_provisioned",
                    "subscription",
                    subscription["id"],
                    "system",
                    None,
                    {"outline_key_id": str(key["id"]), "activated_at": activated_at},
                )
                connection.execute(
                    """UPDATE provisioning_jobs SET status = 'done', locked_at = NULL, last_error = NULL
                       WHERE id = ?""",
                    (job["id"],),
                )
            try:
                self._ensure_paid_generation(
                    {
                        "id": None,
                        "subscription_id": subscription["id"],
                        "outline_key_id": str(key["id"]),
                        "access_url": self._encrypt_access_url(str(key["accessUrl"])),
                        "quota_bytes": desired_quota,
                        "status": "active",
                        "endpoint_id": endpoint_id,
                    },
                    subscription,
                    now,
                    grant,
                )
            except Exception as exc:
                # Remote access exists, but the accounting projection did not
                # commit. Leave a durable reconciliation job rather than a
                # misleading terminal success or destructive cleanup attempt.
                with self.database.connect() as recovery_connection:
                    recovery_connection.execute(
                        """UPDATE provisioning_jobs SET status = 'pending', locked_at = NULL,
                                  last_error = ? WHERE id = ?""",
                        (f"generation projection: {type(exc).__name__}: {str(exc)[:400]}", job["id"]),
                    )
                raise
            # A paid account supersedes any free/trial key.  This is best-effort
            # cleanup; the paid key remains authoritative and the next startup
            # reconciliation can retry removal if the inventory call failed.
            self._revoke_legacy_free_keys(
                subscription["telegram_id"], str(key["id"]), subscription["username"]
            )
        except Exception:
            # Do not delete here.  The remote create may have committed even
            # when the local transaction failed, and local state is not proof
            # that this worker owns the remote credential.  Deterministic
            # reconciliation converges the durable generation on retry.
            raise

    def _expire(self, now: datetime) -> int:
        now_text = _now_text(now)
        count = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            rows = connection.execute(
                """SELECT id FROM subscriptions
                   WHERE status = 'active' AND expires_at <= ?""",
                (now_text,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE subscriptions SET status = 'expired' WHERE id = ?",
                    (row["id"],),
                )
                self._audit(
                    connection,
                    "subscription_expired",
                    "subscription",
                    row["id"],
                    "system",
                    None,
                    {"detected_at": now_text},
                )
                connection.execute(
                    """INSERT INTO provisioning_jobs
                       (id, subscription_id, operation, status, next_attempt_at, created_at)
                       VALUES (?, ?, 'revoke', 'pending', ?, ?)
                       ON CONFLICT(subscription_id, operation) DO NOTHING""",
                    (_new_id(), row["id"], now_text, now_text),
                )
                count += 1
        return count

    def _revoke_generation_set(self, entitlement_key: str, now: datetime) -> dict[str, tuple[str, bool]]:
        """Revoke every remote generation while preserving proof boundaries."""
        identity = getattr(self, "identity", None)
        if identity is None:
            return {}
        result: dict[str, tuple[str, bool]] = {}
        for generation in identity.generations_for_accounting(entitlement_key):
            endpoint_id = str(generation["endpoint_id"])
            protocol = str(generation.get("protocol") or "outline").strip().lower()
            client = self.outline
            if self.connectivity is not None:
                client = self.connectivity.client(endpoint_id)
            route = {
                "protocol": protocol,
                "route_id": f"{protocol}:{endpoint_id}",
                "endpoint_id": endpoint_id,
            }
            adapter = self._adapter_for_route(route, client)
            grant = {
                **route,
                "external_id": str(generation["external_id"]),
                "access_url": self._decrypt_access_url(generation.get("access_url_ciphertext"))
                or ("ss://legacy-redacted" if protocol == "outline" else "managed://legacy-redacted"),
            }
            adapter.revoke_auth(grant)
            verification = adapter.verify_auth_revoked(grant)
            if verification.get("exists") is True:
                raise CommerceError("remote credential still exists after delete")
            verified = bool(verification.get("verified"))
            session_result = adapter.terminate_sessions(grant)
            sessions_terminated = bool(
                isinstance(session_result, dict)
                and session_result.get("supported")
                and session_result.get("terminated")
            )
            identity.mark_remote_revoked(
                str(generation["generation_id"]),
                verified=verified,
                sessions_terminated=sessions_terminated,
                now=_now_text(now),
            )
            result[str(generation["external_id"])] = (
                "deleted_verified" if verified else "delete_accepted",
                verified,
            )
        return result

    def _revoke(self, job: dict[str, Any], now: datetime) -> None:
        with self.database.connect() as connection:
            key = connection.execute(
                """SELECT k.*, s.status AS subscription_status,
                          o.id AS order_id, o.refund_status
                   FROM paid_vpn_keys k
                   JOIN subscriptions s ON s.id = k.subscription_id
                   JOIN orders o ON o.id = s.order_id
                   WHERE k.subscription_id = ?""",
                (job["subscription_id"],),
            ).fetchone()
        generation_results = self._revoke_generation_set(f"paid:{job['subscription_id']}", now)
        if key is None or (key["status"] == "revoked" and not generation_results):
            with self.database.connect() as connection:
                connection.execute(
                    """UPDATE endpoint_assignments SET status = 'released', released_at = ?
                       WHERE subscription_id = ? AND status = 'active'""",
                    (_now_text(now), job["subscription_id"]),
                )
            self._job_done(job["id"])
            return
        try:
            endpoint_id = str(key["endpoint_id"] or "legacy-default")
            remote_state, verified = generation_results.get(
                str(key["outline_key_id"]), ("delete_accepted", False)
            )
            if str(key["outline_key_id"]) not in generation_results:
                outline = self.outline
                if self.connectivity is not None and key["endpoint_id"]:
                    outline = self.connectivity.client(endpoint_id)
                adapter = self._adapter_for_endpoint(endpoint_id, outline)
                adapter.revoke_auth(
                    {
                        "protocol": "outline",
                        "route_id": f"outline:{endpoint_id}",
                        "endpoint_id": endpoint_id,
                        "external_id": str(key["outline_key_id"]),
                        "access_url": self._decrypt_access_url(key["access_url"]) or "ss://legacy-redacted",
                    }
                )
                getter = getattr(outline, "get_key", None)
                if callable(getter):
                    if getter(str(key["outline_key_id"])) is not None:
                        raise CommerceError("Outline key still exists after delete")
                    remote_state = "deleted_verified"
                    verified = True
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                connection.execute(
                    """UPDATE paid_vpn_keys SET status = 'revoked', revoked_at = ?
                       WHERE id = ?""",
                    (_now_text(now), key["id"]),
                )
                connection.execute(
                    """UPDATE endpoint_assignments SET status = 'released', released_at = ?
                       WHERE subscription_id = ? AND status = 'active'""",
                    (_now_text(now), job["subscription_id"]),
                )
                connection.execute(
                    "UPDATE provisioning_jobs SET status = 'done', locked_at = NULL WHERE id = ?",
                    (job["id"],),
                )
                quota_reason = key["quota_reason"] if "quota_reason" in key.keys() else None
                quota_event = connection.execute(
                    """SELECT observed_bytes, quota_bytes, observed_at FROM quota_events
                       WHERE subscription_id = ? AND reason = 'quota'""",
                    (job["subscription_id"],),
                ).fetchone()
                if key["refund_status"] == "refunded":
                    notice = "Your AuriX order was refunded to your wallet and its VPN access was terminated."
                    notice_kind = "payment_refunded"
                elif quota_reason == "quota":
                    usage = (
                        f" Observed usage: {int(quota_event['observed_bytes']):,} / "
                        f"{int(quota_event['quota_bytes']):,} bytes."
                        if quota_event is not None
                        else ""
                    )
                    notice = (
                        "Your AuriX VPN key reached its data limit and was terminated."
                        + usage
                        + " Renew to receive a new key."
                    )
                    notice_kind = "vpn_quota"
                else:
                    notice = "Your AuriX VPN subscription expired and its key was terminated. Renew to restore access."
                    notice_kind = "vpn_expired"
                if remote_state == "deleted_verified":
                    notice += " Outline confirmed the credential is deleted."
                connection.execute(
                    """INSERT INTO notifications
                       (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
                       VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                       ON CONFLICT(dedupe_key) DO NOTHING""",
                    (
                        _new_id(),
                        (
                            f"access-revoked:{key['order_id']}"
                            if key["refund_status"] == "refunded"
                            else f"vpn-{notice_kind}:{job['subscription_id']}"
                        ),
                        key["telegram_id"],
                        notice_kind,
                        notice,
                        _now_text(now),
                        _now_text(now),
                    ),
                )
                self._audit(
                    connection,
                    "key_revoked",
                    "subscription",
                    job["subscription_id"],
                    "system",
                    None,
                    {
                        "outline_key_id": key["outline_key_id"],
                        "reason": (
                            "refund"
                            if key["refund_status"] == "refunded"
                            else (quota_reason or "expiry")
                        ),
                        "remote_state": remote_state,
                        "last_usage_bytes": key["last_usage_bytes"],
                        "quota_bytes": key["quota_bytes"],
                    },
                )
            identity = getattr(self, "identity", None)
            if identity is not None:
                identity.mark_remote_revoked(
                    self._generation_id_for_paid_key(key),
                    verified=verified,
                    now=_now_text(now),
                )
        except Exception:
            # Keep the entitlement marked active until the remote delete is
            # actually confirmed. The job status/attempts are the retry state;
            # exposing ``revoke_failed`` as an access state made customers and
            # operators believe a credential had already been revoked.
            raise

    def process_jobs(self, now: datetime | None = None, max_jobs: int = 10) -> int:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        processed = 0
        # Revokes run first so an expired/quota-exhausted key is removed before
        # a scheduled renewal provisions its replacement.
        while processed < max_jobs:
            job = self._claim_job("revoke", current)
            if job is None:
                break
            try:
                self._revoke(job, current)
            except Exception as exc:
                self._job_failed(job["id"], exc, current)
            processed += 1
        while processed < max_jobs:
            job = self._claim_job("provision", current)
            if job is None:
                break
            try:
                self._provision(job, current)
            except Exception as exc:
                self._job_failed(job["id"], exc, current)
            processed += 1
        return processed

    def expire_and_process(self, now: datetime | None = None) -> int:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        identity = getattr(self, "identity", None)
        if identity is not None:
            try:
                identity.reconcile_legacy(now=_now_text(current))
            except Exception as exc:
                # Reconciliation is retried on the next maintenance pass; it
                # must not turn a transient inventory/schema issue into an
                # unsafe remote revoke.
                print(f"generation reconciliation error: {type(exc).__name__}", file=sys.stderr)
        self.release_expired_wallet_reservations(current)
        self.expire_open_orders(current)
        self._expire(current)
        return self.process_jobs(current)

    def queue_quota_warnings(
        self,
        now: datetime | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> int:
        """Queue one Telegram warning as each remaining-quota threshold is crossed."""
        if metrics is None:
            try:
                metrics = self.outline.transfer_metrics()
            except Exception:
                return 0
        current = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = _now_text(current)
        queued = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            rows = connection.execute(
                """SELECT k.id, k.subscription_id, k.telegram_id, k.outline_key_id, k.endpoint_id,
                          k.quota_bytes, k.status, k.quota_warning_percent,
                          s.plan_code, s.expires_at
                   FROM paid_vpn_keys k JOIN subscriptions s ON s.id = k.subscription_id
                   WHERE k.status = 'active' AND s.status = 'active'
                     AND k.quota_bytes IS NOT NULL"""
            ).fetchall()
            for row in rows:
                endpoint_id = str(row["endpoint_id"])
                if not self._endpoint_observed(metrics, endpoint_id):
                    continue
                by_key = self._usage_map(metrics, endpoint_id)
                try:
                    used = max(0, int(by_key.get(str(row["outline_key_id"]), 0) or 0))
                    quota = int(row["quota_bytes"])
                except (TypeError, ValueError):
                    continue
                if quota <= 0 or used >= quota:
                    continue
                remaining = quota - used
                reached = next(
                    (
                        percent
                        for percent, fraction in reversed(QUOTA_WARNING_THRESHOLDS)
                        if remaining <= quota * fraction
                    ),
                    None,
                )
                if reached is None:
                    continue
                previous = row["quota_warning_percent"]
                if previous is not None and int(previous) <= reached:
                    continue
                dedupe_key = f"quota-warning:paid:{row['subscription_id']}:{reached}"
                try:
                    existing = connection.execute(
                        "SELECT id FROM notifications WHERE dedupe_key = ?",
                        (dedupe_key,),
                    ).fetchone()
                    if existing is None:
                        remaining_percent = remaining * 100 / quota
                        text = (
                            f"Quota warning: your AuriX {row['plan_code']} key has "
                            f"{_human_bytes(remaining)} remaining "
                            f"({remaining_percent:.1f}% of {_human_bytes(quota)}).\n"
                            "This is based on Outline's trailing-30-day usage. "
                            "When no quota remains, the key will be blocked and deleted. "
                            f"Expires: {row['expires_at']}"
                        )
                        connection.execute(
                            """INSERT INTO notifications
                               (id, dedupe_key, telegram_id, kind, text, status,
                                next_attempt_at, created_at)
                               VALUES (?, ?, ?, 'quota_warning', ?, 'pending', ?, ?)""",
                            (_new_id(), dedupe_key, row["telegram_id"], text, now_text, now_text),
                        )
                        queued += 1
                    connection.execute(
                        "UPDATE paid_vpn_keys SET quota_warning_percent = ? WHERE id = ?",
                        (reached, row["id"]),
                    )
                except Exception as exc:
                    if self.database.is_integrity_error(exc):
                        continue
                    raise
        return queued

    def enforce_quotas(
        self,
        now: datetime | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> int:
        """Observe Outline transfer metrics and queue one idempotent hard revoke.

        Outline's per-key data limit is the immediate safety brake.  Metrics are
        only an observation; once ``used >= quota`` is seen we fail closed in
        AuriX and delete the known remote key.  Missing/stale metrics never
        restore or disable a key.
        """
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if metrics is None:
            try:
                metrics = self.outline.transfer_metrics()
            except Exception:
                return 0
        exhausted = self._record_generation_usage(metrics, current)
        scheduled = self._queue_aggregate_revoke_jobs(exhausted, current)
        try:
            self.queue_quota_warnings(current, metrics)
        except Exception as exc:
            # A notification outage must never delay the hard quota revoke.
            print(f"paid quota warning error: {type(exc).__name__}", file=sys.stderr)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT k.id, k.subscription_id, k.outline_key_id, k.endpoint_id, k.quota_bytes,
                          k.status, s.status AS subscription_status
                   FROM paid_vpn_keys k JOIN subscriptions s ON s.id = k.subscription_id
                   WHERE k.status = 'active' AND s.status = 'active' AND k.quota_bytes IS NOT NULL"""
            ).fetchall()
        for row in rows:
            endpoint_id = str(row["endpoint_id"])
            if not self._endpoint_observed(metrics, endpoint_id):
                continue
            by_key = self._usage_map(metrics, endpoint_id)
            try:
                used = int(by_key.get(str(row["outline_key_id"]), 0) or 0)
            except (TypeError, ValueError):
                continue
            quota = int(row["quota_bytes"])
            if used < quota:
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE paid_vpn_keys SET last_usage_bytes = ?, last_usage_observed_at = ? WHERE id = ?",
                        (used, _now_text(current), row["id"]),
                    )
                continue
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                existing = connection.execute(
                    "SELECT id FROM quota_events WHERE subscription_id = ? AND reason = 'quota'",
                    (row["subscription_id"],),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """INSERT INTO quota_events
                           (id, subscription_id, reason, observed_bytes, quota_bytes, observed_at)
                           VALUES (?, ?, 'quota', ?, ?, ?)""",
                        (_new_id(), row["subscription_id"], used, quota, _now_text(current)),
                    )
                    scheduled += 1
                connection.execute(
                    """UPDATE paid_vpn_keys SET status = 'active',
                              last_usage_bytes = ?, last_usage_observed_at = ?, quota_reason = 'quota'
                       WHERE id = ? AND status = 'active'""",
                    (used, _now_text(current), row["id"]),
                )
                connection.execute(
                    "UPDATE subscriptions SET status = 'revoked' WHERE id = ? AND status = 'active'",
                    (row["subscription_id"],),
                )
                connection.execute(
                    """INSERT INTO provisioning_jobs
                       (id, subscription_id, operation, status, next_attempt_at, created_at)
                       VALUES (?, ?, 'revoke', 'pending', ?, ?)
                       ON CONFLICT(subscription_id, operation) DO NOTHING""",
                    (_new_id(), row["subscription_id"], _now_text(current), _now_text(current)),
                )
        return scheduled

    def capacity_snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        """Return operator-only counts and mapped transfer usage, never access URLs."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        expiring_at = _now_text(current + timedelta(hours=24))
        endpoints: list[dict[str, Any]] = []
        if self.connectivity is None:
            server = self.outline.server_info()
            outline_version = str(server.get("version", "unknown"))[:64]
        else:
            endpoints = self.connectivity.list_endpoints()
            versions = sorted(
                {str(item.get("outline_version")) for item in endpoints if item.get("outline_version")}
            )
            outline_version = ", ".join(versions)[:64] or "unknown"
        with self.database.connect() as connection:
            counts = connection.execute(
                """SELECT
                       (SELECT COUNT(*) FROM subscriptions WHERE status = 'active') AS active_subscriptions,
                       (SELECT COUNT(*) FROM paid_vpn_keys WHERE status = 'active') AS active_keys,
                       (SELECT COUNT(*) FROM provisioning_jobs WHERE status IN ('pending', 'running')) AS pending_jobs,
                       (SELECT COUNT(*) FROM provisioning_jobs WHERE status = 'failed') AS failed_jobs,
                       (SELECT COUNT(*) FROM subscriptions
                          WHERE status = 'active'
                            AND expires_at <= ?) AS expiring_24h""",
                (expiring_at,),
            ).fetchone()
            key_rows = connection.execute(
                """SELECT outline_key_id, endpoint_id, telegram_id, quota_bytes
                   FROM paid_vpn_keys WHERE status = 'active'"""
            ).fetchall()
        metrics = (
            self.connectivity.collect_metrics()
            if self.connectivity is not None
            else self.outline.transfer_metrics()
        )
        usage = []
        for row in key_rows:
            by_key = self._usage_map(metrics, str(row["endpoint_id"]))
            raw_used = by_key.get(str(row["outline_key_id"]), 0)
            try:
                used_bytes = max(0, int(raw_used or 0))
            except (TypeError, ValueError):
                used_bytes = 0
            usage.append(
                {
                    "outline_key_id": row["outline_key_id"],
                    "endpoint_id": row["endpoint_id"],
                    "telegram_id": row["telegram_id"],
                    "quota_bytes": row["quota_bytes"],
                    "used_bytes": used_bytes,
                }
            )
        if endpoints:
            endpoint_usage: dict[str, int] = {}
            for item in usage:
                endpoint_usage[str(item["endpoint_id"])] = (
                    endpoint_usage.get(str(item["endpoint_id"]), 0) + int(item["used_bytes"])
                )
            for endpoint in endpoints:
                endpoint["mapped_transfer_bytes"] = endpoint_usage.get(str(endpoint["id"]), 0)
        return {
            **dict(counts),
            "outline_version": outline_version,
            "usage": usage,
            "endpoints": endpoints,
            "metrics_errors": metrics.get("errors", {}) if isinstance(metrics, dict) else {},
        }

    def protocol_profile_promotion_readiness(
        self,
        endpoint_id: str,
        protocol: str,
        *,
        required_signals: tuple[str, ...] | list[str],
        required_capabilities: tuple[str, ...] | list[str] = (),
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return the registry's redacted, non-mutating promotion preview."""
        if self.connectivity is None:
            raise CommerceError("Protocol profile management is not configured")
        method = getattr(self.connectivity, "protocol_profile_promotion_readiness", None)
        if not callable(method):
            raise CommerceError("Protocol profile readiness is not available")
        try:
            result = dict(
                method(
                    endpoint_id,
                    protocol,
                    required_signals=required_signals,
                    required_capabilities=required_capabilities,
                    now=now,
                )
            )
        except ConnectivityError as exc:
            raise CommerceError(str(exc)) from exc
        if not self._protocol_adapter_registered(protocol):
            result["adapter_registered"] = False
            reasons = list(result.get("reasons") or [])
            reasons.append("protocol adapter is not registered")
            result["reasons"] = reasons
            result["promotable"] = False
        else:
            result["adapter_registered"] = True
        return result

    def _protocol_adapter_registered(self, protocol: str) -> bool:
        """Require an installed adapter before the commerce boundary can enable it."""
        normalized = str(protocol or "").strip().lower()
        registry = getattr(self, "adapter_registry", None) or ConnectivityAdapterRegistry()
        catalog = getattr(registry, "protocol_catalog", None)
        if not callable(catalog):
            return False
        try:
            return any(
                str(item.get("protocol") or "").strip().lower() == normalized
                for item in catalog()
                if isinstance(item, dict)
            )
        except Exception:
            return False

    def promote_protocol_profile(
        self,
        endpoint_id: str,
        protocol: str,
        admin_id: int,
        *,
        required_signals: tuple[str, ...] | list[str],
        required_capabilities: tuple[str, ...] | list[str] = (),
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Apply an explicit, audited protocol promotion decision."""
        if self.connectivity is None:
            raise CommerceError("Protocol profile management is not configured")
        if not self._protocol_adapter_registered(protocol):
            raise CommerceError("protocol adapter is not registered")
        method = getattr(self.connectivity, "promote_protocol_profile", None)
        if not callable(method):
            raise CommerceError("Protocol profile promotion is not available")
        try:
            return method(
                endpoint_id,
                protocol,
                required_signals=required_signals,
                required_capabilities=required_capabilities,
                actor_id=admin_id,
                now=now,
            )
        except ConnectivityError as exc:
            raise CommerceError(str(exc)) from exc

    def configure_endpoint_capacity(
        self,
        endpoint_id: str,
        admin_id: int,
        *,
        max_active_keys: int | None,
        accepts_new_assignments: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Apply a reversible allocation gate; it never deletes or moves keys."""
        if self.connectivity is None:
            raise CommerceError("Endpoint capacity management is not configured")
        if max_active_keys is not None and not 1 <= int(max_active_keys) <= 100_000:
            raise CommerceError("Endpoint key capacity must be between 1 and 100000")
        timestamp = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            existing = connection.execute(
                "SELECT id, state FROM vpn_endpoints WHERE id = ? AND state != 'RETIRED'",
                (endpoint_id,),
            ).fetchone()
            if existing is None:
                raise CommerceError("VPN endpoint does not exist")
            if accepts_new_assignments and str(existing["state"]) != "ACTIVE":
                raise CommerceError("Only an active endpoint can accept new assignments")
            connection.execute(
                """UPDATE vpn_endpoints SET max_active_keys = ?, accepts_new_assignments = ?
                   WHERE id = ?""",
                (max_active_keys, bool(accepts_new_assignments), endpoint_id),
            )
            connection.execute(
                """INSERT INTO infrastructure_events
                   (id, endpoint_id, event_type, metadata_json, created_at)
                   VALUES (?, ?, 'capacity_configured', ?, ?)""",
                (
                    _new_id(),
                    endpoint_id,
                    json.dumps(
                        {
                            "admin_id": int(admin_id),
                            "max_active_keys": max_active_keys,
                            "accepts_new_assignments": bool(accepts_new_assignments),
                        },
                        sort_keys=True,
                    ),
                    timestamp,
                ),
            )
        return self.connectivity.endpoint(endpoint_id)

    def endpoint_drain_preview(
        self,
        source_endpoint_id: str,
        *,
        target_endpoint_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return a read-only preview for the operator drain confirmation."""
        if self.connectivity is None or self.failover is None:
            raise CommerceError("Endpoint drain management is not configured")
        try:
            return self.failover.endpoint_drain_preview(
                source_endpoint_id,
                target_endpoint_id=target_endpoint_id,
                limit=limit,
            )
        except FailoverError as exc:
            raise CommerceError(str(exc)) from exc

    def request_endpoint_drain(
        self,
        source_endpoint_id: str,
        admin_id: int,
        *,
        target_endpoint_id: str | None = None,
        limit: int = 50,
        reason: str = "operator-drain",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Pause one endpoint and queue a bounded, auditable migration cohort."""
        if self.connectivity is None or self.failover is None:
            raise CommerceError("Endpoint drain management is not configured")
        try:
            return self.failover.request_endpoint_drain(
                source_endpoint_id,
                target_endpoint_id=target_endpoint_id,
                limit=limit,
                actor_id=admin_id,
                reason=reason,
                now=now,
            )
        except FailoverError as exc:
            raise CommerceError(str(exc)) from exc

    def configure_endpoint_plan_limit(
        self,
        endpoint_id: str,
        plan_code: str,
        admin_id: int,
        *,
        max_active_assignments: int | None,
        enabled: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Set one endpoint/plan allocation without changing active credentials."""
        if self.connectivity is None:
            raise CommerceError("Endpoint plan allocation is not configured")
        normalized = str(plan_code).strip()
        if not normalized or len(normalized) > 64:
            raise CommerceError("Plan code is invalid")
        if max_active_assignments is not None and not 0 <= int(max_active_assignments) <= 100_000:
            raise CommerceError("Plan capacity must be between 0 and 100000")
        timestamp = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            if connection.execute(
                "SELECT id FROM vpn_endpoints WHERE id = ? AND state != 'RETIRED'",
                (endpoint_id,),
            ).fetchone() is None:
                raise CommerceError("VPN endpoint does not exist")
            connection.execute(
                """INSERT INTO endpoint_plan_limits
                   (endpoint_id, plan_code, enabled, max_active_assignments)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(endpoint_id, plan_code) DO UPDATE SET
                     enabled = excluded.enabled,
                     max_active_assignments = excluded.max_active_assignments""",
                (endpoint_id, normalized, bool(enabled), max_active_assignments),
            )
            connection.execute(
                """INSERT INTO infrastructure_events
                   (id, endpoint_id, event_type, metadata_json, created_at)
                   VALUES (?, ?, 'plan_capacity_configured', ?, ?)""",
                (
                    _new_id(),
                    endpoint_id,
                    json.dumps(
                        {
                            "admin_id": int(admin_id),
                            "plan_code": normalized,
                            "max_active_assignments": max_active_assignments,
                            "enabled": bool(enabled),
                        },
                        sort_keys=True,
                    ),
                    timestamp,
                ),
            )
        return {
            "endpoint_id": endpoint_id,
            "plan_code": normalized,
            "max_active_assignments": max_active_assignments,
            "enabled": bool(enabled),
        }

    def endpoint_plan_capacity(self, endpoint_id: str) -> list[dict[str, Any]]:
        """Return policy and active reservations for one admin endpoint panel."""
        if self.connectivity is None:
            raise CommerceError("Endpoint plan allocation is not configured")
        with self.database.connect() as connection:
            if connection.execute(
                "SELECT id FROM vpn_endpoints WHERE id = ?", (endpoint_id,)
            ).fetchone() is None:
                raise CommerceError("VPN endpoint does not exist")
            rows = connection.execute(
                """SELECT plans.plan_code,
                          COALESCE(l.enabled, ?) AS enabled,
                          l.max_active_assignments,
                          COUNT(a.id) AS active_assignments
                   FROM (
                     SELECT code AS plan_code FROM plans WHERE active = ?
                     UNION SELECT 'FREE300MB'
                     UNION SELECT 'FREE3GB'
                   ) plans
                   LEFT JOIN endpoint_plan_limits l
                     ON l.endpoint_id = ? AND l.plan_code = plans.plan_code
                   LEFT JOIN endpoint_assignments a
                     ON a.endpoint_id = ? AND a.plan_code = plans.plan_code
                    AND a.status = 'active'
                   GROUP BY plans.plan_code, l.enabled, l.max_active_assignments
                   ORDER BY plans.plan_code""",
                (True, True, endpoint_id, endpoint_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_notifications(
        self, now: datetime | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        now_text = _now_text(now)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM notifications
                   WHERE status IN ('pending', 'failed')
                     AND dead_lettered_at IS NULL
                     AND next_attempt_at <= ?
                     AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                   ORDER BY created_at LIMIT ?""",
                (now_text, now_text, max(1, min(limit, 100))),
            ).fetchall()
        notifications = []
        for row in rows:
            notification = dict(row)
            access_url = self._decrypt_access_url(notification.get("access_url_ciphertext"))
            if access_url:
                notification["text"] += f"\n\nYour Outline key:\n{access_url}"
                notification["access_url"] = access_url
            elif notification.get("access_url_ciphertext"):
                notification["secret_unavailable"] = True
            notifications.append(notification)
        return notifications

    def claim_notifications(
        self,
        *,
        lease_owner: str,
        now: datetime | None = None,
        limit: int = 20,
        lease_seconds: int = 300,
    ) -> list[dict[str, Any]]:
        """Atomically claim due notifications for one delivery worker.

        A crashed worker's lease becomes reclaimable after the bounded expiry.
        Completion methods require the returned token, so a stale worker cannot
        mark a message sent after another worker has reclaimed it.
        """
        owner = str(lease_owner or "").strip()
        if not owner or len(owner) > 128:
            raise CommerceError("notification lease owner is invalid")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = current.isoformat()
        expires_text = (current + timedelta(seconds=max(30, min(int(lease_seconds), 3600)))).isoformat()
        claimed: list[dict[str, Any]] = []
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            lock_clause = (
                " FOR UPDATE SKIP LOCKED" if isinstance(connection, _PostgresConnection) else ""
            )
            rows = connection.execute(
                """SELECT * FROM notifications
                   WHERE status IN ('pending', 'failed')
                     AND dead_lettered_at IS NULL
                     AND next_attempt_at <= ?
                     AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                   ORDER BY created_at LIMIT ?"""
                + lock_clause,
                (now_text, now_text, max(1, min(int(limit), 100))),
            ).fetchall()
            for row in rows:
                token = _new_id()
                result = connection.execute(
                    """UPDATE notifications
                       SET lease_owner = ?, lease_token = ?, lease_expires_at = ?
                       WHERE id = ?
                         AND (lease_expires_at IS NULL OR lease_expires_at <= ?)""",
                    (owner, token, expires_text, row["id"], now_text),
                )
                if int(getattr(result, "rowcount", 0) or 0) != 1:
                    continue
                notification = dict(row)
                notification.update(
                    {
                        "lease_owner": owner,
                        "lease_token": token,
                        "lease_expires_at": expires_text,
                    }
                )
                claimed.append(notification)
        for notification in claimed:
            access_url = self._decrypt_access_url(notification.get("access_url_ciphertext"))
            if access_url:
                notification["text"] += f"\n\nYour Outline key:\n{access_url}"
                notification["access_url"] = access_url
            elif notification.get("access_url_ciphertext"):
                notification["secret_unavailable"] = True
        return claimed

    def mark_notification_sent(
        self,
        notification_id: str,
        now: datetime | None = None,
        lease_token: str | None = None,
    ) -> bool:
        with self.database.connect() as connection:
            if lease_token:
                result = connection.execute(
                    """UPDATE notifications
                       SET status = 'sent', sent_at = ?,
                           lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                       WHERE id = ? AND lease_token = ?""",
                    (_now_text(now), notification_id, str(lease_token)),
                )
            else:
                result = connection.execute(
                    """UPDATE notifications SET status = 'sent', sent_at = ? WHERE id = ?""",
                    (_now_text(now), notification_id),
                )
            return int(getattr(result, "rowcount", 0) or 0) == 1

    def mark_notification_failed(
        self,
        notification_id: str,
        now: datetime | None = None,
        lease_token: str | None = None,
    ) -> bool:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with self.database.connect() as connection:
            lease_clause = " AND lease_token = ?" if lease_token else ""
            params: tuple[Any, ...] = (
                _now_text(current),
                _now_text(current + NOTIFICATION_RETRY_DELAY),
                notification_id,
                *((str(lease_token),) if lease_token else ()),
            )
            result = connection.execute(
                f"""UPDATE notifications
                   SET status = 'failed', attempts = attempts + 1,
                       dead_lettered_at = CASE WHEN attempts + 1 >= 8 THEN ? ELSE dead_lettered_at END,
                       next_attempt_at = CASE WHEN attempts + 1 >= 8 THEN '9999-12-31T00:00:00+00:00' ELSE ? END,
                       lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                   WHERE id = ?{lease_clause}""",
                params,
            )
            return int(getattr(result, "rowcount", 0) or 0) == 1
