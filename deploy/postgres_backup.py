#!/usr/bin/env python3
"""Encrypted PostgreSQL logical backups for AuriX recovery.

The application uses PostgreSQL as one durable control-plane database when
``COMMERCE_DATABASE_URL`` is set.  This module wraps the standard PostgreSQL
client tools without ever putting the database password in a command-line
argument.  Archives are encrypted before they are written locally or mirrored
to the configured off-site object store.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, unquote, urlsplit

from cryptography.fernet import Fernet, InvalidToken

try:
    from deploy.fleet_backup import truthy, write_private
    from deploy.fleet_reconcile import FleetError, load_dotenv
    from deploy import offsite_storage
except ImportError:  # Direct execution from deploy/.
    from fleet_backup import truthy, write_private
    from fleet_reconcile import FleetError, load_dotenv
    import offsite_storage


ARCHIVE_SUFFIX = ".pgdump.fernet"
MAX_RETENTION_COPIES = 3650


def backup_key(env: dict[str, str]) -> Fernet:
    value = env.get("AURIX_DATABASE_BACKUP_KEY") or env.get("AURIX_FLEET_BACKUP_KEY", "")
    try:
        return Fernet(value.encode())
    except (TypeError, ValueError) as exc:
        raise FleetError(
            "AURIX_DATABASE_BACKUP_KEY or AURIX_FLEET_BACKUP_KEY must be a valid Fernet key"
        ) from exc


def database_url(env: dict[str, str]) -> str:
    value = env.get("COMMERCE_DATABASE_URL", "").strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise FleetError("COMMERCE_DATABASE_URL must be a PostgreSQL URL")
    if parsed.username is None:
        raise FleetError("COMMERCE_DATABASE_URL must include a database user")
    if parsed.path in {"", "/"}:
        raise FleetError("COMMERCE_DATABASE_URL must include a database name")
    return value


def local_root(env: dict[str, str]) -> Path:
    return Path(env.get("AURIX_DATABASE_BACKUP_DIR", "/var/lib/aurix-bot/db-backups"))


def metadata_path(archive: Path) -> Path:
    return archive.with_suffix(archive.suffix + ".json")


def latest_archive(root: Path) -> Path:
    archives = sorted(root.glob(f"*{ARCHIVE_SUFFIX}"), reverse=True)
    if not archives:
        raise FleetError(f"no encrypted PostgreSQL backups found in {root}")
    return archives[0]


def prune(root: Path, keep: int) -> None:
    try:
        retention = int(keep)
    except (TypeError, ValueError) as exc:
        raise FleetError("AURIX_DATABASE_BACKUP_RETENTION must be an integer") from exc
    if not 1 <= retention <= MAX_RETENTION_COPIES:
        raise FleetError(
            f"AURIX_DATABASE_BACKUP_RETENTION must be between 1 and {MAX_RETENTION_COPIES}"
        )
    archives = sorted(root.glob(f"*{ARCHIVE_SUFFIX}"), reverse=True)
    for obsolete in archives[retention:]:
        obsolete.unlink(missing_ok=True)
        metadata_path(obsolete).unlink(missing_ok=True)


def _pgpass_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


@contextmanager
def _client_environment(url: str) -> Iterator[tuple[dict[str, str], list[str]]]:
    """Yield a safe client environment and connection arguments.

    PostgreSQL command-line clients expose their arguments through process
    listings.  We therefore pass host, port, user, and database separately and
    put only the decoded password in a mode-0600 temporary ``.pgpass`` file.
    Query parameters such as ``sslmode=require`` are forwarded through their
    documented environment variables.
    """
    parsed = urlsplit(url)
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    database = unquote(parsed.path.lstrip("/"))
    if not user or not database:
        raise FleetError("COMMERCE_DATABASE_URL user and database are required")
    host = parsed.hostname or ""
    port = str(parsed.port or 5432)
    environment = os.environ.copy()
    arguments = ["--host", host, "--port", port, "--username", user, "--dbname", database]
    temporary = Path(tempfile.mkdtemp(prefix="aurix-pgpass-"))
    pgpass = temporary / "pgpass"
    try:
        write_private(
            pgpass,
            f"{_pgpass_escape(host)}:{_pgpass_escape(port)}:{_pgpass_escape(database)}:"
            f"{_pgpass_escape(user)}:{_pgpass_escape(password)}\n".encode(),
        )
        environment["PGPASSFILE"] = str(pgpass)
        options = parse_qs(parsed.query, keep_blank_values=True)
        for key in ("sslmode", "sslrootcert", "sslcert", "sslkey", "connect_timeout"):
            values = options.get(key)
            if values:
                environment["PG" + key.upper()] = values[-1]
        yield environment, arguments
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _run(command: list[str], *, env: dict[str, str], timeout: int) -> None:
    try:
        result = subprocess.run(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FleetError(f"required PostgreSQL client is not installed: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise FleetError(f"{command[0]} timed out") from exc
    if result.returncode != 0:
        raise FleetError(f"{command[0]} failed with exit code {result.returncode}")


def _dump_bytes(url: str) -> bytes:
    temporary = Path(tempfile.mkdtemp(prefix="aurix-pgdump-"))
    dump = temporary / "commerce.dump"
    try:
        with _client_environment(url) as (environment, arguments):
            _run(
                ["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--file", str(dump), *arguments],
                env=environment,
                timeout=20 * 60,
            )
        if not dump.is_file() or dump.stat().st_size <= 0:
            raise FleetError("pg_dump produced an empty archive")
        return dump.read_bytes()
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _restore_bytes(url: str, raw: bytes, *, allow_existing: bool) -> None:
    temporary = Path(tempfile.mkdtemp(prefix="aurix-pgrestore-"))
    dump = temporary / "commerce.dump"
    try:
        write_private(dump, raw)
        with _client_environment(url) as (environment, arguments):
            command = [
                "pg_restore",
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--exit-on-error",
            ]
            if allow_existing:
                command.extend(("--clean", "--if-exists"))
            # For pg_restore, --file means the SQL output destination.  The
            # archive must be the final positional argument when restoring
            # directly into the database.
            command.extend((*arguments, str(dump)))
            _run(command, env=environment, timeout=20 * 60)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _verify_dump(raw: bytes) -> None:
    temporary = Path(tempfile.mkdtemp(prefix="aurix-pgverify-"))
    dump = temporary / "commerce.dump"
    try:
        write_private(dump, raw)
        with tempfile.NamedTemporaryFile() as listing:
            try:
                result = subprocess.run(
                    ["pg_restore", "--list", str(dump)],
                    stdout=listing,
                    stderr=subprocess.PIPE,
                    timeout=120,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise FleetError("required PostgreSQL client is not installed: pg_restore") from exc
            except subprocess.TimeoutExpired as exc:
                raise FleetError("pg_restore verification timed out") from exc
            if result.returncode != 0:
                raise FleetError("pg_restore could not inspect the archive")
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _verified_archive_bytes(
    env: dict[str, str], ciphertext: bytes, metadata_raw: bytes, label: str
) -> tuple[bytes, dict[str, Any], str]:
    try:
        metadata = json.loads(metadata_raw.decode("utf-8"))
        raw = backup_key(env).decrypt(ciphertext)
    except (UnicodeDecodeError, json.JSONDecodeError, InvalidToken) as exc:
        raise FleetError(f"PostgreSQL backup cannot be verified: {label}") from exc
    if not isinstance(metadata, dict) or metadata.get("format") != "postgres-custom":
        raise FleetError(f"PostgreSQL backup metadata is invalid: {label}")
    actual = hashlib.sha256(ciphertext).hexdigest()
    if metadata.get("ciphertext_sha256") != actual:
        raise FleetError(f"PostgreSQL backup hash mismatch: {label}")
    _verify_dump(raw)
    return raw, metadata, actual


def mirror_offsite(env: dict[str, str], archive: Path) -> None:
    if offsite_storage.configured(env):
        key = f"database/{archive.name}"
        offsite_storage.put(env, key, archive.read_bytes())
        offsite_storage.put(env, key + ".json", metadata_path(archive).read_bytes())
        offsite_storage.prune(
            env,
            "database/",
            ARCHIVE_SUFFIX,
            offsite_storage.retention_count(
                env.get("AURIX_DATABASE_BACKUP_OFFSITE_RETENTION"),
                "AURIX_DATABASE_BACKUP_OFFSITE_RETENTION",
                30,
            ),
        )
        return
    raw_root = env.get("AURIX_DATABASE_BACKUP_OFFSITE_DIR", "").strip()
    if not raw_root:
        if truthy(env.get("AURIX_DATABASE_BACKUP_REQUIRE_OFFSITE")):
            raise FleetError(
                "database offsite backups are required but AURIX_DATABASE_BACKUP_OFFSITE_DIR is empty"
            )
        return
    root = Path(raw_root).expanduser()
    if not root.is_absolute():
        raise FleetError("AURIX_DATABASE_BACKUP_OFFSITE_DIR must be absolute")
    destination = root / archive.name
    write_private(destination, archive.read_bytes())
    write_private(metadata_path(destination), metadata_path(archive).read_bytes())
    prune(
        root,
        offsite_storage.retention_count(
            env.get("AURIX_DATABASE_BACKUP_OFFSITE_RETENTION"),
            "AURIX_DATABASE_BACKUP_OFFSITE_RETENTION",
            30,
        ),
    )


def backup(env: dict[str, str]) -> Path:
    url = database_url(env)
    raw = _dump_bytes(url)
    ciphertext = backup_key(env).encrypt(raw)
    root = local_root(env)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = root / f"{stamp}{ARCHIVE_SUFFIX}"
    write_private(archive, ciphertext)
    metadata = {
        "format": "postgres-custom",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
        "plaintext_sha256": hashlib.sha256(raw).hexdigest(),
    }
    write_private(metadata_path(archive), (json.dumps(metadata, indent=2) + "\n").encode())
    prune(root, env.get("AURIX_DATABASE_BACKUP_RETENTION", "14"))
    mirror_offsite(env, archive)
    return archive


def _verify_archive(env: dict[str, str], archive: Path) -> dict[str, Any]:
    meta = metadata_path(archive)
    if not meta.is_file():
        raise FleetError(f"PostgreSQL backup metadata is missing: {meta}")
    _raw, metadata, actual = _verified_archive_bytes(
        env, archive.read_bytes(), meta.read_bytes(), str(archive)
    )
    return {
        "archive": str(archive),
        "created_at": metadata.get("created_at"),
        "ciphertext_sha256": actual,
    }


def _verify_archive_bytes(
    env: dict[str, str], ciphertext: bytes, metadata_raw: bytes, label: str
) -> dict[str, Any]:
    _raw, metadata, actual = _verified_archive_bytes(env, ciphertext, metadata_raw, label)
    return {
        "archive": label,
        "created_at": metadata.get("created_at"),
        "ciphertext_sha256": actual,
    }


def verify(env: dict[str, str]) -> dict[str, Any]:
    """Verify every available PostgreSQL recovery source.

    A rebuilt control plane commonly has no local backup directory yet.  In
    that case an authenticated off-site archive is sufficient evidence and
    must be checked directly instead of failing while looking for a local
    file first.  If a local archive exists, it is still verified so local
    corruption is never silently hidden by a healthy mirror.
    """
    result: dict[str, Any] = {}
    local = local_root(env)
    if local.is_dir() and any(local.glob(f"*{ARCHIVE_SUFFIX}")):
        result["local"] = _verify_archive(env, latest_archive(local))
    if offsite_storage.configured(env):
        key = offsite_storage.latest_key(env, "database/", ARCHIVE_SUFFIX)
        result["offsite"] = _verify_archive_bytes(
            env,
            offsite_storage.get(env, key),
            offsite_storage.get(env, key + ".json"),
            f"object://{key}",
        )
    else:
        raw_root = env.get("AURIX_DATABASE_BACKUP_OFFSITE_DIR", "").strip()
        if raw_root:
            result["offsite"] = _verify_archive(env, latest_archive(Path(raw_root).expanduser()))
        elif truthy(env.get("AURIX_DATABASE_BACKUP_REQUIRE_OFFSITE")):
            raise FleetError(
                "database offsite backups are required but AURIX_DATABASE_BACKUP_OFFSITE_DIR is empty"
            )
    if not result:
        raise FleetError(
            "no authenticated PostgreSQL backup archive is available for verification"
        )
    return result


def _latest_source(env: dict[str, str], archive: Path | None) -> tuple[bytes, str]:
    if archive is not None:
        if not archive.is_file():
            raise FleetError(f"PostgreSQL backup archive is not readable: {archive}")
        meta = metadata_path(archive)
        if not meta.is_file():
            raise FleetError(f"PostgreSQL backup metadata is missing: {meta}")
        raw, _metadata, _hash = _verified_archive_bytes(
            env, archive.read_bytes(), meta.read_bytes(), str(archive)
        )
        return raw, str(archive)
    failures: list[str] = []
    local = local_root(env)
    if local.is_dir() and any(local.glob(f"*{ARCHIVE_SUFFIX}")):
        selected = latest_archive(local)
        try:
            meta = metadata_path(selected)
            raw, _metadata, _hash = _verified_archive_bytes(
                env, selected.read_bytes(), meta.read_bytes(), str(selected)
            )
            return raw, str(selected)
        except (FleetError, OSError) as exc:
            failures.append(type(exc).__name__)
    if offsite_storage.configured(env):
        try:
            key = offsite_storage.latest_key(env, "database/", ARCHIVE_SUFFIX)
            raw, _metadata, _hash = _verified_archive_bytes(
                env,
                offsite_storage.get(env, key),
                offsite_storage.get(env, key + ".json"),
                f"object://{key}",
            )
            return raw, f"object://{key}"
        except (FleetError, OSError) as exc:
            failures.append(type(exc).__name__)
    raw_root = env.get("AURIX_DATABASE_BACKUP_OFFSITE_DIR", "").strip()
    if raw_root:
        try:
            selected = latest_archive(Path(raw_root).expanduser())
            raw, _metadata, _hash = _verified_archive_bytes(
                env, selected.read_bytes(), metadata_path(selected).read_bytes(), str(selected)
            )
            return raw, str(selected)
        except (FleetError, OSError) as exc:
            failures.append(type(exc).__name__)
    if failures:
        raise FleetError("no authenticated PostgreSQL backup archive is available for restore")
    raise FleetError("no PostgreSQL backup archive is available for restore")


def restore(
    env: dict[str, str], archive: Path | None = None, *, allow_existing: bool = False
) -> dict[str, Any]:
    raw, source = _latest_source(env, archive)
    _restore_bytes(database_url(env), raw, allow_existing=allow_existing)
    return {"source": source, "status": "restored"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backup", "verify", "restore"))
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--allow-existing", action="store_true")
    parser.add_argument(
        "--env-file",
        default=os.environ.get("AURIX_FLEET_ENV_FILE", "/etc/aurix-bot/aurix.env"),
    )
    args = parser.parse_args([] if argv is None else argv)
    try:
        env = load_dotenv(Path(args.env_file), overwrite=False)
        if args.command == "backup":
            print(json.dumps({"status": "complete", "archive": str(backup(env))}, indent=2))
        elif args.command == "verify":
            print(json.dumps({"status": "verified", **verify(env)}, indent=2))
        else:
            if args.archive is not None and not args.archive.is_absolute():
                raise FleetError("--archive must be an absolute path")
            print(
                json.dumps(
                    {
                        "status": "restored",
                        **restore(env, args.archive, allow_existing=args.allow_existing),
                    },
                    indent=2,
                )
            )
    except (FleetError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"PostgreSQL backup failed: {type(exc).__name__}: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(os.sys.argv[1:]))
