"""Small node-side runner for centrally scheduled fleet probe jobs.

The runner intentionally has a narrow contract: it accepts already scoped
instructions, performs only the requested network check, and returns a bounded
result.  It never receives Outline management credentials or customer access
keys.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlsplit, urlunsplit

from fleet_probe import FleetProbeError, sign_probe_result
from fleet_probe_api import sign_agent_request

UTC = timezone.utc
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024


def _now_text() -> str:
    return datetime.now(UTC).isoformat()


def _host(instruction: Mapping[str, Any]) -> tuple[str, int | None]:
    host = str(instruction.get("host") or "").strip()
    if not host or len(host) > 253 or any(char in host for char in "\r\n\x00"):
        raise FleetProbeError("probe target host is invalid")
    raw_port = instruction.get("port")
    port = None if raw_port in {None, ""} else int(raw_port)
    if port is not None and not 1 <= port <= 65535:
        raise FleetProbeError("probe target port is invalid")
    return host, port


def _timeout(instruction: Mapping[str, Any]) -> float:
    try:
        value = float(instruction.get("timeout_ms", 2000)) / 1000
    except (TypeError, ValueError) as exc:
        raise FleetProbeError("probe timeout is invalid") from exc
    return max(0.1, min(30.0, value))


def _success(started: float, *, bytes_transferred: int | None = None, **fields: Any) -> dict[str, Any]:
    result = {
        "status": "success",
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "observed_at": _now_text(),
    }
    if bytes_transferred is not None:
        result["bytes_transferred"] = int(bytes_transferred)
        result["duration_ms"] = result["latency_ms"]
    result.update(fields)
    return result


def _failure(started: float, error_class: str) -> dict[str, Any]:
    normalized = str(error_class or "error")[:128]
    if normalized in {"TimeoutError", "timeout"}:
        status = "timeout"
    elif normalized in {"ConnectionRefusedError", "refused"}:
        status = "refused"
    elif normalized in {"FileNotFoundError", "PermissionError", "unavailable"}:
        status = "unavailable"
    else:
        status = "error"
    return {
        "status": status,
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "observed_at": _now_text(),
        "error_class": normalized,
    }


def _tcp(instruction: Mapping[str, Any], connector: Callable[..., Any]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        host, port = _host(instruction)
        if port is None:
            raise FleetProbeError("tcp probe requires a port")
        connection = connector((host, port), timeout=_timeout(instruction))
        try:
            return _success(started)
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
    except Exception as exc:
        return _failure(started, type(exc).__name__)


def _udp(instruction: Mapping[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    sock: socket.socket | None = None
    try:
        host, port = _host(instruction)
        if port is None:
            raise FleetProbeError("udp probe requires a port")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(_timeout(instruction))
        sock.sendto(b"AURIX-PROBE", (host, port))
        sock.recvfrom(4096)
        return _success(started)
    except Exception as exc:
        return _failure(started, type(exc).__name__)
    finally:
        if sock is not None:
            sock.close()


def _dns(instruction: Mapping[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        host, _ = _host(instruction)
        socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return _success(started)
    except Exception as exc:
        return _failure(started, type(exc).__name__)


def _http(instruction: Mapping[str, Any], *, download: bool) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        host, port = _host(instruction)
        scheme = str(instruction.get("scheme") or "https").lower()
        if scheme not in {"https", "http"} or port is None:
            raise FleetProbeError("http probe requires http(s) and a port")
        url = f"{scheme}://{host}:{port}/"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(url, method="GET", headers={"User-Agent": "AuriX-Probe/1"})
        with opener.open(request, timeout=_timeout(instruction)) as response:
            remaining = int(instruction.get("payload_bytes") or (64 * 1024)) if download else 4096
            remaining = max(1, min(MAX_DOWNLOAD_BYTES, remaining))
            body = response.read(remaining)
            return _success(
                started,
                bytes_transferred=len(body) if download else None,
                http_status=int(getattr(response, "status", 200) or 200),
            )
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return _failure(started, type(reason).__name__)
    except Exception as exc:
        return _failure(started, type(exc).__name__)


def _icmp(instruction: Mapping[str, Any], runner: Callable[..., Any]) -> dict[str, Any]:
    started = time.perf_counter()
    host, _ = _host(instruction)
    ping = shutil.which("ping")
    if not ping:
        return _failure(started, "unavailable")
    timeout_seconds = max(1, min(10, int(round(_timeout(instruction)))))
    try:
        completed = runner(
            [ping, "-c", "3", "-W", str(timeout_seconds), host],
            capture_output=True,
            text=True,
            timeout=max(3, timeout_seconds * 4),
            check=False,
        )
        if int(getattr(completed, "returncode", 1)) != 0:
            return _failure(started, "timeout")
        output = str(getattr(completed, "stdout", ""))
        packet_loss = None
        for line in output.splitlines():
            if "% packet loss" in line:
                try:
                    packet_loss = float(line.split("%", 1)[0].rsplit(",", 1)[-1].strip())
                except (ValueError, IndexError):
                    packet_loss = None
        result = _success(started, packet_loss_percent=packet_loss or 0.0)
        return result
    except Exception as exc:
        return _failure(started, type(exc).__name__)


def run_instruction(
    instruction: Mapping[str, Any],
    *,
    connector: Callable[..., Any] = socket.create_connection,
    command_runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Execute one centrally scoped instruction without leaking target content."""
    if not isinstance(instruction, Mapping):
        raise FleetProbeError("probe instruction must be an object")
    probe_type = str(instruction.get("probe_type") or "").strip()
    if probe_type == "tcp" or probe_type == "node_to_node":
        return _tcp(instruction, connector)
    if probe_type == "udp":
        return _udp(instruction)
    if probe_type == "dns":
        return _dns(instruction)
    if probe_type == "https":
        return _http(instruction, download=False)
    if probe_type == "download":
        return _http(instruction, download=True)
    if probe_type == "icmp":
        return _icmp(instruction, command_runner)
    raise FleetProbeError("probe type is unsupported")


