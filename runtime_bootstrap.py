"""Application collaborator construction and startup reconciliation."""

from __future__ import annotations

import sys
import urllib.request
from typing import Any, Callable, Mapping

from access_control import StaffAccessError
from commerce import CommerceError
from entitlements import PUBLIC_LIMIT_BYTES, OutlineError
from fleet_probe import FleetProbeError
from runtime_models import RuntimeApplication, RuntimeFactories
from runtime_outline import _build_outline, _group_staff
from runtime_settings import RuntimeSettings


def compose_application(
    *,
    settings: RuntimeSettings | None = None,
    env: Mapping[str, str] | None = None,
    factories: RuntimeFactories | None = None,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> RuntimeApplication:
    """Build and reconcile the application collaborators for one process."""
    settings = settings or RuntimeSettings.from_environment(env)
    factories = factories or RuntimeFactories()
    if settings.commerce_database_url:
        database = factories.postgres_commerce_database(settings.commerce_database_url)
        commerce_database = database
    else:
        database = factories.database(settings.database_path)
        commerce_database = factories.commerce_database(database.path)
    if settings.supabase_url and settings.supabase_service_key:
        try:
            receipt_storage = factories.receipt_storage(
                settings.supabase_url,
                settings.supabase_service_key,
                settings.supabase_receipts_bucket,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    else:
        if settings.receipt_storage_required:
            raise SystemExit(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for receipt storage"
            )
        receipt_storage = factories.null_receipt_storage()
    database.initialize()
    outline, server_labels, server_provider_ids, server_endpoint_metadata = _build_outline(
        settings, factories
    )
    commerce = factories.commerce(
        commerce_database,
        outline,
        settings.access_url_key,
        allow_legacy_text_approval=settings.allow_text_payment,
        receipt_storage=receipt_storage,
        receipt_storage_required=settings.receipt_storage_required,
    )
    commerce.initialize()
    try:
        probe_service = factories.probe_service(
            commerce_database,
            agent_secrets=settings.probe_agent_secrets,
            stale_after_seconds=settings.probe_stale_after,
            job_ttl_seconds=settings.probe_job_ttl,
        )
    except ValueError as exc:
        raise SystemExit(f"Fleet probe configuration is invalid: {exc}") from exc
    commerce.probe_service = probe_service
    if not settings.probe_agent_secrets:
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
        for item in settings.probe_targets:
            probe_service.register_target(**item)
        for item in settings.probe_schedules:
            probe_service.register_schedule(**item)
    except (ValueError, TypeError, FleetProbeError) as exc:
        raise SystemExit(f"Fleet probe target configuration is invalid: {exc}") from exc
    try:
        identity_service = factories.identity_service(commerce_database)
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
                {
                    str(item.get("version") or "unknown")
                    for item in inventory
                    if item["status"] == "healthy"
                }
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
    claim_service = factories.claim_service(
        commerce_database,
        outline,
        limit_bytes=PUBLIC_LIMIT_BYTES,
        probe_service=probe_service,
        access_url_key=settings.access_url_key,
    )
    try:
        promo_limits_reconciled = claim_service.reconcile_giveaway_limits()
    except OutlineError as exc:
        promo_limits_reconciled = 0
        print(f"WARNING: promo quota reconciliation deferred: {type(exc).__name__}", file=sys.stderr)
    if promo_limits_reconciled:
        print(f"Promo quotas reconciled: {promo_limits_reconciled} active key(s)")

    staff_access = factories.staff_access(commerce_database, settings.owner_id)
    control_group_id = settings.control_group_id
    if control_group_id is None:
        stored_control_group = staff_access.control_group()
        if stored_control_group is not None:
            control_group_id = int(stored_control_group["control_group_id"])
    group_owner: dict[str, Any] | None = None
    group_admins: list[dict[str, Any]] = []
    if settings.owner_id is None or not settings.admin_ids:
        try:
            group_owner, group_admins = _group_staff(settings.token, control_group_id, urlopen)
        except StaffAccessError as exc:
            print(f"WARNING: {exc}", file=sys.stderr)
    try:
        staff = staff_access.bootstrap(
            owner_id=settings.owner_id,
            admin_ids=settings.admin_ids,
            group_owner=group_owner,
            group_admins=group_admins,
        )
    except StaffAccessError as exc:
        raise SystemExit(f"Staff authorization failed: {exc}") from exc
    if not staff.get("owner_id"):
        print(
            "WARNING: no AuriX owner is configured; privileged operations are unavailable.",
            file=sys.stderr,
        )
    if not all(settings.receipt_llm_config):
        print(
            "WARNING: receipt vision extraction is disabled; screenshots require manual transaction entry.",
            file=sys.stderr,
        )
    bot = factories.telegram_bot(
        settings.token,
        claim_service,
        commerce,
        set(staff["admin_ids"]),
        settings.trial_ids,
        allow_text_payment=settings.allow_text_payment,
        maintenance_interval_seconds=settings.maintenance_interval_seconds,
        command_scope_cleanup_ids=settings.command_scope_cleanup_ids,
        staff_access=staff_access,
        control_group_id=control_group_id,
        probe_service=probe_service,
        device_api_url=settings.device_api_url,
    )
    return RuntimeApplication(
        database=database,
        commerce_database=commerce_database,
        outline=outline,
        commerce=commerce,
        claim_service=claim_service,
        probe_service=probe_service,
        staff_access=staff_access,
        bot=bot,
    )
