#!/usr/bin/env python3
"""Fail closed on Render configuration errors without printing secrets."""

from __future__ import annotations

import os
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
        "ADMIN_TELEGRAM_IDS",
        "OUTLINE_API_URL",
        "OUTLINE_CERT_SHA256",
        "AURIX_ACCESS_URL_KEY",
    )
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        fail("missing required environment variables: " + ", ".join(missing))

    try:
        admin_ids = {
            int(value.strip())
            for value in os.environ["ADMIN_TELEGRAM_IDS"].split(",")
            if value.strip()
        }
    except ValueError:
        fail("ADMIN_TELEGRAM_IDS must contain comma-separated numeric IDs")
    if not admin_ids or any(value <= 0 for value in admin_ids):
        fail("ADMIN_TELEGRAM_IDS must contain at least one positive numeric ID")

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

    profile = "persistent disk" if storage_mode == "disk" else "hosted PostgreSQL"
    print(f"Render preflight passed: single-worker {profile} configuration is valid")


if __name__ == "__main__":
    main()
