"""Shared infrastructure safety primitives."""

from __future__ import annotations

import os
from datetime import timezone

UTC = timezone.utc


class InfrastructureError(RuntimeError):
    """A provider or fleet safety decision failed."""


def _enabled(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in {"1", "true", "yes", "on"}
