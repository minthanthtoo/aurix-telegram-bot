"""Runtime composition and environment-backed startup for AuriX."""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from access_control import StaffAccessControl, StaffAccessError
from commerce import CommerceDatabase, CommerceError, CommerceService, PostgresCommerceDatabase
from entitlements import PUBLIC_LIMIT_BYTES, ClaimService, OutlineError
from fleet_probe import FleetProbeError, FleetProbeService
from identity import IdentityService
from free_repository import Database
from outline_adapter import OutlineClient, OutlineServerPool
from supabase_storage import NullReceiptStorage, SupabaseReceiptStorage
from telegram_transport import DEFAULT_MAINTENANCE_INTERVAL_SECONDS, TelegramBot


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    api_url = os.environ.get("OUTLINE_API_URL", "")
    fingerprint = os.environ.get("OUTLINE_CERT_SHA256", "")
    servers_json = os.environ.get("OUTLINE_SERVERS_JSON", "").strip()
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
    if not servers_json and (not api_url or not fingerprint):
        raise SystemExit("Configure OUTLINE_API_URL and OUTLINE_CERT_SHA256, or OUTLINE_SERVERS_JSON")
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
    try:
        outline_timeout = float(os.environ.get("OUTLINE_REQUEST_TIMEOUT_SECONDS", "5"))
    except ValueError as exc:
        raise SystemExit("OUTLINE_REQUEST_TIMEOUT_SECONDS must be numeric") from exc
    if not 1.0 <= outline_timeout <= 30.0:
        raise SystemExit("OUTLINE_REQUEST_TIMEOUT_SECONDS must be between 1 and 30")
    try:
        outline_cooldown = float(os.environ.get("OUTLINE_CIRCUIT_BREAKER_SECONDS", "60"))
    except ValueError as exc:
        raise SystemExit("OUTLINE_CIRCUIT_BREAKER_SECONDS must be numeric") from exc
    if not 0.0 <= outline_cooldown <= 900.0:
        raise SystemExit("OUTLINE_CIRCUIT_BREAKER_SECONDS must be between 0 and 900")
    server_labels: dict[str, str] = {}
    server_provider_ids: dict[str, str] = {}
    server_endpoint_metadata: dict[str, dict[str, str]] = {}
    if servers_json:
        try:
            configured_servers = json.loads(servers_json)
            if not isinstance(configured_servers, list) or not configured_servers:
                raise ValueError
            clients = {}
            for item in configured_servers:
                if not isinstance(item, dict):
                    raise ValueError
                server_id = str(item.get("id") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", server_id) or server_id in clients:
                    raise ValueError
                clients[server_id] = OutlineClient(
                    str(item.get("api_url") or ""),
                    str(item.get("cert_sha256") or ""),
                    timeout_seconds=outline_timeout,
                    circuit_breaker_seconds=outline_cooldown,
                )
                server_labels[server_id] = str(item.get("label") or server_id)[:64]
                transport = str(item.get("transport") or "outline").strip().lower()
                if transport != "outline":
                    raise ValueError("only the outline transport is currently supported")
                server_endpoint_metadata[server_id] = {
                    "provider": str(item.get("provider") or "manual")[:64],
                    "region": str(item.get("region") or "unknown")[:64],
                    "transport": transport,
                }
                provider_resource_id = str(item.get("provider_resource_id") or "").strip()
                if provider_resource_id:
                    if not re.fullmatch(r"\d{1,20}", provider_resource_id):
                        raise ValueError("provider_resource_id must be a numeric Droplet ID")
                    server_provider_ids[server_id] = provider_resource_id
            default_server_id = str(
                os.environ.get("OUTLINE_DEFAULT_SERVER_ID", "").strip() or next(iter(clients))
            )
            outline: Any = OutlineServerPool(clients, default_server_id)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise SystemExit("OUTLINE_SERVERS_JSON must be a valid non-empty server array") from exc
    else:
        server_labels = {"primary": os.environ.get("OUTLINE_SERVER_LABEL", "Primary")[:64]}
        server_endpoint_metadata = {
            "primary": {
                "provider": os.environ.get("OUTLINE_PROVIDER", "manual")[:64],
                "region": os.environ.get("OUTLINE_REGION", "unknown")[:64],
                "transport": "outline",
            }
        }
        provider_resource_id = os.environ.get("OUTLINE_PROVIDER_RESOURCE_ID", "").strip()
        if provider_resource_id:
            if not re.fullmatch(r"\d{1,20}", provider_resource_id):
                raise SystemExit("OUTLINE_PROVIDER_RESOURCE_ID must be a numeric Droplet ID")
            server_provider_ids["primary"] = provider_resource_id
        outline = OutlineServerPool(
            {
                "primary": OutlineClient(
                    api_url,
                    fingerprint,
                    timeout_seconds=outline_timeout,
                    circuit_breaker_seconds=outline_cooldown,
                )
            },
            "primary",
        )
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
    try:
        probe_agent_secrets = json.loads(
            os.environ.get("AURIX_PROBE_AGENT_SECRETS_JSON", "{}").strip() or "{}"
        )
    except json.JSONDecodeError as exc:
        raise SystemExit("AURIX_PROBE_AGENT_SECRETS_JSON must be a JSON object") from exc
    if not isinstance(probe_agent_secrets, dict):
        raise SystemExit("AURIX_PROBE_AGENT_SECRETS_JSON must be a JSON object")
    try:
        probe_stale_after = int(os.environ.get("AURIX_PROBE_STALE_AFTER_SECONDS", "900"))
        probe_job_ttl = int(os.environ.get("AURIX_PROBE_JOB_TTL_SECONDS", "180"))
    except ValueError as exc:
        raise SystemExit("AURIX_PROBE_STALE_AFTER_SECONDS and AURIX_PROBE_JOB_TTL_SECONDS must be integers") from exc
    try:
        probe_service = FleetProbeService(
            commerce_database,
            agent_secrets={str(key): str(value) for key, value in probe_agent_secrets.items()},
            stale_after_seconds=probe_stale_after,
            job_ttl_seconds=probe_job_ttl,
        )
    except ValueError as exc:
        raise SystemExit(f"Fleet probe configuration is invalid: {exc}") from exc
    commerce.probe_service = probe_service
    if not probe_agent_secrets:
        print(
            "WARNING: fleet probes are configured without agent secrets; scheduling is available "
            "but node result submission is disabled.",
            file=sys.stderr,
        )
    register_servers = getattr(commerce, "register_outline_servers", None)
    if callable(register_servers):
        try:
            register_servers(
                server_labels,
                provider_resource_ids=server_provider_ids,
                endpoint_metadata=server_endpoint_metadata,
            )
        except CommerceError as exc:
            raise SystemExit(f"Outline server registration failed: {exc}") from exc
    try:
        configured_targets = json.loads(os.environ.get("AURIX_PROBE_TARGETS_JSON", "[]") or "[]")
        configured_schedules = json.loads(os.environ.get("AURIX_PROBE_SCHEDULES_JSON", "[]") or "[]")
        if not isinstance(configured_targets, list) or not isinstance(configured_schedules, list):
            raise ValueError("probe target and schedule settings must be arrays")
        for item in configured_targets:
            if not isinstance(item, dict):
                raise ValueError("probe targets must be objects")
            probe_service.register_target(**item)
        for item in configured_schedules:
            if not isinstance(item, dict):
                raise ValueError("probe schedules must be objects")
            probe_service.register_schedule(**item)
    except (ValueError, TypeError, json.JSONDecodeError, FleetProbeError) as exc:
        raise SystemExit(f"Fleet probe target configuration is invalid: {exc}") from exc
    # Endpoint registration rebuilds the provider-neutral credential registry;
    # only then can existing legacy credentials converge into generations and
    # device-manifest routes.
    try:
        identity_service = IdentityService(commerce_database)
        if callable(getattr(commerce_database, "connect", None)):
            identity_service.sync_existing_users()
            identity_service.sync_existing_entitlements()
    except Exception as exc:
        raise SystemExit(f"Identity backfill failed: {type(exc).__name__}") from exc
    order_reconciliation = commerce.reconcile_duplicate_open_orders()
    if order_reconciliation["cancelled"]:
        print(f"Reconciled {order_reconciliation['cancelled']} empty duplicate open order(s).")
    if order_reconciliation["manual_conflicts"]:
        print(
            "WARNING: duplicate open orders with payment evidence require manual review.",
            file=sys.stderr,
        )
    refresh_inventory = getattr(commerce, "refresh_server_inventory", None)
    inventory: list[dict[str, Any]] = []
    if callable(refresh_inventory):
        inventory = refresh_inventory()
        healthy_servers = sum(1 for item in inventory if item["status"] == "healthy")
        print(f"Outline inventory ready: {healthy_servers}/{len(inventory)} server(s) healthy")
        if healthy_servers:
            versions = sorted(
                {str(item.get("version") or "unknown") for item in inventory if item["status"] == "healthy"}
            )
            print(f"Outline connected: version {','.join(versions)}")
        else:
            print(
                "WARNING: no Outline endpoint is currently healthy; customer issuance is paused "
                "while Telegram/admin recovery remains available.",
                file=sys.stderr,
            )
    else:
        try:
            outline_info = outline.server_info()
            print(f"Outline connected: version {outline_info.get('version', 'unknown')}")
        except OutlineError:
            print(
                "WARNING: Outline endpoint is unavailable; Telegram/admin recovery remains available.",
                file=sys.stderr,
            )
    claim_service = ClaimService(
        database,
        outline,
        limit_bytes=PUBLIC_LIMIT_BYTES,
        probe_service=probe_service,
        access_url_key=access_url_key,
    )
    try:
        promo_limits_reconciled = claim_service.reconcile_giveaway_limits()
    except OutlineError as exc:
        promo_limits_reconciled = 0
        print(
            f"WARNING: promo quota reconciliation deferred: {type(exc).__name__}",
            file=sys.stderr,
        )
    if promo_limits_reconciled:
        print(f"Promo quotas reconciled: {promo_limits_reconciled} active key(s)")

    def parse_ids(name: str) -> set[int]:
        try:
            return {
                int(value.strip()) for value in os.environ.get(name, "").split(",") if value.strip()
            }
        except ValueError as exc:
            raise SystemExit(f"{name} must be comma-separated Telegram numeric IDs") from exc

    admin_ids = parse_ids("ADMIN_TELEGRAM_IDS")
    owner_ids = parse_ids("OWNER_TELEGRAM_ID")
    if len(owner_ids) > 1:
        raise SystemExit("OWNER_TELEGRAM_ID must contain exactly one Telegram numeric ID")
    owner_id = next(iter(owner_ids), None)
    control_group_value = os.environ.get("AURIX_CONTROL_GROUP_ID", "").strip()
    try:
        control_group_id = int(control_group_value) if control_group_value else None
    except ValueError as exc:
        raise SystemExit("AURIX_CONTROL_GROUP_ID must be a numeric Telegram group ID") from exc
    if control_group_id is not None and control_group_id >= 0:
        raise SystemExit("AURIX_CONTROL_GROUP_ID must be a negative Telegram group ID")

    staff_access = StaffAccessControl(database, owner_id)
    if control_group_id is None:
        stored_control_group = staff_access.control_group()
        if stored_control_group is not None:
            control_group_id = int(stored_control_group["control_group_id"])

    def group_staff() -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        if control_group_id is None:
            return None, []
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/getChatAdministrators",
            data=json.dumps({"chat_id": control_group_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.load(response)
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            raise StaffAccessError(
                "AuriX control-group administrators could not be loaded"
            ) from exc
        members = payload.get("result") if isinstance(payload, dict) and payload.get("ok") else None
        if not isinstance(members, list):
            raise StaffAccessError("AuriX control-group administrator response was invalid")
        owner: dict[str, Any] | None = None
        administrators: list[dict[str, Any]] = []
        for member in members:
            user = member.get("user") if isinstance(member, dict) else None
            if not isinstance(user, dict) or user.get("is_bot") or not isinstance(user.get("id"), int):
                continue
            profile = {
                "id": int(user["id"]),
                "username": user.get("username"),
                "display_name": " ".join(
                    part for part in (str(user.get("first_name") or "").strip(), str(user.get("last_name") or "").strip()) if part
                ),
                "is_bot": False,
            }
            if member.get("status") == "creator":
                owner = profile
            elif member.get("status") == "administrator":
                administrators.append(profile)
        return owner, administrators

    group_owner: dict[str, Any] | None = None
    group_admins: list[dict[str, Any]] = []
    if owner_id is None or not admin_ids:
        try:
            group_owner, group_admins = group_staff()
        except StaffAccessError as exc:
            print(f"WARNING: {exc}", file=sys.stderr)

    try:
        staff = staff_access.bootstrap(
            owner_id=owner_id,
            admin_ids=admin_ids,
            group_owner=group_owner,
            group_admins=group_admins,
        )
    except StaffAccessError as exc:
        raise SystemExit(f"Staff authorization failed: {exc}") from exc
    admin_ids = set(staff["admin_ids"])
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
    if not staff.get("owner_id"):
        print(
            "WARNING: no AuriX owner is configured; privileged operations are unavailable.",
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
        claim_service,
        commerce,
        admin_ids,
        trial_ids,
        allow_text_payment=allow_text_payment,
        maintenance_interval_seconds=maintenance_interval_seconds,
        command_scope_cleanup_ids=command_scope_cleanup_ids,
        staff_access=staff_access,
        control_group_id=control_group_id,
        probe_service=probe_service,
        device_api_url=os.environ.get("AURIX_DEVICE_API_URL", "").strip(),
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
