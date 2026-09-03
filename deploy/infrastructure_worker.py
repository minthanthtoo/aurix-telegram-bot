#!/usr/bin/env python3
"""Run one bounded DigitalOcean infrastructure reconciliation pass.

This process is intentionally separate from the Telegram bot. Endpoint
activation is optional and fail-closed: the worker can commit it only after
the declared fleet reconciler proves the exact provider/IP/SSH identity.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Support both ``python -m deploy.infrastructure_worker`` (systemd) and the
# direct path form used by operators during a controlled dry run.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commerce import CommerceDatabase, PostgresCommerceDatabase
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


def _due_jobs(database: Any) -> list[dict[str, Any]]:
    now = datetime.now(UTC).isoformat()
    statuses = "'pending', 'running'"
    if _auto_activation_enabled():
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
        from deploy.fleet_reconcile import environment, parse_manifest

        env = environment(env_file)
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


def _auto_activate(
    controller: FleetController,
    job_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Run the declared fleet reconciler and commit activation if it succeeds."""
    if not _auto_activation_enabled() or result.get("status") != "awaiting_verification":
        return result
    env_file = Path(os.environ.get("AURIX_FLEET_ENV_FILE", "/etc/aurix-bot/aurix.env"))
    match = _declared_activation_node(
        provider_resource_id=str(result.get("provider_resource_id") or ""),
        public_ip=str(result.get("public_ip") or ""),
        env_file=env_file,
    )
    if match is None:
        print("infrastructure_worker: activation_waiting_for_pinned_manifest")
        return result
    node_id, _ = match
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
            env=dict(os.environ),
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
    controller = FleetController(database, DigitalOceanClient(token))
    inventory = controller.reconcile_provider_inventory()
    print(
        "infrastructure_worker: inventory="
        f"{inventory['managed']} managed/{inventory['matched']} registered"
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
