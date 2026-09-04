#!/usr/bin/env python3
"""Sanitized recovery-readiness audit for AuriX control-plane revival."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy.fleet_backup import offsite_root, verify_node  # noqa: E402
from deploy.fleet_reconcile import (  # noqa: E402
    FleetError,
    FleetNode,
    load_dotenv,
    parse_manifest,
)
from deploy import database_backup  # noqa: E402
from deploy import dns_records  # noqa: E402
from deploy import offsite_storage  # noqa: E402

TRUTHY = {"1", "true", "yes", "on"}
FAIL = "fail"
WARN = "warn"
PASS = "pass"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUTHY


def has_value(env: dict[str, str], name: str) -> bool:
    return bool(env.get(name, "").strip())


def check_required(env: dict[str, str], names: list[str]) -> Check:
    missing = [name for name in names if not has_value(env, name)]
    if missing:
        return Check("required_secrets", FAIL, "missing " + ", ".join(missing))
    return Check("required_secrets", PASS, "all required secret names are present")


def check_repository(env: dict[str, str]) -> Check:
    repository = env.get(
        "AURIX_DEPLOY_REPOSITORY", "https://github.com/minthanthtoo/aurix-telegram-bot.git"
    ).strip()
    branch = env.get("AURIX_DEPLOY_BRANCH", "main").strip()
    parsed = urlsplit(repository)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        return Check("source_repository", FAIL, "AURIX_DEPLOY_REPOSITORY must be an HTTPS GitHub URL")
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,160}", branch):
        return Check("source_repository", FAIL, "AURIX_DEPLOY_BRANCH has invalid characters")
    return Check("source_repository", PASS, "GitHub source and branch are configured")


def check_database(env: dict[str, str], verify_archives: bool) -> Check:
    database_url = env.get("COMMERCE_DATABASE_URL", "").strip()
    if database_url:
        parsed = urlsplit(database_url)
        if parsed.scheme in {"postgres", "postgresql"} and parsed.hostname:
            return Check("database_recovery", PASS, "external PostgreSQL is configured")
        return Check("database_recovery", FAIL, "COMMERCE_DATABASE_URL must be PostgreSQL")
    if has_value(env, "DATABASE_PATH") and offsite_storage.configured(env):
        try:
            offsite_storage.from_env(env)
        except FleetError as exc:
            return Check("database_recovery", FAIL, str(exc))
        if not truthy(env.get("AURIX_DATABASE_BACKUP_REQUIRE_OFFSITE")):
            return Check("database_recovery", WARN, "offsite object storage is set but database REQUIRE_OFFSITE is not enabled")
        if not verify_archives:
            return Check("database_recovery", WARN, "run with --verify-archives to prove SQLite backup decryptability")
        try:
            database_backup.verify(env)
        except (FleetError, OSError, ValueError) as exc:
            return Check("database_recovery", FAIL, str(exc))
        return Check("database_recovery", PASS, "SQLite local/offsite-object-store backups are verified")
    if has_value(env, "DATABASE_PATH") and has_value(env, "AURIX_DATABASE_BACKUP_OFFSITE_DIR"):
        path = Path(env["AURIX_DATABASE_BACKUP_OFFSITE_DIR"]).expanduser()
        if not path.is_absolute():
            return Check("database_recovery", FAIL, "AURIX_DATABASE_BACKUP_OFFSITE_DIR must be absolute")
        if not truthy(env.get("AURIX_DATABASE_BACKUP_REQUIRE_OFFSITE")):
            return Check("database_recovery", WARN, "SQLite offsite path is set but REQUIRE_OFFSITE is not enabled")
        if not verify_archives:
            return Check("database_recovery", WARN, "run with --verify-archives to prove SQLite backup decryptability")
        try:
            database_backup.verify(env)
        except (FleetError, OSError, ValueError) as exc:
            return Check("database_recovery", FAIL, str(exc))
        return Check("database_recovery", PASS, "SQLite local/offsite backups are verified")
    return Check(
        "database_recovery",
        FAIL,
        "use COMMERCE_DATABASE_URL or configure DATABASE_PATH plus an offsite object store/path",
    )


def check_fleet_manifest(env: dict[str, str]) -> tuple[Check, list[FleetNode]]:
    raw = env.get("AURIX_FLEET_NODES_JSON", "").strip()
    if not raw:
        return Check("fleet_manifest", WARN, "no fleet manifest configured"), []
    try:
        strict_allocations = truthy(env.get("AURIX_FLEET_STRICT_ALLOCATION_VALIDATION"))
        nodes = parse_manifest(raw, strict_allocations=strict_allocations)
    except FleetError as exc:
        return Check("fleet_manifest", FAIL, str(exc)), []
    return Check("fleet_manifest", PASS, f"{len(nodes)} fleet node(s) configured"), nodes


def check_allocation_policy(env: dict[str, str], nodes: list[FleetNode]) -> Check:
    """Make compatibility-mode over-allocation visible in every readiness audit."""
    if not nodes:
        return Check("allocation_policy", WARN, "not required without fleet nodes")
    overallocated = []
    for node in nodes:
        saleable = max(0, int(node.max_keys) - int(node.reserved_keys))
        allocated = sum(node.plan_slots.values()) + sum(node.tier_slots.values())
        if allocated > saleable:
            overallocated.append(f"{node.node_id}={allocated}/{saleable}")
    orphaned = _sqlite_orphan_inventory(env)
    strict = truthy(env.get("AURIX_FLEET_STRICT_ALLOCATION_VALIDATION"))
    if strict and overallocated:
        detail = "strict allocation validation rejected over-allocation"
        if orphaned:
            detail += "; untracked remote keys must also be audited (" + ", ".join(orphaned) + ")"
        return Check("allocation_policy", FAIL, detail)
    if strict and orphaned:
        return Check(
            "allocation_policy",
            FAIL,
            "strict allocation validation is blocked by untracked remote keys ("
            + ", ".join(orphaned)
            + ")",
        )
    if overallocated:
        detail = "legacy over-allocation; enable strict validation after owner normalization ("
        detail += ", ".join(overallocated) + ")"
        if orphaned:
            detail += "; audit untracked remote keys (" + ", ".join(orphaned) + ")"
        return Check("allocation_policy", WARN, detail)
    if orphaned:
        return Check(
            "allocation_policy",
            WARN,
            "audit untracked remote keys before enabling strict allocation ("
            + ", ".join(orphaned)
            + ")",
        )
    return Check("allocation_policy", PASS, "declared plan/tier slots fit saleable key headroom")


def _sqlite_orphan_inventory(env: dict[str, str]) -> list[str]:
    """Read only the sanitized orphan counters when the MVP uses SQLite.

    Readiness must never open or mutate a hosted PostgreSQL connection. The
    live SQLite schema is maintained by the bot's inventory reconciler; a
    missing/legacy schema simply leaves this optional signal unavailable.
    """
    if has_value(env, "COMMERCE_DATABASE_URL"):
        return []
    raw_path = env.get("DATABASE_PATH", "").strip()
    if not raw_path:
        return []
    path = Path(raw_path).expanduser()
    if not path.is_absolute() or not path.is_file():
        return []
    try:
        with sqlite3.connect(path) as connection:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(outline_servers)").fetchall()
            }
            if "remote_orphan_key_count" not in columns:
                return []
            rows = connection.execute(
                """SELECT server_id, remote_orphan_key_count FROM outline_servers
                   WHERE remote_orphan_key_count > 0 ORDER BY server_id"""
            ).fetchall()
    except sqlite3.Error:
        return []
    return [f"{str(row[0])}={int(row[1])}" for row in rows]


def check_backup_secret(env: dict[str, str], has_fleet: bool) -> Check:
    if not has_fleet:
        return Check("fleet_backup_key", WARN, "not required without fleet nodes")
    if not has_value(env, "AURIX_FLEET_BACKUP_KEY"):
        return Check("fleet_backup_key", FAIL, "missing AURIX_FLEET_BACKUP_KEY")
    try:
        Fernet(env["AURIX_FLEET_BACKUP_KEY"].encode())
    except (TypeError, ValueError):
        return Check("fleet_backup_key", FAIL, "AURIX_FLEET_BACKUP_KEY is invalid")
    return Check("fleet_backup_key", PASS, "fleet backup key is valid")


def check_offsite_config(env: dict[str, str], nodes: list[FleetNode]) -> Check:
    if not nodes:
        return Check("fleet_offsite", WARN, "not required without fleet nodes")
    if offsite_storage.configured(env):
        try:
            offsite_storage.from_env(env)
        except FleetError as exc:
            return Check("fleet_offsite", FAIL, str(exc))
        if not truthy(env.get("AURIX_FLEET_BACKUP_REQUIRE_OFFSITE")):
            return Check("fleet_offsite", WARN, "offsite object storage is set but fleet REQUIRE_OFFSITE is not enabled")
        return Check("fleet_offsite", PASS, "offsite-object-store fleet backups are required")
    raw = env.get("AURIX_FLEET_BACKUP_OFFSITE_DIR", "").strip()
    if not raw:
        return Check("fleet_offsite", FAIL, "missing AURIX_FLEET_BACKUP_OFFSITE_DIR")
    root = Path(raw).expanduser()
    if not root.is_absolute():
        return Check("fleet_offsite", FAIL, "AURIX_FLEET_BACKUP_OFFSITE_DIR must be absolute")
    if not truthy(env.get("AURIX_FLEET_BACKUP_REQUIRE_OFFSITE")):
        return Check("fleet_offsite", WARN, "offsite path is set but REQUIRE_OFFSITE is not enabled")
    missing = [node.node_id for node in nodes if offsite_root(env, node) is None]
    if missing:
        return Check("fleet_offsite", FAIL, "missing offsite roots for " + ", ".join(missing))
    return Check("fleet_offsite", PASS, "offsite fleet backups are required")


def check_backup_archives(env: dict[str, str], nodes: list[FleetNode], verify: bool) -> Check:
    if not nodes:
        return Check("fleet_backup_archives", WARN, "not required without fleet nodes")
    if not verify:
        return Check("fleet_backup_archives", WARN, "run with --verify-archives to prove backup decryptability")
    try:
        verified = [verify_node(node, env)["node"] for node in nodes]
    except (FleetError, OSError, ValueError) as exc:
        return Check("fleet_backup_archives", FAIL, str(exc))
    return Check("fleet_backup_archives", PASS, "verified archives for " + ", ".join(verified))


def check_provider(env: dict[str, str]) -> Check:
    if not truthy(env.get("AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED")):
        return Check("provider_automation", WARN, "provider mutations are disabled")
    if has_value(env, "DIGITALOCEAN_API_TOKEN"):
        key_ids = [
            item.strip()
            for item in env.get("AURIX_DIGITALOCEAN_SSH_KEY_IDS", "").split(",")
            if item.strip()
        ]
        if not key_ids:
            return Check(
                "provider_automation",
                FAIL,
                "AURIX_DIGITALOCEAN_SSH_KEY_IDS is required for reachable provider bootstrap",
            )
        if len(key_ids) > 10 or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", value)
            for value in key_ids
        ):
            return Check("provider_automation", FAIL, "AURIX_DIGITALOCEAN_SSH_KEY_IDS is invalid")
        return Check(
            "provider_automation",
            PASS,
            "DigitalOcean provider token and SSH-key attachment are configured",
        )
    return Check("provider_automation", FAIL, "provider mutations enabled without DIGITALOCEAN_API_TOKEN")


def check_enrollment(env: dict[str, str]) -> Check:
    """Validate the optional callback contract without making network calls."""
    registration = truthy(env.get("AURIX_FLEET_REGISTRATION_ENABLED"))
    automatic = truthy(env.get("AURIX_FLEET_AUTO_REGISTRATION_ENABLED"))
    if automatic and not registration:
        return Check(
            "fleet_enrollment",
            FAIL,
            "AURIX_FLEET_AUTO_REGISTRATION_ENABLED requires AURIX_FLEET_REGISTRATION_ENABLED=1",
        )
    if not registration:
        return Check("fleet_enrollment", PASS, "zero-touch enrollment is disabled")
    parsed = urlsplit(env.get("AURIX_FLEET_REGISTRATION_URL", "").strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path != "/fleet/register"
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        return Check("fleet_enrollment", FAIL, "registration URL must be a credential-free HTTPS URL")
    try:
        Fernet(env.get("AURIX_FLEET_ENROLLMENT_KEY", "").encode())
    except (TypeError, ValueError):
        return Check("fleet_enrollment", FAIL, "AURIX_FLEET_ENROLLMENT_KEY is invalid")
    return Check("fleet_enrollment", PASS, "HTTPS enrollment callback and encryption key are configured")


def check_dns(env: dict[str, str], nodes: list[FleetNode]) -> Check:
    if not nodes:
        required = truthy(env.get("AURIX_DNS_REQUIRE"))
        return Check(
            "dns_automation",
            FAIL if required else WARN,
            "DNS is required but no fleet nodes are configured"
            if required
            else "not required without fleet nodes",
        )
    if dns_records.configured(env):
        try:
            config = dns_records.from_env(env)
            dns_records.desired_records(nodes, config)
        except (FleetError, ValueError) as exc:
            return Check("dns_automation", FAIL, str(exc))
        return Check("dns_automation", PASS, "Cloudflare DNS endpoint sync is configured")
    if truthy(env.get("AURIX_DNS_REQUIRE")):
        return Check("dns_automation", FAIL, "AURIX_DNS_REQUIRE=1 but DNS automation is not configured")
    return Check("dns_automation", WARN, "stable DNS automation is not configured")


def check_recovery_entrypoint() -> Check:
    path = ROOT / "deploy" / "recover_control_plane.sh"
    if path.is_file() and os.access(path, os.X_OK):
        return Check("recovery_entrypoint", PASS, "recover_control_plane.sh is executable")
    return Check("recovery_entrypoint", FAIL, "deploy/recover_control_plane.sh is missing or not executable")


def summarize(checks: list[Check]) -> str:
    if any(check.status == FAIL for check in checks):
        return FAIL
    if any(check.status == WARN for check in checks):
        return WARN
    return PASS


def run_audit(env_file: Path, verify_archives: bool) -> dict[str, object]:
    # The explicit recovery file is authoritative. A long-lived shell or
    # service may already contain stale values; do not let those shadow the
    # file being audited. The recovery contract requires a self-contained
    # canonical env file, so values absent from it are intentionally absent
    # from this audit rather than inherited from the process.
    loaded = load_dotenv(env_file, overwrite=False)
    env = dict(loaded)
    fleet_check, nodes = check_fleet_manifest(env)
    checks = [
        check_required(env, [
            "TELEGRAM_BOT_TOKEN",
            "AURIX_ACCESS_URL_KEY",
            "SUPABASE_URL",
            "SUPABASE_SERVICE_ROLE_KEY",
            "RECEIPT_LLM_BASE_URL",
            "RECEIPT_LLM_MODEL",
            "RECEIPT_LLM_API_KEY",
        ]),
        check_repository(env),
        check_database(env, verify_archives),
        fleet_check,
        check_allocation_policy(env, nodes),
        check_backup_secret(env, bool(nodes)),
        check_offsite_config(env, nodes),
        check_backup_archives(env, nodes, verify_archives),
        check_provider(env),
        check_enrollment(env),
        check_dns(env, nodes),
        check_recovery_entrypoint(),
    ]
    return {
        "status": summarize(checks),
        "checks": [check.__dict__ for check in checks],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=os.environ.get(
        "AURIX_FLEET_ENV_FILE", "/etc/aurix-bot/aurix.env"))
    parser.add_argument("--verify-archives", action="store_true")
    args = parser.parse_args()
    try:
        report = run_audit(Path(args.env_file), args.verify_archives)
    except (FleetError, OSError, ValueError) as exc:
        print(f"recovery readiness failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(report, indent=2))
    if report["status"] == FAIL:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
