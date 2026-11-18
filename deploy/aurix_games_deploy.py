#!/usr/bin/env python3
"""Root-owned, lock-protected AuriX Games release switcher."""

from __future__ import annotations

import fcntl
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import BinaryIO
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


HELPER_ROOT = Path(__file__).resolve().parents[1]
if str(HELPER_ROOT) not in sys.path:
    sys.path.insert(0, str(HELPER_ROOT))

from scripts.package_game_store import package_snapshot, sha256_stream, verify_release_package  # noqa: E402
from scripts.game_store_scope import LOCK_FILE  # noqa: E402


BASE = Path(os.environ.get("AURIX_GAMES_INSTALL_ROOT", "/opt/aurix-games"))
RELEASES = BASE / "releases"
CURRENT = BASE / "current"
STATE = BASE / "deployment-state.json"
JOURNAL = BASE / "deployment-journal.json"
LOCK = Path("/run/lock/aurix-games-deploy.lock")
STAGE_ROOT = BASE / ".deploy-staging"
VENV_PYTHON = Path("/opt/aurix-venv/bin/python")
HEALTH_URL = "http://127.0.0.1:64100"
PUBLIC_URL = "https://games.aurix-mart.tech/game-store/"
WEB_SERVICE = "aurix-games.service"
EDGE_SERVICE = "aurix-games-edge.service"
TIMERS = (
    "aurix-games-catalog.timer", "aurix-games-dispatch.timer", "aurix-games-fulfill.timer",
    "aurix-games-reconcile.timer", "aurix-games-integrity.timer", "aurix-games-backup.timer",
)
WORKERS = tuple(f"aurix-games-{name}.service" for name in (
    "catalog", "dispatch", "fulfill", "reconcile", "integrity", "backup",
))
MAX_ARCHIVE_BYTES = 264 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
ID_RE = re.compile(r"(?:[0-9a-f]{40}|legacy-[0-9a-f]{64})\Z")


class DeployError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return sha256_stream(stream)


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict | None:
    try:
        if path.is_symlink() or path.stat().st_size > MAX_MANIFEST_BYTES:
            raise DeployError("deployment metadata is unsafe")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise DeployError("deployment metadata is unavailable") from exc
    if not isinstance(value, dict):
        raise DeployError("deployment metadata is invalid")
    return value


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=60)
    if check and result.returncode:
        raise DeployError(f"host command failed: {args[0]}")
    return result


def _active(unit: str) -> bool:
    return _run("systemctl", "is-active", "--quiet", unit, check=False).returncode == 0


def _enabled(unit: str) -> bool:
    return _run("systemctl", "is-enabled", "--quiet", unit, check=False).returncode == 0


def _current_target() -> Path:
    if not CURRENT.is_symlink():
        raise DeployError("Games current release pointer is not a symlink")
    target = CURRENT.resolve(strict=True)
    if target.parent != RELEASES.resolve(strict=True) or not target.is_dir():
        raise DeployError("Games current release points outside its managed release directory")
    return target


def _state() -> dict | None:
    value = _read_json(STATE)
    if value is None:
        return None
    if value.get("schema_version") != 1:
        raise DeployError("unsupported Games deployment state")
    if not isinstance(value.get("active"), dict):
        raise DeployError("Games deployment state is invalid")
    for key in ("active", "rollback"):
        release = value.get(key)
        if release is not None and (
            not isinstance(release, dict)
            or not isinstance(release.get("release_id"), str)
            or not ID_RE.fullmatch(release["release_id"])
            or not re.fullmatch(r"[0-9a-f]{64}", str(release.get("source_sha256", "")))
        ):
            raise DeployError("Games deployment state is invalid")
    return value


def _marker(path: Path) -> dict:
    marker_path = path / ".aurix-games-release.json"
    value = _read_json(marker_path)
    if not value or not ID_RE.fullmatch(str(value.get("release_id", ""))):
        raise DeployError("installed release marker is missing or invalid")
    return value


def _symlink_release(target: Path) -> None:
    resolved = target.resolve(strict=True)
    if resolved.parent != RELEASES.resolve(strict=True) or not resolved.is_dir():
        raise DeployError("refusing to switch outside the managed release directory")
    temporary = BASE / f".current-{os.getpid()}"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(resolved)
    os.replace(temporary, CURRENT)


def _restore_timers(active_timers: list[str]) -> None:
    for timer in TIMERS:
        if timer in active_timers and not _active(timer):
            _run("systemctl", "start", timer)
        elif timer not in active_timers and _active(timer):
            _run("systemctl", "stop", timer)
    if any(_active(timer) != (timer in active_timers) for timer in TIMERS):
        raise DeployError("Games timer state could not be restored")


