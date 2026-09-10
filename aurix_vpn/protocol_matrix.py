"""Bounded local mixed-protocol contract and load matrix.

This runner deliberately exercises the authenticated node-agent boundary with
an in-memory provider.  It is useful for staged controller regressions and
latency evidence, but it must never be described as proof of real Xray,
Hysteria2, UDP, ISP, or Myanmar client-path behavior.
"""

from __future__ import annotations

import io
import json
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from .connectivity_adapters import Hysteria2ConnectivityAdapter, XrayConnectivityAdapter
from .node_agent import NodeAgentClient, NodeAgentError
from .node_agent_app import NodeAgentService, create_node_agent_wsgi_app


UTC = timezone.utc
SUPPORTED_PROTOCOLS = ("xray", "hysteria2")


class ThreadSafeProtocolProvider:
    """Small deterministic provider for bounded mixed-protocol checks."""

    def __init__(self):
        self.lock = threading.RLock()
        self.users: dict[str, dict[str, Any]] = {}
        self.quotas: dict[str, int] = {}

    def server_info(self) -> dict[str, Any]:
        return {"version": "matrix-agent", "capabilities": ["usage", "quota"]}

    def list_users(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(value) for value in self.users.values()]

    def get_user(self, external_id: str) -> dict[str, Any] | None:
        with self.lock:
            value = self.users.get(str(external_id))
            return dict(value) if value else None

    def create_user(
        self,
        external_id: str,
        name: str,
        route: Mapping[str, Any],
        intent: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self.lock:
            value = {
                "external_id": str(external_id),
                "name": str(name),
                "protocol": str(route.get("protocol")),
                "secret": str(intent.get("secret")),
            }
            self.users[str(external_id)] = value
            self.quotas[str(external_id)] = int(intent.get("quota_bytes") or 0)
            return dict(value)

    def set_user_quota(self, external_id: str, quota_bytes: int) -> None:
        with self.lock:
            self.quotas[str(external_id)] = int(quota_bytes)

    def get_user_usage(self, external_id: str) -> dict[str, Any]:
        with self.lock:
            if str(external_id) not in self.users:
                raise KeyError(external_id)
            return {"external_id": str(external_id), "tx_bytes": 7, "rx_bytes": 11}

    def delete_user(self, external_id: str) -> None:
        with self.lock:
            self.users.pop(str(external_id), None)
            self.quotas.pop(str(external_id), None)

    def terminate_user_sessions(self, _external_id: str) -> dict[str, bool]:
        return {"terminated": True}

    def probe_data_plane(self, route: Mapping[str, Any]) -> dict[str, Any]:
        return {"status": "healthy", "exit_ip": route["public_address"]}


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _latency_summary(samples: list[float]) -> dict[str, float]:
    return {
        "count": len(samples),
        "mean_ms": round(mean(samples), 3) if samples else 0.0,
        "p50_ms": round(_percentile(samples, 0.50), 3),
        "p95_ms": round(_percentile(samples, 0.95), 3),
        "p99_ms": round(_percentile(samples, 0.99), 3),
        "max_ms": round(max(samples), 3) if samples else 0.0,
    }


class ProtocolMatrixRunner:
    """Run one bounded staged matrix against the local node-agent contract."""

    def __init__(
        self,
        *,
        customers_per_protocol: int = 8,
        workers: int = 8,
        quota_bytes: int = 1_000_000,
        protocols: tuple[str, ...] = SUPPORTED_PROTOCOLS,
    ):
        if not 1 <= int(customers_per_protocol) <= 200:
            raise ValueError("customers_per_protocol must be between 1 and 200")
        if not 1 <= int(workers) <= 64:
            raise ValueError("workers must be between 1 and 64")
        if int(quota_bytes) <= 0:
            raise ValueError("quota_bytes must be positive")
        normalized = tuple(str(protocol).strip().lower() for protocol in protocols)
        if not normalized or any(protocol not in SUPPORTED_PROTOCOLS for protocol in normalized):
            raise ValueError("protocols must be a non-empty subset of the supported matrix")
        self.customers_per_protocol = int(customers_per_protocol)
        self.workers = int(workers)
        self.quota_bytes = int(quota_bytes)
        self.protocols = normalized
        self.provider = ThreadSafeProtocolProvider()
        self.app = create_node_agent_wsgi_app(NodeAgentService(self.provider, bearer_token="matrix-token"))

    @staticmethod
    def route(protocol: str) -> dict[str, Any]:
        if protocol == "xray":
            return {
                "route_id": "xray:sg-a",
                "endpoint_id": "sg-a",
                "protocol": "xray",
                "public_address": "198.51.100.10",
                "port": 18443,
                "public_key": "public-key",
                "server_name": "example.com",
                "short_id": "abcd",
            }
        if protocol == "hysteria2":
            return {
                "route_id": "hysteria2:sg-a",
                "endpoint_id": "sg-a",
                "protocol": "hysteria2",
                "public_address": "198.51.100.10",
                "port": 18444,
                "server_name": "example.com",
            }
        raise ValueError(f"unsupported matrix protocol: {protocol}")

    def requester(self, method: str, path: str, payload: Mapping[str, Any] | None) -> Any:
        body = b"" if payload is None else json.dumps(dict(payload)).encode("utf-8")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_LENGTH": str(len(body)),
            "HTTP_AUTHORIZATION": "Bearer matrix-token",
            "wsgi.input": io.BytesIO(body),
        }
        captured: dict[str, str] = {}

        def start_response(status: str, _headers: list[tuple[str, str]]) -> None:
            captured["status"] = status

        value = json.loads(b"".join(self.app(environ, start_response)))
        code = int(str(captured["status"]).split(" ", 1)[0])
        if code >= 400:
            raise NodeAgentError(value.get("error") or "agent request failed", status_code=code)
        return value

    def _adapter(self, protocol: str) -> Any:
        client = NodeAgentClient(requester=self.requester)
        if protocol == "xray":
            return XrayConnectivityAdapter(client)
        return Hysteria2ConnectivityAdapter(client)

    def run(self) -> dict[str, Any]:
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        jobs = [
            (protocol, index)
            for index in range(self.customers_per_protocol)
            for protocol in self.protocols
        ]
        grants: list[tuple[str, Any, dict[str, Any], float]] = []
        errors: list[dict[str, str]] = []

        def issue(protocol: str, index: int) -> tuple[str, Any, dict[str, Any], float]:
            adapter = self._adapter(protocol)
            route = self.route(protocol)
            external_id = f"{protocol}-customer-{index}"
            point = time.perf_counter()
            grant = adapter.provision(
                route,
                {"external_id": external_id, "name": external_id, "quota_bytes": self.quota_bytes},
            )
            return protocol, adapter, grant, (time.perf_counter() - point) * 1000

        with ThreadPoolExecutor(max_workers=min(self.workers, len(jobs))) as executor:
            futures = [executor.submit(issue, protocol, index) for protocol, index in jobs]
            for future in as_completed(futures):
                try:
                    grants.append(future.result())
                except Exception as exc:  # pragma: no cover - surfaced in report and test assertion
                    errors.append({"type": type(exc).__name__, "message": str(exc)[:256]})

        provision_samples = [item[3] for item in grants]
        protocol_counts = {
            protocol: sum(1 for item in grants if item[0] == protocol) for protocol in self.protocols
        }
        usage_ok = True
        probe_ok = True
        reconcile_counts: dict[str, int] = {}
        for protocol in self.protocols:
            adapter = self._adapter(protocol)
            route = self.route(protocol)
            for grant_protocol, grant_adapter, grant, _latency in grants:
                if grant_protocol != protocol:
                    continue
                usage_ok = usage_ok and grant_adapter.read_usage(grant)["bytes_transferred"] == 18
                probe_ok = probe_ok and grant_adapter.probe_data_plane(route)["status"] == "healthy"
            reconcile_counts[protocol] = int(adapter.reconcile(route)["users"])

        users_before_revoke = dict(self.provider.users)
        quotas_before_revoke = dict(self.provider.quotas)
        revoked = 0
        if not errors:
            with ThreadPoolExecutor(max_workers=min(self.workers, max(1, len(grants)))) as executor:
                futures = [executor.submit(adapter.revoke_auth, grant) for _, adapter, grant, _ in grants]
                for future in as_completed(futures):
                    try:
                        future.result()
                        revoked += 1
                    except Exception as exc:  # pragma: no cover - surfaced in report and test assertion
                        errors.append({"type": type(exc).__name__, "message": str(exc)[:256]})

        isolation_ok = (
            not errors
            and len(users_before_revoke) == len(grants)
            and {str(value.get("protocol")) for value in users_before_revoke.values()}
            == set(self.protocols)
        )
        quota_ok = not errors and set(quotas_before_revoke.values()) == {self.quota_bytes}
        inventory_ok = all(value == len(grants) for value in reconcile_counts.values())
        revocation_ok = revoked == len(grants) and not self.provider.users and not self.provider.quotas
        checks = {
            "provisioned_expected": len(grants) == len(jobs),
            "protocol_isolation": isolation_ok,
            "quota_assignment": quota_ok,
            "usage_contract": usage_ok and not errors,
            "data_plane_probe": probe_ok and not errors,
            "inventory_reconciliation": inventory_ok and not errors,
            "revocation": revocation_ok,
        }
        return {
            "status": "passed" if not errors and all(checks.values()) else "failed",
            "scope": "local-in-memory-node-agent",
            "started_at": started_at.isoformat(),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "protocols": list(self.protocols),
            "customers_per_protocol": self.customers_per_protocol,
            "workers": self.workers,
            "total_customers": len(jobs),
            "provisioned": len(grants),
            "revoked": revoked,
            "protocol_counts": protocol_counts,
            "reconcile_counts": reconcile_counts,
            "latency_ms": {"provision": _latency_summary(provision_samples)},
            "checks": checks,
            "errors": errors,
        }


__all__ = ["ProtocolMatrixRunner", "SUPPORTED_PROTOCOLS", "ThreadSafeProtocolProvider"]
