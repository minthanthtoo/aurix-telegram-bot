"""Create and verify AuriX database backup artifacts.

The SQLite path is self-contained and is suitable for a scheduled single-writer
deployment. It uses SQLite's online backup API, writes artifacts atomically, and
records logical table counts plus receipt-object paths for restore reconciliation.
The PostgreSQL path delegates to ``pg_dump``/``pg_restore`` without printing the
connection string; an isolated restore still requires an operator-owned target.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


UTC = timezone.utc
DEFAULT_REQUIRED_TABLES = ("users", "keys", "schema_migrations")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sqlite_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _temporary_path(directory: Path, prefix: str) -> Path:
    handle = tempfile.NamedTemporaryFile(prefix=prefix, dir=directory, delete=False)
    handle.close()
    return Path(handle.name)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = _temporary_path(path.parent, f".{path.name}.")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_sqlite_backup(source: Path, destination: Path) -> None:
    temporary = _temporary_path(destination.parent, f".{destination.name}.")
    source_connection: sqlite3.Connection | None = None
    target_connection: sqlite3.Connection | None = None
    try:
        source_connection = _sqlite_read_only(source)
        target_connection = sqlite3.connect(temporary)
        source_connection.backup(target_connection)
        target_connection.commit()
        target_connection.close()
        target_connection = None
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        if target_connection is not None:
            target_connection.close()
        if source_connection is not None:
            source_connection.close()
        temporary.unlink(missing_ok=True)


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {
        table: int(connection.execute(f' SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in tables
    }


def inspect_sqlite(path: Path) -> dict[str, Any]:
    """Return integrity, schema, row-count, and receipt-path evidence."""
    connection = _sqlite_read_only(path)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_errors = [
            tuple(row)
            for row in connection.execute("PRAGMA foreign_key_check").fetchall()
        ]
        table_counts = _table_counts(connection)
        receipt_paths: list[str] = []
        if "payment_evidence" in table_counts:
            receipt_paths = sorted(
                {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT storage_path FROM payment_evidence "
                        "WHERE storage_path IS NOT NULL AND storage_path != ''"
                    )
                }
            )
        return {
            "integrity_check": integrity,
            "foreign_key_errors": foreign_key_errors,
            "table_counts": table_counts,
            "receipt_path_count": len(receipt_paths),
            "receipt_paths": receipt_paths,
        }
    finally:
        connection.close()


def _load_receipt_inventory(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {line.strip() for line in text.splitlines() if line.strip()}
    if isinstance(value, dict):
        value = value.get("paths", value.get("objects", []))
    if not isinstance(value, list):
        raise ValueError("receipt inventory must be a JSON list/object or newline-delimited text")
    return {str(item).strip() for item in value if str(item).strip()}


def _receipt_reconciliation(
    expected: list[str], inventory_path: Path | None
) -> dict[str, Any]:
    result: dict[str, Any] = {"checked": False, "missing": [], "unexpected": []}
    if inventory_path is None:
        return result
    actual = _load_receipt_inventory(inventory_path)
    expected_set = set(expected)
    result.update(
        {
            "checked": True,
            "inventory_path": str(inventory_path),
            "missing": sorted(expected_set - actual),
            "unexpected": sorted(actual - expected_set),
        }
    )
    return result


def verify_sqlite(
    path: Path,
    *,
    manifest: Path | None = None,
    receipt_inventory: Path | None = None,
    check_hash: bool = True,
) -> dict[str, Any]:
    """Verify a SQLite artifact without opening it for writes."""
    evidence = inspect_sqlite(path)
    manifest_path = manifest or Path(f"{path}.manifest.json")
    manifest_value: dict[str, Any] | None = None
    if manifest_path.exists():
        manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
    hash_matches = True
    counts_match = True
    if manifest_value is not None:
        if check_hash:
            hash_matches = str(manifest_value.get("sha256")) == _sha256(path)
        counts_match = manifest_value.get("table_counts") == evidence["table_counts"]
    receipt_check = _receipt_reconciliation(evidence["receipt_paths"], receipt_inventory)
    required_tables = set(DEFAULT_REQUIRED_TABLES)
    missing_tables = sorted(required_tables - set(evidence["table_counts"]))
    ok = (
        evidence["integrity_check"].lower() == "ok"
        and not evidence["foreign_key_errors"]
        and not missing_tables
        and hash_matches
        and counts_match
        and not receipt_check["missing"]
    )
    return {
        "status": "ok" if ok else "failed",
        "artifact": str(path),
        "sha256": _sha256(path),
        "manifest": str(manifest_path) if manifest_value is not None else None,
        "hash_matches_manifest": hash_matches,
        "table_counts_match_manifest": counts_match,
        "missing_required_tables": missing_tables,
        "receipt_reconciliation": receipt_check,
        **evidence,
    }


def backup_sqlite(source: Path, output_dir: Path, name: str | None = None) -> dict[str, Any]:
    """Create one immutable SQLite backup plus its manifest."""
    if not source.exists():
        raise FileNotFoundError(source)
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = name or f"aurix-sqlite-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.db"
    destination = output_dir / filename
    manifest_path = Path(f"{destination}.manifest.json")
    if destination.exists() or manifest_path.exists():
        raise FileExistsError(destination)
    _atomic_sqlite_backup(source, destination)
    evidence = inspect_sqlite(destination)
    manifest = {
        "format": 1,
        "kind": "sqlite",
        "created_at": _now(),
        "backup_file": destination.name,
        "sha256": _sha256(destination),
        **evidence,
    }
    _atomic_json(manifest_path, manifest)
    return {"status": "ok", "artifact": str(destination), "manifest": str(manifest_path), **manifest}


def restore_sqlite(
    backup: Path,
    target: Path,
    *,
    overwrite: bool = False,
    receipt_inventory: Path | None = None,
) -> dict[str, Any]:
    """Restore a backup into an isolated SQLite path and verify its contents."""
    if target.exists() and not overwrite:
        raise FileExistsError(f"restore target exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(target.parent, f".{target.name}.")
    source_connection: sqlite3.Connection | None = None
    target_connection: sqlite3.Connection | None = None
    try:
        source_connection = _sqlite_read_only(backup)
        target_connection = sqlite3.connect(temporary)
        source_connection.backup(target_connection)
        target_connection.commit()
        target_connection.close()
        target_connection = None
        if target.exists():
            target.unlink()
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    finally:
        if target_connection is not None:
            target_connection.close()
        if source_connection is not None:
            source_connection.close()
        temporary.unlink(missing_ok=True)
    report = verify_sqlite(
        target,
        manifest=Path(f"{backup}.manifest.json"),
        receipt_inventory=receipt_inventory,
        check_hash=False,
    )
    report["restored_from"] = str(backup)
    return report


def backup_postgres(database_url: str, output: Path) -> dict[str, Any]:
    """Create a custom-format PostgreSQL backup without logging its URL."""
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(output)
    temporary = _temporary_path(output.parent, f".{output.name}.")
    try:
        command = [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            f"--file={temporary}",
            f"--dbname={database_url}",
        ]
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if result.returncode:
            raise RuntimeError(f"pg_dump failed with status {result.returncode}")
        os.replace(temporary, output)
        os.chmod(output, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return verify_postgres(output)


def verify_postgres(archive: Path) -> dict[str, Any]:
    """Verify that a PostgreSQL custom-format archive can be listed."""
    result = subprocess.run(
        ["pg_restore", "--list", str(archive)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(f"pg_restore --list failed with status {result.returncode}")
    entries = [line for line in result.stdout.splitlines() if line.strip() and not line.startswith(";")]
    return {
        "status": "ok",
        "artifact": str(archive),
        "sha256": _sha256(archive),
        "archive_entries": len(entries),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sqlite_backup = subparsers.add_parser("sqlite-backup")
    sqlite_backup.add_argument("--source", type=Path, required=True)
    sqlite_backup.add_argument("--output-dir", type=Path, required=True)
    sqlite_backup.add_argument("--name")

    sqlite_verify = subparsers.add_parser("sqlite-verify")
    sqlite_verify.add_argument("--backup", type=Path, required=True)
    sqlite_verify.add_argument("--receipt-inventory", type=Path)

    sqlite_restore = subparsers.add_parser("sqlite-restore")
    sqlite_restore.add_argument("--backup", type=Path, required=True)
    sqlite_restore.add_argument("--target", type=Path, required=True)
    sqlite_restore.add_argument("--overwrite", action="store_true")
    sqlite_restore.add_argument("--receipt-inventory", type=Path)

    postgres_backup = subparsers.add_parser("postgres-backup")
    postgres_backup.add_argument("--database-url-env", default="COMMERCE_DATABASE_URL")
    postgres_backup.add_argument("--output", type=Path, required=True)

    postgres_verify = subparsers.add_parser("postgres-verify")
    postgres_verify.add_argument("--archive", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "sqlite-backup":
        report = backup_sqlite(args.source, args.output_dir, args.name)
    elif args.command == "sqlite-verify":
        report = verify_sqlite(args.backup, receipt_inventory=args.receipt_inventory)
    elif args.command == "sqlite-restore":
        report = restore_sqlite(
            args.backup,
            args.target,
            overwrite=args.overwrite,
            receipt_inventory=args.receipt_inventory,
        )
    elif args.command == "postgres-backup":
        database_url = os.environ.get(args.database_url_env)
        if not database_url:
            raise SystemExit(f"Missing database URL environment variable: {args.database_url_env}")
        report = backup_postgres(database_url, args.output)
    else:
        report = verify_postgres(args.archive)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, FileExistsError, RuntimeError, ValueError) as exc:
        print(f"backup operation failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
