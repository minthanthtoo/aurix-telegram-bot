"""Application collaborator construction and startup reconciliation."""

from __future__ import annotations

import urllib.request
from typing import Any, Callable, Mapping

from runtime_bootstrap_steps import compose_runtime_application
from runtime_models import RuntimeApplication, RuntimeFactories
from runtime_outline import _build_outline, _group_staff
from runtime_settings import RuntimeSettings


def compose_application(
    *,
    settings: RuntimeSettings | None = None,
    env: Mapping[str, str] | None = None,
    factories: RuntimeFactories | None = None,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> RuntimeApplication:
    """Build and reconcile the application collaborators for one process."""
    return compose_runtime_application(
        settings=settings or RuntimeSettings.from_environment(env),
        factories=factories or RuntimeFactories(),
        urlopen=urlopen,
    )
