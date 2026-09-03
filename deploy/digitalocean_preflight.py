#!/usr/bin/env python3
"""Fail-closed DigitalOcean release checks without disclosing secrets."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit

from cryptography.fernet import Fernet

try:
    from deploy.fleet_reconcile import FleetError, parse_manifest
except ModuleNotFoundError:  # Direct execution sets deploy/ as sys.path[0].
    from fleet_reconcile import FleetError, parse_manifest


TRUTHY = {"1", "true", "yes", "on"}


def fail(message: str) -> None:
    raise SystemExit(f"DigitalOcean preflight failed: {message}")


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        fail(f"missing required environment variable: {name}")
    return value


def _validate_configuration() -> dict[str, str]:
    token = _required("TELEGRAM_BOT_TOKEN")
    access_key = _required("AURIX_ACCESS_URL_KEY")
    try:
        Fernet(access_key.encode())
    except (TypeError, ValueError):
        fail("AURIX_ACCESS_URL_KEY is not a valid Fernet key")

    owner = os.environ.get("OWNER_TELEGRAM_ID", "").strip()
    admins = os.environ.get("ADMIN_TELEGRAM_IDS", "").strip()
    control_group = os.environ.get("AURIX_CONTROL_GROUP_ID", "").strip()
    if not any((owner, admins, control_group)):
        fail("configure OWNER_TELEGRAM_ID, ADMIN_TELEGRAM_IDS, or AURIX_CONTROL_GROUP_ID")
    for name, raw in (("OWNER_TELEGRAM_ID", owner), ("ADMIN_TELEGRAM_IDS", admins)):
        try:
            values = [int(item.strip()) for item in raw.split(",") if item.strip()]
        except ValueError:
            fail(f"{name} must contain numeric Telegram IDs")
        if any(value <= 0 for value in values):
            fail(f"{name} must contain positive Telegram IDs")
    if control_group:
        try:
            if int(control_group) >= 0:
                raise ValueError
        except ValueError:
            fail("AURIX_CONTROL_GROUP_ID must be a negative Telegram group ID")

    servers_json = os.environ.get("OUTLINE_SERVERS_JSON", "").strip()
    provider_resource_id = os.environ.get("OUTLINE_PROVIDER_RESOURCE_ID", "").strip()
    if provider_resource_id and not re.fullmatch(r"\d{1,20}", provider_resource_id):
        fail("OUTLINE_PROVIDER_RESOURCE_ID must be a numeric Droplet ID")
    if servers_json:
        try:
            servers = json.loads(servers_json)
            if not isinstance(servers, list) or not servers:
                raise ValueError
            for server in servers:
                parsed = urlsplit(str(server.get("api_url") or ""))
                provider_resource_id = str(server.get("provider_resource_id") or "").strip()
                fingerprint = str(server.get("cert_sha256") or "").replace(":", "")
                if (
                    not str(server.get("id") or "").strip()
                    or parsed.scheme != "https"
                    or not parsed.hostname
                    or not parsed.path.strip("/")
                    or not re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint)
                    or (provider_resource_id and not re.fullmatch(r"\d{1,20}", provider_resource_id))
                ):
                    raise ValueError
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            fail("OUTLINE_SERVERS_JSON contains an invalid server entry")
    else:
        outline_url = urlsplit(_required("OUTLINE_API_URL"))
        fingerprint = _required("OUTLINE_CERT_SHA256").replace(":", "")
        if (
            outline_url.scheme != "https"
            or not outline_url.hostname
            or not outline_url.path.strip("/")
        ):
            fail("OUTLINE_API_URL must be the complete secret HTTPS management URL")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint):
            fail("OUTLINE_CERT_SHA256 must contain 64 hexadecimal characters")

    fleet_raw = os.environ.get("AURIX_FLEET_NODES_JSON", "").strip()
    if fleet_raw:
        try:
            fleet_nodes = parse_manifest(fleet_raw)
        except FleetError as exc:
            fail(str(exc))
        fleet_ids = {node.node_id for node in fleet_nodes}
        default_id = os.environ.get("OUTLINE_DEFAULT_SERVER_ID", "").strip()
        if default_id and default_id not in fleet_ids:
            fail("OUTLINE_DEFAULT_SERVER_ID must exist in AURIX_FLEET_NODES_JSON")
        for variable in ("AURIX_FLEET_SSH_KEY", "AURIX_FLEET_KNOWN_HOSTS"):
            configured_path = Path(_required(variable))
            if not configured_path.is_absolute() or not configured_path.is_file():
                fail(f"{variable} must be an existing absolute file")
        source = _required("AURIX_FLEET_CONTROL_PLANE_SOURCE")
        try:
            ipaddress.ip_network(source, strict=False)
        except ValueError:
            fail("AURIX_FLEET_CONTROL_PLANE_SOURCE must be an IP address or CIDR")
        backup_key = _required("AURIX_FLEET_BACKUP_KEY")
        try:
            Fernet(backup_key.encode())
        except (TypeError, ValueError):
            fail("AURIX_FLEET_BACKUP_KEY is not a valid Fernet key")
        offsite = os.environ.get("AURIX_FLEET_BACKUP_OFFSITE_DIR", "").strip()
        require_offsite = os.environ.get(
            "AURIX_FLEET_BACKUP_REQUIRE_OFFSITE", ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        if offsite:
            offsite_path = Path(offsite)
            if not offsite_path.is_absolute():
                fail("AURIX_FLEET_BACKUP_OFFSITE_DIR must be an absolute path")
            if require_offsite and not offsite_path.is_dir():
                fail("AURIX_FLEET_BACKUP_OFFSITE_DIR must exist when offsite is required")
        elif require_offsite:
            fail("AURIX_FLEET_BACKUP_REQUIRE_OFFSITE needs AURIX_FLEET_BACKUP_OFFSITE_DIR")

    database_url = os.environ.get("COMMERCE_DATABASE_URL", "").strip()
    database_path = os.environ.get("DATABASE_PATH", "").strip()
    if database_url:
        parsed_database = urlsplit(database_url)
        if parsed_database.scheme not in {"postgres", "postgresql"} or not parsed_database.hostname:
            fail("COMMERCE_DATABASE_URL must be a PostgreSQL URL")
    else:
        if not database_path or not Path(database_path).is_absolute():
            fail("DATABASE_PATH must be an absolute persistent path")
        resolved = Path(database_path).resolve()
        if str(resolved).startswith(("/opt/aurix-current/", "/opt/aurix-releases/")):
            fail("DATABASE_PATH must live outside versioned application releases")
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            fail(f"DATABASE_PATH parent is unavailable: {type(exc).__name__}")
        database_offsite = os.environ.get("AURIX_DATABASE_BACKUP_OFFSITE_DIR", "").strip()
        require_database_offsite = os.environ.get(
            "AURIX_DATABASE_BACKUP_REQUIRE_OFFSITE", ""
        ).strip().lower() in TRUTHY
        if database_offsite:
            database_offsite_path = Path(database_offsite)
            if not database_offsite_path.is_absolute():
                fail("AURIX_DATABASE_BACKUP_OFFSITE_DIR must be an absolute path")
            if require_database_offsite and not database_offsite_path.is_dir():
                fail("AURIX_DATABASE_BACKUP_OFFSITE_DIR must exist when offsite is required")
            database_backup_key = os.environ.get(
                "AURIX_DATABASE_BACKUP_KEY", os.environ.get("AURIX_FLEET_BACKUP_KEY", "")
            )
            try:
                Fernet(database_backup_key.encode())
            except (TypeError, ValueError):
                fail("AURIX_DATABASE_BACKUP_KEY or AURIX_FLEET_BACKUP_KEY is not a valid Fernet key")
        elif require_database_offsite:
            fail("AURIX_DATABASE_BACKUP_REQUIRE_OFFSITE needs AURIX_DATABASE_BACKUP_OFFSITE_DIR")

    if os.environ.get("ALLOW_TEXT_PAYMENT_REFERENCES", "0").strip().lower() in TRUTHY:
        fail("ALLOW_TEXT_PAYMENT_REFERENCES must remain disabled")
    if os.environ.get("RECEIPT_STORAGE_REQUIRED", "0").strip().lower() not in TRUTHY:
        fail("RECEIPT_STORAGE_REQUIRED must be enabled for receipt-backed payments")

    supabase_url = _required("SUPABASE_URL")
    supabase_key = _required("SUPABASE_SERVICE_ROLE_KEY")
    bucket = os.environ.get("SUPABASE_RECEIPTS_BUCKET", "payment-receipts").strip()
    parsed_supabase = urlsplit(supabase_url)
    if parsed_supabase.scheme != "https" or not parsed_supabase.hostname:
        fail("SUPABASE_URL must be an HTTPS project URL")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", bucket):
        fail("SUPABASE_RECEIPTS_BUCKET contains invalid characters")

    llm_url = _required("RECEIPT_LLM_BASE_URL")
    llm_model = _required("RECEIPT_LLM_MODEL")
    llm_key = _required("RECEIPT_LLM_API_KEY")
    parsed_llm = urlsplit(llm_url)
    if parsed_llm.scheme != "https" or not parsed_llm.hostname:
        fail("RECEIPT_LLM_BASE_URL must use HTTPS")
    if len(llm_key) < 12 or len(llm_model) > 200:
        fail("receipt vision credentials or model are invalid")
    recipients_raw = _required("PAYMENT_RECIPIENTS_JSON")
    try:
        recipients = json.loads(recipients_raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        fail("PAYMENT_RECIPIENTS_JSON must be valid JSON")
    required_methods = {"kbzpay", "wavepay", "ayapay", "uabpay", "cbpay"}
    if not isinstance(recipients, dict) or not required_methods.issubset(recipients):
        fail("PAYMENT_RECIPIENTS_JSON must configure all five payment methods")
    for method in required_methods:
        profile = recipients.get(method)
        if not isinstance(profile, dict) or not (profile.get("names") or profile.get("accounts")):
            fail(f"PAYMENT_RECIPIENTS_JSON profile is empty for {method}")

    return {
        "telegram_token": token,
        "database_url": database_url,
        "database_path": database_path,
        "supabase_url": supabase_url.rstrip("/"),
        "supabase_key": supabase_key,
        "bucket": bucket,
        "llm_url": llm_url.rstrip("/"),
        "llm_key": llm_key,
    }


def _json_request(url: str, headers: dict[str, str], timeout: int = 15) -> object:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        fail(f"live dependency check failed for {urlsplit(url).hostname}: {type(exc).__name__}")


def _validate_live(values: dict[str, str]) -> None:
    telegram = _json_request(
        f"https://api.telegram.org/bot{values['telegram_token']}/getMe", {}, timeout=15
    )
    if not isinstance(telegram, dict) or not telegram.get("ok"):
        fail("Telegram getMe did not authorize the configured bot")

    bucket = quote(values["bucket"], safe="")
    storage = _json_request(
        f"{values['supabase_url']}/storage/v1/bucket/{bucket}",
        {
            "Authorization": f"Bearer {values['supabase_key']}",
            "apikey": values["supabase_key"],
        },
    )
    if not isinstance(storage, dict):
        fail("Supabase receipt bucket check returned an invalid response")

    models = _json_request(
        f"{values['llm_url']}/models",
        {"Authorization": f"Bearer {values['llm_key']}"},
        timeout=30,
    )
    if not isinstance(models, dict) or not isinstance(models.get("data"), list):
        fail("receipt vision model listing returned an invalid response")

    if values["database_url"]:
        try:
            import psycopg

            with psycopg.connect(values["database_url"], connect_timeout=10) as connection:
                connection.execute("SELECT 1").fetchone()
        except Exception as exc:
            fail(f"PostgreSQL connectivity check failed: {type(exc).__name__}")
    else:
        try:
            database_uri = f"file:{quote(str(Path(values['database_path']).resolve()), safe='/')}?mode=rw"
            with sqlite3.connect(database_uri, timeout=5, uri=True) as connection:
                connection.execute("SELECT 1").fetchone()
        except sqlite3.Error as exc:
            fail(f"SQLite connectivity check failed: {type(exc).__name__}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="check configured external services")
    args = parser.parse_args(argv)
    values = _validate_configuration()
    if args.live:
        _validate_live(values)
    mode = "configuration and live dependencies" if args.live else "configuration"
    print(f"DigitalOcean preflight passed: {mode} are valid")


if __name__ == "__main__":
    main()
