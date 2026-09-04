#!/usr/bin/env python3
"""Run one bounded DigitalOcean infrastructure reconciliation pass.

This process is intentionally separate from the Telegram bot. Endpoint
activation is optional and fail-closed: the worker can commit it only after
the declared fleet reconciler proves the exact provider/IP/SSH identity.
"""

from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# Support both ``python -m deploy.infrastructure_worker`` (systemd) and the
# direct path form used by operators during a controlled dry run.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commerce import CommerceDatabase, PostgresCommerceDatabase
from deploy.fleet_reconcile import FleetError as FleetManifestError
from fleet_enrollment import EnrollmentError, expire_pending_enrollments, mark_consumed, read_enrollment
from infrastructure import DigitalOceanClient, FleetController, InfrastructureError


UTC = timezone.utc
LOCK_PATH = Path(os.environ.get("AURIX_INFRASTRUCTURE_LOCK", "/var/lib/aurix-infrastructure/worker.lock"))


def _database() -> Any:
    database_url = os.environ.get("COMMERCE_DATABASE_URL", "").strip()
    if database_url:
        return PostgresCommerceDatabase(database_url)
    database_path = os.environ.get("DATABASE_PATH", "").strip()
    if not database_path or not Path(database_path).is_absolute():
        raise InfrastructureError("DATABASE_PATH must be an absolute persistent path")
    return CommerceDatabase(Path(database_path))


