#!/usr/bin/env python3
"""Fail closed on Render configuration errors without printing secrets."""

from __future__ import annotations

import os
import re
import json
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet


def fail(message: str) -> None:
    raise SystemExit(f"Render preflight failed: {message}")


def main() -> None:
    storage_mode = os.environ.get("AURIX_STORAGE_MODE", "disk").strip().lower()
    if storage_mode not in {"disk", "postgres"}:
        fail("AURIX_STORAGE_MODE must be 'disk' or 'postgres'")

    required = (
        "TELEGRAM_BOT_TOKEN",
        "AURIX_ACCESS_URL_KEY",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
    )
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        fail("missing required environment variables: " + ", ".join(missing))
    servers_json = os.environ.get("OUTLINE_SERVERS_JSON", "").strip()
    provider_resource_id = os.environ.get("OUTLINE_PROVIDER_RESOURCE_ID", "").strip()
    if provider_resource_id and not re.fullmatch(r"\d{1,20}", provider_resource_id):
        fail("OUTLINE_PROVIDER_RESOURCE_ID must be a numeric Droplet ID")
    if not servers_json:
        outline_missing = [
            name
            for name in ("OUTLINE_API_URL", "OUTLINE_CERT_SHA256")
            if not os.environ.get(name, "").strip()
        ]
        if outline_missing:
            fail("missing required environment variables: " + ", ".join(outline_missing))

    for name in ("OWNER_TELEGRAM_ID", "ADMIN_TELEGRAM_IDS"):
        try:
            values = {
                int(value.strip())
                for value in os.environ.get(name, "").split(",")
                if value.strip()
            }
        except ValueError:
            fail(f"{name} must contain Telegram numeric IDs")
        if any(value <= 0 for value in values):
            fail(f"{name} must contain positive Telegram numeric IDs")
        if name == "OWNER_TELEGRAM_ID" and len(values) > 1:
            fail("OWNER_TELEGRAM_ID must contain exactly one ID")
    control_group = os.environ.get("AURIX_CONTROL_GROUP_ID", "").strip()
    if control_group:
        try:
            if int(control_group) >= 0:
                raise ValueError
        except ValueError:
            fail("AURIX_CONTROL_GROUP_ID must be a negative Telegram group ID")
    if not any(
        os.environ.get(name, "").strip()
        for name in ("OWNER_TELEGRAM_ID", "ADMIN_TELEGRAM_IDS", "AURIX_CONTROL_GROUP_ID")
    ):
        fail("configure OWNER_TELEGRAM_ID or the AuriX control group/admin bootstrap")

    if servers_json:
        try:
            servers = json.loads(servers_json)
            if not isinstance(servers, list) or not servers:
                raise ValueError
            seen = set()
            for server in servers:
                server_id = str(server.get("id") or "")
                provider_resource_id = str(server.get("provider_resource_id") or "").strip()
                parsed = urlsplit(str(server.get("api_url") or ""))
                fingerprint = str(server.get("cert_sha256") or "").lower().replace(":", "")
                if (
                    not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", server_id)
                    or server_id in seen
                    or parsed.scheme != "https"
                    or not parsed.hostname
                    or not parsed.path.strip("/")
                    or len(fingerprint) != 64
                    or any(char not in "0123456789abcdef" for char in fingerprint)
                    or (provider_resource_id and not re.fullmatch(r"\d{1,20}", provider_resource_id))
                ):
                    raise ValueError
                seen.add(server_id)
            default_id = os.environ.get("OUTLINE_DEFAULT_SERVER_ID", "").strip()
            if default_id and default_id not in seen:
                fail("OUTLINE_DEFAULT_SERVER_ID is not present in OUTLINE_SERVERS_JSON")
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            fail("OUTLINE_SERVERS_JSON contains an invalid server entry")
    else:
        parsed = urlsplit(os.environ["OUTLINE_API_URL"].strip())
        if parsed.scheme != "https" or not parsed.hostname or not parsed.path.strip("/"):
            fail("OUTLINE_API_URL must be the complete secret HTTPS management URL")
        fingerprint = os.environ["OUTLINE_CERT_SHA256"].lower().replace(":", "")
        if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
            fail("OUTLINE_CERT_SHA256 must contain exactly 64 hexadecimal characters")

    try:
        Fernet(os.environ["AURIX_ACCESS_URL_KEY"].encode())
    except (TypeError, ValueError):
        fail("AURIX_ACCESS_URL_KEY is not a valid Fernet key")

    supabase_url = urlsplit(os.environ["SUPABASE_URL"].strip())
    if (
        supabase_url.scheme != "https"
        or not supabase_url.netloc
        or supabase_url.query
        or supabase_url.fragment
    ):
        fail("SUPABASE_URL must be an https project URL without query parameters")
    bucket = os.environ.get("SUPABASE_RECEIPTS_BUCKET", "payment-receipts").strip()
    if not bucket or not re.fullmatch(r"[A-Za-z0-9_-]+", bucket):
        fail("SUPABASE_RECEIPTS_BUCKET contains invalid characters")
    if os.environ.get("RECEIPT_STORAGE_REQUIRED", "1").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        fail("RECEIPT_STORAGE_REQUIRED must be enabled for Render")

    if storage_mode == "disk":
        database_value = os.environ.get("DATABASE_PATH", "").strip()
        if not database_value:
            fail("DATABASE_PATH is required when AURIX_STORAGE_MODE=disk")
        database_path = Path(database_value)
        if not database_path.is_absolute():
            fail("DATABASE_PATH must be absolute on Render")
        if str(database_path) != "/var/data/bot.db":
            fail("DATABASE_PATH must be /var/data/bot.db so state uses the persistent disk")
        try:
            database_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            fail(f"the /var/data persistent disk is not accessible: {type(exc).__name__}")
        if not os.access(database_path.parent, os.W_OK):
            fail("the /var/data persistent disk is not writable")
    else:
        if not os.environ.get("COMMERCE_DATABASE_URL", "").strip():
            fail("COMMERCE_DATABASE_URL is required when AURIX_STORAGE_MODE=postgres")
        parsed_database = urlsplit(os.environ["COMMERCE_DATABASE_URL"].strip())
        if parsed_database.scheme not in {"postgres", "postgresql"} or not parsed_database.hostname:
            fail("COMMERCE_DATABASE_URL must be a PostgreSQL URL")

    if os.environ.get("ALLOW_TEXT_PAYMENT_REFERENCES", "0").strip().lower() in {
        "1", "true", "yes",
    }:
        fail("ALLOW_TEXT_PAYMENT_REFERENCES must remain disabled for production")

    llm_values = [
        os.environ.get("RECEIPT_LLM_BASE_URL", "").strip(),
        os.environ.get("RECEIPT_LLM_MODEL", "").strip(),
        os.environ.get("RECEIPT_LLM_API_KEY", "").strip(),
    ]
    if any(llm_values) and not all(llm_values):
        fail("configure all three RECEIPT_LLM_* values together or leave all blank")
    fallback_models = [
        item.strip()
        for item in os.environ.get("RECEIPT_LLM_FALLBACK_MODELS", "").split(",")
        if item.strip()
    ]
    if fallback_models and not all(llm_values):
        fail("receipt fallback models require the primary RECEIPT_LLM_* configuration")
    if len(fallback_models) > 3:
        fail("configure at most three receipt fallback models")

    profile = "persistent disk" if storage_mode == "disk" else "hosted PostgreSQL"
    print(f"Render preflight passed: single-worker {profile} configuration is valid")


if __name__ == "__main__":
    main()