def _recover_locked(*, at_boot: bool = False) -> str:
    journal = _read_json(JOURNAL)
    if not journal:
        return "clean"
    phase = journal.get("phase")
    previous = journal.get("previous_target")
    timers = journal.get("active_timers")
    if not isinstance(previous, str) or not isinstance(timers, list) or any(timer not in TIMERS for timer in timers):
        raise DeployError("deployment recovery record is invalid; workers remain paused")
    if phase == "verified":
        candidate = journal.get("candidate_target")
        if not isinstance(candidate, str) or not Path(candidate).is_dir():
            raise DeployError("verified deployment recovery target is missing")
        target = Path(candidate)
        pointer_changed = not CURRENT.is_symlink() or CURRENT.resolve() != target.resolve()
        if pointer_changed:
            _symlink_release(target)
        if not at_boot and (pointer_changed or not _active(WEB_SERVICE)):
            _run("systemctl", "restart", WEB_SERVICE)
        candidate_state = journal.get("candidate_state")
        if not isinstance(candidate_state, dict):
            raise DeployError("verified deployment state is missing")
        _atomic_json(STATE, candidate_state)
        if not at_boot:
            _health(candidate_state["active"]["release_id"])
        _prune_releases(Path(candidate))
    elif phase in {"pausing", "paused", "switched"}:
        old = Path(previous)
        if not old.is_dir():
            raise DeployError("previous release is unavailable; workers remain paused")
        _symlink_release(old)
        if not at_boot and journal.get("web_was_active"):
            _run("systemctl", "restart", WEB_SERVICE)
            _health(str(journal.get("previous_release_id", "legacy-")))
        previous_state = journal.get("previous_state")
        if previous_state is None:
            STATE.unlink(missing_ok=True)
        elif isinstance(previous_state, dict):
            _atomic_json(STATE, previous_state)
        else:
            raise DeployError("previous deployment state is invalid")
        _prune_releases(old)
    else:
        raise DeployError("deployment recovery phase is invalid; workers remain paused")
    # At boot systemd starts enabled units after this dependency completes.
    if not at_boot:
        _restore_timers(timers)
    JOURNAL.unlink(missing_ok=True)
    return "recovered"


def _prune_releases(keep: Path) -> None:
    keep = keep.resolve(strict=True)
    if keep.parent != RELEASES.resolve(strict=True):
        raise DeployError("refusing to prune outside the managed Games releases")
    for path in RELEASES.iterdir():
        if not path.is_symlink() and path.is_dir() and path.resolve() != keep:
            shutil.rmtree(path)


def _clean_staging() -> None:
    if STAGE_ROOT.is_symlink():
        raise DeployError("Games deployment staging directory is unsafe")
    STAGE_ROOT.mkdir(parents=True, exist_ok=True)
    for path in STAGE_ROOT.iterdir():
        if path.is_symlink() or not path.is_dir() or not path.name.startswith("candidate-"):
            raise DeployError("unexpected file in the Games deployment staging directory")
        shutil.rmtree(path)


def _health(release_id: str) -> None:
    for path in ("/api/healthz",):
        deadline = time.monotonic() + 60
        last_error = None
        while time.monotonic() < deadline:
            try:
                with urlopen(HEALTH_URL + path, timeout=4) as response:
                    data = json.loads(response.read(128 * 1024))
                    if response.status != 200:
                        raise DeployError(f"local health probe failed: {path}")
                    if data.get("service") != "aurix-games":
                        raise DeployError("local Games health response is invalid")
                    break
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                time.sleep(2)
        else:
            raise DeployError(f"local Games health probe failed: {path}") from last_error
    if not release_id.startswith("legacy-"):
        try:
            with urlopen(HEALTH_URL + "/api/version", timeout=4) as response:
                version = json.loads(response.read(128 * 1024))
                if response.status != 200 or version.get("release_id") != release_id:
                    raise DeployError("served Games release does not match the candidate")
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise DeployError("local Games release version probe failed") from exc
    try:
        with urlopen(PUBLIC_URL, timeout=15) as response:
            if response.status != 200:
                raise DeployError("public storefront probe failed")
            response.read(1024)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise DeployError("public storefront probe failed") from exc


