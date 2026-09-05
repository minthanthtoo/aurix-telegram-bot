#!/usr/bin/env python3
"""Read-only, sanitized health and access-port diagnostics for Outline nodes.

The command deliberately performs no Outline mutation.  It probes each
configured management endpoint with the same certificate-pinned client used by
the bot, then tests the public TCP ports advertised by the returned access
keys.  Management URLs, certificate fingerprints, access URLs and transfer
values are never printed.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy.fleet_reconcile import FleetError, load_dotenv  # noqa: E402
from outline_adapter import OutlineClient  # noqa: E402


def _timeout() -> float:
    try:
        value = float(os.environ.get("OUTLINE_REQUEST_TIMEOUT_SECONDS", "5"))
    except (TypeError, ValueError) as exc:
        raise FleetError("OUTLINE_REQUEST_TIMEOUT_SECONDS must be numeric") from exc
    if not 1.0 <= value <= 30.0:
        raise FleetError("OUTLINE_REQUEST_TIMEOUT_SECONDS must be between 1 and 30")
    return value


def _data_timeout() -> float:
    try:
        value = float(os.environ.get("AURIX_DATA_PORT_PROBE_TIMEOUT_SECONDS", "3"))
    except (TypeError, ValueError) as exc:
        raise FleetError("AURIX_DATA_PORT_PROBE_TIMEOUT_SECONDS must be numeric") from exc
    if not 0.5 <= value <= 10.0:
        raise FleetError("AURIX_DATA_PORT_PROBE_TIMEOUT_SECONDS must be between 0.5 and 10")
    return value


def _server_configs() -> list[dict[str, Any]]:
    raw = os.environ.get("OUTLINE_SERVERS_JSON", "").strip()
    if raw:
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FleetError("OUTLINE_SERVERS_JSON is not valid JSON") from exc
        if not isinstance(values, list) or not values:
            raise FleetError("OUTLINE_SERVERS_JSON must be a non-empty array")
        result = []
        for item in values:
            if not isinstance(item, dict):
                raise FleetError("OUTLINE_SERVERS_JSON contains a non-object entry")
            if not str(item.get("id") or "").strip():
                raise FleetError("every Outline server requires an id")
            if not str(item.get("api_url") or "").strip() or not str(
                item.get("cert_sha256") or ""
            ).strip():
                raise FleetError(f"Outline server {item.get('id')!r} lacks pinned identity")
            result.append(dict(item))
        fleet_raw = os.environ.get("AURIX_FLEET_NODES_JSON", "").strip()
        if fleet_raw:
            try:
                fleet_nodes = json.loads(fleet_raw)
            except json.JSONDecodeError:
                fleet_nodes = []
            if isinstance(fleet_nodes, list):
                by_id = {
                    str(node.get("id")): node
                    for node in fleet_nodes
                    if isinstance(node, dict) and node.get("id")
                }
                for item in result:
                    fleet_node = by_id.get(str(item.get("id")))
                    if isinstance(fleet_node, dict) and fleet_node.get("keys_port"):
                        item.setdefault("keys_port", fleet_node["keys_port"])
        return result
    api_url = os.environ.get("OUTLINE_API_URL", "").strip()
    fingerprint = os.environ.get("OUTLINE_CERT_SHA256", "").strip()
    if not api_url or not fingerprint:
        raise FleetError("configure OUTLINE_SERVERS_JSON or OUTLINE_API_URL/OUTLINE_CERT_SHA256")
    return [{"id": "primary", "label": os.environ.get("OUTLINE_SERVER_LABEL", "Primary"),
             "api_url": api_url, "cert_sha256": fingerprint,
             "keys_port": os.environ.get("OUTLINE_KEYS_PORT", "")}]


def _tcp_probe(
    host: str,
    port: int,
    timeout: float,
    *,
    connector: Callable[..., Any] = socket.create_connection,
) -> dict[str, Any]:
    started = time.perf_counter()
    status = "open"
    try:
        connection = connector((host, port), timeout=timeout)
        close = getattr(connection, "close", None)
        if callable(close):
            close()
    except ConnectionRefusedError:
        status = "refused"
    except TimeoutError:
        status = "timeout"
    except OSError:
        status = "error"
    return {
        "host": host,
        "port": port,
        "status": status,
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def _access_targets(keys: list[Any], fallback_host: str, fallback_port: Any) -> list[tuple[str, int]]:
    targets: set[tuple[str, int]] = set()
    for item in keys:
        if not isinstance(item, dict):
            continue
        parsed = urlsplit(str(item.get("accessUrl") or ""))
        if not parsed.hostname or not parsed.port:
            continue
        targets.add((str(parsed.hostname), int(parsed.port)))
        if len(targets) >= 8:
            break
    if not targets and str(fallback_port or "").strip():
        try:
            port = int(fallback_port)
        except (TypeError, ValueError):
            port = 0
        if 1 <= port <= 65535 and fallback_host:
            targets.add((fallback_host, port))
    return sorted(targets)


def probe_server(
    config: dict[str, Any],
    *,
    request_timeout: float,
    data_timeout: float,
    client_factory: Callable[..., Any] = OutlineClient,
    connector: Callable[..., Any] = socket.create_connection,
) -> dict[str, Any]:
    server_id = str(config.get("id") or "unknown")
    label = str(config.get("label") or server_id)[:64]
    api_url = str(config.get("api_url") or "")
    try:
        parsed = urlsplit(api_url)
        api_port = parsed.port or 443
    except ValueError:
        parsed = None
        api_port = None
    result: dict[str, Any] = {
        "server_id": server_id,
        "label": label,
        "host": parsed.hostname if parsed is not None and parsed.hostname else "unknown",
        "management_port": api_port,
        "status": "unreachable",
        "management_tcp": None,
        "latency_ms": None,
        "outline_version": None,
        "key_count": 0,
        "metrics_key_count": 0,
        "data_ports": [],
        "error": None,
    }
    if parsed is None or parsed.scheme != "https" or not parsed.hostname:
        result["error"] = "invalid_management_url"
        return result
    result["management_tcp"] = _tcp_probe(
        parsed.hostname, api_port, request_timeout, connector=connector
    )
    if result["management_tcp"]["status"] != "open":
        result["latency_ms"] = result["management_tcp"]["latency_ms"]
        result["error"] = "management_tcp_" + str(result["management_tcp"]["status"])
        return result
    started = time.perf_counter()
    try:
        client = client_factory(
            api_url,
            str(config.get("cert_sha256") or ""),
            timeout_seconds=request_timeout,
            circuit_breaker_seconds=0,
        )
        info = client.server_info()
        inventory = client.list_keys()
        keys = inventory.get("accessKeys", []) if isinstance(inventory, dict) else []
        if not isinstance(keys, list):
            raise ValueError("invalid access-key response")
        metrics = client.transfer_metrics()
        by_key = metrics.get("bytesTransferredByUserId", {}) if isinstance(metrics, dict) else {}
        if not isinstance(by_key, dict):
            raise ValueError("invalid transfer-metrics response")
        result.update(
            {
                "status": "healthy",
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "outline_version": str(info.get("version") or "unknown")[:64]
                if isinstance(info, dict)
                else "unknown",
                "key_count": len(keys),
                "metrics_key_count": len(by_key),
            }
        )
        for host, port in _access_targets(keys, parsed.hostname, config.get("keys_port")):
            port_result = _tcp_probe(host, port, data_timeout, connector=connector)
            # Access-key hosts are public endpoint metadata; do not include
            # usernames, fragments or encoded access credentials.
            result["data_ports"].append(port_result)
        if any(item["status"] != "open" for item in result["data_ports"]):
            result["status"] = "degraded"
    except Exception as exc:
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
        result["error"] = type(exc).__name__
    return result


def run(env_file: Path) -> dict[str, Any]:
    load_dotenv(env_file, overwrite=True)
    request_timeout = _timeout()
    data_timeout = _data_timeout()
    configs = _server_configs()
    with ThreadPoolExecutor(max_workers=min(8, len(configs))) as executor:
        results = list(
            executor.map(
                lambda item: probe_server(
                    item, request_timeout=request_timeout, data_timeout=data_timeout
                ),
                configs,
            )
        )
    healthy = sum(1 for item in results if item["status"] == "healthy")
    return {
        "status": "healthy" if healthy == len(results) else "degraded" if healthy else "unreachable",
        "healthy_servers": healthy,
        "server_count": len(results),
        "servers": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--allow-partial", action="store_true", help="exit 0 when at least one server is healthy")
    args = parser.parse_args(argv)
    try:
        report = run(Path(args.env_file))
    except (FleetError, OSError, ValueError) as exc:
        print(json.dumps({"status": "invalid", "error": type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["status"] == "healthy":
        return 0
    if args.allow_partial and report["healthy_servers"] > 0:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
