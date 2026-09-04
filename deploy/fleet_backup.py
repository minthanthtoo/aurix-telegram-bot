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
from datetime import datetime, timezone
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
from deploy import offsite_storage  # noqa: E402


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


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def backup_root(env: dict[str, str], node: FleetNode) -> Path:
    return Path(env.get("AURIX_FLEET_BACKUP_DIR", "/var/lib/aurix-fleet/backups")) / node.node_id


def offsite_root(env: dict[str, str], node: FleetNode) -> Path | None:
    raw = env.get("AURIX_FLEET_BACKUP_OFFSITE_DIR", "").strip()
    if not raw:
        return None
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise FleetError("AURIX_FLEET_BACKUP_OFFSITE_DIR must be an absolute path")
    return root / node.node_id


def metadata_path(archive: Path) -> Path:
    return archive.with_suffix(archive.suffix + ".json")


def write_private(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp"
    temporary.write_bytes(content)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def latest_archive(root: Path) -> Path:
    archives = sorted(root.glob("*.tar.gz.fernet"), reverse=True)
    if not archives:
        raise FleetError(f"no encrypted backups found in {root}")
    return archives[0]


def verify_archive(env: dict[str, str], archive: Path) -> dict[str, object]:
    metadata_file = metadata_path(archive)
    if not metadata_file.is_file():
        raise FleetError(f"backup metadata is missing: {metadata_file}")
    try:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        ciphertext = archive.read_bytes()
        raw = fernet(env).decrypt(ciphertext)
    except (OSError, json.JSONDecodeError, InvalidToken) as exc:
        raise FleetError(f"backup cannot be verified: {archive}") from exc
    validate_archive(raw)
    expected_hash = metadata.get("ciphertext_sha256")
    actual_hash = hashlib.sha256(ciphertext).hexdigest()
    if expected_hash != actual_hash:
        raise FleetError(f"backup hash mismatch: {archive}")
    return {
        "archive": str(archive),
        "metadata": str(metadata_file),
        "created_at": metadata.get("created_at"),
        "ciphertext_sha256": actual_hash,
    }


def verify_archive_bytes(
    env: dict[str, str], ciphertext: bytes, metadata_raw: bytes, label: str
) -> dict[str, object]:
    try:
        metadata = json.loads(metadata_raw.decode())
        raw = fernet(env).decrypt(ciphertext)
    except (UnicodeDecodeError, json.JSONDecodeError, InvalidToken) as exc:
        raise FleetError(f"backup cannot be verified: {label}") from exc
    validate_archive(raw)
    expected_hash = metadata.get("ciphertext_sha256")
    actual_hash = hashlib.sha256(ciphertext).hexdigest()
    if expected_hash != actual_hash:
        raise FleetError(f"backup hash mismatch: {label}")
    return {
        "archive": label,
        "metadata": f"{label}.json",
        "created_at": metadata.get("created_at"),
        "ciphertext_sha256": actual_hash,
    }


def prune_backups(root: Path, keep: int) -> None:
    archives = sorted(root.glob("*.tar.gz.fernet"), reverse=True)
    for obsolete in archives[keep:]:
        obsolete.unlink(missing_ok=True)
        metadata_path(obsolete).unlink(missing_ok=True)


def mirror_offsite(node: FleetNode, env: dict[str, str], archive: Path) -> Path | None:
    if offsite_storage.configured(env):
        object_key = f"fleet/{node.node_id}/{archive.name}"
        offsite_storage.put(env, object_key, archive.read_bytes())
        offsite_storage.put(env, object_key + ".json", metadata_path(archive).read_bytes())
        offsite_storage.prune(
            env,
            f"fleet/{node.node_id}/",
            ".tar.gz.fernet",
            offsite_storage.retention_count(
                env.get("AURIX_FLEET_BACKUP_OFFSITE_RETENTION"),
                "AURIX_FLEET_BACKUP_OFFSITE_RETENTION",
                30,
            ),
        )
        return None
    root = offsite_root(env, node)
    if root is None:
        if truthy(env.get("AURIX_FLEET_BACKUP_REQUIRE_OFFSITE")):
            raise FleetError("offsite backups are required but AURIX_FLEET_BACKUP_OFFSITE_DIR is empty")
        return None
    metadata_file = metadata_path(archive)
    destination = root / archive.name
    write_private(destination, archive.read_bytes())
    write_private(metadata_path(destination), metadata_file.read_bytes())
    keep = offsite_storage.retention_count(
        env.get(
            "AURIX_FLEET_BACKUP_OFFSITE_RETENTION",
            env.get("AURIX_FLEET_BACKUP_RETENTION", "14"),
        ),
        "AURIX_FLEET_BACKUP_OFFSITE_RETENTION",
        14,
    )
    prune_backups(root, keep)
    return destination


def backup_node(node: FleetNode, env: dict[str, str]) -> Path:
    limit_mb = int(env.get("AURIX_FLEET_BACKUP_MAX_MB", "256"))
    select_dir = (
        "d=/opt/outline; test -s /root/shadowbox/access.txt && d=/root/shadowbox; "
    )
    size = int(run_ssh(
        node, env, select_dir + "du -sm \"$d/persisted-state\" | awk '{print $1}'"
    ).strip())
    if size > limit_mb:
        raise FleetError(f"node {node.node_id} state exceeds the configured backup limit")
    command = select_dir + "tar --numeric-owner -C \"$d\" -czf - access.txt persisted-state"
    process = subprocess.run(
        # Binary stream is necessary here; run_ssh intentionally decodes text.
        ssh_base(node, env) + [command],
        capture_output=True, timeout=600, check=False,
    )
    if process.returncode:
        raise FleetError(f"node {node.node_id} backup stream failed")
    validate_archive(process.stdout)
    ciphertext = fernet(env).encrypt(process.stdout)
    root = backup_root(env, node)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = root / f"{stamp}.tar.gz.fernet"
    write_private(destination, ciphertext)
    identity = read_identity(node, env)
    metadata = {
        "node_id": node.node_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
        "management_identity_sha256": hashlib.sha256(
            (identity["api_url"] + identity["cert_sha256"]).encode()
        ).hexdigest(),
    }
    write_private(metadata_path(destination), (json.dumps(metadata, indent=2) + "\n").encode())
    keep = offsite_storage.retention_count(
        env.get("AURIX_FLEET_BACKUP_RETENTION"),
        "AURIX_FLEET_BACKUP_RETENTION",
        14,
    )
    prune_backups(root, keep)
    mirror_offsite(node, env, destination)
    return destination


def verify_node(node: FleetNode, env: dict[str, str]) -> dict[str, object]:
    """Verify every available recovery source for a node.

    A rebuilt control plane may have only the off-site copy of a node's
    Shadowbox state.  Do not require a local archive before checking the
    configured mirror; when local state is present, still verify it so
    corruption cannot be silently masked by an off-site copy.
    """
    result: dict[str, object] = {"node": node.node_id}
    local_root = backup_root(env, node)
    if local_root.is_dir() and any(local_root.glob("*.tar.gz.fernet")):
        result["local"] = verify_archive(env, latest_archive(local_root))
    if offsite_storage.configured(env):
        object_key = offsite_storage.latest_key(
            env, f"fleet/{node.node_id}/", ".tar.gz.fernet"
        )
        result["offsite"] = verify_archive_bytes(
            env,
            offsite_storage.get(env, object_key),
            offsite_storage.get(env, object_key + ".json"),
            f"object://{object_key}",
        )
    elif (remote_root := offsite_root(env, node)) is not None:
        result["offsite"] = verify_archive(env, latest_archive(remote_root))
    elif truthy(env.get("AURIX_FLEET_BACKUP_REQUIRE_OFFSITE")):
        raise FleetError("offsite backups are required but AURIX_FLEET_BACKUP_OFFSITE_DIR is empty")
    if len(result) == 1:
        raise FleetError(
            f"no authenticated backup archive is available for node {node.node_id}"
        )
    return result


def restore_node(node: FleetNode, env: dict[str, str], archive: Path) -> None:
    try:
        raw = fernet(env).decrypt(archive.read_bytes())
    except (OSError, InvalidToken) as exc:
        raise FleetError("backup cannot be read or authenticated") from exc
    validate_archive(raw)
    rollback_suffix = datetime.now(timezone.utc).strftime(".rollback-%Y%m%dT%H%M%SZ")
    command = (
        "set -euo pipefail; d=/opt/outline; "
        "test -s /root/shadowbox/access.txt && d=/root/shadowbox; "
        f"cp -a \"$d\" \"$d{rollback_suffix}\"; "
        "docker rm -f shadowbox >/dev/null 2>&1 || true; "
        "rm -rf \"$d/persisted-state\" \"$d/access.txt\"; "
        "tar -C \"$d\" -xzf -; "
        "bash \"$d/persisted-state/start_container.sh\" >/dev/null; "
        "test -s \"$d/access.txt\""
    )
    process = subprocess.run(ssh_base(node, env) + [command], input=raw, capture_output=True,
                             timeout=600, check=False)
    if process.returncode:
        raise FleetError(
            f"node {node.node_id} restore failed; remote rollback has suffix {rollback_suffix}"
        )
    read_identity(node, env)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--node", default="all")
    verify = sub.add_parser("verify")
    verify.add_argument("--node", default="all")
    restore = sub.add_parser("restore")
    restore.add_argument("--node", required=True)
    restore.add_argument("--archive", required=True)
    restore.add_argument("--confirm-node", required=True)
    for command in (backup, verify, restore):
        command.add_argument("--env-file", default=os.environ.get(
            "AURIX_FLEET_ENV_FILE", "/etc/aurix-bot/aurix.env"))
    args = parser.parse_args()
    try:
        env = environment(Path(args.env_file))
        nodes = parse_manifest(env.get("AURIX_FLEET_NODES_JSON", ""))
        if args.command == "backup":
            outputs = [str(backup_node(node, env)) for node in select_nodes(nodes, args.node)]
            print(json.dumps({"status": "complete", "archives": outputs}, indent=2))
        elif args.command == "verify":
            outputs = [verify_node(node, env) for node in select_nodes(nodes, args.node)]
            print(json.dumps({"status": "verified", "nodes": outputs}, indent=2))
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
