"""Structural repository contracts used while the modular monolith is extracted."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class RepositoryDatabase(Protocol):
    """Minimum transaction boundary required by domain services."""

    def connect(self) -> AbstractContextManager[Any]: ...

    def begin_write(self, connection: Any) -> None: ...

    def is_integrity_error(self, error: Exception) -> bool: ...

    def initialize(self) -> None: ...


@runtime_checkable
class HostedRepositoryDatabase(RepositoryDatabase, Protocol):
    """Repository whose external connection resources need process shutdown."""

    def close(self) -> None: ...
