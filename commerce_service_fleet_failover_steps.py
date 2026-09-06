"""Validation, provisioning, and commit steps for route failover."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from commerce_models import UTC, _now_text
from route_failover import FailoverError


@dataclass(frozen=True, slots=True)
class FailoverPreparation:
    record: dict[str, Any]
    source_usage: dict[str, Any]
    target_route: dict[str, str]
    target_adapter: Any
    target_server: str
    lease_bytes: int


def prepare_route_failover(
    self: Any, decision: dict[str, Any], current: datetime
) -> FailoverPreparation:
    """Reconcile source usage and verify target readiness before provisioning."""
    record = self._failover_source_record(decision)
    if (
        record is None
        or str(record["entitlement_status"]) != "active"
        or str(record["generation_status"]) != "active"
        or str(record["credential_status"]) != "active"
    ):
        raise FailoverError("failover entitlement or source credential is no longer active")
    source_secret = self._decrypt_access_url(record["secret_ciphertext"])
    if not source_secret:
        raise FailoverError("source credential secret is unavailable for usage reconciliation")
    source_adapter = self._connectivity_adapter(
        service_route_id=str(record["source_route_id"])
    )
    required = {"managed_config", "quota_cap", "usage", "rotation", "management_probe"}
    if not required.issubset(
        name for name, enabled in source_adapter.capabilities.items() if enabled
    ):
        raise FailoverError("source adapter lacks the required failover capabilities")
    source_usage = source_adapter.read_usage(
        {
            "route_id": str(record["source_route_id"]),
            "external_id": str(record["external_id"]),
            "access_url": source_secret,
        }
    )
    self.identity.record_remote_usage(
        str(record["source_endpoint_id"]),
        str(record["external_id"]),
        int(source_usage["bytes_transferred"]),
        observed_at=str(source_usage["observed_at"]),
    )
    snapshot = self.identity.quota_snapshot(str(record["entitlement_id"]))
    if snapshot is None or int(snapshot["remaining_bytes"]) <= 0:
        raise FailoverError("entitlement has no remaining quota")
    target_route = {
        "route_id": str(record["target_route_id"]),
        "endpoint_id": str(record["target_endpoint_id"]),
        "protocol": str(record["target_protocol"]),
    }
    target_adapter = self._connectivity_adapter(
        service_route_id=str(record["target_route_id"])
    )
    if not all(
        bool(target_adapter.capabilities.get(name))
        for name in ("managed_config", "quota_cap", "usage", "rotation", "management_probe")
    ):
        raise FailoverError("target adapter does not support safe failover")
    management = target_adapter.probe_management(target_route)
    if str(management.get("status")) != "healthy":
        raise FailoverError("target management probe did not pass")
    target_server = str(record["target_server_id"] or "")
    if not self._failover_target_has_fresh_probe(target_server, current):
        raise FailoverError("target data-plane probe evidence is stale or unavailable")
    with self.database.connect() as connection:
        policy_row = self.failover_reads.standby_lease_bytes(
            connection, str(record["entitlement_id"])
        )
    if policy_row is None:
        raise FailoverError("failover policy disappeared before execution")
    lease_bytes = min(
        int(policy_row["standby_lease_bytes"]), int(snapshot["remaining_bytes"])
    )
    return FailoverPreparation(
        record=dict(record),
        source_usage=dict(source_usage),
        target_route=target_route,
        target_adapter=target_adapter,
        target_server=target_server,
        lease_bytes=lease_bytes,
    )


def provision_failover_target(
    preparation: FailoverPreparation, decision: dict[str, Any]
) -> dict[str, Any]:
    """Create the deterministic target credential after readiness checks pass."""
    target_external_id = f"aurix-failover-{str(decision['decision_id'])[-24:]}"
    target_grant = preparation.target_adapter.provision(
        preparation.target_route,
        {
            "external_id": target_external_id,
            "name": f"AuriX failover {str(preparation.record['entitlement_id'])[-12:]}",
            "quota_bytes": preparation.lease_bytes,
        },
    )
    if not target_grant.get("access_url"):
        raise FailoverError("target adapter returned no customer credential")
    return dict(target_grant)


def commit_route_failover(
    self: Any,
    decision: dict[str, Any],
    preparation: FailoverPreparation,
    target_grant: dict[str, Any],
    current: datetime,
) -> None:
    """Bind the target and converge identity leases before closing the decision."""
    now_text = _now_text(current)
    encrypted = self._encrypt_access_url(str(target_grant["access_url"]))
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        current_decision = self.failover_reads.decision_state(
            connection, str(decision["decision_id"])
        )
        if current_decision is None or str(current_decision["state"]) != "creating":
            raise FailoverError("failover decision is no longer owned by this worker")
        self.failover_reads.bind_credential(
            connection,
            telegram_id=int(preparation.record["telegram_id"]),
            server_id=preparation.target_server,
            external_id=str(target_grant["external_id"]),
            secret_ciphertext=encrypted,
            now_text=now_text,
            profile_kind=str(preparation.record["kind"]),
            subscription_id=(
                str(preparation.record["subscription_id"])
                if preparation.record["subscription_id"]
                else None
            ),
            route_id=str(preparation.record["target_route_id"]),
        )
        self._audit(
            connection,
            "route_failover_verified",
            "failover_decision",
            str(decision["decision_id"]),
            "system",
            None,
            {
                "source_generation_id": str(preparation.record["source_generation_id"]),
                "target_route_id": str(preparation.record["target_route_id"]),
                "target_external_id": str(target_grant["external_id"]),
                "source_usage_bytes": int(preparation.source_usage["bytes_transferred"]),
                "standby_lease_bytes": preparation.lease_bytes,
            },
        )
    self.failover.mark_verified(str(decision["decision_id"]), now=now_text)
    with self.database.connect() as connection:
        target_credential_id = self.failover_reads.target_credential_id(
            connection,
            str(preparation.record["target_endpoint_id"]),
            str(target_grant["external_id"]),
        )
    if not target_credential_id:
        raise FailoverError("target credential binding did not converge")
    new_generation = self.identity.ensure_generation_for_credential(
        str(preparation.record["entitlement_id"]),
        str(preparation.record["target_endpoint_id"]),
        credential_id=target_credential_id,
        now=now_text,
    )
    for lease in self.identity.lease_snapshot(int(preparation.record["telegram_id"])):
        if (
            str(lease.get("entitlement_id")) == str(preparation.record["entitlement_id"])
            and str(lease.get("status")) == "active"
            and str(lease.get("endpoint_id")) == str(preparation.record["source_endpoint_id"])
        ):
            self.identity.release_lease(str(lease["lease_id"]), now=now_text)
    self.identity.grant_lease(
        str(preparation.record["entitlement_id"]),
        str(preparation.record["target_endpoint_id"]),
        lease_bytes=preparation.lease_bytes,
        generation_id=new_generation,
        ttl_seconds=900,
        now=now_text,
    )
    self.identity.revoke_generation(str(preparation.record["source_generation_id"]), now=now_text)
    self.failover.mark_committed(str(decision["decision_id"]), now=now_text)
