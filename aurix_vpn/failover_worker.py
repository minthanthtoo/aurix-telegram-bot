"""Verified route-failover executor.

`RouteFailoverService` records observations and durable decisions. This module
performs the external work in the required order: provision, probe, transfer
the single accounting lease, attach the target generation, and only then
commit the decision. Ambiguous provider operations are left for reconciliation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from .identity import IdentityService
from .route_failover import RouteFailoverService


UTC = timezone.utc


class FailoverExecutionError(RuntimeError):
    """A target could not be proven usable without deleting uncertain state."""


class RouteFailoverExecutor:
    def __init__(
        self,
        database: Any,
        *,
        identity: IdentityService,
        failover: RouteFailoverService | None = None,
        route_provider: Callable[[str], Mapping[str, Any]] | None = None,
        adapter_provider: Callable[[Mapping[str, Any]], Any] | None = None,
        assignment_transfer: Callable[[str, str, str], Mapping[str, Any] | None] | None = None,
        access_url_encryptor: Callable[[str], str] | None = None,
        clock: Callable[[], datetime] | None = None,
        require_data_plane_probe: bool = True,
    ):
        self.database = database
        self.identity = identity
        self.failover = failover or RouteFailoverService(database)
        self.route_provider = route_provider or self._default_route
        self.adapter_provider = adapter_provider
        self.assignment_transfer = assignment_transfer
        self.access_url_encryptor = access_url_encryptor or (lambda value: value)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.require_data_plane_probe = bool(require_data_plane_probe)

    @staticmethod
    def _default_route(endpoint_id: str) -> Mapping[str, Any]:
        return {"endpoint_id": str(endpoint_id), "route_id": str(endpoint_id)}

    def _generation(self, generation_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM credential_generations WHERE generation_id = ?",
                (str(generation_id),),
            ).fetchone()
        if row is None:
            raise FailoverExecutionError("failover source generation does not exist")
        return dict(row)

    @staticmethod
    def _probe_ok(value: Any, *, kind: str) -> bool:
        if not isinstance(value, Mapping):
            raise FailoverExecutionError(f"target {kind} probe returned an invalid response")
        status = str(value.get("status") or "").lower()
        if status not in {"healthy", "ok", "ready"}:
            raise FailoverExecutionError(f"target {kind} probe was not healthy")
        return True

    def run_once(self, *, now: datetime | str | None = None) -> dict[str, Any] | None:
        timestamp = now or self.clock()
        decision = self.failover.claim(now=timestamp)
        if decision is None:
            return None
        decision_id = str(decision["decision_id"])
        source_generation_id = str(decision["source_generation_id"])
        target_generation_id: str | None = None
        target_grant: dict[str, Any] | None = None
        lease_transferred = False
        adapter: Any | None = None
        assignment_transferred = False
        try:
            source = self._generation(source_generation_id)
            entitlement_key = str(source["entitlement_key"])
            authorization = self.identity.recovery_authorization(
                entitlement_key, source_generation_id, now=timestamp
            )
            if not authorization.get("authorized"):
                raise FailoverExecutionError(
                    f"source generation is not recoverable: {authorization.get('reason', 'unknown')}"
                )
            target_endpoint = str(decision["target_endpoint_id"])
            target_route = dict(self.route_provider(target_endpoint))
            target_route.setdefault("endpoint_id", target_endpoint)
            source_protocol = str(source.get("protocol") or "outline").strip().lower()
            target_route.setdefault("protocol", source_protocol)
            target_protocol = str(target_route.get("protocol") or "").strip().lower()
            if target_protocol != source_protocol:
                raise FailoverExecutionError(
                    "failover target protocol does not match the source generation"
                )
            target_route["protocol"] = target_protocol
            target_route.setdefault("route_id", f"{target_protocol}:{target_endpoint}")
            if not str(target_route.get("endpoint_id") or ""):
                raise FailoverExecutionError("target route has no endpoint identity")
            if self.adapter_provider is None:
                raise FailoverExecutionError("failover adapter provider is not configured")
            adapter = self.adapter_provider(target_route)
            intent = {
                # Stable per-decision identity makes retries read back the same
                # provider user instead of creating duplicates.
                "external_id": f"aurix-failover-{decision_id}",
                "name": f"AuriX failover {entitlement_key}",
                "quota_bytes": int(authorization.get("remaining_bytes") or 0),
            }
            if intent["quota_bytes"] <= 0:
                raise FailoverExecutionError("source entitlement has no remaining quota")
            target_grant = dict(adapter.provision(target_route, intent))
            if str(target_grant.get("external_id") or "") == str(source.get("external_id") or ""):
                raise FailoverExecutionError("failover provider reused the source credential")
            target_generation_id = self.identity.ensure_generation_for_credential(
                entitlement_key,
                target_endpoint,
                external_id=str(target_grant["external_id"]),
                protocol=target_protocol,
                access_url_ciphertext=self.access_url_encryptor(str(target_grant["access_url"])),
                status="active",
                remote_state="observed",
                intent_key=f"failover:{decision_id}",
                usage_baseline_provenance="new",
                now=timestamp,
            )
            # Persist the target before probing so an owned credential can be
            # revoked and marked remote-revoked if validation fails.
            self._probe_ok(adapter.probe_management(target_route), kind="management")
            data_probe = adapter.probe_data_plane(target_route)
            if self.require_data_plane_probe:
                self._probe_ok(data_probe, kind="data-plane")
            elif isinstance(data_probe, Mapping) and str(data_probe.get("status")) == "failed":
                raise FailoverExecutionError("target data-plane probe failed")
            if self.assignment_transfer is not None:
                assignment = self.assignment_transfer(
                    entitlement_key, target_endpoint, "failover"
                )
                assignment_transferred = bool(
                    isinstance(assignment, Mapping) and assignment.get("changed") is True
                )
            lease_id = self.identity.transfer_generation_lease(
                entitlement_key,
                source_generation_id,
                target_generation_id,
                target_endpoint,
                now=timestamp,
            )
            if lease_id is None:
                raise FailoverExecutionError("source generation has no active accounting lease")
            lease_transferred = True
            self.failover.attach_target_generation(decision_id, target_generation_id, now=timestamp)
            self.failover.mark_committed(decision_id, now=timestamp)
            return {
                "status": "committed",
                "decision_id": decision_id,
                "source_generation_id": source_generation_id,
                "target_generation_id": target_generation_id,
                "target_endpoint_id": target_endpoint,
                "data_plane": data_probe,
            }
        except Exception as exc:
            if lease_transferred and target_generation_id is not None:
                try:
                    source = self._generation(source_generation_id)
                    self.identity.transfer_generation_lease(
                        str(source["entitlement_key"]),
                        target_generation_id,
                        source_generation_id,
                        str(source["endpoint_id"]),
                        now=timestamp,
                    )
                except Exception:
                    # Do not hide the original failure; the durable lease is
                    # still inspectable and the next reconciliation can repair it.
                    pass
            if assignment_transferred and self.assignment_transfer is not None:
                try:
                    self.assignment_transfer(entitlement_key, str(source["endpoint_id"]), "failover-rollback")
                except Exception:
                    # The durable endpoint assignment remains inspectable for
                    # reconciliation if a rollback races another operator.
                    pass
            if target_generation_id is not None and target_grant is not None and adapter is not None:
                if str(target_grant.get("ownership")) == "owned":
                    try:
                        adapter.revoke_auth(target_grant)
                        verified = adapter.verify_auth_revoked(target_grant)
                        if bool(verified.get("verified")):
                            self.identity.mark_remote_revoked(
                                target_generation_id, verified=True, now=timestamp
                            )
                            self.failover.mark_rolled_back(decision_id, exc, now=timestamp)
                            return {
                                "status": "rolled_back",
                                "decision_id": decision_id,
                                "error": type(exc).__name__,
                            }
                    except Exception:
                        pass
            self.failover.mark_failed(decision_id, exc, now=timestamp)
            return {"status": "failed", "decision_id": decision_id, "error": type(exc).__name__}