def _auto_activation_enabled() -> bool:
    return os.environ.get("AURIX_INFRASTRUCTURE_AUTO_ACTIVATION_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _auto_registration_enabled() -> bool:
    return os.environ.get("AURIX_FLEET_AUTO_REGISTRATION_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _registration_endpoint_enabled() -> bool:
    return os.environ.get("AURIX_FLEET_REGISTRATION_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _due_jobs(database: Any) -> list[dict[str, Any]]:
    now = datetime.now(UTC).isoformat()
    statuses = "'pending', 'running'"
    if _auto_activation_enabled() or _auto_registration_enabled():
        statuses += ", 'awaiting_verification'"
    with database.connect() as connection:
        rows = connection.execute(
            f"""SELECT id, status, provider_resource_id FROM infrastructure_jobs
               WHERE operation = 'provision'
                 AND status IN ({statuses})
                 AND next_attempt_at <= ?
               ORDER BY created_at LIMIT 8""",
            (now,),
        ).fetchall()
    return [dict(row) for row in rows]


def _declared_activation_node(
    *, provider_resource_id: str, public_ip: str | None, env_file: Path
) -> tuple[str, dict[str, str]] | None:
    """Return a manifest node only when provider identity and address match.

    The provider API is not a cryptographic SSH host-key authority.  We never
    accept a newly observed address into known_hosts, and we never activate a
    node that is absent from the operator-owned manifest.
    """
    if not provider_resource_id or not public_ip or not env_file.is_file():
        return None
    try:
        from deploy.fleet_reconcile import load_dotenv, parse_manifest

        # The control-plane env file is authoritative for fleet identity.  A
        # worker service may already have a stale manifest in its own
        # EnvironmentFile; do not let that shadow the explicit path.
        loaded = load_dotenv(env_file, overwrite=False)
        env = {**os.environ, **loaded}
        strict = env.get("AURIX_FLEET_STRICT_ALLOCATION_VALIDATION", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        nodes = parse_manifest(env.get("AURIX_FLEET_NODES_JSON", ""), strict_allocations=strict)
    except Exception:
        return None
    matches = [
        node
        for node in nodes
        if node.provider_resource_id == provider_resource_id and node.host == public_ip
    ]
    if len(matches) != 1:
        return None
    node = matches[0]
    # The reconciler will materialize base64 trust files if configured, but a
    # path must still be explicit so a missing trust chain fails closed.
    if not env.get("AURIX_FLEET_SSH_KEY", "").strip() or not env.get(
        "AURIX_FLEET_KNOWN_HOSTS", ""
    ).strip():
        return None
    return node.node_id, env


_SSH_HOST_KEY_RE = re.compile(
    r"(?:ssh|ecdsa)-[A-Za-z0-9+_.-]+\s+[A-Za-z0-9+/=]+(?:\s+[^\r\n]+)?\Z"
)


def _enrollment_identity(payload: dict[str, str], provider_ip: str) -> dict[str, str | int]:
    """Validate the management identity against the provider-observed address."""
    try:
        ipaddress.ip_address(provider_ip)
    except ValueError as exc:
        raise EnrollmentError("provider returned an invalid public IP") from exc
    if payload.get("public_ip") != provider_ip:
        raise EnrollmentError("enrollment public IP does not match provider")
    values: dict[str, str] = {}
    for line in str(payload.get("access_txt") or "").splitlines():
        key, separator, value = line.partition(":")
        if separator and key in {"apiUrl", "certSha256"}:
            values[key] = value.strip()
    parsed = urlsplit(values.get("apiUrl", ""))
    fingerprint = values.get("certSha256", "").replace(":", "").lower()
    if (
        parsed.scheme != "https"
        or parsed.hostname != provider_ip
        or parsed.port is None
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/[A-Za-z0-9_-]{16,64}/?", parsed.path)
        or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
    ):
        raise EnrollmentError("enrollment Outline identity is not bound to provider IP")
    host_key = str(payload.get("ssh_host_key") or "").strip()
    if not _SSH_HOST_KEY_RE.fullmatch(host_key):
        raise EnrollmentError("enrollment SSH host key is invalid")
    return {
        "api_url": values["apiUrl"].rstrip("/"),
        "cert_sha256": fingerprint,
        "api_port": int(parsed.port),
        "ssh_host_key": host_key,
    }


def _append_pinned_host(known_hosts: Path, host: str, host_key: str) -> bytes:
    """Append one exact IP host key, refusing conflicts and shell injection."""
    if not known_hosts.is_absolute() or not known_hosts.is_file():
        raise EnrollmentError("AURIX_FLEET_KNOWN_HOSTS must be an existing absolute file")
    if not re.fullmatch(r"[0-9A-Fa-f:.]+", host) or "\n" in host_key or "\r" in host_key:
        raise EnrollmentError("invalid pinned host data")
    original = known_hosts.read_bytes()
    for raw in original.decode("utf-8", "strict").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        hosts = set(parts[0].split(","))
        if host not in hosts:
            continue
        existing = " ".join(parts[1:])
        if existing != host_key:
            raise EnrollmentError("pinned host key conflicts with existing known_hosts entry")
        return original
    suffix = b"" if not original or original.endswith(b"\n") else b"\n"
    from deploy.fleet_reconcile import atomic_write

    atomic_write(known_hosts, original + suffix + f"{host} {host_key}\n".encode())
    return original


def _manifest_item(node: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": node.node_id,
        "label": node.label,
        "host": node.host,
        "api_port": node.api_port,
        "keys_port": node.keys_port,
        "provider": node.provider,
        "provider_resource_id": node.provider_resource_id,
        "region": node.region,
        "ssh_user": node.ssh_user,
        "ssh_port": node.ssh_port,
        "max_keys": node.max_keys,
        "reserved_keys": node.reserved_keys,
        "tier_slots": dict(node.tier_slots),
        "plan_slots": dict(node.plan_slots),
        "swap_mb": node.swap_mb,
    }
    if node.dns_name:
        item["dns_name"] = node.dns_name
    if node.monthly_traffic_bytes is not None:
        item["monthly_traffic_bytes"] = node.monthly_traffic_bytes
    return item


def _json_slots(name: str) -> dict[str, int]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EnrollmentError(f"{name} is not valid JSON") from exc
    if not isinstance(values, dict):
        raise EnrollmentError(f"{name} must be a JSON object")
    result: dict[str, int] = {}
    for key, value in values.items():
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise EnrollmentError(f"{name} contains a non-integer slot") from exc
        if number < 0:
            raise EnrollmentError(f"{name} contains a negative slot")
        result[str(key)] = number
    return result


def _auto_activate_registered(
    controller: FleetController,
    job_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Enroll a provider-created node only after HTTPS + pinned SSH checks."""
    if not _registration_endpoint_enabled():
        print("infrastructure_worker: registration_waiting_for_callback_gate")
        return result
    key = os.environ.get("AURIX_FLEET_ENROLLMENT_KEY", "").strip()
    if not key:
        print("infrastructure_worker: registration_waiting_for_enrollment_key")
        return result
    try:
        payload = read_enrollment(controller.database, job_id=job_id, encryption_key=key)
    except EnrollmentError as exc:
        print(f"infrastructure_worker: registration_rejected={type(exc).__name__}", file=sys.stderr)
        return result
    if payload is None:
        # If the process crashed after consuming the token but before writing
        # the job transition, recover the already-reconciled manifest entry
        # instead of requiring a new (single-use) callback.
        try:
            with controller.database.connect() as connection:
                enrollment = connection.execute(
                    "SELECT status FROM infrastructure_enrollments WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
            if enrollment is not None and str(enrollment["status"]) == "consumed":
                env_file = Path(os.environ.get("AURIX_FLEET_ENV_FILE", "/etc/aurix-bot/aurix.env"))
                from deploy.fleet_reconcile import load_dotenv, parse_manifest

                loaded = load_dotenv(env_file, overwrite=False)
                nodes = parse_manifest((({**os.environ, **loaded}).get("AURIX_FLEET_NODES_JSON", "")))
                provider_resource_id = str(result.get("provider_resource_id") or "")
                matching = [node for node in nodes if node.provider_resource_id == provider_resource_id]
                if len(matching) == 1:
                    return controller.mark_provision_activated(job_id, matching[0].node_id)
        except (OSError, ValueError, FleetManifestError, InfrastructureError):
            pass
        print("infrastructure_worker: registration_waiting_for_callback")
        return result
    provider_resource_id = str(result.get("provider_resource_id") or "")
    if not provider_resource_id or controller.provider is None:
        return result
    try:
        droplet = controller.provider.droplet(provider_resource_id)
        provider_ip = next(
            str(item.get("ip_address"))
            for item in (droplet.get("networks") or {}).get("v4", [])
            if isinstance(item, dict) and item.get("type") == "public"
        )
        identity = _enrollment_identity(payload, provider_ip)
        env_file = Path(os.environ.get("AURIX_FLEET_ENV_FILE", "/etc/aurix-bot/aurix.env"))
        if not env_file.is_absolute() or not env_file.is_file():
            raise EnrollmentError("AURIX_FLEET_ENV_FILE must be an existing absolute file")
        from deploy.fleet_reconcile import atomic_write, load_dotenv, parse_manifest, update_env_file

        loaded = load_dotenv(env_file, overwrite=False)
        fleet_env = {**os.environ, **loaded}
        strict = fleet_env.get("AURIX_FLEET_STRICT_ALLOCATION_VALIDATION", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        nodes = parse_manifest(fleet_env.get("AURIX_FLEET_NODES_JSON", ""), strict_allocations=strict)
        if any(node.node_id == payload["node_id"] for node in nodes):
            raise EnrollmentError("enrollment node id already exists in fleet manifest")
        region_value = droplet.get("region") or {}
        if isinstance(region_value, dict):
            region_value = region_value.get("slug")
        region = str(region_value or os.environ.get("AURIX_SCALE_REGION", ""))
        max_keys = int(os.environ.get("AURIX_AUTO_NODE_MAX_KEYS", "10"))
        reserved_keys = int(os.environ.get("AURIX_AUTO_NODE_RESERVED_KEYS", "2"))
        if max_keys <= 0 or reserved_keys < 0 or reserved_keys >= max_keys:
            raise EnrollmentError("automatic node capacity is invalid")
        new_node = {
            "id": payload["node_id"],
            "label": f"AuriX {payload['node_id']}",
            "host": provider_ip,
            "api_port": int(identity["api_port"]),
            "keys_port": int(os.environ.get("AURIX_SCALE_KEYS_PORT", "443")),
            "provider": "digitalocean",
            "provider_resource_id": provider_resource_id,
            "region": region,
            "ssh_user": "root",
            "ssh_port": int(os.environ.get("AURIX_SCALE_SSH_PORT", "22")),
            "max_keys": max_keys,
            "reserved_keys": reserved_keys,
            "tier_slots": _json_slots("AURIX_AUTO_NODE_TIER_SLOTS_JSON"),
            "plan_slots": _json_slots("AURIX_AUTO_NODE_PLAN_SLOTS_JSON"),
            "swap_mb": int(os.environ.get("AURIX_SCALE_SWAP_MB", "1024")),
        }
        monthly = os.environ.get("AURIX_AUTO_NODE_MONTHLY_TRAFFIC_BYTES", "").strip()
        if monthly:
            new_node["monthly_traffic_bytes"] = int(monthly)
        candidate_manifest = [_manifest_item(node) for node in nodes] + [new_node]
        # Parse before writing so duplicate endpoint/provider/capacity errors
        # cannot partially alter the operator-owned environment.
        parse_manifest(json.dumps(candidate_manifest), strict_allocations=strict)
        known_hosts = Path(fleet_env.get("AURIX_FLEET_KNOWN_HOSTS", ""))
        original_env = env_file.read_bytes()
        original_hosts = _append_pinned_host(known_hosts, provider_ip, str(identity["ssh_host_key"]))
        try:
            update_env_file(env_file, {
                "AURIX_FLEET_NODES_JSON": json.dumps(candidate_manifest, separators=(",", ":")),
            })
            fleet_env = {**os.environ, **load_dotenv(env_file, overwrite=False)}
            command = [
                sys.executable,
                str(Path(__file__).with_name("fleet_reconcile.py")),
                "reconcile",
                "--env-file",
                str(env_file),
            ]
            completed = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=20 * 60,
                check=False,
                env={**os.environ, **fleet_env},
            )
            if completed.returncode != 0:
                raise EnrollmentError("fleet reconcile rejected enrolled node")
        except Exception:
            atomic_write(env_file, original_env)
            atomic_write(known_hosts, original_hosts)
            raise
        if not mark_consumed(controller.database, job_id=job_id):
            raise EnrollmentError("enrollment could not be consumed")
        activated = controller.mark_provision_activated(job_id, payload["node_id"])
        print(f"infrastructure_worker: job={job_id[:12]} status=completed node={payload['node_id']}")
        return activated
    except (EnrollmentError, OSError, ValueError, json.JSONDecodeError, InfrastructureError) as exc:
        print(f"infrastructure_worker: registration_activation_failed={type(exc).__name__}", file=sys.stderr)
        return result


def _auto_activate(
    controller: FleetController,
    job_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Run the declared fleet reconciler and commit activation if it succeeds."""
    if not (_auto_activation_enabled() or _auto_registration_enabled()) or result.get("status") != "awaiting_verification":
        return result
    env_file = Path(os.environ.get("AURIX_FLEET_ENV_FILE", "/etc/aurix-bot/aurix.env"))
    match = _declared_activation_node(
        provider_resource_id=str(result.get("provider_resource_id") or ""),
        public_ip=str(result.get("public_ip") or ""),
        env_file=env_file,
    )
    if match is None:
        if _auto_registration_enabled():
            return _auto_activate_registered(controller, job_id, result)
        print("infrastructure_worker: activation_waiting_for_pinned_manifest")
        return result
    node_id, fleet_env = match
    command = [
        sys.executable,
        str(Path(__file__).with_name("fleet_reconcile.py")),
        "reconcile",
        "--env-file",
        str(env_file),
    ]
    try:
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20 * 60,
            check=False,
            # Preserve the worker's provider credentials while making the
            # explicit fleet env file authoritative for reconciliation.
            env={**os.environ, **fleet_env},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"infrastructure_worker: activation_failed={type(exc).__name__}", file=sys.stderr)
        return result
    if completed.returncode != 0:
        print("infrastructure_worker: activation_failed=fleet_reconcile", file=sys.stderr)
        return result
    try:
        activated = controller.mark_provision_activated(job_id, node_id)
    except InfrastructureError as exc:
        print(f"infrastructure_worker: activation_commit_failed={type(exc).__name__}", file=sys.stderr)
        return result
    print(f"infrastructure_worker: job={job_id[:12]} status=completed node={node_id}")
    return activated


def run_once() -> int:
    token = os.environ.get("DIGITALOCEAN_API_TOKEN", "").strip()
    if not token:
        raise InfrastructureError("DIGITALOCEAN_API_TOKEN is not configured")
    database = _database()
    database.initialize()
    expired_enrollments = expire_pending_enrollments(database)
    if expired_enrollments:
        print(f"infrastructure_worker: expired_enrollments={expired_enrollments}")
    controller = FleetController(database, DigitalOceanClient(token))
    inventory = controller.reconcile_provider_inventory()
    print(
        "infrastructure_worker: inventory="
        f"{inventory['managed']} managed/{inventory['matched']} registered"
    )
    # Orphan cleanup is a separate, exact-confirmation gate.  Calling the
    # method on every pass keeps the audit visible while its default remains a
    # no-op; it can never delete a node merely because provider inventory is
    # temporarily unavailable or an activation job is still in flight.
    try:
        orphan_cleanup = controller.cleanup_provider_orphans()
        print(
            "infrastructure_worker: orphan_cleanup="
            f"{orphan_cleanup['status']} candidates={orphan_cleanup['candidates']} "
            f"deleted={orphan_cleanup['deleted']} failed={orphan_cleanup['failed']}"
        )
    except InfrastructureError as exc:
        # A bad confirmation/configuration should be visible but must not
        # prevent ordinary provisioning reconciliation from running.
        print(
            f"infrastructure_worker: orphan_cleanup_failed={type(exc).__name__}",
            file=sys.stderr,
        )
    jobs = _due_jobs(database)
    if not jobs:
        print("infrastructure_worker: no due jobs")
        return 0
    failures = 0
    for job in jobs:
        job_id = str(job["id"])
        try:
            if str(job["status"]) == "pending":
                result = controller.execute_provision(job_id)
            elif str(job["status"]) == "running":
                result = controller.reconcile_provision(job_id)
            else:
                result = {
                    "job_id": job_id,
                    "status": "awaiting_verification",
                    "provider_resource_id": str(job.get("provider_resource_id") or ""),
                }
            result = _auto_activate(controller, job_id, result)
            print(f"infrastructure_worker: job={job_id[:12]} status={result.get('status', 'unknown')}")
        except InfrastructureError as exc:
            # A disabled mutation gate is an intentional no-op, not a crash.
            if "mutations are disabled" in str(exc):
                print("infrastructure_worker: mutations_disabled")
                continue
            failures += 1
            print(
                f"infrastructure_worker: job={job_id[:12]} failed={type(exc).__name__}",
                file=sys.stderr,
            )
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one guarded AuriX infrastructure pass")
    parser.add_argument("--once", action="store_true", help="run one bounded pass (default)")
    args = parser.parse_args()
    del args
    if not os.environ.get("DIGITALOCEAN_API_TOKEN", "").strip():
        print("infrastructure_worker: DIGITALOCEAN_API_TOKEN is not configured", file=sys.stderr)
        return 1
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("infrastructure_worker: another pass is running")
            return 0
        return run_once()


if __name__ == "__main__":
    raise SystemExit(main())
