"""Authenticated encrypted archive support for AuriX Games backups.

Encrypted archives use scrypt-derived AES-256-GCM keys and a compressed tar
container. The archive is authenticated before any file is extracted or
restored. Passphrases are never serialized or logged.
"""

from __future__ import annotations

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import tarfile
import tempfile
from typing import Any, BinaryIO

from .backup_restore import (
    MANIFEST_HASH_NAME,
    MANIFEST_NAME,
    GamesBackupError,
    _backup_root,
    _file_entry,
    _require_outside,
    _safe_relative_path,
    _source_root,
    _summary,
    _target_file,
    _verify_backup_root,
    create_backup,
)


ENCRYPTED_BACKUP_MAGIC = b"AURIXGB1"
_SALT_BYTES = 16
_NONCE_BYTES = 12
_TAG_BYTES = 16
_HEADER_BYTES = len(ENCRYPTED_BACKUP_MAGIC) + _SALT_BYTES + _NONCE_BYTES
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_MAX_ARCHIVE_FILES = 100_002
_MAX_ARCHIVE_PLAIN_BYTES = 16 * 1024**3
_ARCHIVE_CHUNK_BYTES = 1024 * 1024
_MANIFEST_FILES = {MANIFEST_NAME, MANIFEST_HASH_NAME}


class _EncryptedWriter:
    """File-like sink for streaming tar/gzip bytes through AES-GCM."""

    def __init__(self, destination: BinaryIO, encryptor: Any) -> None:
        self._destination = destination
        self._encryptor = encryptor
        self._closed = False

    def write(self, value: bytes) -> int:
        if self._closed:
            raise ValueError("encrypted archive writer is closed")
        encrypted = self._encryptor.update(value)
        if encrypted:
            written = self._destination.write(encrypted)
            if written != len(encrypted):
                raise OSError("encrypted archive write was incomplete")
        return len(value)

    def flush(self) -> None:
        self._destination.flush()

    def close(self) -> None:
        if self._closed:
            return
        final_bytes = self._encryptor.finalize()
        if final_bytes:
            self._destination.write(final_bytes)
        self._destination.write(self._encryptor.tag)
        self._destination.flush()
        self._closed = True


def create_encrypted_backup(
    source: Path | str,
    output: Path | str,
    passphrase: str,
) -> dict[str, Any]:
    """Create a verified encrypted backup file, leaving no plaintext snapshot."""
    _passphrase_bytes(passphrase)
    source_path = _source_root(source)
    output_path = _new_output_file(output)
    _require_outside(source_path, output_path, "encrypted backup output")
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    # SQLite needs a snapshot copy. Keep that short-lived plaintext staging
    # directory private and on the same protected volume as the destination.
    with tempfile.TemporaryDirectory(
        prefix=f".{output_path.name}.work-", dir=output_path.parent
    ) as temporary:
        snapshot_path = Path(temporary) / "snapshot"
        create_backup(source_path, snapshot_path)
        manifest = _verify_backup_root(snapshot_path)
        _seal_verified_backup(snapshot_path, output_path, passphrase, manifest)

    return {
        **_summary(manifest),
        "encrypted": True,
        "cipher": "AES-256-GCM",
        "kdf": "scrypt",
        "backup_path": str(output_path),
        "plaintext_staging_removed": True,
    }


def verify_encrypted_backup(
    backup: Path | str, passphrase: str
) -> dict[str, Any]:
    """Authenticate and validate an encrypted archive without restoring it."""
    backup_path = _input_file(backup)
    with tempfile.TemporaryDirectory(prefix="aurix-games-verify-") as temporary:
        work = Path(temporary)
        archive_path = work / "authenticated.tar.gz"
        _decrypt_to_file(backup_path, archive_path, passphrase)
        payload_path = work / "snapshot"
        payload_path.mkdir(mode=0o700)
        manifest = _extract_and_verify(archive_path, payload_path)
    return {
        **_summary(manifest),
        "encrypted": True,
        "cipher": "AES-256-GCM",
        "kdf": "scrypt",
    }


