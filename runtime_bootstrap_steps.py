"""Explicit startup steps used by the runtime composition root."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Callable

from access_control import StaffAccessError
from commerce import CommerceError
from entitlements import PUBLIC_LIMIT_BYTES, OutlineError
from fleet_probe import FleetProbeError
from runtime_models import RuntimeApplication, RuntimeFactories
from runtime_outline import _build_outline, _group_staff
from runtime_settings import RuntimeSettings


@dataclass(frozen=True)
class RuntimeCollaborators:
    database: Any
    commerce_database: Any
    outline: Any
    commerce: Any
    probe_service: Any
    claim_service: Any
    staff_access: Any
    control_group_id: int | None
    staff: dict[str, Any]


def compose_runtime_application(
    *,
    settings: RuntimeSettings,
    factories: RuntimeFactories,
    urlopen: Callable[..., Any],
) -> RuntimeApplication:
    database, commerce_database = build_databases(settings, factories)
    receipt_storage = build_receipt_storage(settings, factories)
    outline, commerce, probe_service = build_commerce_runtime(
        settings, factories, commerce_database, receipt_storage
    )
    claim_service = build_claim_service(settings, factories, commerce_database, outline, probe_service)
    staff_access, control_group_id, staff = build_staff(settings, factories, commerce_database, urlopen)
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
    collaborators = RuntimeCollaborators(
        database=database,
        commerce_database=commerce_database,
        outline=outline,
        commerce=commerce,
        probe_service=probe_service,
        claim_service=claim_service,
        staff_access=staff_access,
        control_group_id=control_group_id,
        staff=staff,
    )
    return RuntimeApplication(
        database=collaborators.database,
        commerce_database=collaborators.commerce_database,
        outline=collaborators.outline,
        commerce=collaborators.commerce,
        claim_service=collaborators.claim_service,
        probe_service=collaborators.probe_service,
        staff_access=collaborators.staff_access,
        bot=bot,
    )


def build_databases(settings: RuntimeSettings, factories: RuntimeFactories) -> tuple[Any, Any]:
    if settings.commerce_database_url:
        database = factories.postgres_commerce_database(settings.commerce_database_url)
        commerce_database = database
    else:
        database = factories.database(settings.database_path)
        commerce_database = factories.commerce_database(database.path)
    database.initialize()
    return database, commerce_database


def build_receipt_storage(settings: RuntimeSettings, factories: RuntimeFactories) -> Any:
    if settings.supabase_url and settings.supabase_service_key:
        try:
            return factories.receipt_storage(
                settings.supabase_url,
                settings.supabase_service_key,
                settings.supabase_receipts_bucket,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    if settings.receipt_storage_required:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for receipt storage")
    return factories.null_receipt_storage()


def build_commerce_runtime(
    settings: RuntimeSettings,
    factories: RuntimeFactories,
    commerce_database: Any,
    receipt_storage: Any,
) -> tuple[Any, Any, Any]:
    outline, server_labels, provider_ids, endpoint_metadata = _build_outline(settings, factories)
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
                provider_resource_ids=provider_ids,
                endpoint_metadata=endpoint_metadata,
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
    sync_identity(settings, factories, commerce_database)
    reconcile_commerce(commerce)
    return outline, commerce, probe_service


def sync_identity(settings: RuntimeSettings, factories: RuntimeFactories, commerce_database: Any) -> None:
    try:
        identity_service = factories.identity_service(commerce_database)
        if callable(getattr(commerce_database, "connect", None)):
            identity_service.sync_existing_users()
            identity_service.sync_existing_entitlements()
    except Exception as exc:
        raise SystemExit(f"Identity backfill failed: {type(exc).__name__}") from exc


def reconcile_commerce(commerce: Any) -> None:
    order_reconciliation = commerce.reconcile_duplicate_open_orders()
    if order_reconciliation["cancelled"]:
        print(f"Reconciled {order_reconciliation['cancelled']} empty duplicate open order(s).")
    if order_reconciliation["manual_conflicts"]:
        print(
            "WARNING: duplicate open orders with payment evidence require manual review.",
            file=sys.stderr,
        )
    refresh_inventory = getattr(commerce, "refresh_server_inventory", None)
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
            outline_info = commerce.outline.server_info()
            print(f"Outline connected: version {outline_info.get('version', 'unknown')}")
        except OutlineError:
            print(
                "WARNING: Outline endpoint is unavailable; Telegram/admin recovery remains available.",
                file=sys.stderr,
            )


def build_claim_service(
    settings: RuntimeSettings,
    factories: RuntimeFactories,
    commerce_database: Any,
    outline: Any,
    probe_service: Any,
) -> Any:
    claim_service = factories.claim_service(
        commerce_database,
        outline,
        limit_bytes=PUBLIC_LIMIT_BYTES,
        probe_service=probe_service,
        access_url_key=settings.access_url_key,
    )
    try:
        reconciled = claim_service.reconcile_giveaway_limits()
    except OutlineError as exc:
        reconciled = 0
        print(f"WARNING: promo quota reconciliation deferred: {type(exc).__name__}", file=sys.stderr)
    if reconciled:
        print(f"Promo quotas reconciled: {reconciled} active key(s)")
    return claim_service


def build_staff(
    settings: RuntimeSettings,
    factories: RuntimeFactories,
    commerce_database: Any,
    urlopen: Callable[..., Any],
) -> tuple[Any, int | None, dict[str, Any]]:
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
    return staff_access, control_group_id, staff
