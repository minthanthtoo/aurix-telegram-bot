"""Explicit capability contract shared by commerce worker composition."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any


PUBLIC_WORKER_METHODS = frozenset(
    {
        "process_jobs",
        "expire_and_process",
        "enforce_quotas",
        "queue_quota_warnings",
        "process_managed_key_repairs",
        "process_endpoint_migrations",
        "failed_jobs",
        "retry_job",
        "retry_failed_job",
        "capacity_snapshot",
        "pending_notifications",
        "claim_pending_notifications",
        "mark_notification_sent",
        "mark_notification_failed",
    }
)


@dataclass(frozen=True, slots=True)
class CommerceWorkerDependencies:
    """Only the application capabilities used by legacy worker handlers."""

    _audit: Any
    _decrypt_access_url: Any
    _encrypt_access_url: Any
    _managed_repair_allow_unknown_usage: Any
    _managed_repair_cached_usage_is_recent: Any
    _outline_client: Any
    _table_exists: Any
    capacity_operations: Any
    database: Any
    expire_open_orders: Any
    lifecycle: Any
    outline: Any
    refresh_server_inventory: Any
    release_expired_wallet_reservations: Any

    @classmethod
    def from_service(cls, service: Any) -> "CommerceWorkerDependencies":
        values: dict[str, Any] = {}
        for field in fields(cls):
            try:
                values[field.name] = getattr(service, field.name)
            except AttributeError as exc:
                raise TypeError(f"worker dependency is not provided: {field.name}") from exc
        return cls(**values)

    def items(self) -> tuple[tuple[str, Any], ...]:
        return tuple((field.name, getattr(self, field.name)) for field in fields(self))


def bind_worker_implementations(
    host: Any, implementations: dict[str, Any], static_names: set[str]
) -> None:
    """Bind the declared handler table without a dynamic attribute resolver."""
    for name, implementation in implementations.items():
        if name in PUBLIC_WORKER_METHODS:
            continue
        if name in static_names:
            setattr(host, name, implementation)
        else:
            setattr(host, name, implementation.__get__(host, type(host)))
