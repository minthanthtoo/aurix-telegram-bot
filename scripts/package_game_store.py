#!/usr/bin/env python3
"""Package the isolated Games service, excluding local data and credential files."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile

if __package__:
    from .game_store_scope import (
        LOCK_FILE, PUBLIC_GAME_CONFIG_FILES, REQUIRED_RELEASE_FILES, SHARED_MODULES,
        SNAPSHOT_REQUIRED_RELEASE_FILES, is_release_input,
    )
else:
    from game_store_scope import (
        LOCK_FILE, PUBLIC_GAME_CONFIG_FILES, REQUIRED_RELEASE_FILES, SHARED_MODULES,
        SNAPSHOT_REQUIRED_RELEASE_FILES, is_release_input,
    )

ROOT = Path(__file__).resolve().parents[1]
WEB_SUFFIXES = {'.html', '.css', '.js', '.mjs', '.json', '.webmanifest', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.avif', '.gif', '.ico', '.txt'}
MAX_RELEASE_BYTES = 256 * 1024 * 1024
MAX_RELEASE_FILES = 4096


def package(output: Path, *, root: Path = ROOT) -> dict:
    files = [root / path for path in SHARED_MODULES]
    files.extend((root / 'aurix_games').rglob('*.py'))
    files.extend(root / path for path in PUBLIC_GAME_CONFIG_FILES)
    for pattern in ('*fazercards*.py', '*game*.py', '*games*.py'):
        files.extend((root / 'scripts').glob(pattern))
    files.extend(path for path in (root / 'web/game-store').rglob('*')
        if path.is_file() and path.suffix.lower() in WEB_SUFFIXES and 'token' not in path.name.lower()
        and not any(part.startswith('.') for part in path.relative_to(root).parts))
    files = sorted(set(files))
    if any(path.is_symlink() or not path.is_file() for path in files):
        raise ValueError('release inputs must be regular files')
    hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.resolve() in {path.resolve() for path in files}:
        raise ValueError('release output must not replace a source file')
    with tarfile.open(output, 'w:gz') as archive:
        for path in files:
            archive.add(path, arcname=str(path.relative_to(root)), recursive=False)
    output.chmod(0o600)
    result = {'files': len(files), 'source_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
        'archive_bytes': output.stat().st_size, 'file_sha256': hashes, 'financial_writes': False}
    manifest = output.with_suffix('.manifest.json')
    manifest.write_text(json.dumps(result, indent=2) + '\n')
    manifest.chmod(0o600)
    return result


def package_snapshot(root: Path, output: Path) -> dict:
    """Make a data-free rollback bundle from the currently installed source tree."""
    root = root.resolve(strict=True)
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and is_release_input(path.relative_to(root).as_posix())
    )
    names = {path.relative_to(root).as_posix() for path in files}
    missing = SNAPSHOT_REQUIRED_RELEASE_FILES - names
    if missing:
        raise ValueError(f"active release is missing rollback files: {', '.join(sorted(missing))}")
    if any(path.is_symlink() for path in files):
        raise ValueError("active release snapshot contains a symlink")
    if len(files) > MAX_RELEASE_FILES:
        raise ValueError("active release snapshot exceeds the file limit")
    hashes = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    expanded = sum(path.stat().st_size for path in files)
    if expanded > MAX_RELEASE_BYTES:
        raise ValueError("active release snapshot exceeds the size limit")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.resolve() == root or output.resolve().is_relative_to(root):
        raise ValueError("snapshot output must be outside the active release")
    if output.exists():
        raise FileExistsError(output)
    with output.open("xb") as stream:
        try:
            output.chmod(0o600)
            with gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0, compresslevel=6) as zipped:
                with tarfile.open(fileobj=zipped, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                    for path in files:
                        name = path.relative_to(root).as_posix()
                        data = path.read_bytes()
                        entry = tarfile.TarInfo(name)
                        entry.size = len(data)
                        entry.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                        archive.addfile(entry, io.BytesIO(data))
        except BaseException:
            output.unlink()
            raise
    digest = _sha256(output)
    release_id = f"legacy-{digest}"
    result = {
        "format_version": 1, "release_id": release_id, "commit_sha": None,
        "snapshot": True, "files": len(hashes), "source_sha256": digest,
        "archive_bytes": output.stat().st_size, "expanded_bytes": expanded,
        "lock_sha256": hashes.get(LOCK_FILE), "file_sha256": hashes,
        "financial_writes": False,
    }
    manifest = output.with_suffix(".manifest.json")
    with manifest.open("x", encoding="utf-8") as file:
        manifest.chmod(0o600)
        file.write(json.dumps(result, indent=2) + "\n")
    return result


def _git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "--literal-pathspecs", *args], cwd=root)


def _require_commit(commit: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("release commit must be a full lowercase Git SHA")


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def package_commit(output: Path, commit: str, *, root: Path = ROOT) -> dict:
    """Build an archive from Git blobs, independent of the index and working tree."""
    _require_commit(commit)
    if _git(root, "cat-file", "-t", commit).strip() != b"commit":
        raise ValueError("release SHA does not identify a commit")
    inputs = {}
    expanded = 0
    for item in _git(root, "ls-tree", "-r", "-z", "-l", commit).split(b"\0"):
        if not item:
            continue
        header, name = item.split(b"\t", 1)
        path = name.decode("utf-8", errors="surrogateescape")
        if not is_release_input(path):
            continue
        mode, kind, oid, size = header.split()
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise ValueError(f"release input is not a regular file: {path!r}")
        inputs[path] = (mode, oid, int(size))
        expanded += int(size)
    missing = REQUIRED_RELEASE_FILES - inputs.keys()
    if missing:
        raise ValueError(f"missing required release files: {', '.join(sorted(missing))}")
    if expanded > MAX_RELEASE_BYTES or len(inputs) > MAX_RELEASE_FILES:
        raise ValueError("release exceeds the supported file count or expanded size")

    # Git archive batches the reads. Compare each member to its original blob
    # to detect export substitutions or omissions before publishing the bundle.
    manifest = output.with_suffix(".manifest.json")
    if output.absolute() == manifest.absolute():
        raise ValueError("archive and manifest paths must differ")
    if output.resolve().is_relative_to(root.resolve()):
        raise ValueError("committed release output must be outside the repository")
    if output.exists() or manifest.exists():
        raise FileExistsError("release output and manifest must be new files")
    source = _git(root, "archive", "--format=tar", commit, "--", *sorted(inputs))
    hashes = {}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(source), mode="r:") as original:
        members = {member.name: member for member in original if not member.isdir()}
        if members.keys() != inputs.keys():
            raise ValueError("Git archive path list differs from the tracked release files")
        with output.open("xb") as stream:
            try:
                output.chmod(0o600)
                with gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0, compresslevel=6) as zipped:
                    with tarfile.open(fileobj=zipped, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                        for name, (mode, oid, size) in sorted(inputs.items()):
                            member = members[name]
                            if not member.isfile() or member.size != size:
                                raise ValueError(f"Git archive changed release file: {name!r}")
                            with original.extractfile(member) as blob:
                                data = blob.read()
                            blob_header = f"blob {size}\0".encode()
                            blob_digest = hashlib.sha1(blob_header, usedforsecurity=False)
                            blob_digest.update(data)
                            if blob_digest.hexdigest().encode() != oid:
                                raise ValueError(f"Git archive changed release contents: {name!r}")
                            hashes[name] = hashlib.sha256(data).hexdigest()
                            entry = tarfile.TarInfo(name)
                            entry.size = size
                            entry.mode = 0o755 if mode == b"100755" else 0o644
                            archive.addfile(entry, io.BytesIO(data))
            except BaseException:
                output.unlink()
                raise
    result = {
        "format_version": 1,
        "release_id": commit,
        "commit_sha": commit,
        "files": len(hashes),
        "source_sha256": _sha256(output),
        "archive_bytes": output.stat().st_size,
        "expanded_bytes": expanded,
        "lock_sha256": hashes[LOCK_FILE],
        "file_sha256": hashes,
        "financial_writes": False,
    }
    manifest_created = False
    try:
        with manifest.open("x", encoding="utf-8") as stream:
            manifest_created = True
            manifest.chmod(0o600)
            stream.write(json.dumps(result, indent=2) + "\n")
    except BaseException:
        if manifest_created:
            manifest.unlink()
        output.unlink()
        raise
    return result


def verify_release_package(
    output: Path,
    expected_digest: str,
    *,
    expected_commit: str | None = None,
    expected_release_id: str | None = None,
) -> dict:
    """Verify a commit build or legacy snapshot against its manifest and digest."""
    if expected_commit is not None:
        _require_commit(expected_commit)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise ValueError("expected archive digest must be SHA-256")
    manifest = output.with_suffix(".manifest.json")
    if manifest.stat().st_size > MAX_RELEASE_FILES * 512:
        raise ValueError("release manifest exceeds the size limit")
    result = json.loads(manifest.read_text(encoding="utf-8"))
    if (
        not isinstance(result, dict)
        or result.get("format_version") != 1
        or (expected_commit is not None and result.get("commit_sha") != expected_commit)
        or (expected_commit is None and result.get("snapshot") is not True)
        or (expected_release_id is not None and result.get("release_id") != expected_release_id)
        or result.get("source_sha256") != expected_digest
        or result.get("archive_bytes") != output.stat().st_size
        or output.stat().st_size > MAX_RELEASE_BYTES + 8 * 1024 * 1024
        or _sha256(output) != expected_digest
    ):
        raise ValueError("archive or manifest does not match the expected release")
    files = result.get("file_sha256")
    required_files = SNAPSHOT_REQUIRED_RELEASE_FILES if result.get("snapshot") else REQUIRED_RELEASE_FILES
    if (
        not isinstance(files, dict) or not required_files <= files.keys()
        or len(files) != result.get("files") or len(files) > MAX_RELEASE_FILES
        or result.get("lock_sha256") != files.get(LOCK_FILE)
    ):
        raise ValueError("release manifest is incomplete")
    expanded = 0
    seen = set()
    with tarfile.open(output, mode="r|gz") as archive:
        for member in archive:
            name = member.name
            if (
                not member.isfile() or member.mode not in (0o644, 0o755)
                or PurePosixPath(name).is_absolute() or not is_release_input(name)
                or name in seen or name not in files
            ):
                raise ValueError(f"unexpected archive member: {name!r}")
            expanded += member.size
            if expanded > MAX_RELEASE_BYTES:
                raise ValueError("release exceeds the expanded size limit")
            with archive.extractfile(member) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != files[name]:
                    raise ValueError(f"release content checksum mismatch: {name!r}")
            seen.add(name)
    if seen != files.keys() or expanded != result.get("expanded_bytes"):
        raise ValueError("archive does not match the manifest inventory")
    return result


def verify_commit_package(output: Path, commit: str, expected_digest: str) -> dict:
    """Check a bundle against its commit and digest from a trusted release record."""
    return verify_release_package(output, expected_digest, expected_commit=commit, expected_release_id=commit)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--commit', help="package only committed files from this full Git SHA")
    parser.add_argument('--verify-digest', help="verify an existing archive using a trusted SHA-256")
    args = parser.parse_args()
    try:
        if args.verify_digest:
            if not args.commit:
                parser.error("--verify-digest requires --commit")
            result = verify_commit_package(args.output, args.commit, args.verify_digest)
        elif args.commit:
            result = package_commit(args.output, args.commit)
        else:
            result = package(args.output)
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as error:
        print(f"Games package: {error}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({key: value for key, value in result.items() if key != 'file_sha256'}))
