"""Runtime composition and environment-backed startup for AuriX."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .commerce import CommerceDatabase, CommerceService, PostgresCommerceDatabase
from .connectivity import (
    DEFAULT_ENDPOINT_ID,
    ConnectivityError,
    DigitalOceanClient,
    EndpointRegistry,
    EndpointScopedOutlineGateway,
    FleetController,
)
from .entitlements import PUBLIC_LIMIT_BYTES, ClaimService, OutlineError
from .free_repository import Database
from .outline_adapter import OutlineClient
from .node_agent_bindings import ManagedNodeAgentBindings, NodeAgentBindingError
from supabase_storage import NullReceiptStorage, SupabaseReceiptStorage
from .telegram_transport import DEFAULT_MAINTENANCE_INTERVAL_SECONDS, TelegramBot


@dataclass
class RuntimeServices:
    """Shared application services used by bot and authenticated web UI."""

    token: str
    database: Any
    commerce_database: Any
    outline: Any
    connectivity: EndpointRegistry
    claim_service: ClaimService
    commerce: CommerceService
    allow_text_payment: bool


def build_runtime_services(
    *,
    validate_telegram: bool = True,
    check_outline: bool = True,
    reconcile: bool = True,
    configure_bootstrap: bool = True,
) -> RuntimeServices:
    """Compose the durable AuriX services without starting a transport.

    The web service uses this same composition but does not run Telegram long
    polling.  Secrets remain server-side; callers receive service objects, not
    management URLs or bot credentials.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    api_url = os.environ.get("OUTLINE_API_URL", "")
    fingerprint = os.environ.get("OUTLINE_CERT_SHA256", "")
    access_url_key = os.environ.get("AURIX_ACCESS_URL_KEY", "")
    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", token),
            ("AURIX_ACCESS_URL_KEY", access_url_key),
        )
        if not value
    ]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))
    if bool(api_url) != bool(fingerprint):
        raise SystemExit("OUTLINE_API_URL and OUTLINE_CERT_SHA256 must be configured together")
    if validate_telegram:
        # Validate token with getMe before starting a transport.
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
    storage_mode = os.environ.get("AURIX_STORAGE_MODE", "disk").strip().lower()
    if storage_mode not in {"disk", "postgres"}:
        raise SystemExit("AURIX_STORAGE_MODE must be 'disk' or 'postgres'")
    commerce_database_url = os.environ.get("COMMERCE_DATABASE_URL", "").strip()
    if storage_mode == "postgres" and not commerce_database_url:
        raise SystemExit("COMMERCE_DATABASE_URL is required when AURIX_STORAGE_MODE=postgres")
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
    connectivity = EndpointRegistry(commerce_database, access_url_key)
    bootstrap_fallback = (
        OutlineClient(api_url, fingerprint)
        if api_url and fingerprint and not configure_bootstrap
        else None
    )
    outline = EndpointScopedOutlineGateway(connectivity, fallback=bootstrap_fallback)
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
        connectivity=connectivity,
    )
    try:
        managed_bindings = ManagedNodeAgentBindings.from_json(
            os.environ.get("AURIX_MANAGED_NODE_AGENTS_JSON"),
            adapter_registry=getattr(commerce, "adapter_registry", None),
        )
    except NodeAgentBindingError as exc:
        raise SystemExit(f"Invalid AURIX_MANAGED_NODE_AGENTS_JSON: {exc}") from exc
    if managed_bindings.configured:
        # Explicit bindings are opt-in. Registering their adapter contracts
        # keeps them visible as candidates; endpoint protocol profiles still
        # require fresh evidence and explicit promotion before allocation.
        commerce.managed_route_provider = managed_bindings.route_for
        commerce.managed_adapter_provider = managed_bindings.adapter_for
        commerce.managed_route_bindings = managed_bindings
    digitalocean_token = os.environ.get("DIGITALOCEAN_API_TOKEN", "").strip()
    commerce.fleet_controller = FleetController(
        commerce_database,
        DigitalOceanClient(digitalocean_token) if digitalocean_token else None,
        connectivity,
    )
    commerce.initialize()
    has_bootstrap_management = connectivity.has_management_capability(DEFAULT_ENDPOINT_ID)
    if not has_bootstrap_management and not (api_url and fingerprint):
        raise SystemExit(
            "OUTLINE_API_URL and OUTLINE_CERT_SHA256 are required to initialize the bootstrap endpoint"
        )
    if configure_bootstrap and api_url and fingerprint:
        # Persist bootstrap credentials before any gateway call. Health is
        # recorded only after the management API itself responds successfully.
        connectivity.configure_bootstrap(
            api_url,
            fingerprint,
            code=os.environ.get("AURIX_BOOTSTRAP_ENDPOINT_CODE", "SGP-01"),
            region=os.environ.get("AURIX_BOOTSTRAP_ENDPOINT_REGION", "sgp1"),
            mark_healthy=False,
        )
    if reconcile:
        backfill_assignments = getattr(connectivity, "backfill_free_assignments", None)
        backfilled_free_assignments = (
            backfill_assignments() if callable(backfill_assignments) else 0
        )
        if backfilled_free_assignments:
            print(f"Endpoint assignments backfilled: {backfilled_free_assignments} free key(s)")
        order_reconciliation = commerce.reconcile_duplicate_open_orders()
        if order_reconciliation["cancelled"]:
            print(f"Reconciled {order_reconciliation['cancelled']} empty duplicate open order(s).")
        if order_reconciliation["manual_conflicts"]:
            print(
                "WARNING: duplicate open orders with payment evidence require manual review.",
                file=sys.stderr,
            )
    # Keep construction compatible with the small test doubles used by the
    # legacy runtime tests, then share the real service seams when present.
    claim_service = ClaimService(
        database,
        outline,
        limit_bytes=PUBLIC_LIMIT_BYTES,
        access_url_key=access_url_key,
    )
    claim_service.connectivity = connectivity
    if getattr(commerce, "adapter_registry", None) is not None:
        claim_service.adapter_registry = commerce.adapter_registry
    if getattr(commerce, "identity", None) is not None:
        claim_service.identity = commerce.identity
    if check_outline:
        try:
            outline_info = outline.server_info()
            print(f"Outline connected: version {outline_info.get('version', 'unknown')}")
            if configure_bootstrap and api_url and fingerprint:
                connectivity.configure_bootstrap(
                    api_url,
                    fingerprint,
                    code=os.environ.get("AURIX_BOOTSTRAP_ENDPOINT_CODE", "SGP-01"),
                    region=os.environ.get("AURIX_BOOTSTRAP_ENDPOINT_REGION", "sgp1"),
                    outline_version=str(outline_info.get("version") or "unknown"),
                    mark_healthy=False,
                )
            connectivity.record_capacity(
                DEFAULT_ENDPOINT_ID,
                healthy=True,
                active_key_count=None,
                observed_transfer_bytes=None,
                management_latency_ms=None,
            )
            promo_limits_reconciled = claim_service.reconcile_giveaway_limits()
            if promo_limits_reconciled:
                print(f"Promo quotas reconciled: {promo_limits_reconciled} active key(s)")
        except (OutlineError, ConnectivityError) as exc:
            # Telegram, wallet, receipt review, and admin inspection remain useful
            # during a VPN management-plane outage. Provisioning fails closed.
            try:
                connectivity.record_capacity(
                    "legacy-default",
                    healthy=False,
                    active_key_count=None,
                    observed_transfer_bytes=None,
                    management_latency_ms=None,
                    last_error=type(exc).__name__,
                )
            except Exception:
                pass
            print(f"WARNING: Outline endpoint is degraded at startup: {exc}", file=sys.stderr)

    return RuntimeServices(
        token=token,
        database=database,
        commerce_database=commerce_database,
        outline=outline,
        connectivity=connectivity,
        claim_service=claim_service,
        commerce=commerce,
        allow_text_payment=allow_text_payment,
    )


def main() -> None:
    runtime = build_runtime_services()
    token = runtime.token
    database = runtime.database
    commerce_database = runtime.commerce_database
    claim_service = runtime.claim_service
    commerce = runtime.commerce
    allow_text_payment = runtime.allow_text_payment

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
    welcome_image_source = os.environ.get("AURIX_WELCOME_IMAGE", "").strip()
    if not welcome_image_source:
        # Keep the default generic and campaign-neutral.  Operators can point
        # this at a launch/promo card or a Telegram file_id without changing
        # the bot code; the checked-in avatar also works on Render and the
        # DigitalOcean systemd deployment because it is a repository asset.
        default_welcome_image = (
            Path(__file__).resolve().parents[1]
            / "brand"
            / "v4"
            / "exports"
            / "aurix-telegram-bot-avatar-v4-1024.png"
        )
        if default_welcome_image.is_file():
            welcome_image_source = str(default_welcome_image)
    bot = TelegramBot(
        token,
        claim_service,
        commerce,
        admin_ids,
        trial_ids,
        allow_text_payment=allow_text_payment,
        maintenance_interval_seconds=maintenance_interval_seconds,
        command_scope_cleanup_ids=command_scope_cleanup_ids,
        welcome_image_source=welcome_image_source,
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