def _readiness() -> dict | None:
    try:
        with urlopen(HEALTH_URL + "/api/readyz", timeout=4) as response:
            payload = response.read(128 * 1024)
    except HTTPError as response:
        payload = response.read(128 * 1024)
    except (URLError, TimeoutError, OSError):
        return None
    try:
        value = json.loads(payload)
    except (ValueError, json.JSONDecodeError):
        return None
    checks = value.get("checks") if isinstance(value, dict) else None
    return checks if isinstance(checks, dict) and all(isinstance(v, bool) for v in checks.values()) else None


def _check_runtime_lock(archive: Path, verified: dict, active_target: Path, operation: str) -> None:
    if LOCK_FILE not in verified["file_sha256"]:
        return
    active_lock = active_target / LOCK_FILE
    if operation == "deploy" and active_lock.is_file() and _sha256(active_lock) != verified["lock_sha256"]:
        raise DeployError("dependency lock changed; update and rehearse the shared runtime separately")
    with tarfile.open(archive, mode="r:gz") as bundle:
        member = bundle.getmember(LOCK_FILE)
        with bundle.extractfile(member) as stream:
            lock = stream.read(64 * 1024)
    if len(lock) >= 64 * 1024:
        raise DeployError("Games dependency lock exceeds the size limit")
    for line in lock.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9.!+_-]+)(?:\s|$)", line)
        if not match:
            raise DeployError("Games dependency lock format is unsupported")
        name, expected = match.groups()
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError as exc:
            raise DeployError(f"locked runtime dependency is missing: {name}") from exc
        if installed != expected:
            raise DeployError(f"locked runtime dependency differs from the server: {name}")


def _read_exact(stream: BinaryIO, count: int) -> bytes:
    chunks = []
    remaining = count
    while remaining:
        chunk = stream.read(min(1024 * 1024, remaining))
        if not chunk:
            raise DeployError("release request ended before its declared size")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _request_header(stream: BinaryIO) -> dict:
    line = stream.readline(8193)
    if not line or len(line) > 8192 or not line.endswith(b"\n"):
        raise DeployError("release request header is invalid")
    try:
        header = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeployError("release request header is invalid") from exc
    if not isinstance(header, dict):
        raise DeployError("release request header is invalid")
    return header


def _snapshot_response(stdout: BinaryIO) -> None:
    current = _current_target()
    with tempfile.TemporaryDirectory(prefix="games-snapshot-", dir=BASE) as temporary:
        archive = Path(temporary) / "active.tgz"
        manifest = package_snapshot(current, archive)
        manifest_bytes = archive.with_suffix(".manifest.json").read_bytes()
        header = {
            "operation": "snapshot", "release_id": manifest["release_id"],
            "commit_sha": None, "source_sha256": manifest["source_sha256"],
            "archive_bytes": archive.stat().st_size, "manifest_bytes": len(manifest_bytes),
        }
        stdout.write((json.dumps(header, separators=(",", ":")) + "\n").encode())
        stdout.write(manifest_bytes)
        with archive.open("rb") as stream:
            shutil.copyfileobj(stream, stdout, 1024 * 1024)
        stdout.flush()


