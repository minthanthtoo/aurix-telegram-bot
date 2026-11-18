#!/usr/bin/env python3
"""Create, verify, or restore a private AuriX Games data backup."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import stat
import sys

from aurix_games.backup_encryption import (
    create_encrypted_backup,
    restore_encrypted_backup,
    verify_encrypted_backup,
)
from aurix_games.backup_restore import (
    GamesBackupError,
    create_backup,
    restore_backup,
    verify_backup,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="snapshot one stopped service data directory")
    backup.add_argument("--source", required=True, type=Path)
    backup.add_argument("--output", required=True, type=Path)
    verify = commands.add_parser("verify", help="check backup hashes and SQLite integrity")
    verify.add_argument("--backup", required=True, type=Path)
    restore = commands.add_parser("restore", help="restore to a new, empty path")
    restore.add_argument("--backup", required=True, type=Path)
    restore.add_argument("--target", required=True, type=Path)
    encrypted = commands.add_parser(
        "backup-encrypted", help="create one authenticated encrypted archive"
    )
    encrypted.add_argument("--source", required=True, type=Path)
    encrypted.add_argument("--output", required=True, type=Path)
    _add_passphrase_file(encrypted)
    verify_encrypted = commands.add_parser(
        "verify-encrypted", help="authenticate and validate an encrypted archive"
    )
    verify_encrypted.add_argument("--backup", required=True, type=Path)
    _add_passphrase_file(verify_encrypted)
    restore_encrypted = commands.add_parser(
        "restore-encrypted", help="restore an encrypted archive to a new path"
    )
    restore_encrypted.add_argument("--backup", required=True, type=Path)
    restore_encrypted.add_argument("--target", required=True, type=Path)
    _add_passphrase_file(restore_encrypted)
    return parser


def _add_passphrase_file(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--passphrase-file",
        type=Path,
        help="read a passphrase from a private file (mode 0600); otherwise prompt",
    )


def _passphrase(args: argparse.Namespace, *, confirm: bool = False) -> str:
    passphrase_file = args.passphrase_file
    if passphrase_file is not None:
        path = Path(passphrase_file).expanduser()
        if path.is_symlink():
            raise GamesBackupError("passphrase file must not be a symlink")
        try:
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode):
                raise GamesBackupError("passphrase path is not a regular file")
            if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
                raise GamesBackupError("passphrase file permissions must be 0600 or stricter")
            value = path.read_text(encoding="utf-8").rstrip("\r\n")
        except (OSError, UnicodeError) as error:
            raise GamesBackupError("passphrase file could not be read") from error
        return value

    value = getpass.getpass("Backup passphrase: ")
    if confirm:
        repeated = getpass.getpass("Repeat backup passphrase: ")
        if value != repeated:
            raise GamesBackupError("passphrases do not match")
    return value


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "backup":
            result = create_backup(args.source, args.output)
            result["backup_path"] = str(args.output.expanduser().resolve())
        elif args.command == "verify":
            result = verify_backup(args.backup)
        elif args.command == "restore":
            result = restore_backup(args.backup, args.target)
        elif args.command == "backup-encrypted":
            result = create_encrypted_backup(
                args.source, args.output, _passphrase(args, confirm=True)
            )
        elif args.command == "verify-encrypted":
            result = verify_encrypted_backup(args.backup, _passphrase(args))
        else:
            result = restore_encrypted_backup(
                args.backup, args.target, _passphrase(args)
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (GamesBackupError, OSError) as error:
        print(f"Games backup operation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
