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
