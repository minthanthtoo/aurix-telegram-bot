"""Validated environment settings for one AuriX process."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from telegram_transport import DEFAULT_MAINTENANCE_INTERVAL_SECONDS


@dataclass(frozen=True)
class RuntimeSettings:
    """Validated, normalized process settings used by the composition root."""

    token: str
    access_url_key: str
    api_url: str
    fingerprint: str
    servers_json: str
    database_path: Path
    commerce_database_url: str
    receipt_storage_required: bool
    supabase_url: str
    supabase_service_key: str
    supabase_receipts_bucket: str
    outline_timeout: float
    outline_cooldown: float
    server_label: str
    provider: str
    region: str
    provider_resource_id: str
    default_server_id: str
    allow_text_payment: bool
    probe_agent_secrets: dict[str, str]
    probe_stale_after: int
    probe_job_ttl: int
    probe_targets: tuple[dict[str, Any], ...]
    probe_schedules: tuple[dict[str, Any], ...]
    admin_ids: set[int]
    owner_id: int | None
    control_group_id: int | None
    command_scope_cleanup_ids: set[int]
    trial_ids: set[int]
    maintenance_interval_seconds: float
    receipt_llm_config: tuple[str, str, str]
    device_api_url: str

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> RuntimeSettings:
        settings = env if env is not None else os.environ
        token = settings.get("TELEGRAM_BOT_TOKEN", "")
        access_url_key = settings.get("AURIX_ACCESS_URL_KEY", "")
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
        api_url = settings.get("OUTLINE_API_URL", "")
        fingerprint = settings.get("OUTLINE_CERT_SHA256", "")
        servers_json = settings.get("OUTLINE_SERVERS_JSON", "").strip()
        if not servers_json and (not api_url or not fingerprint):
            raise SystemExit(
                "Configure OUTLINE_API_URL and OUTLINE_CERT_SHA256, or OUTLINE_SERVERS_JSON"
            )
        try:
            outline_timeout = float(settings.get("OUTLINE_REQUEST_TIMEOUT_SECONDS", "5"))
        except ValueError as exc:
            raise SystemExit("OUTLINE_REQUEST_TIMEOUT_SECONDS must be numeric") from exc
        if not 1.0 <= outline_timeout <= 30.0:
            raise SystemExit("OUTLINE_REQUEST_TIMEOUT_SECONDS must be between 1 and 30")
        try:
            outline_cooldown = float(settings.get("OUTLINE_CIRCUIT_BREAKER_SECONDS", "60"))
        except ValueError as exc:
            raise SystemExit("OUTLINE_CIRCUIT_BREAKER_SECONDS must be numeric") from exc
        if not 0.0 <= outline_cooldown <= 900.0:
            raise SystemExit("OUTLINE_CIRCUIT_BREAKER_SECONDS must be between 0 and 900")

        commerce_database_url = settings.get("COMMERCE_DATABASE_URL", "").strip()
        receipt_storage_required = settings.get("RECEIPT_STORAGE_REQUIRED", "0").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        supabase_url = settings.get("SUPABASE_URL", "").strip()
        supabase_service_key = settings.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        if bool(supabase_url) != bool(supabase_service_key):
            raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be configured together")
        try:
            probe_agent_secrets = json.loads(
                settings.get("AURIX_PROBE_AGENT_SECRETS_JSON", "{}").strip() or "{}"
            )
        except json.JSONDecodeError as exc:
            raise SystemExit("AURIX_PROBE_AGENT_SECRETS_JSON must be a JSON object") from exc
        if not isinstance(probe_agent_secrets, dict):
            raise SystemExit("AURIX_PROBE_AGENT_SECRETS_JSON must be a JSON object")
        try:
            probe_stale_after = int(settings.get("AURIX_PROBE_STALE_AFTER_SECONDS", "900"))
            probe_job_ttl = int(settings.get("AURIX_PROBE_JOB_TTL_SECONDS", "180"))
        except ValueError as exc:
            raise SystemExit(
                "AURIX_PROBE_STALE_AFTER_SECONDS and AURIX_PROBE_JOB_TTL_SECONDS must be integers"
            ) from exc
        try:
            configured_targets = json.loads(settings.get("AURIX_PROBE_TARGETS_JSON", "[]") or "[]")
            configured_schedules = json.loads(settings.get("AURIX_PROBE_SCHEDULES_JSON", "[]") or "[]")
            if not isinstance(configured_targets, list) or not isinstance(configured_schedules, list):
                raise ValueError("probe target and schedule settings must be arrays")
            if any(not isinstance(item, dict) for item in configured_targets):
                raise ValueError("probe targets must be objects")
            if any(not isinstance(item, dict) for item in configured_schedules):
                raise ValueError("probe schedules must be objects")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Fleet probe target configuration is invalid: {exc}") from exc
        owner_ids = _parse_ids(settings, "OWNER_TELEGRAM_ID")
        if len(owner_ids) > 1:
            raise SystemExit("OWNER_TELEGRAM_ID must contain exactly one Telegram numeric ID")
        control_group_value = settings.get("AURIX_CONTROL_GROUP_ID", "").strip()
        try:
            control_group_id = int(control_group_value) if control_group_value else None
        except ValueError as exc:
            raise SystemExit("AURIX_CONTROL_GROUP_ID must be a numeric Telegram group ID") from exc
        if control_group_id is not None and control_group_id >= 0:
            raise SystemExit("AURIX_CONTROL_GROUP_ID must be a negative Telegram group ID")
        try:
            maintenance_interval_seconds = float(
                settings.get(
                    "AURIX_MAINTENANCE_INTERVAL_SECONDS",
                    str(DEFAULT_MAINTENANCE_INTERVAL_SECONDS),
                )
            )
        except ValueError as exc:
            raise SystemExit("AURIX_MAINTENANCE_INTERVAL_SECONDS must be numeric") from exc
        if maintenance_interval_seconds < 1:
            raise SystemExit("AURIX_MAINTENANCE_INTERVAL_SECONDS must be at least 1")
        receipt_llm_config = (
            settings.get("RECEIPT_LLM_BASE_URL", "").strip(),
            settings.get("RECEIPT_LLM_MODEL", "").strip(),
            settings.get("RECEIPT_LLM_API_KEY", "").strip(),
        )
        if any(receipt_llm_config) and not all(receipt_llm_config):
            raise SystemExit(
                "RECEIPT_LLM_BASE_URL, RECEIPT_LLM_MODEL, and RECEIPT_LLM_API_KEY must be configured together"
            )
        provider_resource_id = settings.get("OUTLINE_PROVIDER_RESOURCE_ID", "").strip()
        if provider_resource_id and not re.fullmatch(r"\d{1,20}", provider_resource_id):
            raise SystemExit("OUTLINE_PROVIDER_RESOURCE_ID must be a numeric Droplet ID")
        return cls(
            token=token,
            access_url_key=access_url_key,
            api_url=api_url,
            fingerprint=fingerprint,
            servers_json=servers_json,
            database_path=Path(settings.get("DATABASE_PATH", "data/bot.db")),
            commerce_database_url=commerce_database_url,
            receipt_storage_required=receipt_storage_required,
            supabase_url=supabase_url,
            supabase_service_key=supabase_service_key,
            supabase_receipts_bucket=settings.get("SUPABASE_RECEIPTS_BUCKET", "payment-receipts"),
            outline_timeout=outline_timeout,
            outline_cooldown=outline_cooldown,
            server_label=settings.get("OUTLINE_SERVER_LABEL", "Primary")[:64],
            provider=settings.get("OUTLINE_PROVIDER", "manual")[:64],
            region=settings.get("OUTLINE_REGION", "unknown")[:64],
            provider_resource_id=provider_resource_id,
            default_server_id=settings.get("OUTLINE_DEFAULT_SERVER_ID", "").strip(),
            allow_text_payment=settings.get("ALLOW_TEXT_PAYMENT_REFERENCES", "0").lower()
            in ("1", "true", "yes"),
            probe_agent_secrets={
                str(key): str(value) for key, value in probe_agent_secrets.items()
            },
            probe_stale_after=probe_stale_after,
            probe_job_ttl=probe_job_ttl,
            probe_targets=tuple(configured_targets),
            probe_schedules=tuple(configured_schedules),
            admin_ids=_parse_ids(settings, "ADMIN_TELEGRAM_IDS"),
            owner_id=next(iter(owner_ids), None),
            control_group_id=control_group_id,
            command_scope_cleanup_ids=_parse_ids(settings, "ADMIN_SCOPE_CLEANUP_IDS"),
            trial_ids=_parse_ids(settings, "TRIAL_TELEGRAM_IDS"),
            maintenance_interval_seconds=maintenance_interval_seconds,
            receipt_llm_config=receipt_llm_config,
            device_api_url=settings.get("AURIX_DEVICE_API_URL", "").strip(),
        )

def _parse_ids(env: Mapping[str, str], name: str) -> set[int]:
    try:
        return {int(value.strip()) for value in env.get(name, "").split(",") if value.strip()}
    except ValueError as exc:
        raise SystemExit(f"{name} must be comma-separated Telegram numeric IDs") from exc
