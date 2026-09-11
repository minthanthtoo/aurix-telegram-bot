#!/usr/bin/env python3
"""Read-only public deployment parity probe for the AuriX AI gateway.

The probe deliberately uses unauthenticated requests only. It verifies the
public health/mode contract, protected route presence, required browser
assets, and byte-for-byte parity between the deployed assets and the local
source snapshot. It never submits a model request or changes remote state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


REQUIRED_MODES = {"english", "translate", "lisu_assistant"}
REQUIRED_ASSETS = ("shared.js", "app.js", "admin.html")
PROTECTED_ROUTES = ("/api/conversations", "/api/admin/usage")
MAX_RESPONSE_BYTES = 256 * 1024


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fetch(base_url: str, path: str, *, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        headers={"Accept": "application/json, text/html, text/javascript"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {
                "status": int(response.status),
                "content_type": response.headers.get_content_type(),
                "body": response.read(MAX_RESPONSE_BYTES),
            }
    except urllib.error.HTTPError as exc:
        return {
            "status": int(exc.code),
            "content_type": exc.headers.get_content_type() if exc.headers else None,
            "body": exc.read(MAX_RESPONSE_BYTES),
        }
    except (OSError, ValueError) as exc:
        return {"status": None, "content_type": None, "body": b"", "error": type(exc).__name__}


def _json_body(response: dict[str, Any]) -> dict[str, Any] | None:
    try:
        value = json.loads(response.get("body", b"{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def run_probe(
    base_url: str,
    *,
    local_root: Path,
    timeout: float = 10.0,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    health = _fetch(base_url, "/api/healthz", timeout=timeout)
    health_payload = _json_body(health)
    checks.append(
        {
            "name": "health",
            "ok": health["status"] == 200
            and health_payload is not None
            and health_payload.get("ok") is True
            and health_payload.get("service") == "aurix-ai",
            "status": health["status"],
        }
    )

    modes = _fetch(base_url, "/api/modes", timeout=timeout)
    modes_payload = _json_body(modes)
    mode_ids = {
        item.get("id")
        for item in (modes_payload or {}).get("modes", [])
        if isinstance(item, dict)
    }
    checks.append(
        {
            "name": "mode_catalog",
            "ok": modes["status"] == 200 and REQUIRED_MODES <= mode_ids,
            "status": modes["status"],
            "missing": sorted(REQUIRED_MODES - mode_ids),
        }
    )

    index = _fetch(base_url, "/", timeout=timeout)
    index_text = index.get("body", b"").decode("utf-8", "replace")
    required_markers = ('src="/shared.js"', 'src="/app.js"', 'id="chat-form"')
    checks.append(
        {
            "name": "browser_shell",
            "ok": index["status"] == 200 and all(marker in index_text for marker in required_markers),
            "status": index["status"],
        }
    )

    asset_checks = []
    for asset in REQUIRED_ASSETS:
        local_path = local_root / asset
        deployed = _fetch(base_url, "/" + asset, timeout=timeout)
        local_bytes = local_path.read_bytes() if local_path.is_file() else b""
        local_hash = _sha256(local_bytes)
        deployed_hash = _sha256(deployed.get("body", b""))
        asset_checks.append(
            {
                "asset": asset,
                "ok": deployed["status"] == 200 and local_path.is_file() and local_hash == deployed_hash,
                "status": deployed["status"],
                "local_sha256": local_hash,
                "deployed_sha256": deployed_hash,
            }
        )
    checks.append(
        {
            "name": "asset_parity",
            "ok": all(item["ok"] for item in asset_checks),
            "assets": asset_checks,
        }
    )

    protected_checks = []
    for path in PROTECTED_ROUTES:
        response = _fetch(base_url, path, timeout=timeout)
        protected_checks.append(
            {
                "path": path,
                "ok": response["status"] in {401, 403},
                "status": response["status"],
            }
        )
    checks.append(
        {
            "name": "protected_route_presence",
            "ok": all(item["ok"] for item in protected_checks),
            "routes": protected_checks,
        }
    )

    return {
        "base_url": base_url.rstrip("/"),
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="public or staging AuriX AI origin")
    parser.add_argument(
        "--local-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "web" / "ai-app",
        help="local web/ai-app asset directory",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    result = run_probe(args.base_url, local_root=args.local_root, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
