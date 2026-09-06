"""Shared fleet manifest values and validation, independent of execution."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NODE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,24}\Z")
KNOWN_TIERS = ("FREE300MB", "FREE3GB", "PROMO")
KNOWN_PLANS = ("basic_50gb", "standard_100gb")


class FleetError(RuntimeError):
    pass


@dataclass(frozen=True)
class FleetNode:
    node_id: str
    label: str
    host: str
    api_port: int
    keys_port: int
    dns_name: str = ""
    provider: str = "manual"
    provider_resource_id: str = ""
    region: str = ""
    ssh_user: str = "root"
    ssh_port: int = 22
    max_keys: int = 10
    reserved_keys: int = 2
    monthly_traffic_bytes: int | None = None
    tier_slots: dict[str, int] = field(default_factory=dict)
    plan_slots: dict[str, int] = field(default_factory=dict)
    swap_mb: int = 1024


def load_dotenv(path: Path, *, overwrite: bool = False) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        raise FleetError(f"environment file does not exist: {path}")
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            raise FleetError(f"invalid environment assignment on line {number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        key = key.strip()
        values[key] = value
        if overwrite or key not in os.environ:
            os.environ[key] = value
    return values


def _positive_port(value: Any, name: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise FleetError(f"{name} must be an integer") from exc
    if not 1 <= port <= 65535:
        raise FleetError(f"{name} must be between 1 and 65535")
    return port


def _nonnegative(value: Any, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise FleetError(f"{name} must be an integer") from exc
    if result < 0:
        raise FleetError(f"{name} cannot be negative")
    return result


def parse_manifest(raw: str, *, strict_allocations: bool = False) -> list[FleetNode]:
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FleetError("AURIX_FLEET_NODES_JSON is not valid JSON") from exc
    if not isinstance(items, list) or not items:
        raise FleetError("AURIX_FLEET_NODES_JSON must be a non-empty array")
    nodes: list[FleetNode] = []
    ids: set[str] = set()
    endpoints: set[tuple[str, int]] = set()
    provider_resource_ids: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise FleetError(f"fleet node {index + 1} must be an object")
        if item.get("enabled", True) is False:
            raise FleetError(
                "disabled nodes cannot be removed automatically; set every allocation to zero, "
                "drain existing keys, then remove the node"
            )
        node_id = str(item.get("id") or "").strip()
        if not NODE_ID_RE.fullmatch(node_id) or node_id in ids:
            raise FleetError(f"invalid or duplicate fleet node id: {node_id!r}")
        host = str(item.get("host") or "").strip()
        try:
            ipaddress.ip_address(host)
        except ValueError as exc:
            raise FleetError(f"node {node_id} host must be a literal IP address") from exc
        dns_name = str(item.get("dns_name") or "").strip().rstrip(".").lower()
        if dns_name:
            if (
                len(dns_name) > 253
                or "." not in dns_name
                or "/" in dns_name
                or ":" in dns_name
                or not re.fullmatch(
                    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+",
                    dns_name,
                )
            ):
                raise FleetError(
                    f"node {node_id} dns_name must be a valid fully qualified host name"
                )
            try:
                ipaddress.ip_address(dns_name)
            except ValueError:
                pass
            else:
                raise FleetError(f"node {node_id} dns_name must not be an IP address")
        api_port = _positive_port(item.get("api_port"), f"{node_id}.api_port")
        keys_port = _positive_port(item.get("keys_port", 443), f"{node_id}.keys_port")
        ssh_port = _positive_port(item.get("ssh_port", 22), f"{node_id}.ssh_port")
        if api_port == keys_port:
            raise FleetError(f"node {node_id} management and key ports must differ")
        endpoint = (host, api_port)
        if endpoint in endpoints:
            raise FleetError(f"duplicate management endpoint: {host}:{api_port}")
        provider = str(item.get("provider") or "manual").strip().lower()
        provider_resource_id = str(item.get("provider_resource_id") or "").strip()
        if (
            provider == "digitalocean"
            and provider_resource_id
            and not provider_resource_id.isdigit()
        ):
            raise FleetError(f"node {node_id} DigitalOcean resource id must be numeric")
        if provider_resource_id and provider_resource_id in provider_resource_ids:
            raise FleetError(
                f"provider resource {provider_resource_id!r} is assigned to more than one node"
            )
        max_keys = _nonnegative(item.get("max_keys", 10), f"{node_id}.max_keys")
        reserved = _nonnegative(item.get("reserved_keys", 2), f"{node_id}.reserved_keys")
        if max_keys <= 0 or reserved >= max_keys:
            raise FleetError(f"node {node_id} must retain usable key capacity")
        monthly = item.get("monthly_traffic_bytes")
        if monthly is not None:
            monthly = _nonnegative(monthly, f"{node_id}.monthly_traffic_bytes")
            if monthly <= 0:
                raise FleetError(f"node {node_id} traffic budget must be positive")
        tier_slots = {
            str(k).upper(): _nonnegative(v, f"{node_id}.tier_slots.{k}")
            for k, v in dict(item.get("tier_slots") or {}).items()
        }
        plan_slots = {
            str(k): _nonnegative(v, f"{node_id}.plan_slots.{k}")
            for k, v in dict(item.get("plan_slots") or {}).items()
        }
        unknown_tiers = set(tier_slots) - set(KNOWN_TIERS)
        if unknown_tiers:
            raise FleetError(f"node {node_id} has unknown tiers: {sorted(unknown_tiers)}")
        unknown_plans = set(plan_slots) - set(KNOWN_PLANS)
        if unknown_plans:
            raise FleetError(f"node {node_id} has unknown plans: {sorted(unknown_plans)}")
        allocated_slots = sum(tier_slots.values()) + sum(plan_slots.values())
        saleable_slots = max_keys - reserved
        if strict_allocations and allocated_slots > saleable_slots:
            raise FleetError(
                f"node {node_id} allocates {allocated_slots} slots but only "
                f"{saleable_slots} remain after reserved headroom"
            )
        ssh_user = str(item.get("ssh_user") or "root").strip()
        if ssh_user != "root":
            raise FleetError(f"node {node_id} currently requires root SSH")
        nodes.append(
            FleetNode(
                node_id=node_id,
                label=str(item.get("label") or node_id)[:64],
                host=host,
                api_port=api_port,
                keys_port=keys_port,
                dns_name=dns_name,
                provider=provider,
                provider_resource_id=provider_resource_id,
                region=str(item.get("region") or "")[:64],
                ssh_user=ssh_user,
                ssh_port=ssh_port,
                max_keys=max_keys,
                reserved_keys=reserved,
                monthly_traffic_bytes=monthly,
                tier_slots=tier_slots,
                plan_slots=plan_slots,
                swap_mb=_nonnegative(item.get("swap_mb", 1024), f"{node_id}.swap_mb"),
            )
        )
        ids.add(node_id)
        endpoints.add(endpoint)
        if provider_resource_id:
            provider_resource_ids.add(provider_resource_id)
    if not nodes:
        raise FleetError("fleet manifest has no enabled nodes")
    return nodes


def environment(path: Path) -> dict[str, str]:
    # The explicit fleet env file is the operator-owned source of truth. A
    # systemd EnvironmentFile or inherited shell may contain stale endpoint,
    # allocation, or trust data; allowing it to win could make a recovery or
    # reconciliation pass apply an unreviewed configuration.
    loaded = load_dotenv(path, overwrite=False)
    return {**os.environ, **loaded}
