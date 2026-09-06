"""Composition helpers for the entitlement compatibility facade."""

from __future__ import annotations

from typing import Any


def bind_claim_implementations(
    host: Any, implementations: dict[str, Any], static_names: set[str]
) -> None:
    """Bind the declared entitlement handlers without dynamic lookup."""
    class_members = vars(type(host))
    for name, implementation in implementations.items():
        if name in class_members:
            continue
        if name in static_names:
            setattr(host, name, implementation)
        else:
            setattr(host, name, implementation.__get__(host, type(host)))
