#!/usr/bin/env python3
"""Transfer verified Games releases over the restricted production SSH command."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.package_game_store import verify_release_package  # noqa: E402

MAX_ARCHIVE_BYTES = 264 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
REMOTE_COMMAND = "aurix-games-deploy"


def _configuration(directory: Path) -> list[str]:
    host = os.environ.get("GAMES_DEPLOY_HOST", "")
    port = os.environ.get("GAMES_DEPLOY_PORT") or "22"
    private_key = os.environ.get("GAMES_DEPLOY_PRIVATE_KEY", "")
    known_hosts = os.environ.get("GAMES_DEPLOY_KNOWN_HOSTS", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host):
        raise ValueError("GAMES_DEPLOY_HOST must be a DNS name or IPv4 address")
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("GAMES_DEPLOY_PORT must be between 1 and 65535")
    if not private_key.startswith("-----BEGIN ") or not known_hosts.strip():
        raise ValueError("the Games deployment key and pinned known-host entry are required")
    key_path = directory / "deploy_key"
    hosts_path = directory / "known_hosts"
    key_path.write_text(private_key, encoding="utf-8")
    key_path.chmod(0o600)
    hosts_path.write_text(known_hosts, encoding="utf-8")
    hosts_path.chmod(0o600)
    return [
        "ssh", "-F", "/dev/null", "-i", str(key_path),
        "-o", f"UserKnownHostsFile={hosts_path}",
        "-o", "GlobalKnownHostsFile=/dev/null", "-o", "StrictHostKeyChecking=yes",
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "PreferredAuthentications=publickey",
        "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
        "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        "-p", port, f"aurix-games-deploy@{host}", REMOTE_COMMAND,
    ]


def _response(process: subprocess.Popen, *, timeout: int = 1200) -> dict:
    line = process.stdout.readline(8193)
    if len(line) > 8192 or not line.endswith(b"\n"):
        _abort(process)
        raise RuntimeError("Games server returned an invalid response")
    try:
        result = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _abort(process)
        raise RuntimeError("Games server returned an invalid response") from exc
    if not isinstance(result, dict):
        _abort(process)
        raise RuntimeError("Games server returned an invalid response")
    process.stdout.close()
    error = process.stderr.read(8193).decode("utf-8", errors="replace")
    code = process.wait(timeout=timeout)
    if code:
        raise RuntimeError(error.strip()[:1000] or "Games SSH operation failed")
    return result


def _start(ssh: list[str]) -> subprocess.Popen:
    return subprocess.Popen(ssh, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _abort(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def _read_exact(source, count: int) -> bytes:
    parts = []
    remaining = count
    while remaining:
        chunk = source.read(min(1024 * 1024, remaining))
        if not chunk:
            raise RuntimeError("Games server response was truncated")
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def status(ssh: list[str]) -> dict:
    process = _start(ssh)
    process.stdin.write(b'{"operation":"status"}\n')
    process.stdin.close()
    return _response(process)


def snapshot(ssh: list[str], archive_path: Path) -> dict:
    process = _start(ssh)
    process.stdin.write(b'{"operation":"snapshot"}\n')
    process.stdin.close()
    line = process.stdout.readline(8193)
    try:
        header = json.loads(line)
        archive_bytes, manifest_bytes = header["archive_bytes"], header["manifest_bytes"]
        if (
            header.get("operation") != "snapshot"
            or not isinstance(archive_bytes, int) or not 1 <= archive_bytes <= MAX_ARCHIVE_BYTES
            or not isinstance(manifest_bytes, int) or not 1 <= manifest_bytes <= MAX_MANIFEST_BYTES
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        _abort(process)
        raise RuntimeError("Games server returned an invalid snapshot header") from exc
    try:
        manifest = _read_exact(process.stdout, manifest_bytes)
    except RuntimeError:
        _abort(process)
        raise
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = archive_path.with_suffix(".manifest.json")
    archive_created = manifest_created = False
    try:
        with archive_path.open("xb") as output:
            archive_created = True
            shutil.copyfileobj(_LimitedReader(process.stdout, archive_bytes), output, 1024 * 1024)
        with manifest_path.open("xb") as output:
            manifest_created = True
            output.write(manifest)
        archive_path.chmod(0o600)
        manifest_path.chmod(0o600)
    except BaseException:
        if archive_created:
            archive_path.unlink(missing_ok=True)
        if manifest_created:
            manifest_path.unlink(missing_ok=True)
        _abort(process)
        raise
    error = process.stderr.read(8193).decode("utf-8", errors="replace")
    code = process.wait(timeout=1200)
    if code:
        archive_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        raise RuntimeError(error.strip()[:1000] or "Games snapshot failed")
    verified = verify_release_package(
        archive_path, header["source_sha256"], expected_release_id=header["release_id"],
    )
    return {key: value for key, value in verified.items() if key != "file_sha256"}


class _LimitedReader:
    def __init__(self, source, count: int):
        self.source = source
        self.remaining = count

    def read(self, size: int = -1) -> bytes:
        if self.remaining == 0:
            return b""
        count = self.remaining if size < 0 else min(size, self.remaining)
        data = self.source.read(count)
        if not data:
            raise RuntimeError("Games server snapshot archive was truncated")
        self.remaining -= len(data)
        return data


def send(ssh: list[str], args: argparse.Namespace) -> dict:
    archive = args.archive
    manifest_path = archive.with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verified = verify_release_package(
        archive, args.digest, expected_commit=args.commit or None,
        expected_release_id=args.release_id,
    )
    if manifest != verified or archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("release package or manifest failed local verification")
    header = {
        "operation": args.operation,
        "release_id": args.release_id,
        "commit_sha": args.commit or None,
        "source_sha256": args.digest,
        "expected_active_release_id": args.expected_active or None,
        "previous_release_id": args.previous_release or None,
        "archive_bytes": archive.stat().st_size,
        "manifest_bytes": manifest_path.stat().st_size,
    }
    if header["manifest_bytes"] > MAX_MANIFEST_BYTES:
        raise ValueError("release manifest exceeds the supported size")
    process = _start(ssh)
    try:
        process.stdin.write((json.dumps(header, separators=(",", ":")) + "\n").encode())
        with manifest_path.open("rb") as source:
            shutil.copyfileobj(source, process.stdin, 1024 * 1024)
        with archive.open("rb") as source:
            shutil.copyfileobj(source, process.stdin, 1024 * 1024)
        process.stdin.close()
        return _response(process)
    except BaseException:
        _abort(process)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    subparsers.add_parser("status")
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--archive", required=True, type=Path)
    send_parser = subparsers.add_parser("send")
    send_parser.add_argument("--operation", choices=("deploy", "rollback"), required=True)
    send_parser.add_argument("--archive", required=True, type=Path)
    send_parser.add_argument("--release-id", required=True)
    send_parser.add_argument("--digest", required=True)
    send_parser.add_argument("--commit")
    send_parser.add_argument("--expected-active")
    send_parser.add_argument("--previous-release")
    args = parser.parse_args()
    try:
        with tempfile.TemporaryDirectory(prefix="aurix-games-ssh-") as directory:
            ssh = _configuration(Path(directory))
            if args.operation == "status":
                result = status(ssh)
            elif args.operation == "snapshot":
                result = snapshot(ssh, args.archive)
            else:
                result = send(ssh, args)
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Games SSH: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
