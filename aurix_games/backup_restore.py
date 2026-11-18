"""Private, verifiable backup and fresh-target restore for Games state.

Backups contain supplier catalog identifiers/costs and potentially encrypted
customer/fulfillment records. They are created with private filesystem modes;
the script never includes secret values. Stop the service before backing up to
keep the group of independent stores mutually consistent.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile
from typing import Any


MANIFEST_NAME = "aurix-games-backup.json"
MANIFEST_HASH_NAME = "aurix-games-backup.sha256"
MANIFEST_SCHEMA_VERSION = 1
_SQLITE_HEADER = b"SQLite format 3\x00"
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_SECRET_ENV_NAMES = (
    "AURIX_GAMES_ADMIN_TOKEN",
    "AURIX_GAMES_CUSTOMER_AUTH_SECRET",
    "AURIX_GAMES_FULFILLMENT_KEY",
    "AURIX_GAMES_PAYMENT_EVIDENCE_KEY",
    "AURIX_GAMES_PAYMENT_REVIEWERS",
    "AURIX_GAMES_RECIPIENT_VAULT_KEY",
    "AURIX_GAMES_CHECKOUT_FINGERPRINT_SECRET",
    "FAZERCARDS_API_KEY",
    "FAZERCARDS_WEBHOOK_SECRET",
)


class GamesBackupError(RuntimeError):
    """A backup is invalid, incomplete, or unsafe to restore."""


def create_backup(source: Path | str, output: Path | str) -> dict[str, Any]:
    """Create a new private backup directory without replacing existing data."""
    source_path = _source_root(source)
    output_candidate = Path(output).expanduser()
    if output_candidate.is_symlink():
        raise GamesBackupError("backup output must not be a symlink")
    output_path = output_candidate.resolve()
    if output_path.exists() or output_path.is_symlink():
        raise GamesBackupError("backup output already exists; choose a new path")
    _require_outside(source_path, output_path, "backup output")
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    files = _source_files(source_path)
    sqlite_files = {path for path in files if _is_sqlite(path)}
    sidecars = {
        Path(f"{path}{suffix}").relative_to(source_path).as_posix()
        for path in sqlite_files
        for suffix in _SIDECAR_SUFFIXES
    }

    with tempfile.TemporaryDirectory(
        prefix=f".{output_path.name}.backup-", dir=output_path.parent
    ) as temporary:
        staged_root = Path(temporary) / "payload"
        staged_root.mkdir(mode=0o700)
        entries: list[dict[str, Any]] = []
        for source_file in files:
            relative = source_file.relative_to(source_path)
            relative_name = relative.as_posix()
            if relative_name in sidecars:
                continue
            staged_file = _target_file(staged_root, relative)
            if source_file in sqlite_files:
                _sqlite_snapshot(source_file, staged_file)
                kind = "sqlite"
            else:
                shutil.copyfile(source_file, staged_file)
                os.chmod(staged_file, 0o600)
                kind = "file"
            entries.append(_file_entry(staged_file, relative_name, kind))

        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "application": "aurix-games",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_name": source_path.name,
            "secret_values_included": False,
            "configured_secret_env_names": [
                name for name in _SECRET_ENV_NAMES if os.environ.get(name)
            ],
            "files": entries,
        }
        manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
        (staged_root / MANIFEST_NAME).write_bytes(manifest_bytes)
        (staged_root / MANIFEST_HASH_NAME).write_text(
            hashlib.sha256(manifest_bytes).hexdigest() + "\n", encoding="ascii"
        )
        os.chmod(staged_root / MANIFEST_NAME, 0o600)
        os.chmod(staged_root / MANIFEST_HASH_NAME, 0o600)
        _verify_backup_root(staged_root)

        # The final path appears only after the complete staged copy verifies.
        if output_path.exists() or output_path.is_symlink():
            raise GamesBackupError("backup output appeared during creation")
        os.rename(staged_root, output_path)

    return _summary(manifest)


def verify_backup(backup: Path | str) -> dict[str, Any]:
    """Verify the manifest, every file hash, and each SQLite integrity check."""
    return _summary(_verify_backup_root(_backup_root(backup)))


def restore_backup(backup: Path | str, target: Path | str) -> dict[str, Any]:
    """Restore to a new directory; never merge with or replace an existing one."""
    backup_path = _backup_root(backup)
    manifest = _verify_backup_root(backup_path)
    target_candidate = Path(target).expanduser()
    if target_candidate.is_symlink():
        raise GamesBackupError("restore target must not be a symlink")
    target_path = target_candidate.resolve()
    if target_path.exists() or target_path.is_symlink():
        raise GamesBackupError("restore target already exists; restore only to a fresh path")
    _require_outside(backup_path, target_path, "restore target")
    target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    with tempfile.TemporaryDirectory(
        prefix=f".{target_path.name}.restore-", dir=target_path.parent
    ) as temporary:
        staged_root = Path(temporary) / "payload"
        staged_root.mkdir(mode=0o700)
        for entry in manifest["files"]:
            relative = _safe_relative_path(entry["path"])
            source_file = backup_path.joinpath(*relative.parts)
            staged_file = _target_file(staged_root, relative)
            shutil.copyfile(source_file, staged_file)
            os.chmod(staged_file, 0o600)
            if _file_entry(staged_file, entry["path"], entry["kind"]) != entry:
                raise GamesBackupError("restored file did not match its verified backup")
            if entry["kind"] == "sqlite":
                _check_sqlite(staged_file)
        if target_path.exists() or target_path.is_symlink():
            raise GamesBackupError("restore target appeared during restoration")
        os.rename(staged_root, target_path)

    return {
        **_summary(manifest),
        "restored_to": str(target_path),
        "secret_values_restored": False,
    }


def _source_root(value: Path | str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise GamesBackupError("source directory must not be a symlink")
    try:
        root = candidate.resolve(strict=True)
    except OSError as error:
        raise GamesBackupError("source directory is unavailable") from error
    if not root.is_dir() or root == root.parent:
        raise GamesBackupError("source must be a non-root directory")
    return root


def _backup_root(value: Path | str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise GamesBackupError("backup directory must not be a symlink")
    try:
        root = candidate.resolve(strict=True)
    except OSError as error:
        raise GamesBackupError("backup directory is unavailable") from error
    if not root.is_dir():
        raise GamesBackupError("backup path is not a directory")
    return root


def _source_files(root: Path) -> list[Path]:
    result: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise GamesBackupError("source contains a symlink; refusing an ambiguous backup")
        if path.is_dir():
            continue
        if not path.is_file():
            raise GamesBackupError("source contains a non-regular file")
        result.append(path)
    return sorted(result, key=lambda item: item.relative_to(root).as_posix())


def _require_outside(container: Path, candidate: Path, label: str) -> None:
    if candidate == container:
        raise GamesBackupError(f"{label} must be a different directory")
    try:
        candidate.relative_to(container)
    except ValueError:
        return
    raise GamesBackupError(f"{label} must be outside the data directory")


def _safe_relative_path(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise GamesBackupError("backup manifest contains an invalid relative path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or "." in relative.parts
        or relative.as_posix() != value
        or value in {MANIFEST_NAME, MANIFEST_HASH_NAME}
    ):
        raise GamesBackupError("backup manifest contains an unsafe relative path")
    return relative


def _target_file(root: Path, relative: PurePosixPath | Path) -> Path:
    parts = relative.parts
    if not parts:
        raise GamesBackupError("backup file path is empty")
    current = root
    for part in parts[:-1]:
        current = current / part
        current.mkdir(mode=0o700, exist_ok=True)
        if current.is_symlink() or not current.is_dir():
            raise GamesBackupError("backup target contains an unsafe directory path")
        os.chmod(current, 0o700)
    target = current / parts[-1]
    if target.exists() or target.is_symlink():
        raise GamesBackupError("backup contains duplicate file paths")
    return target


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError as error:
        raise GamesBackupError("source file could not be inspected") from error


def _sqlite_snapshot(source: Path, target: Path) -> None:
    source_uri = source.resolve(strict=True).as_uri() + "?mode=ro"
    try:
        source_db = sqlite3.connect(source_uri, uri=True, timeout=20)
        try:
            target_db = sqlite3.connect(target, timeout=20)
            try:
                source_db.backup(target_db)
                target_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                journal_mode = target_db.execute("PRAGMA journal_mode=DELETE").fetchone()
                if journal_mode != ("delete",):
                    raise GamesBackupError("SQLite snapshot could not be consolidated")
                _check_sqlite_connection(target_db)
            finally:
                target_db.close()
        finally:
            source_db.close()
        for suffix in _SIDECAR_SUFFIXES:
            Path(f"{target}{suffix}").unlink(missing_ok=True)
    except sqlite3.Error as error:
        target.unlink(missing_ok=True)
        raise GamesBackupError("SQLite snapshot could not be created") from error
    os.chmod(target, 0o600)


def _check_sqlite(path: Path) -> None:
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=20)
        try:
            _check_sqlite_connection(connection)
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise GamesBackupError("SQLite integrity check failed") from error


def _check_sqlite_connection(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    if rows != [("ok",)]:
        raise GamesBackupError("SQLite integrity check failed")


def _file_entry(path: Path, relative_name: str, kind: str) -> dict[str, Any]:
    return {
        "path": relative_name,
        "kind": kind,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_backup_root(root: Path) -> dict[str, Any]:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise GamesBackupError("backup contains a symlink")
        if not path.is_file() and not path.is_dir():
            raise GamesBackupError("backup contains a non-regular file")
    manifest_path = root / MANIFEST_NAME
    digest_path = root / MANIFEST_HASH_NAME
    if manifest_path.is_symlink() or digest_path.is_symlink():
        raise GamesBackupError("backup manifest files must not be symlinks")
    try:
        manifest_bytes = manifest_path.read_bytes()
        declared_digest = digest_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as error:
        raise GamesBackupError("backup manifest is incomplete") from error
    if len(manifest_bytes) > 8 * 1024 * 1024 or not re.fullmatch(r"[0-9a-f]{64}", declared_digest):
        raise GamesBackupError("backup manifest checksum is malformed")
    if hashlib.sha256(manifest_bytes).hexdigest() != declared_digest:
        raise GamesBackupError("backup manifest checksum does not match")
    try:
        manifest = json.loads(manifest_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise GamesBackupError("backup manifest is invalid JSON") from error
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {
            "schema_version", "application", "created_at", "source_name",
            "secret_values_included", "configured_secret_env_names", "files",
        }
        or manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or manifest.get("application") != "aurix-games"
        or manifest.get("secret_values_included") is not False
        or not isinstance(manifest.get("created_at"), str)
        or not manifest.get("created_at")
        or not isinstance(manifest.get("source_name"), str)
        or not manifest.get("source_name")
        or not isinstance(manifest.get("configured_secret_env_names"), list)
        or any(name not in _SECRET_ENV_NAMES for name in manifest.get("configured_secret_env_names", []))
        or not isinstance(manifest.get("files"), list)
        or len(manifest["files"]) > 100_000
    ):
        raise GamesBackupError("backup manifest schema is unsupported")

    expected_files: set[str] = set()
    expected_directories: set[str] = set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "kind", "size_bytes", "sha256"}:
            raise GamesBackupError("backup manifest file record is malformed")
        relative = _safe_relative_path(entry["path"])
        name = relative.as_posix()
        if name in expected_files:
            raise GamesBackupError("backup manifest contains duplicate paths")
        if entry["kind"] not in {"file", "sqlite"}:
            raise GamesBackupError("backup manifest file kind is invalid")
        if (
            isinstance(entry["size_bytes"], bool)
            or not isinstance(entry["size_bytes"], int)
            or entry["size_bytes"] < 0
            or not isinstance(entry["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        ):
            raise GamesBackupError("backup manifest file metadata is invalid")
        expected_files.add(name)
        expected_directories.update(parent.as_posix() for parent in relative.parents if parent.as_posix() != ".")
        file_path = root.joinpath(*relative.parts)
        if file_path.is_symlink() or not file_path.is_file():
            raise GamesBackupError("backup is missing a regular manifest-listed file")
        if file_path.stat().st_size != entry["size_bytes"] or _sha256(file_path) != entry["sha256"]:
            raise GamesBackupError("backup file hash or size does not match the manifest")
        if entry["kind"] == "sqlite":
            _check_sqlite(file_path)
        elif _is_sqlite(file_path):
            raise GamesBackupError("SQLite file is mislabeled in the backup manifest")

    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise GamesBackupError("backup contains a symlink")
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            actual_directories.add(relative)
        elif path.is_file():
            actual_files.add(relative)
        else:
            raise GamesBackupError("backup contains a non-regular file")
    reserved_files = {MANIFEST_NAME, MANIFEST_HASH_NAME}
    if actual_files != expected_files | reserved_files or actual_directories != expected_directories:
        raise GamesBackupError("backup contains missing or unexpected files")
    return manifest


def _summary(manifest: dict[str, Any]) -> dict[str, Any]:
    files = manifest["files"]
    return {
        "status": "verified",
        "application": "aurix-games",
        "created_at": manifest.get("created_at"),
        "file_count": len(files),
        "sqlite_count": sum(item["kind"] == "sqlite" for item in files),
        "total_bytes": sum(item["size_bytes"] for item in files),
        "secret_values_included": False,
        "configured_secret_env_names": list(manifest.get("configured_secret_env_names", [])),
    }
