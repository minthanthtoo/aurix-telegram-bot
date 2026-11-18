#!/usr/bin/env python3
"""Read-only smoke check for a deployed AuriX Games custom domain.

The checker performs only DNS resolution and HTTPS GET requests. It never
changes DNS, Caddy, Render, supplier state, catalog state, or customer data.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_MAX_BODY = 128 * 1024


class DomainCheckError(RuntimeError):
    """The deployed domain did not satisfy the Games service contract."""


def _hostname(value: str) -> str:
    if not isinstance(value, str):
        raise DomainCheckError("hostname must be a string")
    host = value.strip().rstrip(".").lower()
    if not host or len(host) > 253 or not _HOST_RE.fullmatch(host) or ".." in host:
        raise DomainCheckError("hostname is invalid")
    if "*" in host or any(len(label) > 63 for label in host.split(".")):
        raise DomainCheckError("hostname is invalid")
    return host


def _read_get(opener: Callable[..., Any], url: str, *, timeout: float) -> tuple[int, bytes]:
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with opener(request, timeout=timeout) as response:
            status = int(response.status)
            body = response.read(_MAX_BODY + 1)
    except HTTPError as exc:
        raise DomainCheckError(f"GET {url} returned HTTP {exc.code}") from None
    except (OSError, URLError, TimeoutError, ValueError) as exc:
        raise DomainCheckError(f"GET {url} failed") from exc
    if len(body) > _MAX_BODY:
        raise DomainCheckError(f"GET {url} exceeded the response limit")
    if status < 200 or status >= 300:
        raise DomainCheckError(f"GET {url} returned HTTP {status}")
    return status, body


def verify_domain(
    hostname: str,
    *,
    timeout: float = 10.0,
    opener: Callable[..., Any] | None = None,
    resolver: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Verify DNS plus the health/static contracts without mutating state."""

    host = _hostname(hostname)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise DomainCheckError("timeout must be positive")
    resolve = resolver or socket.getaddrinfo
    try:
        addresses = resolve(host, 443, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror) as exc:
        raise DomainCheckError("hostname does not resolve") from exc
    if not addresses:
        raise DomainCheckError("hostname does not resolve")

    fetch = opener or urlopen
    base = f"https://{host}"
    health_status, health_body = _read_get(fetch, f"{base}/api/healthz", timeout=float(timeout))
    try:
        health = json.loads(health_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DomainCheckError("health response was not JSON") from None
    if (
        not isinstance(health, dict)
        or health.get("status") != "ok"
        or health.get("service") != "aurix-games"
        or health.get("checkout_enabled") is not False
    ):
        raise DomainCheckError("health response did not match the Games contract")
    storefront_status, _ = _read_get(fetch, f"{base}/game-store/", timeout=float(timeout))
    return {
        "hostname": host,
        "resolved": True,
        "health_status": health_status,
        "storefront_status": storefront_status,
        "checkout_enabled": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hostname", help="deployed Games hostname, e.g. store.aurix-mart.online")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    try:
        result = verify_domain(args.hostname, timeout=args.timeout)
    except DomainCheckError as exc:
        print(f"domain check failed: {exc}")
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
