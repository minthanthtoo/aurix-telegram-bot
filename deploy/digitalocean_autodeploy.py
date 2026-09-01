#!/usr/bin/env python3
"""GitHub-CI-gated, atomic DigitalOcean deployment with rollback."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


STATE_DIR = Path(os.environ.get("AURIX_DEPLOY_STATE_DIR", "/var/lib/aurix-deploy"))
RELEASES_DIR = Path(os.environ.get("AURIX_DEPLOY_RELEASES_DIR", "/opt/aurix-releases"))
CURRENT_LINK = Path(os.environ.get("AURIX_DEPLOY_CURRENT_LINK", "/opt/aurix-current"))
REPOSITORY = os.environ.get(
    "AURIX_DEPLOY_REPOSITORY", "https://github.com/minthanthtoo/aurix-telegram-bot.git"
)
BRANCH = os.environ.get("AURIX_DEPLOY_BRANCH", "main")
SERVICE = os.environ.get("AURIX_DEPLOY_SERVICE", "aurix-bot")
REQUIRE_CI = os.environ.get("AURIX_DEPLOY_REQUIRE_GITHUB_CI", "1").lower() not in {
    "0",
    "false",
    "no",
}
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
RELEASE_GATE_VARIABLES = (
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "RECEIPT_LLM_BASE_URL",
    "RECEIPT_LLM_MODEL",
    "RECEIPT_LLM_API_KEY",
)


class DeployError(RuntimeError):
    pass


def missing_release_configuration(environment: dict[str, str]) -> list[str]:
    """Return only variable names so blocked timer logs never expose values."""
    missing = [name for name in RELEASE_GATE_VARIABLES if not environment.get(name, "").strip()]
    if environment.get("RECEIPT_STORAGE_REQUIRED", "0").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        missing.append("RECEIPT_STORAGE_REQUIRED=1")
    return missing


def run(
    *command: str,
    cwd: Path | None = None,
    timeout: int = 600,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        output = getattr(exc, "stdout", "") or ""
        safe_output = "\n".join(str(output).splitlines()[-20:])[:4000]
        raise DeployError(f"command failed: {command[0]}\n{safe_output}") from exc


def github_slug(repository: str) -> str | None:
    parsed = urlsplit(repository)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        return None
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path) else None


def ci_conclusion(payload: object, check_name: str = "safety-net") -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get("check_runs"), list):
        return "invalid"
    matching = [
        item
        for item in payload["check_runs"]
        if isinstance(item, dict) and str(item.get("name")) == check_name
    ]
    if not matching:
        return "pending"
    if any(item.get("status") != "completed" for item in matching):
        return "pending"
    return "success" if any(item.get("conclusion") == "success" for item in matching) else "failed"


def require_github_ci(sha: str) -> str:
    if not REQUIRE_CI:
        return "success"
    slug = github_slug(REPOSITORY)
    if slug is None:
        raise DeployError("CI gating requires an HTTPS github.com repository URL")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{slug}/commits/{sha}/check-runs?per_page=100",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "aurix-digitalocean-deployer/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        raise DeployError(f"GitHub CI status check failed: {type(exc).__name__}") from exc
    return ci_conclusion(payload)


def _safe_extract(archive: Path, destination: Path) -> None:
    root = destination.resolve()
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if root != target and root not in target.parents:
                raise DeployError("release archive contains an unsafe path")
            if member.issym() or member.islnk():
                raise DeployError("release archive contains an unsupported link")
        bundle.extractall(destination)


def _test_environment() -> dict[str, str]:
    result = dict(os.environ)
    for name in list(result):
        if name in {
            "TELEGRAM_BOT_TOKEN",
            "OUTLINE_API_URL",
            "OUTLINE_SERVERS_JSON",
            "OUTLINE_CERT_SHA256",
            "AURIX_ACCESS_URL_KEY",
            "COMMERCE_DATABASE_URL",
            "SUPABASE_URL",
            "SUPABASE_SERVICE_ROLE_KEY",
            "RECEIPT_LLM_BASE_URL",
            "RECEIPT_LLM_MODEL",
            "RECEIPT_LLM_API_KEY",
            "RECEIPT_LLM_FALLBACK_MODELS",
        }:
            result.pop(name, None)
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    return result


def make_release_traversable(release: Path) -> None:
    """Allow the unprivileged bot service to enter a root-built release."""
    release.chmod(0o755)


def build_release(repository: Path, sha: str) -> Path:
    RELEASES_DIR.mkdir(parents=True, exist_ok=True)
    target = RELEASES_DIR / sha
    if target.exists():
        if CURRENT_LINK.is_symlink() and CURRENT_LINK.resolve() == target.resolve():
            raise DeployError("refusing to replace the active release")
        shutil.rmtree(target)
    build = Path(tempfile.mkdtemp(prefix=f".build-{sha[:12]}-", dir=RELEASES_DIR))
    archive = STATE_DIR / f"{sha}.tar"
    try:
        run("git", "archive", "--format=tar", f"--output={archive}", sha, cwd=repository)
        _safe_extract(archive, build)
        run(sys.executable, "-m", "venv", str(build / ".venv"), timeout=180)
        python = build / ".venv/bin/python"
        run(
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--requirement",
            str(build / "requirements.txt"),
            timeout=600,
        )
        run(str(python), "-m", "compileall", "-q", ".", cwd=build, timeout=120)
        run(
            str(python),
            "-m",
            "unittest",
            "discover",
            "-q",
            cwd=build,
            timeout=300,
            env=_test_environment(),
        )
        run(
            str(python),
            str(build / "deploy/digitalocean_preflight.py"),
            "--live",
            cwd=build,
            timeout=120,
        )
        make_release_traversable(build)
        build.rename(target)
        return target
    except Exception:
        shutil.rmtree(build, ignore_errors=True)
        raise
    finally:
        archive.unlink(missing_ok=True)


def _replace_link(target: Path) -> None:
    temporary = CURRENT_LINK.parent / f".{CURRENT_LINK.name}-{os.getpid()}"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, CURRENT_LINK)


def _service_ready(started_at: int, timeout: int = 45) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        active = subprocess.run(
            ("systemctl", "is-active", "--quiet", SERVICE), check=False
        ).returncode == 0
        journal = subprocess.run(
            (
                "journalctl",
                "-u",
                SERVICE,
                "--since",
                f"@{started_at}",
                "--no-pager",
                "-o",
                "cat",
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        ).stdout
        if active and "Bot authorized:" in journal and "Outline connected:" in journal:
            return True
        time.sleep(2)
    return False


def activate_release(target: Path) -> None:
    old_target = CURRENT_LINK.resolve() if CURRENT_LINK.exists() else None
    _replace_link(target)
    started_at = int(time.time())
    try:
        run("systemctl", "restart", SERVICE, timeout=60)
        if not _service_ready(started_at):
            raise DeployError("new release did not pass Telegram/Outline startup health")
    except Exception:
        if old_target is not None and old_target.exists():
            _replace_link(old_target)
            subprocess.run(("systemctl", "restart", SERVICE), check=False)
        raise


def _write_state(sha: str) -> None:
    temporary = STATE_DIR / ".deployed-sha.tmp"
    temporary.write_text(sha + "\n", encoding="utf-8")
    os.replace(temporary, STATE_DIR / "deployed-sha")


def _cleanup_releases(keep: int = 3) -> None:
    current = CURRENT_LINK.resolve() if CURRENT_LINK.exists() else None
    releases = sorted(
        (
            item
            for item in RELEASES_DIR.iterdir()
            if item.is_dir() and SHA_PATTERN.fullmatch(item.name)
        ),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    retained = 0
    for item in releases:
        if current is not None and item.resolve() == current:
            continue
        retained += 1
        if retained >= keep:
            shutil.rmtree(item)


def deploy() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    RELEASES_DIR.mkdir(parents=True, exist_ok=True)
    missing = missing_release_configuration(dict(os.environ))
    if missing:
        print("AuriX deploy: blocked by missing production configuration: " + ", ".join(missing))
        return
    repository = STATE_DIR / "repository"
    if not (repository / ".git").is_dir():
        if repository.exists():
            shutil.rmtree(repository)
        run("git", "clone", "--no-tags", REPOSITORY, str(repository), timeout=180)
    run("git", "remote", "set-url", "origin", REPOSITORY, cwd=repository)
    run("git", "fetch", "--no-tags", "--prune", "origin", BRANCH, cwd=repository, timeout=180)
    sha = run("git", "rev-parse", f"origin/{BRANCH}", cwd=repository).stdout.strip()
    if not SHA_PATTERN.fullmatch(sha):
        raise DeployError("remote branch did not resolve to a commit")
    deployed_file = STATE_DIR / "deployed-sha"
    deployed = deployed_file.read_text(encoding="utf-8").strip() if deployed_file.exists() else ""
    if deployed == sha and CURRENT_LINK.exists():
        print(f"AuriX deploy: already current at {sha[:12]}")
        return
    conclusion = require_github_ci(sha)
    if conclusion == "pending":
        print(f"AuriX deploy: CI is pending for {sha[:12]}")
        return
    if conclusion != "success":
        raise DeployError(f"GitHub CI did not pass for {sha[:12]} ({conclusion})")
    target = build_release(repository, sha)
    activate_release(target)
    _write_state(sha)
    _cleanup_releases()
    print(f"AuriX deploy: activated {sha[:12]}")


def main() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "deploy.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("AuriX deploy: another deployment is already running")
            return
        try:
            deploy()
        except DeployError as exc:
            print(f"AuriX deploy failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
