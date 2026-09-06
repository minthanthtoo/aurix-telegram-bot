"""Focused reconciliation steps for the additive identity entitlement model."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from identity_support import _parse_time


def sync_subscriptions(
    service: Any,
    rows: list[Any],
    *,
    current_time: datetime,
    timestamp: str,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Backfill subscription entitlements and return lookup maps for paid keys."""
    entitlements_by_subscription: dict[str, str] = {}
    subscription_expiry: dict[str, str] = {}
    subscription_status: dict[str, str] = {}
    for row in rows:
        status = str(row["status"])
        try:
            if status == "active" and _parse_time(str(row["expires_at"])) <= current_time:
                status = "expired"
        except (TypeError, ValueError, OverflowError):
            status = "expired"
        subscription_id = str(row["id"])
        entitlement_id = service.ensure_subscription_entitlement(
            int(row["telegram_id"]),
            subscription_id,
            kind="paid",
            quota_bytes=int(row["quota_bytes"]),
            expires_at=str(row["expires_at"]),
            status=status,
            now=timestamp,
        )
        entitlements_by_subscription[subscription_id] = entitlement_id
        subscription_expiry[subscription_id] = str(row["expires_at"])
        subscription_status[subscription_id] = status
    return entitlements_by_subscription, subscription_expiry, subscription_status


def sync_free_keys(
    service: Any,
    rows: list[Any],
    *,
    current_time: datetime,
    timestamp: str,
) -> dict[tuple[str, str], str]:
    """Backfill non-paid legacy keys and map them by server/external identity."""
    kind_map = {"daily_free": "free", "monthly_trial": "trial", "paid": "paid"}
    entitlements_by_key: dict[tuple[str, str], str] = {}
    for row in rows:
        kind = "promo" if row["campaign_code"] else kind_map.get(str(row["key_type"]), "free")
        if kind == "paid":
            continue
        expires_at = str(row["expires_at"])
        status = (
            "active"
            if str(row["status"]) == "active"
            and _parse_time(expires_at) > current_time
            else "expired"
        )
        entitlement_id = service.ensure_key_entitlement(
            int(row["telegram_id"]),
            server_id=str(row["server_id"]),
            local_key_ref=str(row["id"]),
            kind=kind,
            quota_bytes=int(row["data_limit_bytes"]),
            expires_at=expires_at,
            status=status,
            now=timestamp,
        )
        entitlements_by_key[(str(row["server_id"]), str(row["outline_key_id"]))] = entitlement_id
    return entitlements_by_key


def build_credential_indexes(inputs: dict[str, Any]) -> tuple[dict[str, str], dict[tuple[str, str], dict[str, Any]]]:
    """Index rebuilt endpoint and credential rows for deterministic matching."""
    server_endpoints = {
        str(row["outline_server_id"]): str(row["endpoint_id"])
        for row in inputs["endpoints"]
    }
    credentials_by_key = {
        (str(row["endpoint_id"]), str(row["external_id"])): dict(row)
        for row in inputs["credentials"]
    }
    return server_endpoints, credentials_by_key


def sync_paid_key_leases(
    service: Any,
    rows: list[Any],
    *,
    entitlements_by_subscription: dict[str, str],
    subscription_expiry: dict[str, str],
    subscription_status: dict[str, str],
    server_endpoints: dict[str, str],
    credentials_by_key: dict[tuple[str, str], dict[str, Any]],
    timestamp: str,
) -> tuple[int, int]:
    """Connect active paid keys to their durable generation and lease."""
    generations = 0
    leases = 0
    for row in rows:
        subscription_id = str(row["subscription_id"])
        entitlement_id = entitlements_by_subscription.get(subscription_id)
        endpoint_id = server_endpoints.get(str(row["server_id"]))
        credential = (
            credentials_by_key.get((endpoint_id, str(row["outline_key_id"])))
            if endpoint_id
            else None
        )
        if (
            not entitlement_id
            or credential is None
            or str(row["status"]) != "active"
            or subscription_status.get(subscription_id) != "active"
        ):
            continue
        generation_id = service.ensure_generation_for_credential(
            entitlement_id,
            str(credential["endpoint_id"]),
            credential_id=str(credential["credential_id"]),
            now=timestamp,
        )
        generations += 1
        if service._active_lease_for_generation(generation_id) is None:
            service.ensure_generation_lease(
                entitlement_id,
                generation_id,
                str(credential["endpoint_id"]),
                int(row["quota_bytes"]),
                subscription_expiry[subscription_id],
                now=timestamp,
            )
            leases += 1
    return generations, leases


def sync_active_free_key_leases(
    service: Any,
    rows: list[Any],
    *,
    entitlements_by_key: dict[tuple[str, str], str],
    server_endpoints: dict[str, str],
    credentials_by_key: dict[tuple[str, str], dict[str, Any]],
    current_time: datetime,
    timestamp: str,
) -> tuple[int, int]:
    """Connect active, unexpired free keys to durable generations and leases."""
    generations = 0
    leases = 0
    for row in rows:
        if str(row["status"]) != "active":
            continue
        try:
            if _parse_time(str(row["expires_at"])) <= current_time:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        key = (str(row["server_id"]), str(row["outline_key_id"]))
        entitlement_id = entitlements_by_key.get(key)
        endpoint_id = server_endpoints.get(str(row["server_id"]))
        credential = (
            credentials_by_key.get((endpoint_id, str(row["outline_key_id"])))
            if endpoint_id
            else None
        )
        if not entitlement_id or credential is None:
            continue
        generation_id = service.ensure_generation_for_credential(
            entitlement_id,
            str(credential["endpoint_id"]),
            credential_id=str(credential["credential_id"]),
            now=timestamp,
        )
        generations += 1
        if service._active_lease_for_generation(generation_id) is None:
            service.ensure_generation_lease(
                entitlement_id,
                generation_id,
                str(credential["endpoint_id"]),
                int(row["data_limit_bytes"]),
                str(row["expires_at"]),
                now=timestamp,
            )
            leases += 1
    return generations, leases
