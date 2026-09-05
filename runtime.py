"""Runtime lifecycle and environment-backed startup for AuriX."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import urllib.error
import urllib.request

from access_control import StaffAccessControl, StaffAccessError
from commerce import CommerceDatabase, CommerceError, CommerceService, PostgresCommerceDatabase
from entitlements import PUBLIC_LIMIT_BYTES, ClaimService, OutlineError
from fleet_probe import FleetProbeError, FleetProbeService
from free_repository import Database
from identity import IdentityService
from outline_adapter import OutlineClient, OutlineServerPool
from runtime_composition import RuntimeFactories, RuntimeSettings, compose_application
from supabase_storage import NullReceiptStorage, SupabaseReceiptStorage
from telegram_transport import DEFAULT_MAINTENANCE_INTERVAL_SECONDS, TelegramBot


def main() -> None:
    settings = RuntimeSettings.from_environment(os.environ)
    # Validate token with getMe before constructing databases or application services.
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{settings.token}/getMe",
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

    application = compose_application(
        settings=settings,
        factories=RuntimeFactories(
            database=Database,
            commerce_database=CommerceDatabase,
            postgres_commerce_database=PostgresCommerceDatabase,
            receipt_storage=SupabaseReceiptStorage,
            null_receipt_storage=NullReceiptStorage,
            outline_client=OutlineClient,
            outline_server_pool=OutlineServerPool,
            commerce=CommerceService,
            probe_service=FleetProbeService,
            identity_service=IdentityService,
            claim_service=ClaimService,
            staff_access=StaffAccessControl,
            telegram_bot=TelegramBot,
        ),
        urlopen=urllib.request.urlopen,
    )
    bot = application.bot
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
        close_database = getattr(application.commerce_database, "close", None)
        if callable(close_database):
            close_database()