def _install_request(header: dict, manifest_bytes: bytes, archive: Path) -> dict:
    operation = header.get("operation")
    release_id = header.get("release_id")
    digest = header.get("source_sha256")
    commit_sha = header.get("commit_sha")
    expected_active = header.get("expected_active_release_id")
    previous_release_id = header.get("previous_release_id")
    if operation not in {"deploy", "rollback"} or not isinstance(release_id, str) or not ID_RE.fullmatch(release_id):
        raise DeployError("release operation or identifier is invalid")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise DeployError("release digest is invalid")
    manifest_path = archive.with_suffix(".manifest.json")
    manifest_path.write_bytes(manifest_bytes)
    manifest_path.chmod(0o600)
    verified = verify_release_package(
        archive, digest, expected_commit=commit_sha,
        expected_release_id=release_id,
    )
    if verified["commit_sha"] != commit_sha:
        raise DeployError("release commit does not match its package manifest")
    active_target = _current_target()
    _check_runtime_lock(archive, verified, active_target, operation)
    state = _state()
    if state is None:
        if operation != "deploy":
            raise DeployError("initial install requires a verified snapshot of the active release")
        with tempfile.TemporaryDirectory(prefix="games-current-check-", dir=BASE) as temporary:
            check_archive = Path(temporary) / "active.tgz"
            active_manifest = package_snapshot(active_target, check_archive)
        if active_manifest["release_id"] != previous_release_id or expected_active is not None:
            raise DeployError("active Games source changed after the rollback snapshot")
        active_marker = active_manifest
        active_record = {
            "release_id": active_manifest["release_id"],
            "source_sha256": active_manifest["source_sha256"],
            "commit_sha": None,
        }
        rollback_record = active_record
    else:
        active_marker = _marker(active_target)
        active_record = state["active"]
        rollback_record = state.get("rollback")
        if active_record["release_id"] != active_marker["release_id"]:
            raise DeployError("installed release and deployment record disagree")
        if release_id == active_record["release_id"] and digest == active_record["source_sha256"]:
            return {"status": "already_active", **state}
        if expected_active != active_record["release_id"]:
            raise DeployError("deployment target is stale; read the current release state again")
        if operation == "deploy":
            if previous_release_id != active_record["release_id"]:
                raise DeployError("rollback package must identify the current active release")
            rollback_record = active_record
        if operation == "rollback" and (
            rollback_record is None or release_id != rollback_record["release_id"]
        ):
            raise DeployError("requested package is not the verified rollback release")
        if operation == "rollback":
            active_record, rollback_record = rollback_record, active_record

    if release_id == active_marker["release_id"]:
        return {"status": "already_active", "active": active_record, "rollback": rollback_record}
    if _active(WEB_SERVICE) is False or _active(EDGE_SERVICE) is False:
        raise DeployError("Games web or edge service is not active before deployment")
    if shutil.disk_usage(BASE).free < verified["expanded_bytes"] + 128 * 1024 * 1024:
        raise DeployError("insufficient free space for one temporary release")
    if not _enabled(WEB_SERVICE):
        raise DeployError("Games web service is not enabled")

    candidate = RELEASES / release_id
    RELEASES.mkdir(parents=True, exist_ok=True)
    _clean_staging()
    if candidate.is_symlink():
        raise DeployError("candidate release path is unsafe")
    if candidate.exists():
        marker = _marker(candidate)
        if marker.get("source_sha256") != digest:
            raise DeployError("candidate release path belongs to another package")
        if candidate != active_target:
            shutil.rmtree(candidate)
    stage = Path(tempfile.mkdtemp(prefix="candidate-", dir=STAGE_ROOT))
    try:
        _extract_package(archive, stage, verified["file_sha256"])
        marker = {"release_id": release_id, "commit_sha": commit_sha, "source_sha256": digest}
        _atomic_json(stage / ".aurix-games-release.json", marker)
        (stage / ".aurix-games-release.json").chmod(0o644)
        for path in stage.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
        stage.chmod(0o755)
        os.replace(stage, candidate)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    previous_target = str(active_target)
    timers_active = [timer for timer in TIMERS if _active(timer)]
    readiness_before = _readiness()
    new_state = {"schema_version": 1, "active": {
        "release_id": release_id, "source_sha256": digest, "commit_sha": commit_sha,
    }, "rollback": rollback_record}
    journal = {
        "phase": "pausing", "previous_target": previous_target,
        "previous_release_id": active_marker["release_id"], "previous_state": state,
        "candidate_target": str(candidate), "active_timers": timers_active,
        "candidate_state": new_state, "web_was_active": True,
    }
    _atomic_json(JOURNAL, journal)
    try:
        for timer in timers_active:
            _run("systemctl", "stop", timer)
        deadline = time.monotonic() + 180
        while any(_active(worker) for worker in WORKERS):
            if time.monotonic() >= deadline:
                raise DeployError("Games workers did not drain within 180 seconds")
            time.sleep(2)
        journal["phase"] = "paused"
        _atomic_json(JOURNAL, journal)
        _symlink_release(candidate)
        journal["phase"] = "switched"
        _atomic_json(JOURNAL, journal)
        _run("systemctl", "restart", WEB_SERVICE)
        _health(release_id)
        readiness_after = _readiness()
        if readiness_before is not None:
            if readiness_after is None or any(
                previous and not readiness_after.get(name, False) for name, previous in readiness_before.items()
            ):
                raise DeployError("Games deployment regressed an existing readiness check")
        _atomic_json(STATE, new_state)
        journal["phase"] = "verified"
        _atomic_json(JOURNAL, journal)
        _restore_timers(timers_active)
        _prune_releases(candidate)
        JOURNAL.unlink(missing_ok=True)
        return {"status": "deployed", **new_state}
    except BaseException as exc:
        if journal["phase"] == "verified":
            raise DeployError("candidate is healthy; timer recovery or release cleanup remains pending") from exc
        try:
            _symlink_release(active_target)
            _run("systemctl", "restart", WEB_SERVICE)
            _health(active_marker["release_id"])
            _restore_timers(timers_active)
            if state is None:
                STATE.unlink(missing_ok=True)
            else:
                _atomic_json(STATE, state)
            JOURNAL.unlink(missing_ok=True)
            if candidate.exists():
                shutil.rmtree(candidate)
        except BaseException as recovery_error:
            # Keep the journal and all release files so the boot recovery unit can retry.
            raise DeployError("deployment failed and automatic recovery is incomplete; workers remain paused") from recovery_error
        if isinstance(exc, DeployError):
            raise
        raise DeployError("deployment failed and the previous release was restored") from exc