def signed_result(job: Mapping[str, Any], *, agent_id: str, secret: str) -> dict[str, Any]:
    """Run one job and produce the exact envelope expected by the control plane."""
    if str(job.get("source_server_id") or "") != str(agent_id):
        raise FleetProbeError("agent is not assigned to this job")
    payload = run_instruction(job)
    job_id = str(job.get("job_id") or "")
    if not job_id:
        raise FleetProbeError("job_id is required")
    return {
        "job_id": job_id,
        "agent_id": str(agent_id),
        "payload": payload,
        "signature": sign_probe_result(job_id, payload, secret),
    }


def load_jobs(path: str) -> list[dict[str, Any]]:
    """Load a bounded local handoff file used by systemd/cron wrappers."""
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, list) or len(value) > 100:
        raise FleetProbeError("job file must contain at most 100 jobs")
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _request_json(
    method: str,
    url: str,
    *,
    agent_id: str,
    secret: str,
    body: bytes = b"",
    timeout: float = 30.0,
) -> dict[str, Any]:
    parsed = urlsplit(url)
    path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    timestamp = str(time.time())
    request = urllib.request.Request(
        url,
        data=body if method.upper() != "GET" else None,
        method=method.upper(),
        headers={
            "Content-Type": "application/json",
            "X-AuriX-Agent-Id": agent_id,
            "X-AuriX-Request-Timestamp": timestamp,
            "X-AuriX-Request-Signature": sign_agent_request(method, path, timestamp, body, secret),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read(MAX_DOWNLOAD_BYTES).decode("utf-8"))
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError, UnicodeError) as exc:
        raise FleetProbeError(f"probe API request failed: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise FleetProbeError("probe API response must be an object")
    return value


def poll_and_submit(
    base_url: str,
    *,
    agent_id: str,
    secret: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Pull one batch, execute it on the node, and submit signed results."""
    root = str(base_url).rstrip("/")
    if not root.startswith("https://"):
        raise FleetProbeError("probe API URL must use HTTPS")
    query = urlencode({"agent_id": agent_id, "limit": int(limit)})
    response = _request_json(
        "GET", f"{root}/v1/probes/jobs?{query}", agent_id=agent_id, secret=secret
    )
    jobs = response.get("jobs")
    if not isinstance(jobs, list):
        raise FleetProbeError("probe API jobs response is invalid")
    submitted: list[dict[str, Any]] = []
    for item in jobs:
        if not isinstance(item, Mapping) or not isinstance(item.get("instruction"), Mapping):
            continue
        instruction = dict(item["instruction"])
        instruction.update({"job_id": item.get("job_id"), "source_server_id": item.get("source_server_id")})
        envelope = signed_result(instruction, agent_id=agent_id, secret=secret)
        body = json.dumps(envelope, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
        submitted.append(
            _request_json(
                "POST", f"{root}/v1/probes/results", agent_id=agent_id, secret=secret, body=body
            )
        )
    return submitted


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", help="JSON file containing centrally issued jobs")
    parser.add_argument("--api-url", help="HTTPS probe API base URL; pulls and submits one batch (defaults to AURIX_PROBE_API_URL)")
    parser.add_argument("--agent-id", help="probe agent id (defaults to AURIX_PROBE_AGENT_ID)")
    parser.add_argument("--output", help="JSON result output path")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)
    api_url = str(args.api_url or os.environ.get("AURIX_PROBE_API_URL", "")).strip()
    agent_id = str(args.agent_id or os.environ.get("AURIX_PROBE_AGENT_ID", "")).strip()
    secret = os.environ.get("AURIX_PROBE_AGENT_SECRET", "")
    if not agent_id:
        parser.error("--agent-id or AURIX_PROBE_AGENT_ID is required")
    if not secret:
        parser.error("AURIX_PROBE_AGENT_SECRET is required")
    try:
        if bool(args.jobs) == bool(api_url):
            raise FleetProbeError("provide exactly one of --jobs or --api-url")
        if api_url:
            results = poll_and_submit(
                api_url, agent_id=agent_id, secret=secret, limit=args.limit
            )
        else:
            if not args.output:
                raise FleetProbeError("--output is required with --jobs")
            results = [
                signed_result(job, agent_id=agent_id, secret=secret)
                for job in load_jobs(args.jobs)
            ]
            with open(args.output, "w", encoding="utf-8") as handle:
                json.dump(results, handle, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
                handle.write("\n")
        print(json.dumps({"submitted": len(results)}, sort_keys=True))
    except (OSError, ValueError, FleetProbeError) as exc:
        print(f"probe agent error: {type(exc).__name__}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
