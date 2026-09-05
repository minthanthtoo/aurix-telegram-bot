#!/usr/bin/env python3
"""Run the repeatable, sanitized AuriX production acceptance audit.

This command is an evidence gate, not a deployment mechanism.  It never
creates/deletes provider resources, changes allocations, or touches customer
keys.  A result of ``pass`` means every check requested by the selected mode
was observed successfully; warnings deliberately prevent a false 100% claim.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy.recovery_readiness import run_audit  # noqa: E402
from deploy.outline_diagnostics import run as run_outline_diagnostics  # noqa: E402

PASS = "pass"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _summarize(checks: list[dict[str, str]]) -> str:
    if any(item["status"] == FAIL for item in checks):
        return FAIL
    if any(item["status"] == WARN for item in checks):
        return WARN
    return PASS


def _default_env_file() -> str:
    """Choose the managed host env by default, with a local-dev fallback.

    Production releases are immutable archives and intentionally do not carry a
    ``.env`` file.  The systemd deployment writes the authoritative secrets to
    ``/etc/aurix-bot/aurix.env``.  Prefer that path when it exists, while keeping
    the command convenient for local checkouts and CI fixtures.
    """

    configured = os.environ.get("AURIX_FLEET_ENV_FILE")
    if configured:
        return configured
    managed = Path("/etc/aurix-bot/aurix.env")
    return str(managed) if managed.is_file() else ".env"


def _git_clean(root: Path, runner: Callable[..., subprocess.CompletedProcess[str]]) -> dict[str, str]:
    # Linked worktrees use a ``.git`` file that points at the common Git
    # directory.  Treat it as a real checkout so cleanliness is still checked;
    # only a release archive with no .git entry should be marked immutable.
    git_marker = root / ".git"
    if not git_marker.is_dir() and not git_marker.is_file():
        return _check("source_clean", SKIP, "deployed release is an immutable archive; CI checked the source checkout")
    try:
        result = runner(
            ("git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _check("source_clean", FAIL, f"git status failed: {type(exc).__name__}")
    if result.returncode != 0:
        return _check("source_clean", FAIL, "git status returned an error")
    changed = [line for line in result.stdout.splitlines() if line.strip()]
    if changed:
        return _check("source_clean", FAIL, f"working tree has {len(changed)} changed/untracked path(s)")
    return _check("source_clean", PASS, "working tree is clean")


def _tool_checks(root: Path, runner: Callable[..., subprocess.CompletedProcess[str]]) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    commands = [
        ("ruff", ("ruff", "check", ".")),
        ("compile", (sys.executable, "-m", "compileall", "-q", ".")),
        ("tests", (sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py")),
    ]
    for name, command in commands:
        if command[0] != sys.executable and shutil.which(command[0]) is None:
            checks.append(_check(name, SKIP, f"{command[0]} is not installed; rely on the CI gate"))
            continue
        try:
            result = runner(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=900 if name == "tests" else 180,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks.append(_check(name, FAIL, f"{name} failed: {type(exc).__name__}"))
            continue
        if result.returncode:
            checks.append(_check(name, FAIL, f"{name} returned exit code {result.returncode}"))
        else:
            checks.append(_check(name, PASS, f"{name} passed"))
    return checks


def _release_check(runner: Callable[..., subprocess.CompletedProcess[str]]) -> dict[str, str]:
    link = Path(os.environ.get("AURIX_DEPLOY_CURRENT_LINK", "/opt/aurix-current"))
    state = Path(os.environ.get("AURIX_DEPLOY_STATE_DIR", "/var/lib/aurix-deploy")) / "deployed-sha"
    if not link.is_symlink() or not link.resolve().is_dir():
        return _check("live_release", FAIL, "current release symlink is missing or invalid")
    try:
        deployed = state.read_text(encoding="utf-8").strip()
    except OSError:
        return _check("live_release", FAIL, "deployed release marker is missing")
    if len(deployed) != 40 or any(char not in "0123456789abcdef" for char in deployed.lower()):
        return _check("live_release", FAIL, "deployed release marker is invalid")
    return _check("live_release", PASS, f"release marker {deployed[:12]} is active")


def _service_checks(runner: Callable[..., subprocess.CompletedProcess[str]]) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    for unit in ("aurix-bot.service", "aurix-fleet-reconcile.timer", "aurix-infrastructure-worker.timer"):
        try:
            result = runner(("systemctl", "is-active", "--quiet", unit), check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks.append(_check(unit, FAIL, f"service check failed: {type(exc).__name__}"))
            continue
        checks.append(_check(unit, PASS, "active") if result.returncode == 0
                      else _check(unit, FAIL, "not active"))
    return checks


def _outline_check(env_file: Path) -> tuple[dict[str, str], dict[str, Any]]:
    """Run the read-only management/data-plane probe as one acceptance check."""
    try:
        report = run_outline_diagnostics(env_file)
    except Exception as exc:
        return (
            _check("outline_endpoints", FAIL, f"diagnostic failed: {type(exc).__name__}"),
            {"status": "invalid", "error": type(exc).__name__},
        )
    status = str(report.get("status") or "unreachable")
    healthy = int(report.get("healthy_servers") or 0)
    total = int(report.get("server_count") or 0)
    if status == "healthy":
        check = _check("outline_endpoints", PASS, f"{healthy}/{total} management and data-plane checks passed")
    elif healthy:
        check = _check("outline_endpoints", WARN, f"{healthy}/{total} Outline endpoint(s) healthy; see diagnostic details")
    else:
        check = _check("outline_endpoints", FAIL, "no configured Outline endpoint passed management/data-plane checks")
    return check, report


def run_acceptance(
    *,
    env_file: Path,
    verify_archives: bool = False,
    live: bool = False,
    outline: bool = False,
    root: Path = ROOT,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    checks = [_git_clean(root, runner)]
    checks.extend(_tool_checks(root, runner))
    try:
        readiness = run_audit(env_file, verify_archives=verify_archives)
    except (OSError, ValueError, RuntimeError) as exc:
        readiness = {"status": FAIL, "checks": [], "error": type(exc).__name__}
    readiness_status = str(readiness.get("status", FAIL))
    checks.append(_check("recovery_readiness", readiness_status, "see readiness_checks"))
    outline_report: dict[str, Any] | None = None
    if live or outline:
        outline_check, outline_report = _outline_check(env_file)
        checks.append(outline_check)
    if live:
        checks.append(_release_check(runner))
        checks.extend(_service_checks(runner))
    else:
        checks.append(_check("live_release", SKIP, "pass --live to inspect the deployed host"))
        checks.append(_check("live_services", SKIP, "pass --live to inspect systemd services"))
    status = _summarize(checks)
    result: dict[str, Any] = {
        "status": status,
        "checks": checks,
        "readiness_checks": readiness.get("checks", []),
    }
    if outline_report is not None:
        result["outline_diagnostics"] = outline_report
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=_default_env_file())
    parser.add_argument("--verify-archives", action="store_true")
    parser.add_argument("--outline", action="store_true", help="run the read-only Outline endpoint/data-port diagnostic")
    parser.add_argument("--live", action="store_true", help="also inspect release and systemd services")
    args = parser.parse_args(argv)
    report = run_acceptance(
        env_file=Path(args.env_file),
        verify_archives=args.verify_archives,
        live=args.live,
        outline=args.outline,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