def _extract_package(archive_path: Path, destination: Path, hashes: dict[str, str]) -> None:
    expanded = 0
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) != len(hashes):
            raise DeployError("release archive file count differs from its manifest")
        for member in members:
            name = member.name
            parts = PurePosixPath(name).parts
            if (
                not member.isfile() or member.mode not in (0o644, 0o755)
                or PurePosixPath(name).is_absolute() or not parts
                or any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
                or name not in hashes
            ):
                raise DeployError("release archive contains an unsafe path")
            expanded += member.size
            if expanded > MAX_EXPANDED_BYTES:
                raise DeployError("release archive exceeds the expanded size limit")
            target = destination.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), member.mode)
            try:
                with os.fdopen(fd, "wb") as output, archive.extractfile(member) as source:
                    if source is None:
                        raise DeployError("release file is unreadable")
                    hasher = hashlib.sha256()
                    remaining = member.size
                    while remaining:
                        block = source.read(min(1024 * 1024, remaining))
                        if not block:
                            raise DeployError("release file ended before its declared size")
                        output.write(block)
                        hasher.update(block)
                        remaining -= len(block)
                    if hasher.hexdigest() != hashes[name]:
                        raise DeployError("release file checksum does not match its manifest")
                os.chmod(target, member.mode)
            except BaseException:
                target.unlink(missing_ok=True)
                raise


def handle_request(stdin: BinaryIO, stdout: BinaryIO) -> None:
    header = _request_header(stdin)
    operation = header.get("operation")
    if operation == "status":
        with LOCK.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if JOURNAL.exists():
                raise DeployError("Games deployment recovery is pending")
            active_target = _current_target()
            state = _state()
            if state is not None and _marker(active_target) != state["active"]:
                raise DeployError("installed release and deployment record disagree")
            stdout.write((json.dumps({"status": "ok", "state": state}, separators=(",", ":")) + "\n").encode())
        return
    if operation == "snapshot":
        with LOCK.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _snapshot_response(stdout)
        return
    if operation not in {"deploy", "rollback"}:
        raise DeployError("release request operation is not allowed")
    archive_bytes = header.get("archive_bytes")
    manifest_bytes_count = header.get("manifest_bytes")
    if (
        not isinstance(archive_bytes, int) or not 1 <= archive_bytes <= MAX_ARCHIVE_BYTES
        or not isinstance(manifest_bytes_count, int) or not 1 <= manifest_bytes_count <= MAX_MANIFEST_BYTES
    ):
        raise DeployError("release package sizes are invalid")
    manifest_bytes = _read_exact(stdin, manifest_bytes_count)
    with tempfile.TemporaryDirectory(prefix="games-release-incoming-", dir=BASE) as temporary:
        archive = Path(temporary) / "release.tgz"
        with archive.open("xb") as stream:
            shutil.copyfileobj(_BoundedReader(stdin, archive_bytes), stream, 1024 * 1024)
            stream.flush()
            os.fsync(stream.fileno())
        with LOCK.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _recover_locked()
            result = _install_request(header, manifest_bytes, archive)
    stdout.write((json.dumps(result, separators=(",", ":")) + "\n").encode())
    stdout.flush()


class _BoundedReader:
    def __init__(self, source: BinaryIO, size: int):
        self.source = source
        self.remaining = size

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        count = self.remaining if size < 0 else min(size, self.remaining)
        data = self.source.read(count)
        self.remaining -= len(data)
        if not data and self.remaining:
            raise DeployError("release archive ended before its declared size")
        return data


def recover_boot() -> None:
    with LOCK.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _recover_locked(at_boot=True)


def main() -> int:
    try:
        if os.geteuid() != 0:
            raise DeployError("Games release helper must run as root")
        if len(sys.argv) == 2 and sys.argv[1] == "--recover":
            recover_boot()
            return 0
        if len(sys.argv) != 1:
            raise DeployError("unsupported Games release helper arguments")
        handle_request(sys.stdin.buffer, sys.stdout.buffer)
        return 0
    except (DeployError, OSError, ValueError, tarfile.TarError, subprocess.TimeoutExpired) as exc:
        print(f"Games release: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
