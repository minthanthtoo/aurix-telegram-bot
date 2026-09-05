"""Typed boundaries between application policy and external systems."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class OutlineGateway(Protocol):
    def server_info(self) -> dict[str, Any]: ...

    def transfer_metrics(self) -> dict[str, Any]: ...

    def create_key(self, name: str, limit_bytes: int | None) -> dict[str, Any]: ...

    def list_keys(self) -> dict[str, Any]: ...

    def get_key(self, key_id: str) -> dict[str, Any] | None: ...

    def set_data_limit(self, key_id: str, limit_bytes: int) -> None: ...

    def delete_key(self, key_id: str) -> None: ...

    def create_key_with_id(
        self, key_id: str, name: str, limit_bytes: int | None
    ) -> dict[str, Any]: ...

    def delete_data_limit(self, key_id: str) -> None: ...

    def rename_key(self, key_id: str, name: str) -> None: ...


@runtime_checkable
class ConnectivityAdapter(Protocol):
    """Protocol-neutral boundary for a customer-facing service route.

    Implementations must expose capability truthfully.  In particular,
    ``terminate_sessions`` means a documented, immediate session operation;
    deleting credentials alone must not be reported as force-disconnect.
    Methods exchange bounded dictionaries so adapters can evolve without
    leaking provider-specific objects into commerce policy.
    """

    protocol: str

    @property
    def capabilities(self) -> dict[str, bool]: ...

    def provision(
        self, route: dict[str, Any], credential_intent: dict[str, Any]
    ) -> dict[str, Any]: ...

    def render_managed_config(self, grant: dict[str, Any]) -> dict[str, Any]: ...

    def render_manual_export(self, grant: dict[str, Any]) -> str: ...

    def apply_quota_cap(self, grant: dict[str, Any], absolute_limit: int) -> None: ...

    def read_usage(self, grant: dict[str, Any]) -> dict[str, Any]: ...

    def rotate(
        self, grant: dict[str, Any], credential_intent: dict[str, Any]
    ) -> dict[str, Any]: ...

    def revoke_auth(self, grant: dict[str, Any]) -> None: ...

    def terminate_sessions(self, grant: dict[str, Any]) -> dict[str, Any]: ...

    def probe_management(self, route: dict[str, Any]) -> dict[str, Any]: ...

    def probe_data_plane(self, route: dict[str, Any]) -> dict[str, Any]: ...

    def reconcile(self, route: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class ReceiptStorageGateway(Protocol):
    configured: bool
    bucket: str | None

    def upload(self, path: str, data: bytes, mime_type: str) -> str: ...

    def signed_url(self, path: str, expires_in: int = 300) -> str | None: ...

    def download(self, path: str) -> bytes | None: ...

    def delete(self, path: str) -> None: ...


@runtime_checkable
class ReceiptExtractorGateway(Protocol):
    def extract(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> Any: ...


@runtime_checkable
class NotificationTransport(Protocol):
    def send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any: ...


@runtime_checkable
class CommerceWorkerPort(Protocol):
    """Explicit background-work surface exposed by the commerce service."""

    def process_jobs(self, now: datetime | None = None, max_jobs: int = 10) -> int: ...

    def expire_and_process(self, now: datetime | None = None) -> int: ...

    def enforce_quotas(
        self,
        now: datetime | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> int: ...

    def queue_quota_warnings(
        self,
        now: datetime | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> int: ...

    def process_managed_key_repairs(
        self, now: datetime | None = None, max_jobs: int = 5
    ) -> int: ...

    def process_endpoint_migrations(
        self, now: datetime | None = None, max_jobs: int = 5
    ) -> int: ...

    def failed_jobs(
        self, limit: int = 20, include_nonterminal: bool = False
    ) -> list[dict[str, Any]]: ...

    def retry_job(self, job_id: str, admin_id: int, now: datetime | None = None) -> str: ...

    def retry_failed_job(
        self,
        order_id: str,
        admin_id: int,
        now: datetime | None = None,
        operation: str | None = None,
    ) -> str: ...

    def capacity_snapshot(
        self, now: datetime | None = None, *, refresh_inventory: bool = True
    ) -> dict[str, Any]: ...

    def pending_notifications(
        self, now: datetime | None = None, limit: int = 20
    ) -> list[dict[str, Any]]: ...

    def claim_pending_notifications(
        self,
        now: datetime | None = None,
        limit: int = 20,
        lease_seconds: int = 120,
    ) -> list[dict[str, Any]]: ...

    def mark_notification_sent(
        self, notification_id: str, now: datetime | None = None
    ) -> None: ...

    def mark_notification_failed(
        self, notification_id: str, now: datetime | None = None
    ) -> None: ...
