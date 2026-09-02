#!/usr/bin/env python3
"""Encrypted backup and guarded restoration of server-bound Outline state."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from cryptography.fernet import Fernet, InvalidToken

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy.fleet_reconcile import (  # noqa: E402
    FleetError,
    FleetNode,
    environment,
    parse_manifest,
    read_identity,
    run_ssh,
    ssh_base,
)


def fernet(env: dict[str, str]) -> Fernet:
    key = env.get("AURIX_FLEET_BACKUP_KEY", "").encode()
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise FleetError("AURIX_FLEET_BACKUP_KEY must be a valid Fernet key") from exc


def validate_archive(raw: bytes) -> None:
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            names = []
            for member in archive.getmembers():
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                    raise FleetError("backup archive contains an unsafe path or link")
                if not path.parts or path.parts[0] not in {"access.txt", "persisted-state"}:
                    raise FleetError("backup archive contains an unexpected path")
                if member.isdev():
                    raise FleetError("backup archive contains a device")
                names.append(member.name)
            if "access.txt" not in names or not any(name.startswith("persisted-state/") for name in names):
                raise FleetError("backup archive is incomplete")
    except tarfile.TarError as exc:
        raise FleetError("backup archive is invalid") from exc


def select_nodes(nodes: list[FleetNode], requested: str) -> list[FleetNode]:
    if requested == "all":
        return nodes
    selected = [node for node in nodes if node.node_id == requested]
    if not selected:
        raise FleetError(f"unknown fleet node: {requested}")
    return selected


def backup_node(node: FleetNode, env: dict[str, str]) -> Path:
    limit_mb = int(env.get("AURIX_FLEET_BACKUP_MAX_MB", "256"))
    size = int(run_ssh(node, env, "du -sm /opt/outline/persisted-state | awk '{print $1}'").strip())
    if size > limit_mb:
        raise FleetError(f"node {node.node_id} state exceeds the configured backup limit")
    command = "tar --numeric-owner -C /opt/outline -czf - access.txt persisted-state"
    process = subprocess.run(
        # Binary stream is necessary here; run_ssh intentionally decodes text.
        ssh_base(node, env) + [command],
        capture_output=True, timeout=600, check=False,
    )
    if process.returncode:
        raise FleetError(f"node {node.node_id} backup stream failed")
    validate_archive(process.stdout)
    ciphertext = fernet(env).encrypt(process.stdout)
    root = Path(env.get("AURIX_FLEET_BACKUP_DIR", "/var/lib/aurix-fleet/backups")) / node.node_id
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = root / f"{stamp}.tar.gz.fernet"
    temporary = root / f".{destination.name}.tmp"
    temporary.write_bytes(ciphertext)
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)
    identity = read_identity(node, env)
    metadata = {
        "node_id": node.node_id,
        "created_at": datetime.now(UTC).isoformat(),
        "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
        "management_identity_sha256": hashlib.sha256(
            (identity["api_url"] + identity["cert_sha256"]).encode()
        ).hexdigest(),
    }
    destination.with_suffix(destination.suffix + ".json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(destination.with_suffix(destination.suffix + ".json"), 0o600)
    keep = max(1, int(env.get("AURIX_FLEET_BACKUP_RETENTION", "14")))
    archives = sorted(root.glob("*.tar.gz.fernet"), reverse=True)
    for obsolete in archives[keep:]:
        obsolete.unlink(missing_ok=True)
        obsolete.with_suffix(obsolete.suffix + ".json").unlink(missing_ok=True)
    return destination


def restore_node(node: FleetNode, env: dict[str, str], archive: Path) -> None:
    try:
        raw = fernet(env).decrypt(archive.read_bytes())
    except (OSError, InvalidToken) as exc:
        raise FleetError("backup cannot be read or authenticated") from exc
    validate_archive(raw)
    rollback = datetime.now(UTC).strftime("/opt/outline.rollback-%Y%m%dT%H%M%SZ")
    command = (
        "set -euo pipefail; "
        f"cp -a /opt/outline {rollback}; "
        "docker rm -f shadowbox >/dev/null 2>&1 || true; "
        "rm -rf /opt/outline/persisted-state /opt/outline/access.txt; "
        "tar -C /opt/outline -xzf -; "
        "bash /opt/outline/persisted-state/start_container.sh >/dev/null; "
        "test -s /opt/outline/access.txt"
    )
    process = subprocess.run(ssh_base(node, env) + [command], input=raw, capture_output=True,
                             timeout=600, check=False)
    if process.returncode:
        raise FleetError(f"node {node.node_id} restore failed; remote rollback is at {rollback}")
    read_identity(node, env)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--node", default="all")
    restore = sub.add_parser("restore")
    restore.add_argument("--node", required=True)
    restore.add_argument("--archive", required=True)
    restore.add_argument("--confirm-node", required=True)
    for command in (backup, restore):
        command.add_argument("--env-file", default=os.environ.get(
            "AURIX_FLEET_ENV_FILE", "/etc/aurix-bot/aurix.env"))
    args = parser.parse_args()
    try:
        env = environment(Path(args.env_file))
        nodes = parse_manifest(env.get("AURIX_FLEET_NODES_JSON", ""))
        if args.command == "backup":
            outputs = [str(backup_node(node, env)) for node in select_nodes(nodes, args.node)]
            print(json.dumps({"status": "complete", "archives": outputs}, indent=2))
        else:
            if args.confirm_node != args.node:
                raise FleetError("--confirm-node must exactly match --node")
            node = select_nodes(nodes, args.node)[0]
            restore_node(node, env, Path(args.archive))
            print(json.dumps({"status": "restored", "node": node.node_id}))
    except (FleetError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"fleet backup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
