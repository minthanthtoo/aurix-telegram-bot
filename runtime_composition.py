"""Compatibility facade for decomposed AuriX runtime composition."""

from runtime_bootstrap import compose_application
from runtime_models import RuntimeApplication, RuntimeFactories
from runtime_outline import _build_outline, _group_staff
from runtime_settings import RuntimeSettings, _parse_ids

__all__ = [
    "RuntimeApplication",
    "RuntimeFactories",
    "RuntimeSettings",
    "compose_application",
]