def restore_encrypted_backup(
    backup: Path | str, target: Path | str, passphrase: str
) -> dict[str, Any]:
    """Authenticate, validate, then restore an archive to a new directory."""
    backup_path = _input_file(backup)
    target_candidate = Path(target).expanduser()
    if target_candidate.is_symlink():
        raise GamesBackupError("restore target must not be a symlink")
    target_path = target_candidate.resolve()
    if target_path.exists() or target_path.is_symlink():
        raise GamesBackupError("restore target already exists; restore only to a fresh path")
    _require_outside(backup_path, target_path, "restore target")
    target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    with tempfile.TemporaryDirectory(
        prefix=f".{target_path.name}.encrypted-restore-", dir=target_path.parent
    ) as temporary:
        work = Path(temporary)
        archive_path = work / "authenticated.tar.gz"
        _decrypt_to_file(backup_path, archive_path, passphrase)
        staged_path = work / "payload"
        staged_path.mkdir(mode=0o700)
        manifest = _extract_and_verify(archive_path, staged_path)
        if target_path.exists() or target_path.is_symlink():
            raise GamesBackupError("restore target appeared during restoration")
        os.rename(staged_path, target_path)

    return {
        **_summary(manifest),
        "encrypted": True,
        "restored_to": str(target_path),
        "secret_values_restored": False,
    }


