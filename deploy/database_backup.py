#!/usr/bin/env python3
"""Encrypted SQLite commerce database backups for control-plane recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy.fleet_backup import truthy, write_private  # noqa: E402
from deploy.fleet_reconcile import FleetError, load_dotenv  # noqa: E402
from deploy import offsite_storage  # noqa: E402


def backup_key(env: dict[str, str]) -> Fernet:
    key = env.get("AURIX_DATABASE_BACKUP_KEY") or env.get("AURIX_FLEET_BACKUP_KEY", "")
    try:
        return Fernet(key.encode())
    except (TypeError, ValueError) as exc:
        raise FleetError(
            "AURIX_DATABASE_BACKUP_KEY or AURIX_FLEET_BACKUP_KEY must be a valid Fernet key"
        ) from exc


def database_path(env: dict[str, str]) -> Path:
    if env.get("COMMERCE_DATABASE_URL", "").strip():
        raise FleetError("database_backup.py handles SQLite only; PostgreSQL needs provider backup")
    raw = env.get("DATABASE_PATH", "").strip()
    if not raw:
        raise FleetError("DATABASE_PATH is required for SQLite database backups")
    path = Path(raw)
    if not path.is_absolute():
        raise FleetError("DATABASE_PATH must be absolute")
    if not path.is_file():
        raise FleetError(f"DATABASE_PATH is not readable: {path}")
    return path


def local_root(env: dict[str, str]) -> Path:
    return Path(env.get("AURIX_DATABASE_BACKUP_DIR", "/var/lib/aurix-bot/db-backups"))


def offsite_root(env: dict[str, str]) -> Path | None:
    raw = env.get("AURIX_DATABASE_BACKUP_OFFSITE_DIR", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise FleetError("AURIX_DATABASE_BACKUP_OFFSITE_DIR must be absolute")
    return path


def metadata_path(archive: Path) -> Path:
    return archive.with_suffix(archive.suffix + ".json")


def latest_archive(root: Path) -> Path:
    archives = sorted(root.glob("*.sqlite3.fernet"), reverse=True)
    if not archives:
        raise FleetError(f"no encrypted database backups found in {root}")
    return archives[0]


def prune(root: Path, keep: int) -> None:
    archives = sorted(root.glob("*.sqlite3.fernet"), reverse=True)
    for obsolete in archives[keep:]:
        obsolete.unlink(missing_ok=True)
        metadata_path(obsolete).unlink(missing_ok=True)


def snapshot_sqlite(source: Path) -> bytes:
    temporary = Path(tempfile.mkdtemp(prefix="aurix-db-backup-")) / "snapshot.sqlite3"
    try:
        source_uri = f"file:{source}?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as source_connection:
            with sqlite3.connect(temporary) as destination:
                source_connection.backup(destination)
            with sqlite3.connect(temporary) as verify_connection:
                row = verify_connection.execute("PRAGMA integrity_check").fetchone()
                if not row or row[0] != "ok":
                    raise FleetError("SQLite integrity_check failed before encryption")
        return temporary.read_bytes()
    finally:
        shutil.rmtree(temporary.parent, ignore_errors=True)


def verify_archive(env: dict[str, str], archive: Path) -> dict[str, object]:
    meta = metadata_path(archive)
    if not meta.is_file():
        raise FleetError(f"database backup metadata is missing: {meta}")
    try:
        metadata = json.loads(meta.read_text(encoding="utf-8"))
        ciphertext = archive.read_bytes()
        raw = backup_key(env).decrypt(ciphertext)
    except (OSError, json.JSONDecodeError, InvalidToken) as exc:
        raise FleetError(f"database backup cannot be verified: {archive}") from exc
    expected = metadata.get("ciphertext_sha256")
    actual = hashlib.sha256(ciphertext).hexdigest()
    if expected != actual:
        raise FleetError(f"database backup hash mismatch: {archive}")
    temporary = Path(tempfile.mkdtemp(prefix="aurix-db-verify-")) / "verify.sqlite3"
    try:
        temporary.write_bytes(raw)
        with sqlite3.connect(temporary) as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                raise FleetError("database backup integrity_check failed")
    finally:
        shutil.rmtree(temporary.parent, ignore_errors=True)
    return {
        "archive": str(archive),
        "metadata": str(meta),
        "created_at": metadata.get("created_at"),
        "ciphertext_sha256": actual,
    }


def verify_archive_bytes(
    env: dict[str, str], ciphertext: bytes, metadata_raw: bytes, label: str
) -> dict[str, object]:
    try:
        metadata = json.loads(metadata_raw.decode())
        raw = backup_key(env).decrypt(ciphertext)
    except (UnicodeDecodeError, json.JSONDecodeError, InvalidToken) as exc:
        raise FleetError(f"database backup cannot be verified: {label}") from exc
    expected = metadata.get("ciphertext_sha256")
    actual = hashlib.sha256(ciphertext).hexdigest()
    if expected != actual:
        raise FleetError(f"database backup hash mismatch: {label}")
    temporary = Path(tempfile.mkdtemp(prefix="aurix-db-object-verify-")) / "verify.sqlite3"
    try:
        temporary.write_bytes(raw)
        with sqlite3.connect(temporary) as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                raise FleetError("database backup integrity_check failed")
    finally:
        shutil.rmtree(temporary.parent, ignore_errors=True)
    return {
        "archive": label,
        "metadata": f"{label}.json",
        "created_at": metadata.get("created_at"),
        "ciphertext_sha256": actual,
    }


def mirror_offsite(env: dict[str, str], archive: Path) -> Path | None:
    if offsite_storage.configured(env):
        object_key = f"database/{archive.name}"
        offsite_storage.put(env, object_key, archive.read_bytes())
        offsite_storage.put(env, object_key + ".json", metadata_path(archive).read_bytes())
        return None
    root = offsite_root(env)
    if root is None:
        if truthy(env.get("AURIX_DATABASE_BACKUP_REQUIRE_OFFSITE")):
            raise FleetError(
                "database offsite backups are required but AURIX_DATABASE_BACKUP_OFFSITE_DIR is empty"
            )
        return None
    destination = root / archive.name
    write_private(destination, archive.read_bytes())
    write_private(metadata_path(destination), metadata_path(archive).read_bytes())
    keep = max(1, int(env.get("AURIX_DATABASE_BACKUP_OFFSITE_RETENTION",
                              env.get("AURIX_DATABASE_BACKUP_RETENTION", "14"))))
    prune(root, keep)
    return destination


def backup(env: dict[str, str]) -> Path:
    raw = snapshot_sqlite(database_path(env))
    ciphertext = backup_key(env).encrypt(raw)
    root = local_root(env)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = root / f"{stamp}.sqlite3.fernet"
    write_private(archive, ciphertext)
    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
        "plaintext_sha256": hashlib.sha256(raw).hexdigest(),
        "database_path": str(database_path(env)),
    }
    write_private(metadata_path(archive), (json.dumps(metadata, indent=2) + "\n").encode())
    keep = max(1, int(env.get("AURIX_DATABASE_BACKUP_RETENTION", "14")))
    prune(root, keep)
    mirror_offsite(env, archive)
    return archive


def verify(env: dict[str, str]) -> dict[str, object]:
    result: dict[str, object] = {"local": verify_archive(env, latest_archive(local_root(env)))}
    if offsite_storage.configured(env):
        object_key = offsite_storage.latest_key(env, "database/", ".sqlite3.fernet")
        result["offsite"] = verify_archive_bytes(
            env,
            offsite_storage.get(env, object_key),
            offsite_storage.get(env, object_key + ".json"),
            f"object://{object_key}",
        )
    elif (remote_root := offsite_root(env)) is not None:
        result["offsite"] = verify_archive(env, latest_archive(remote_root))
    elif truthy(env.get("AURIX_DATABASE_BACKUP_REQUIRE_OFFSITE")):
        raise FleetError(
            "database offsite backups are required but AURIX_DATABASE_BACKUP_OFFSITE_DIR is empty"
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backup", "verify"))
    parser.add_argument("--env-file", default=os.environ.get(
        "AURIX_FLEET_ENV_FILE", "/etc/aurix-bot/aurix.env"))
    args = parser.parse_args()
    try:
        env = load_dotenv(Path(args.env_file), overwrite=False)
        if args.command == "backup":
            print(json.dumps({"status": "complete", "archive": str(backup(env))}, indent=2))
        else:
            print(json.dumps({"status": "verified", **verify(env)}, indent=2))
    except (FleetError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"database backup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
