"""Outline and Telegram control-group construction helpers."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Callable

from access_control import StaffAccessError
from outline_adapter import OutlineClient, OutlineServerPool
from runtime_models import RuntimeFactories
from runtime_settings import RuntimeSettings


def _build_outline(
    settings: RuntimeSettings,
    factories: RuntimeFactories,
) -> tuple[Any, dict[str, str], dict[str, str], dict[str, dict[str, str]]]:
    server_labels: dict[str, str] = {}
    server_provider_ids: dict[str, str] = {}
    server_endpoint_metadata: dict[str, dict[str, str]] = {}
    servers_json = settings.servers_json
    if servers_json:
        try:
            configured_servers = json.loads(servers_json)
            if not isinstance(configured_servers, list) or not configured_servers:
                raise ValueError
            clients: dict[str, Any] = {}
            for item in configured_servers:
                if not isinstance(item, dict):
                    raise ValueError
                server_id = str(item.get("id") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", server_id) or server_id in clients:
                    raise ValueError
                clients[server_id] = factories.outline_client(
                    str(item.get("api_url") or ""),
                    str(item.get("cert_sha256") or ""),
                    timeout_seconds=settings.outline_timeout,
                    circuit_breaker_seconds=settings.outline_cooldown,
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
                settings.default_server_id or next(iter(clients))
            )
            outline = factories.outline_server_pool(clients, default_server_id)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise SystemExit("OUTLINE_SERVERS_JSON must be a valid non-empty server array") from exc
    else:
        server_labels = {"primary": settings.server_label}
        server_endpoint_metadata = {
            "primary": {
                "provider": settings.provider,
                "region": settings.region,
                "transport": "outline",
            }
        }
        provider_resource_id = settings.provider_resource_id
        if provider_resource_id:
            server_provider_ids["primary"] = provider_resource_id
        outline = factories.outline_server_pool(
            {
                "primary": factories.outline_client(
                    settings.api_url,
                    settings.fingerprint,
                    timeout_seconds=settings.outline_timeout,
                    circuit_breaker_seconds=settings.outline_cooldown,
                )
            },
            "primary",
        )
    return outline, server_labels, server_provider_ids, server_endpoint_metadata

def _group_staff(
    token: str,
    control_group_id: int | None,
    urlopen: Callable[..., Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if control_group_id is None:
        return None, []
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getChatAdministrators",
        data=json.dumps({"chat_id": control_group_id}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise StaffAccessError("AuriX control-group administrators could not be loaded") from exc
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
                part
                for part in (
                    str(user.get("first_name") or "").strip(),
                    str(user.get("last_name") or "").strip(),
                )
                if part
            ),
            "is_bot": False,
        }
        if member.get("status") == "creator":
            owner = profile
        elif member.get("status") == "administrator":
            administrators.append(profile)
    return owner, administrators
