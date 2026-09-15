#!/usr/bin/env python3
"""Preserve and recover local 9Router contributions across upstream updates.

This tool operates on a separate 9Router checkout. It never changes a source
checkout unless ``restore --apply`` is explicitly used, and it never reads
provider credentials from environment files into the report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


SENSITIVE_FILENAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.staging",
    "credentials.json",
    "secrets.json",
}
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
SENSITIVE_WORDS = ("secret", "password", "credential")


class GuardError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode:
        raise GuardError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def _repo(path: str | Path) -> Path:
    repo = Path(path).expanduser().resolve()
    if not (repo / ".git").exists():
        raise GuardError(f"not a Git checkout: {repo}")
    return repo


def _safe_remote(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme and parsed.netloc:
        return urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))
    if "@" in value and ":" in value:
        return value.rsplit("@", 1)[-1]
    return value


def _sensitive(path: str) -> bool:
    parts = [part.lower() for part in Path(path).parts]
    filename = parts[-1] if parts else ""
    if filename in SENSITIVE_FILENAMES or filename.endswith(SENSITIVE_SUFFIXES):
        return True
    if any(part.startswith(".") and part in {".secrets", ".credentials"} for part in parts):
        return True
    return any(word in filename for word in SENSITIVE_WORDS)


def _local_contributions(repo: Path, base: str | None) -> list[dict[str, str | None]]:
    """Record local commits without assuming a PR was accepted upstream."""

    if not base:
        return []
    raw = _git(repo, "log", "--reverse", "--format=%H%x09%an%x09%s", f"{base}..HEAD")
    contributions: list[dict[str, str | None]] = []
    for line in raw.splitlines():
        commit, author, subject = (line.split("\t", 2) + ["", "", ""])[:3]
        if not commit:
            continue
        contributions.append({
            "commit": commit,
            "author": author,
            "subject": subject,
            "status": "local-unreviewed",
            "upstream_pr": None,
        })
    return contributions


def inventory(repo_path: str | Path) -> dict[str, object]:
    repo = _repo(repo_path)
    status = _git(repo, "status", "--short")
    remotes = {}
    for line in _git(repo, "remote", "-v").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[2] == "(fetch)":
            remotes[parts[0]] = _safe_remote(parts[1])
    head = _git(repo, "rev-parse", "HEAD").strip()
    branch = _git(repo, "branch", "--show-current").strip() or None
    return {
        "schema": "aurix.9routerSourceInventory.v1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "repo": str(repo),
        "head": head,
        "branch": branch,
        "remotes": remotes,
        "dirty": bool(status.strip()),
        "status": status.splitlines(),
        "upstream_commit": _git(repo, "rev-parse", "--verify", "upstream/HEAD", check=False).strip()
        or None,
    }


def _changed_paths(repo: Path) -> list[str]:
    return [
        line.strip()
        for line in _git(repo, "diff", "--name-only", "HEAD").splitlines()
        if line.strip()
    ]


def snapshot(repo_path: str | Path, output_path: str | Path, *, base: str | None) -> dict[str, object]:
    repo = _repo(repo_path)
    output = Path(output_path).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    sensitive_tracked = [path for path in _changed_paths(repo) if _sensitive(path)]
    if sensitive_tracked:
        raise GuardError(
            "refusing to export changed sensitive paths: " + ", ".join(sensitive_tracked)
        )

    (output / "untracked").mkdir(exist_ok=True)
    (output / "commits").mkdir(exist_ok=True)
    tracked_patch = _git(repo, "diff", "--binary", "HEAD")
    (output / "working-tree.patch").write_text(tracked_patch, encoding="utf-8")

    untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    untracked_paths = [path for path in untracked.split("\0") if path]
    copied: list[str] = []
    skipped: list[str] = []
    for relative in untracked_paths:
        source = repo / relative
        if _sensitive(relative) or source.is_symlink() or not source.is_file():
            skipped.append(relative)
            continue
        destination = output / "untracked" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(relative)
    (output / "untracked-files.json").write_text(
        json.dumps({"copied": copied, "skipped": skipped}, indent=2) + "\n",
        encoding="utf-8",
    )

    bundle = output / "source.bundle"
    _git(repo, "bundle", "create", str(bundle), "--all")
    commit_patch = None
    if base:
        commit_patch = output / "commits" / "local-contributions.patch"
        with commit_patch.open("w", encoding="utf-8") as stream:
            result = subprocess.run(
                ["git", "-C", str(repo), "format-patch", "--binary", "--stdout", f"{base}..HEAD"],
                check=False,
                stdout=stream,
                stderr=subprocess.PIPE,
                text=True,
            )
        if result.returncode:
            raise GuardError(result.stderr.strip() or "format-patch failed")

    metadata = {
        **inventory(repo),
        "snapshot": {
            "base": base,
            "working_tree_patch": "working-tree.patch",
            "source_bundle": "source.bundle",
            "commit_patch": str(commit_patch.relative_to(output)) if commit_patch else None,
            "copied_untracked": copied,
            "skipped_sensitive": skipped,
        },
        "contributions": _local_contributions(repo, base),
    }
    (output / "manifest.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return metadata


def verify(snapshot_path: str | Path) -> dict[str, object]:
    root = Path(snapshot_path).expanduser().resolve()
    manifest = root / "manifest.json"
    bundle = root / "source.bundle"
    if not manifest.is_file() or not bundle.is_file():
        raise GuardError("snapshot requires manifest.json and source.bundle")
    result = subprocess.run(
        ["git", "bundle", "verify", str(bundle)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise GuardError(result.stderr.strip() or result.stdout.strip() or "bundle verification failed")
    return {
        "ok": True,
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        "bundle_verification": result.stdout.strip().splitlines()[-1:] or ["verified"],
    }


def restore(snapshot_path: str | Path, repo_path: str | Path, *, apply: bool) -> dict[str, object]:
    root = Path(snapshot_path).expanduser().resolve()
    repo = _repo(repo_path)
    verified = verify(root)
    patch = root / "working-tree.patch"
    untracked_manifest = json.loads((root / "untracked-files.json").read_text(encoding="utf-8"))
    if not apply:
        return {"mode": "check", **verified, "would_apply": patch.is_file(), "would_copy": untracked_manifest}
    if _git(repo, "status", "--porcelain").strip():
        raise GuardError("refusing restore into a dirty checkout")
    copy_plan: list[tuple[Path, Path, str]] = []
    for relative in untracked_manifest.get("copied", []):
        source = root / "untracked" / relative
        destination = repo / relative
        if not source.is_file():
            raise GuardError(f"snapshot is missing copied file: {relative}")
        if destination.exists():
            raise GuardError(f"restore would overwrite existing file: {relative}")
        copy_plan.append((source, destination, relative))
    if patch.is_file() and patch.stat().st_size:
        result = subprocess.run(
            ["git", "-C", str(repo), "apply", "--whitespace=nowarn", str(patch)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise GuardError(result.stderr.strip() or "working-tree patch could not be applied")
    copied: list[str] = []
    for source, destination, relative in copy_plan:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(relative)
    return {"mode": "applied", **verified, "copied_untracked": copied}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser("inventory")
    inventory_parser.add_argument("--repo", required=True)

    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--repo", required=True)
    snapshot_parser.add_argument("--output", required=True)
    snapshot_parser.add_argument("--base", help="upstream release/ref for a contribution patch")

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--snapshot", required=True)

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--snapshot", required=True)
    restore_parser.add_argument("--repo", required=True)
    restore_parser.add_argument("--apply", action="store_true", help="perform the restore")

    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            result = inventory(args.repo)
        elif args.command == "snapshot":
            result = snapshot(args.repo, args.output, base=args.base)
        elif args.command == "verify":
            result = verify(args.snapshot)
        else:
            result = restore(args.snapshot, args.repo, apply=args.apply)
    except (GuardError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
