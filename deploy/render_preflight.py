#!/usr/bin/env python3
"""Fail closed on Render configuration errors without printing secrets."""

from __future__ import annotations

import os
import re
import json
import sqlite3
import urllib.error
import urllib.request
import argparse
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit

from cryptography.fernet import Fernet


PAYMENT_QR_ASSETS = {
    "kbzpay": "kbzpay.png",
    "wavepay": "wavepay.png",
    "ayapay": "ayapay.png",
    "uabpay": "uabpay.png",
    "cbpay": "cbpay.png",
}


def fail(message: str) -> None:
    raise SystemExit(f"Render preflight failed: {message}")


def _check_payment_qr_assets() -> None:
    """Refuse a release whose five customer payment cards are incomplete."""
    directory = Path(__file__).resolve().parents[1] / "assets" / "payment_qr"
    missing = [
        provider
        for provider, filename in PAYMENT_QR_ASSETS.items()
        if not (directory / filename).is_file() or (directory / filename).stat().st_size <= 0
    ]
    if missing:
        fail("missing or empty payment QR asset(s): " + ", ".join(missing))


def _json_request(url: str, headers: dict[str, str], timeout: int = 15) -> object:
    """Read one dependency endpoint without echoing credentials or payloads."""
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        host = urlsplit(url).hostname or "dependency"
        fail(f"live dependency check failed for {host}: {type(exc).__name__}")


def _validate_live(values: dict[str, object]) -> None:
    """Verify the external services required by the hosted profile.

    This is intentionally a read-only startup canary: Telegram ``getMe``, the
    private Supabase bucket metadata endpoint, the vision gateway model list,
    a database ``SELECT 1``, and each pinned Outline ``GET /server`` request.
    No payment, key, provider, or DNS mutation is performed.
    """
    telegram = _json_request(
        f"https://api.telegram.org/bot{values['telegram_token']}/getMe", {}, timeout=15
    )
    if not isinstance(telegram, dict) or not telegram.get("ok"):
        fail("Telegram getMe did not authorize the configured bot")

    bucket = quote(str(values["bucket"]), safe="")
    storage = _json_request(
        f"{values['supabase_url']}/storage/v1/bucket/{bucket}",
        {
            "Authorization": f"Bearer {values['supabase_key']}",
            "apikey": values["supabase_key"],
        },
    )
    if not isinstance(storage, dict):
        fail("Supabase receipt bucket check returned an invalid response")
    if storage.get("public") is not False:
        # Missing metadata is treated as unknown rather than silently allowing
        # a bucket whose visibility was not proven.
        fail("Supabase receipt bucket must remain private")

    models = _json_request(
        f"{values['llm_url']}/models",
        {"Authorization": f"Bearer {values['llm_key']}"},
        timeout=30,
    )
    if not isinstance(models, dict) or not isinstance(models.get("data"), list):
        fail("receipt vision model listing returned an invalid response")
    # A reachable gateway is not enough: some OpenAI-compatible providers
    # return 200 for /models while silently rejecting an unknown model at
    # inference time.  Verify every configured route is advertised before the
    # worker starts accepting receipts.  Keep this conditional for lightweight
    # compatibility callers that only exercise the dependency probe itself.
    configured_models = [str(values.get("llm_model") or "").strip()]
    configured_models.extend(
        str(item).strip()
        for item in (values.get("llm_fallback_models") or [])
        if str(item).strip()
    )
    if any(configured_models):
        advertised_models = {
            str(item.get("id") or "").strip()
            for item in models["data"]
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        }
        if not advertised_models:
            fail("receipt vision model listing contains no usable model IDs")
        missing_models = [item for item in configured_models if item and item not in advertised_models]
        if missing_models:
            fail("configured receipt vision model is not advertised by the gateway")

    database_url = str(values["database_url"])
    if database_url:
        try:
            import psycopg

            with psycopg.connect(database_url, connect_timeout=10) as connection:
                connection.execute("SELECT 1").fetchone()
        except Exception as exc:  # pragma: no cover - exercised by live deploys
            fail(f"PostgreSQL connectivity check failed: {type(exc).__name__}")
    else:
        try:
            database_uri = f"file:{Path(str(values['database_path'])).resolve()}?mode=rw"
            with sqlite3.connect(database_uri, timeout=5, uri=True) as connection:
                connection.execute("SELECT 1").fetchone()
        except sqlite3.Error as exc:
            fail(f"SQLite connectivity check failed: {type(exc).__name__}")

    try:
        from outline_adapter import OutlineClient

        for item in values["outline_servers"]:
            client = OutlineClient(
                item["api_url"],
                item["cert_sha256"],
                timeout_seconds=float(os.environ.get("OUTLINE_REQUEST_TIMEOUT_SECONDS", "5")),
            )
            client.server_info()
    except Exception as exc:  # pragma: no cover - exercised by live deploys
        fail(f"Outline management check failed: {type(exc).__name__}")


