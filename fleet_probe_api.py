"""Minimal HTTPS-facing probe API for node-agent pull and result submission.

TLS termination belongs to the deployment edge.  The application authenticates
every request with the per-agent HMAC secret and authenticates every result a
second time at :class:`FleetProbeService`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fleet_probe import FleetProbeError, FleetProbeService, _canonical_json

MAX_BODY_BYTES = 128 * 1024
REQUEST_CLOCK_SKEW_SECONDS = 300


def sign_agent_request(method: str, path: str, timestamp: str, body: bytes, secret: str) -> str:
    message = b"\n".join(
        (str(method).upper().encode(), str(path).encode(), str(timestamp).encode(), body)
    )
    return hmac.new(str(secret).encode("utf-8"), message, hashlib.sha256).hexdigest()


def _json(status: str, value: Mapping[str, Any]) -> tuple[str, list[tuple[str, str]], list[bytes]]:
    body = json.dumps(dict(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return status, [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("Pragma", "no-cache"),
    ], [body]


def create_probe_wsgi_app(
    service: FleetProbeService,
    *,
    clock: Callable[[], float] = time.time,
) -> Callable[..., Any]:
    """Create a WSGI app for the node-agent API."""

    def app(environ: Mapping[str, Any], start_response: Callable[..., Any]):
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        raw_path = str(environ.get("PATH_INFO") or "/")
        query = parse_qs(str(environ.get("QUERY_STRING") or ""), keep_blank_values=False)
        agent_id = str(environ.get("HTTP_X_AURIX_AGENT_ID") or "").strip()
        timestamp = str(environ.get("HTTP_X_AURIX_REQUEST_TIMESTAMP") or "").strip()
        request_signature = str(environ.get("HTTP_X_AURIX_REQUEST_SIGNATURE") or "").strip()
        try:
            content_length = int(environ.get("CONTENT_LENGTH") or 0)
        except (TypeError, ValueError):
            content_length = -1
        if content_length < 0 or content_length > MAX_BODY_BYTES:
            status, headers, body = _json("413 Request Entity Too Large", {"error": "body_too_large"})
            start_response(status, headers)
            return body
        stream = environ.get("wsgi.input")
        body = stream.read(content_length) if stream is not None else b""
        if method == "GET" and raw_path == "/healthz":
            status, headers, body_parts = _json("200 OK", {"status": "ok"})
            start_response(status, headers)
            return body_parts
        secret = service.agent_secrets.get(agent_id)
        try:
            request_time = float(timestamp)
        except (TypeError, ValueError):
            request_time = 0
        authenticated = bool(
            secret
            and timestamp
            and abs(float(clock()) - request_time) <= REQUEST_CLOCK_SKEW_SECONDS
            and hmac.compare_digest(
                sign_agent_request(method, raw_path + ("?" + str(environ.get("QUERY_STRING")) if environ.get("QUERY_STRING") else ""), timestamp, body, secret),
                request_signature,
            )
        )
        if not authenticated:
            status, headers, body_parts = _json("401 Unauthorized", {"error": "request_not_authenticated"})
            start_response(status, headers)
            return body_parts
        request_now = datetime.fromtimestamp(request_time, timezone.utc).isoformat()
        try:
            if method == "GET" and raw_path == "/v1/probes/jobs":
                requested_agent = str((query.get("agent_id") or [agent_id])[0])
                if requested_agent != agent_id:
                    raise FleetProbeError("agent_id does not match authenticated agent")
                limit = int((query.get("limit") or [10])[0])
                jobs = service.claim_jobs(
                    agent_id=agent_id,
                    source_server_id=agent_id,
                    now=request_now,
                    limit=limit,
                )
                value = {
                    "jobs": [
                        {
                            "job_id": item.job_id,
                            "schedule_id": item.schedule_id,
                            "source_server_id": item.source_server_id,
                            "target_id": item.target_id,
                            "probe_type": item.probe_type,
                            "instruction": item.instruction,
                            "nonce": item.nonce,
                            "expires_at": item.expires_at,
                        }
                        for item in jobs
                    ]
                }
            elif method == "POST" and raw_path == "/v1/probes/results":
                value = json.loads(body.decode("utf-8"))
                if not isinstance(value, dict):
                    raise FleetProbeError("result body must be an object")
                if str(value.get("agent_id") or "") != agent_id:
                    raise FleetProbeError("agent_id does not match authenticated agent")
                accepted = service.submit_result(
                    job_id=str(value.get("job_id") or ""),
                    agent_id=agent_id,
                    payload=value.get("payload") or {},
                    signature=str(value.get("signature") or ""),
                    now=request_now,
                )
                value = accepted
            elif method == "GET" and raw_path == "/v1/probes/summary":
                value = service.summary()
            else:
                status, headers, body_parts = _json("404 Not Found", {"error": "not_found"})
                start_response(status, headers)
                return body_parts
        except (FleetProbeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            status, headers, body_parts = _json("400 Bad Request", {"error": str(exc)[:240]})
            start_response(status, headers)
            return body_parts
        status, headers, body_parts = _json("200 OK", value)
        start_response(status, headers)
        return body_parts

    return app
