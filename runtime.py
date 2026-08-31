"""Runtime composition and environment-backed startup for AuriX."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from commerce import CommerceDatabase, CommerceService, PostgresCommerceDatabase
from entitlements import PUBLIC_LIMIT_BYTES, ClaimService, OutlineError
from free_repository import Database
from outline_adapter import OutlineClient
from supabase_storage import NullReceiptStorage, SupabaseReceiptStorage
from telegram_transport import DEFAULT_MAINTENANCE_INTERVAL_SECONDS, TelegramBot


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    api_url = os.environ.get("OUTLINE_API_URL", "")
    fingerprint = os.environ.get("OUTLINE_CERT_SHA256", "")
    access_url_key = os.environ.get("AURIX_ACCESS_URL_KEY", "")
    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", token),
            ("OUTLINE_API_URL", api_url),
            ("OUTLINE_CERT_SHA256", fingerprint),
            ("AURIX_ACCESS_URL_KEY", access_url_key),
        )
        if not value
    ]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))
    # Validate token with getMe before starting
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getMe",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise SystemExit("Telegram getMe failed: " + str(result))
        print(f"Bot authorized: @{result['result'].get('username', 'unknown')}")
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Telegram getMe failed: {exc}")
    commerce_database_url = os.environ.get("COMMERCE_DATABASE_URL", "").strip()
    if commerce_database_url:
        # The free Render profile stores both free entitlements and commerce
        # state in one hosted PostgreSQL database.  This avoids losing claim
        # timestamps and Telegram-update deduplication on an ephemeral web FS.
        database: Any = PostgresCommerceDatabase(commerce_database_url)
        commerce_database: Any = database
    else:
        database = Database(Path(os.environ.get("DATABASE_PATH", "data/bot.db")))
        commerce_database = CommerceDatabase(database.path)
    receipt_storage_required = os.environ.get("RECEIPT_STORAGE_REQUIRED", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if bool(supabase_url) != bool(supabase_service_key):
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be configured together")
    if supabase_url and supabase_service_key:
        try:
            receipt_storage: Any = SupabaseReceiptStorage(
                supabase_url,
                supabase_service_key,
                os.environ.get("SUPABASE_RECEIPTS_BUCKET", "payment-receipts"),
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    else:
        if receipt_storage_required:
            raise SystemExit(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for receipt storage"
            )
        receipt_storage = NullReceiptStorage()
    database.initialize()
    outline = OutlineClient(api_url, fingerprint)
    allow_text_payment = os.environ.get("ALLOW_TEXT_PAYMENT_REFERENCES", "0").lower() in (
        "1",
        "true",
        "yes",
    )
    commerce = CommerceService(
        commerce_database,
        outline,
        access_url_key,
        allow_legacy_text_approval=allow_text_payment,
        receipt_storage=receipt_storage,
        receipt_storage_required=receipt_storage_required,
    )
    commerce.initialize()
    order_reconciliation = commerce.reconcile_duplicate_open_orders()
    if order_reconciliation["cancelled"]:
        print(f"Reconciled {order_reconciliation['cancelled']} empty duplicate open order(s).")
    if order_reconciliation["manual_conflicts"]:
        print(
            "WARNING: duplicate open orders with payment evidence require manual review.",
            file=sys.stderr,
        )
    try:
        outline_info = outline.server_info()
    except OutlineError as exc:
        raise SystemExit(f"Outline readiness check failed: {exc}") from exc
    print(f"Outline connected: version {outline_info.get('version', 'unknown')}")

    def parse_ids(name: str) -> set[int]:
        try:
            return {
                int(value.strip()) for value in os.environ.get(name, "").split(",") if value.strip()
            }
        except ValueError as exc:
            raise SystemExit(f"{name} must be comma-separated Telegram numeric IDs") from exc

    admin_ids = parse_ids("ADMIN_TELEGRAM_IDS")
    command_scope_cleanup_ids = parse_ids("ADMIN_SCOPE_CLEANUP_IDS")
    trial_ids = parse_ids("TRIAL_TELEGRAM_IDS")
    try:
        maintenance_interval_seconds = float(
            os.environ.get(
                "AURIX_MAINTENANCE_INTERVAL_SECONDS",
                str(DEFAULT_MAINTENANCE_INTERVAL_SECONDS),
            )
        )
    except ValueError as exc:
        raise SystemExit("AURIX_MAINTENANCE_INTERVAL_SECONDS must be numeric") from exc
    if maintenance_interval_seconds < 1:
        raise SystemExit("AURIX_MAINTENANCE_INTERVAL_SECONDS must be at least 1")
    if not admin_ids:
        print(
            "WARNING: ADMIN_TELEGRAM_IDS is empty; paid receipt verification and approvals are unavailable.",
            file=sys.stderr,
        )
    receipt_llm_config = [
        os.environ.get("RECEIPT_LLM_BASE_URL", "").strip(),
        os.environ.get("RECEIPT_LLM_MODEL", "").strip(),
        os.environ.get("RECEIPT_LLM_API_KEY", "").strip(),
    ]
    if any(receipt_llm_config) and not all(receipt_llm_config):
        raise SystemExit(
            "RECEIPT_LLM_BASE_URL, RECEIPT_LLM_MODEL, and RECEIPT_LLM_API_KEY must be configured together"
        )
    if not all(receipt_llm_config):
        print(
            "WARNING: receipt vision extraction is disabled; screenshots require manual transaction entry.",
            file=sys.stderr,
        )
    bot = TelegramBot(
        token,
        ClaimService(database, outline, limit_bytes=PUBLIC_LIMIT_BYTES),
        commerce,
        admin_ids,
        trial_ids,
        allow_text_payment=allow_text_payment,
        maintenance_interval_seconds=maintenance_interval_seconds,
        command_scope_cleanup_ids=command_scope_cleanup_ids,
    )
    # Long polling cannot coexist with a previously configured webhook. Keep
    # queued updates while explicitly converging the bot into polling mode.
    try:
        bot.request("deleteWebhook", {"drop_pending_updates": False})
    except Exception as exc:
        raise SystemExit(f"Telegram webhook cleanup failed: {type(exc).__name__}") from exc
    def configure_command_menu() -> None:
        try:
            bot.configure_commands()
        except Exception as exc:
            print(
                f"WARNING: Telegram command menu configuration failed: {type(exc).__name__}",
                file=sys.stderr,
            )

    # Command-scope synchronization takes several Telegram round trips. It is
    # administrative metadata and must not delay long polling after a restart.
    command_menu_thread = threading.Thread(
        target=configure_command_menu,
        name="aurix-command-menu",
        daemon=True,
    )
    command_menu_thread.start()
    signal.signal(signal.SIGTERM, lambda *_: bot.stop())
    try:
        bot.run()
    finally:
        command_menu_thread.join(timeout=1)
        close_database = getattr(commerce_database, "close", None)
        if callable(close_database):
            close_database()