def main(argv: list[str] | None = None) -> None:
    _check_payment_qr_assets()
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
    outline_servers: list[dict[str, str]] = []
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
                outline_servers.append(
                    {
                        "api_url": str(server["api_url"]),
                        "cert_sha256": fingerprint,
                    }
                )
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
        outline_servers.append(
            {
                "api_url": os.environ["OUTLINE_API_URL"].strip(),
                "cert_sha256": fingerprint,
            }
        )

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

    registration_enabled = os.environ.get(
        "AURIX_FLEET_REGISTRATION_ENABLED", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}
    auto_registration = os.environ.get(
        "AURIX_FLEET_AUTO_REGISTRATION_ENABLED", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}
    if registration_enabled or auto_registration:
        registration_url = os.environ.get("AURIX_FLEET_REGISTRATION_URL", "").strip()
        parsed_registration = urlsplit(registration_url)
        if (
            parsed_registration.scheme != "https"
            or not parsed_registration.hostname
            or parsed_registration.path != "/fleet/register"
            or parsed_registration.fragment
            or parsed_registration.username
            or parsed_registration.password
        ):
            fail("AURIX_FLEET_REGISTRATION_URL must be a credential-free HTTPS URL")
        enrollment_key = os.environ.get("AURIX_FLEET_ENROLLMENT_KEY", "").strip()
        try:
            Fernet(enrollment_key.encode())
        except (TypeError, ValueError):
            fail("AURIX_FLEET_ENROLLMENT_KEY is not a valid Fernet key")
    if auto_registration and not registration_enabled:
        fail("AURIX_FLEET_AUTO_REGISTRATION_ENABLED requires AURIX_FLEET_REGISTRATION_ENABLED=1")

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
    vision_required = os.environ.get("RECEIPT_VISION_REQUIRED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if vision_required and not all(llm_values):
        missing = [
            name
            for name, value in zip(
                ("RECEIPT_LLM_BASE_URL", "RECEIPT_LLM_MODEL", "RECEIPT_LLM_API_KEY"),
                llm_values,
            )
            if not value
        ]
        fail(
            "RECEIPT_VISION_REQUIRED=1 requires configured receipt vision values: "
            + ", ".join(missing)
        )
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
    selection_mode = os.environ.get(
        "RECEIPT_LLM_SELECTION_MODE", "first_acceptable"
    ).strip().lower()
    if selection_mode not in {"first_acceptable", "rank_all", "consensus"}:
        fail("RECEIPT_LLM_SELECTION_MODE must be first_acceptable, rank_all, or consensus")
    if selection_mode == "consensus" and len(fallback_models) < 1:
        fail("consensus receipt selection requires at least one fallback model")
    if vision_required:
        recipients_raw = os.environ.get("PAYMENT_RECIPIENTS_JSON", "").strip()
        try:
            recipients = json.loads(recipients_raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            fail("PAYMENT_RECIPIENTS_JSON must be valid JSON when receipt vision is required")
        required_methods = {"kbzpay", "wavepay", "ayapay", "uabpay", "cbpay"}
        if not isinstance(recipients, dict) or not required_methods.issubset(recipients):
            fail("PAYMENT_RECIPIENTS_JSON must configure all five payment methods")
        for method in required_methods:
            profile = recipients.get(method)
            if not isinstance(profile, dict) or not (
                profile.get("names") or profile.get("accounts")
            ):
                fail(f"PAYMENT_RECIPIENTS_JSON profile is empty for {method}")

    values: dict[str, object] = {
        "telegram_token": os.environ["TELEGRAM_BOT_TOKEN"].strip(),
        "supabase_url": os.environ["SUPABASE_URL"].strip().rstrip("/"),
        "supabase_key": os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip(),
        "bucket": bucket,
        "llm_url": os.environ.get("RECEIPT_LLM_BASE_URL", "").strip().rstrip("/"),
        "llm_key": os.environ.get("RECEIPT_LLM_API_KEY", "").strip(),
        "llm_model": os.environ.get("RECEIPT_LLM_MODEL", "").strip(),
        "llm_fallback_models": fallback_models,
        "database_url": os.environ.get("COMMERCE_DATABASE_URL", "").strip(),
        "database_path": os.environ.get("DATABASE_PATH", "").strip(),
        "outline_servers": outline_servers,
    }
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="run read-only external dependency canaries")
    # Unit callers historically invoke ``main()`` directly while unittest's
    # own flags are present in ``sys.argv``.  Keep that API deterministic and
    # pass real CLI arguments explicitly from the module entrypoint.
    args = parser.parse_args([] if argv is None else argv)
    if args.live:
        _validate_live(values)

    profile = "persistent disk" if storage_mode == "disk" else "hosted PostgreSQL"
    if args.live:
        print(
            f"Render preflight passed: single-worker {profile} configuration and live dependencies are valid"
        )
    else:
        print(f"Render preflight passed: single-worker {profile} configuration is valid")


if __name__ == "__main__":
    main(sys.argv[1:])