def _new_output_file(value: Path | str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise GamesBackupError("encrypted backup output must not be a symlink")
    result = candidate.resolve()
    if result.exists() or result.is_symlink():
        raise GamesBackupError("encrypted backup output already exists; choose a new path")
    return result


def _input_file(value: Path | str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise GamesBackupError("encrypted backup must not be a symlink")
    try:
        result = candidate.resolve(strict=True)
    except OSError as error:
        raise GamesBackupError("encrypted backup is unavailable") from error
    if not result.is_file():
        raise GamesBackupError("encrypted backup path is not a regular file")
    return result


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    encoded = _passphrase_bytes(passphrase)
    try:
        return Scrypt(
            salt=salt,
            length=32,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
        ).derive(encoded)
    except (ValueError, MemoryError) as error:
        raise GamesBackupError("backup key derivation failed") from error


def _passphrase_bytes(passphrase: str) -> bytes:
    if not isinstance(passphrase, str):
        raise GamesBackupError("backup passphrase must be text")
    try:
        encoded = passphrase.encode("utf-8")
    except UnicodeEncodeError as error:
        raise GamesBackupError("backup passphrase is not valid UTF-8") from error
    if len(encoded) < 16:
        raise GamesBackupError("backup passphrase must contain at least 16 UTF-8 bytes")
    return encoded


def _seal_verified_backup(
    backup_root: Path,
    output_path: Path,
    passphrase: str,
    manifest: dict[str, Any],
) -> None:
    salt = secrets.token_bytes(_SALT_BYTES)
    nonce = secrets.token_bytes(_NONCE_BYTES)
    header = ENCRYPTED_BACKUP_MAGIC + salt + nonce
    encryptor = Cipher(
        algorithms.AES(_derive_key(passphrase, salt)), modes.GCM(nonce)
    ).encryptor()
    encryptor.authenticate_additional_data(header)

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.partial-", dir=output_path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as destination:
            os.chmod(temporary_path, 0o600)
            destination.write(header)
            writer = _EncryptedWriter(destination, encryptor)
            try:
                with tarfile.open(fileobj=writer, mode="w|gz") as archive:
                    names = [entry["path"] for entry in manifest["files"]]
                    names.extend((MANIFEST_NAME, MANIFEST_HASH_NAME))
                    for name in names:
                        relative = PurePosixPath(name)
                        source = backup_root.joinpath(*relative.parts)
                        info = tarfile.TarInfo(name)
                        info.size = source.stat().st_size
                        info.mode = 0o600
                        info.mtime = 0
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        with source.open("rb") as stream:
                            archive.addfile(info, stream)
            finally:
                writer.close()
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary_path, 0o600)
        try:
            os.link(temporary_path, output_path, follow_symlinks=False)
        except FileExistsError as error:
            raise GamesBackupError("encrypted backup output appeared during creation") from error
        temporary_path.unlink()
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _decrypt_to_file(source: Path, destination: Path, passphrase: str) -> None:
    total_size = source.stat().st_size
    if total_size < _HEADER_BYTES + _TAG_BYTES:
        raise GamesBackupError("encrypted backup is truncated")
    with source.open("rb") as encrypted:
        header = encrypted.read(_HEADER_BYTES)
        if len(header) != _HEADER_BYTES or not header.startswith(ENCRYPTED_BACKUP_MAGIC):
            raise GamesBackupError("encrypted backup format is unsupported")
        offset = len(ENCRYPTED_BACKUP_MAGIC)
        salt = header[offset : offset + _SALT_BYTES]
        nonce_offset = offset + _SALT_BYTES
        nonce = header[nonce_offset : nonce_offset + _NONCE_BYTES]
        decryptor = Cipher(
            algorithms.AES(_derive_key(passphrase, salt)), modes.GCM(nonce)
        ).decryptor()
        decryptor.authenticate_additional_data(header)
        ciphertext_size = total_size - _HEADER_BYTES - _TAG_BYTES
        with destination.open("xb") as plaintext:
            os.chmod(destination, 0o600)
            remaining = ciphertext_size
            while remaining:
                block = encrypted.read(min(_ARCHIVE_CHUNK_BYTES, remaining))
                if not block:
                    raise GamesBackupError("encrypted backup is truncated")
                remaining -= len(block)
                plain = decryptor.update(block)
                if plain:
                    plaintext.write(plain)
            tag = encrypted.read(_TAG_BYTES)
            if len(tag) != _TAG_BYTES or encrypted.read(1):
                raise GamesBackupError("encrypted backup framing is invalid")
            try:
                final = decryptor.finalize_with_tag(tag)
            except InvalidTag as error:
                raise GamesBackupError(
                    "encrypted backup authentication failed (wrong passphrase or damaged file)"
                ) from error
            if final:
                plaintext.write(final)
            plaintext.flush()
            os.fsync(plaintext.fileno())


def _extract_and_verify(archive_path: Path, destination: Path) -> dict[str, Any]:
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = archive.getmembers()
            if not members or len(members) > _MAX_ARCHIVE_FILES:
                raise GamesBackupError("encrypted archive file count is invalid")
            names: set[str] = set()
            total_bytes = 0
            for member in members:
                if not member.isfile() or member.size < 0:
                    raise GamesBackupError("encrypted archive contains a non-regular file")
                name = member.name
                if name not in _MANIFEST_FILES:
                    relative = _safe_relative_path(name)
                    name = relative.as_posix()
                if name in names:
                    raise GamesBackupError("encrypted archive contains duplicate paths")
                names.add(name)
                total_bytes += member.size
                if total_bytes > _MAX_ARCHIVE_PLAIN_BYTES:
                    raise GamesBackupError("encrypted archive exceeds the expanded-size limit")

            if not _MANIFEST_FILES.issubset(names):
                raise GamesBackupError("encrypted archive omits its backup manifest")
            for member in members:
                if member.name in _MANIFEST_FILES:
                    target = destination / member.name
                    if target.exists() or target.is_symlink():
                        raise GamesBackupError("encrypted archive contains duplicate paths")
                else:
                    target = _target_file(destination, _safe_relative_path(member.name))
                stream = archive.extractfile(member)
                if stream is None:
                    raise GamesBackupError("encrypted archive member could not be read")
                remaining = member.size
                with stream, target.open("xb") as output:
                    os.chmod(target, 0o600)
                    while remaining:
                        chunk = stream.read(min(_ARCHIVE_CHUNK_BYTES, remaining))
                        if not chunk:
                            raise GamesBackupError("encrypted archive member is truncated")
                        output.write(chunk)
                        remaining -= len(chunk)
                os.chmod(target, 0o600)
    except (tarfile.TarError, EOFError, OSError) as error:
        raise GamesBackupError("encrypted backup archive is invalid") from error
    return _verify_backup_root(destination)
