"""Composable control-plane WSGI surface for probe and device APIs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


def create_control_wsgi_app(
    probe_app: Callable[..., Any], device_app: Callable[..., Any]
) -> Callable[..., Any]:
    """Route public control API paths to their bounded subsystem."""

    def app(environ: Mapping[str, Any], start_response: Callable[..., Any]):
        path = str(environ.get("PATH_INFO") or "")
        if path.startswith("/v1/devices/") or path == "/v1/devices/pair":
            return device_app(environ, start_response)
        return probe_app(environ, start_response)

    return app
