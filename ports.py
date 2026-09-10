"""Typed boundaries between application policy and external systems."""

from __future__ import annotations

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

    Adapters are the only layer allowed to translate provider APIs into the
    lifecycle contract.  Capabilities are descriptive: an unsupported
    operation must be reported as unsupported, never inferred from local
    state or from credential deletion.
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

    def rotate(self, grant: dict[str, Any]) -> dict[str, Any]: ...

    def revoke_auth(self, grant: dict[str, Any]) -> None: ...

    def verify_auth_revoked(self, grant: dict[str, Any]) -> dict[str, Any]: ...

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
