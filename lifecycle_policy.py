"""Shared endpoint lifecycle policy with no persistence or provider dependencies."""

from __future__ import annotations

from typing import Final


LIFECYCLE_STATES: Final = frozenset({"active", "draining", "retired"})


def normalize_lifecycle_state(value: str | None, *, strict: bool = False) -> str:
    """Normalize a lifecycle value and optionally reject unknown states."""
    normalized = str(value if value is not None else ("" if strict else "active")).strip().lower()
    if normalized in LIFECYCLE_STATES:
        return normalized
    if strict:
        raise ValueError("Endpoint lifecycle must be active, draining or retired")
    return "active"


def endpoint_status(lifecycle_state: str | None, health_status: str | None) -> str:
    """Map independent lifecycle and health observations to registry status."""
    lifecycle = normalize_lifecycle_state(lifecycle_state)
    health = str(health_status or "unknown").strip().lower()
    if lifecycle == "retired":
        return "retired"
    if lifecycle == "draining":
        return "draining"
    if health == "unreachable":
        return "failed"
    if health == "degraded":
        return "degraded"
    return "active" if health == "healthy" else "provisioning"


def accepts_new_keys(lifecycle_state: str | None, health_status: str | None) -> bool:
    """Return whether the endpoint may receive a new credential assignment."""
    return (
        normalize_lifecycle_state(lifecycle_state) == "active"
        and str(health_status or "unknown").strip().lower() == "healthy"
    )
