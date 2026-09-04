#!/usr/bin/env python3
"""Declarative, provider-neutral reconciliation for AuriX Outline nodes.

The manifest contains node policy and SSH coordinates.  Outline management
credentials are discovered on each node only after pinned-SSH verification,
then written atomically to the control-plane environment.  Output is always
sanitized: management URL paths and certificate fingerprints are never logged.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from commerce import CommerceDatabase, CommerceService, PostgresCommerceDatabase  # noqa: E402
from free_repository import Database  # noqa: E402
from outline_adapter import OutlineClient, OutlineServerPool  # noqa: E402

NODE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,24}\Z")
FINGERPRINT_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
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
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", dns_name)
            ):
                raise FleetError(f"node {node_id} dns_name must be a valid fully qualified host name")
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
        if provider == "digitalocean" and provider_resource_id and not provider_resource_id.isdigit():
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
        tier_slots = {str(k).upper(): _nonnegative(v, f"{node_id}.tier_slots.{k}")
                      for k, v in dict(item.get("tier_slots") or {}).items()}
        plan_slots = {str(k): _nonnegative(v, f"{node_id}.plan_slots.{k}")
                      for k, v in dict(item.get("plan_slots") or {}).items()}
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
        nodes.append(FleetNode(
            node_id=node_id, label=str(item.get("label") or node_id)[:64], host=host,
            api_port=api_port, keys_port=keys_port, dns_name=dns_name, provider=provider,
            provider_resource_id=provider_resource_id, region=str(item.get("region") or "")[:64],
            ssh_user=ssh_user, ssh_port=ssh_port, max_keys=max_keys,
            reserved_keys=reserved, monthly_traffic_bytes=monthly,
            tier_slots=tier_slots, plan_slots=plan_slots,
            swap_mb=_nonnegative(item.get("swap_mb", 1024), f"{node_id}.swap_mb"),
        ))
        ids.add(node_id)
        endpoints.add(endpoint)
        if provider_resource_id:
            provider_resource_ids.add(provider_resource_id)
    if not nodes:
        raise FleetError("fleet manifest has no enabled nodes")
    return nodes


def ssh_base(node: FleetNode, env: dict[str, str]) -> list[str]:
    materialize_trust_files(env)
    key = Path(env.get("AURIX_FLEET_SSH_KEY", ""))
    known_hosts = Path(env.get("AURIX_FLEET_KNOWN_HOSTS", ""))
    if not key.is_absolute() or not key.is_file():
        raise FleetError("AURIX_FLEET_SSH_KEY must be an existing absolute private-key path")
    if not known_hosts.is_absolute() or not known_hosts.is_file():
        raise FleetError("AURIX_FLEET_KNOWN_HOSTS must be an existing absolute file")
    return ["ssh", "-i", str(key), "-p", str(node.ssh_port), "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=15",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
            f"{node.ssh_user}@{node.host}"]


def materialize_trust_files(env: dict[str, str]) -> None:
    """Restore SSH trust files from the private environment when provided."""
    pairs = (
        ("AURIX_FLEET_SSH_PRIVATE_KEY_B64", "AURIX_FLEET_SSH_KEY", b"PRIVATE KEY"),
        ("AURIX_FLEET_KNOWN_HOSTS_B64", "AURIX_FLEET_KNOWN_HOSTS", b"ssh-"),
    )
    for source_name, path_name, marker in pairs:
        encoded = env.get(source_name, "").strip()
        if not encoded:
            continue
        target = Path(env.get(path_name, ""))
        if not target.is_absolute():
            raise FleetError(f"{path_name} must be an absolute path")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise FleetError(f"{source_name} is not valid base64") from exc
        if marker not in content or b"\x00" in content:
            raise FleetError(f"{source_name} does not contain the expected SSH material")
        if not content.endswith(b"\n"):
            content += b"\n"
        if not target.exists() or target.read_bytes() != content:
            atomic_write(target, content)


def run_ssh(node: FleetNode, env: dict[str, str], command: str, *, stdin: bytes | None = None) -> str:
    result = subprocess.run(ssh_base(node, env) + [command], input=stdin, capture_output=True,
                            timeout=600, check=False)
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise FleetError(f"node {node.node_id} SSH action failed: {(message[-1] if message else 'unknown error')[:180]}")
    return result.stdout.decode("utf-8", "replace")


def parse_access_text(text: str, node: FleetNode) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key in {"apiUrl", "certSha256"}:
            values[key] = value.strip()
    parsed = urlsplit(values.get("apiUrl", ""))
    fingerprint = values.get("certSha256", "").replace(":", "").lower()
    if (parsed.scheme != "https" or parsed.hostname != node.host or parsed.port != node.api_port
            or not re.fullmatch(r"/[A-Za-z0-9_-]{16,64}/?", parsed.path)
            or not FINGERPRINT_RE.fullmatch(fingerprint)):
        raise FleetError(f"node {node.node_id} returned an invalid management identity")
    return {"api_url": values["apiUrl"].rstrip("/"), "cert_sha256": fingerprint}


def read_identity(node: FleetNode, env: dict[str, str]) -> dict[str, str]:
    command = (
        "for d in /opt/outline /root/shadowbox; do "
        "if test -s \"$d/access.txt\"; then cat \"$d/access.txt\"; exit 0; fi; done; exit 1"
    )
    return parse_access_text(run_ssh(node, env, command), node)


def bootstrap(node: FleetNode, env: dict[str, str]) -> dict[str, Any]:
    source = env.get("AURIX_FLEET_CONTROL_PLANE_SOURCE", "").strip()
    try:
        ipaddress.ip_network(source, strict=False)
    except ValueError as exc:
        raise FleetError("AURIX_FLEET_CONTROL_PLANE_SOURCE must be an IP address or CIDR") from exc
    script = (ROOT / "deploy/node_bootstrap.sh").read_bytes()
    variables = {
        "AURIX_NODE_ID": node.node_id, "AURIX_NODE_HOST": node.host,
        "AURIX_NODE_API_PORT": str(node.api_port), "AURIX_NODE_KEYS_PORT": str(node.keys_port),
        "AURIX_NODE_SSH_PORT": str(node.ssh_port),
        "AURIX_NODE_SWAP_MB": str(node.swap_mb), "AURIX_CONTROL_PLANE_SOURCE": source,
        "AURIX_FLEET_REVISION": env.get("AURIX_FLEET_REVISION", "unknown"),
        "AURIX_OUTLINE_INSTALLER_URL": env.get("AURIX_OUTLINE_INSTALLER_URL", ""),
        "AURIX_OUTLINE_INSTALLER_SHA256": env.get("AURIX_OUTLINE_INSTALLER_SHA256", ""),
    }
    prefix = " ".join(f"{key}={shlex.quote(value)}" for key, value in variables.items() if value)
    raw = run_ssh(node, env, f"{prefix} bash -s", stdin=script)
    try:
        result = json.loads(raw.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise FleetError(f"node {node.node_id} returned invalid bootstrap status") from exc
    if result.get("status") != "ready":
        raise FleetError(f"node {node.node_id} did not become ready")
    return result


def server_config(nodes: list[FleetNode], identities: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    result = []
    for node in nodes:
        item = {"id": node.node_id, "label": node.label, **identities[node.node_id]}
        if node.provider_resource_id:
            item["provider_resource_id"] = node.provider_resource_id
        item["provider"] = node.provider or "manual"
        item["region"] = node.region or "unknown"
        item["transport"] = "outline"
        result.append(item)
    return result


def update_env_file(path: Path, values: dict[str, str]) -> bool:
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    pending = dict(values)
    output: list[str] = []
    for raw in original.splitlines():
        match = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", raw)
        if match and match.group(1) in pending:
            key = match.group(1)
            output.append(f"{key}={shlex.quote(pending.pop(key))}")
        else:
            output.append(raw)
    if output and output[-1]:
        output.append("")
    output.extend(f"{key}={shlex.quote(value)}" for key, value in pending.items())
    updated = "\n".join(output).rstrip() + "\n"
    if updated == original:
        return False
    atomic_write(path, updated.encode())
    return True


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def verify_identities(nodes: list[FleetNode], identities: dict[str, dict[str, str]]) -> None:
    for node in nodes:
        client = OutlineClient(**identities[node.node_id])
        client.server_info()
        client.list_keys()


def apply_policy(nodes: list[FleetNode], identities: dict[str, dict[str, str]], env: dict[str, str]) -> None:
    database_url = env.get("COMMERCE_DATABASE_URL", "").strip()
    if database_url:
        database: Any = PostgresCommerceDatabase(database_url)
    else:
        path = Path(env.get("DATABASE_PATH", "data/bot.db"))
        free_db = Database(path)
        free_db.initialize()
        database = CommerceDatabase(path)
    clients = {node.node_id: OutlineClient(**identities[node.node_id]) for node in nodes}
    pool = OutlineServerPool(clients, env.get("OUTLINE_DEFAULT_SERVER_ID") or nodes[0].node_id)
    service = CommerceService(database, pool, env.get("AURIX_ACCESS_URL_KEY", "fleet-reconcile"))
    service.initialize()
    labels = {node.node_id: node.label for node in nodes}
    provider_ids = {node.node_id: node.provider_resource_id for node in nodes if node.provider_resource_id}
    endpoint_metadata = {
        node.node_id: {
            "provider": node.provider or "manual",
            "region": node.region or "unknown",
            "transport": "outline",
        }
        for node in nodes
    }
    service.register_outline_servers(
        labels,
        provider_resource_ids=provider_ids,
        endpoint_metadata=endpoint_metadata,
    )
    health = {item["server_id"]: item["status"] for item in service.refresh_server_inventory()}
    if any(health.get(node.node_id) != "healthy" for node in nodes):
        raise FleetError("policy activation refused because a node is unhealthy")
    owner = int((env.get("OWNER_TELEGRAM_ID") or env.get("ADMIN_TELEGRAM_IDS") or "0").split(",")[0])
    for node in nodes:
        service.apply_server_policy(
            node.node_id,
            owner,
            max_keys=node.max_keys,
            reserved_keys=node.reserved_keys,
            monthly_traffic_bytes=node.monthly_traffic_bytes,
            plan_slots={
                plan: int(node.plan_slots.get(plan, 0))
                for plan in sorted(set(KNOWN_PLANS) | set(node.plan_slots))
            },
            tier_slots={
                tier: int(node.tier_slots.get(tier, 0))
                for tier in KNOWN_TIERS
            },
        )


def environment(path: Path) -> dict[str, str]:
    # The explicit fleet env file is the operator-owned source of truth. A
    # systemd EnvironmentFile or inherited shell may contain stale endpoint,
    # allocation, or trust data; allowing it to win could make a recovery or
    # reconciliation pass apply an unreviewed configuration.
    loaded = load_dotenv(path, overwrite=False)
    return {**os.environ, **loaded}


def reconcile(args: argparse.Namespace) -> None:
    # Import lazily: dns_records reuses manifest parsing and therefore imports
    # this module too; a top-level import would create a circular dependency.
    from deploy import dns_records

    env_path = Path(args.env_file)
    env = environment(env_path)
    strict_allocations = env.get("AURIX_FLEET_STRICT_ALLOCATION_VALIDATION", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    nodes = parse_manifest(
        env.get("AURIX_FLEET_NODES_JSON", ""),
        strict_allocations=strict_allocations,
    )
    lock_path = Path(env.get("AURIX_FLEET_LOCK_FILE", "/run/aurix-fleet-reconcile.lock"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        statuses = {}
        identities = {}
        for node in nodes:
            if not args.check:
                statuses[node.node_id] = bootstrap(node, env)
            identities[node.node_id] = read_identity(node, env)
        verify_identities(nodes, identities)
        changed = False
        if not args.check:
            before = env_path.read_bytes()
            default_id = env.get("OUTLINE_DEFAULT_SERVER_ID") or nodes[0].node_id
            if default_id not in {node.node_id for node in nodes}:
                raise FleetError("OUTLINE_DEFAULT_SERVER_ID is not in the fleet manifest")
            changed = update_env_file(env_path, {
                "OUTLINE_SERVERS_JSON": json.dumps(server_config(nodes, identities), separators=(",", ":")),
                "OUTLINE_DEFAULT_SERVER_ID": default_id,
            })
            try:
                current_env = environment(env_path)
                apply_policy(nodes, identities, current_env)
                if dns_records.configured(current_env):
                    dns_report = dns_records.sync(current_env)
                    print(json.dumps({"dns": dns_report}, indent=2))
                    for node in nodes:
                        if node.dns_name:
                            OutlineClient(**identities[node.node_id]).set_hostname_for_access_keys(
                                node.dns_name
                            )
                if changed and not args.no_restart:
                    subprocess.run(["systemctl", "restart", "aurix-bot.service"], check=True, timeout=60)
                    subprocess.run(["systemctl", "is-active", "--quiet", "aurix-bot.service"], check=True)
            except Exception:
                if changed:
                    atomic_write(env_path, before)
                    if not args.no_restart:
                        subprocess.run(["systemctl", "restart", "aurix-bot.service"], check=False)
                raise
        summary = [{"id": node.node_id, "host": node.host, "ready": True,
                    **({"outline_version": statuses[node.node_id].get("outline_version"),
                        "remote_key_count": statuses[node.node_id].get("remote_key_count")} if node.node_id in statuses else {})}
                   for node in nodes]
        print(json.dumps({"status": "healthy", "mode": "check" if args.check else "reconcile",
                          "configuration_changed": changed, "nodes": summary}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "check", "reconcile"))
    parser.add_argument("--env-file", default=os.environ.get("AURIX_FLEET_ENV_FILE", "/etc/aurix-bot/aurix.env"))
    parser.add_argument("--no-restart", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        env = environment(Path(args.env_file))
        strict_allocations = env.get(
            "AURIX_FLEET_STRICT_ALLOCATION_VALIDATION", ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        nodes = parse_manifest(
            env.get("AURIX_FLEET_NODES_JSON", ""),
            strict_allocations=strict_allocations,
        )
        if args.command == "validate":
            print(json.dumps({"status": "valid", "nodes": [node.node_id for node in nodes]}))
            return
        args.check = args.command == "check"
        reconcile(args)
    except (FleetError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"fleet reconcile failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
