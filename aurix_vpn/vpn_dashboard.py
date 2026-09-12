"""Shared authenticated customer VPN snapshot assembly."""

from __future__ import annotations

import sys
from typing import Any


def collect_customer_vpn_state(
    service: Any, commerce: Any | None, telegram_id: int
) -> dict[str, Any]:
    """Collect the same customer state used by Telegram and the web app.

    This function deliberately accepts application services instead of a
    transport object. It performs no Telegram I/O and returns only the
    authenticated customer's records. With the endpoint registry configured,
    usage and free-key URLs come from maintenance-owned durable snapshots; a
    customer request never inventories every provider endpoint.
    """
    giveaway = service.giveaway_status(telegram_id)
    connectivity = getattr(service, "connectivity", None)
    endpoint_snapshot: dict[str, Any] | None = None
    if connectivity is not None:
        try:
            cached_metrics = getattr(connectivity, "cached_usage_metrics", None)
            if callable(cached_metrics):
                endpoint_snapshot = {"metrics": cached_metrics()}
            else:
                # Compatibility for older injected registries; the production
                # EndpointRegistry exposes cached_usage_metrics.
                endpoint_snapshot = {"metrics": connectivity.collect_customer_snapshot()["metrics"]}
        except Exception as exc:
            print(f"customer endpoint snapshot error: {type(exc).__name__}", file=sys.stderr)

    usage_available = True
    try:
        usage_by_key = (
            endpoint_snapshot["metrics"]
            if endpoint_snapshot is not None
            else (
                {"byEndpoint": {}, "errors": {"registry": "unavailable"}}
                if connectivity is not None
                else service.outline.transfer_metrics()
            )
        )
        if not isinstance(usage_by_key, dict):
            raise ValueError("invalid Outline metrics response")
        usage_available = not bool(usage_by_key.get("errors"))
    except Exception as exc:
        usage_available = False
        usage_by_key = {}
        print(f"customer usage error: {type(exc).__name__}", file=sys.stderr)

    access_available = True
    access_by_key: dict[str, Any] = {}
    try:
        cached_access = getattr(service, "cached_access_urls", None)
        if connectivity is not None and callable(cached_access):
            access_by_key = cached_access(telegram_id)
            access_available = not bool(access_by_key.get("errors"))
        elif endpoint_snapshot is not None:
            access_by_key = {"byEndpoint": {}, "errors": {"cache": "unavailable"}}
            access_available = False
        elif connectivity is not None:
            access_by_key = {"byEndpoint": {}, "errors": {"registry": "unavailable"}}
            access_available = False
        else:
            remote = service.outline.list_keys()
            remote_keys = remote.get("accessKeys", []) if isinstance(remote, dict) else []
            if not isinstance(remote_keys, list):
                raise ValueError("invalid Outline key response")
            for item in remote_keys:
                if not isinstance(item, dict) or not item.get("id") or not item.get("accessUrl"):
                    continue
                value = str(item["accessUrl"]).replace("\r", "").replace("\n", "").strip()
                if value:
                    access_by_key[str(item["id"])] = value
    except Exception as exc:
        access_available = False
        print(f"customer key retrieval error: {type(exc).__name__}", file=sys.stderr)

    entries = service.user_usage(telegram_id, usage_by_key, access_by_key)
    subscriptions: list[dict[str, Any]] = []
    open_order: dict[str, Any] | None = None
    if commerce is not None:
        paid_usage = {
            (str(item.get("endpoint_id")), str(item.get("outline_key_id"))): item
            for item in commerce.user_usage(telegram_id, usage_by_key)
            if item.get("outline_key_id")
        }
        subscriptions = commerce.user_vpns(telegram_id)
        relevant = [
            item
            for item in subscriptions
            if item.get("status") in ("active", "pending")
            or item.get("key_status") in ("active", "revoke_failed")
        ]
        if not relevant and subscriptions:
            relevant = subscriptions[:1]
        for item in relevant:
            key_id = str(item.get("outline_key_id") or "")
            usage = paid_usage.get((str(item.get("endpoint_id")), key_id), {})
            status = str(usage.get("status") or item.get("status") or "unknown")
            if item.get("status") == "pending" and not item.get("key_status"):
                status = "activation pending"
            quota = int(usage.get("quota_bytes") or item.get("quota_bytes") or 0)
            entries.append(
                {
                    "outline_key_id": key_id,
                    "endpoint_id": item.get("endpoint_id"),
                    "protocol": item.get("preferred_protocol") or "outline",
                    "key_type": "paid",
                    "tier": item.get("plan_name") or item.get("plan_code") or "Paid VPN",
                    "plan_code": item.get("plan_code"),
                    "used_bytes": int(usage.get("used_bytes") or 0),
                    "quota_bytes": quota,
                    "remaining_bytes": int(usage.get("remaining_bytes") or quota),
                    "usage_observed": bool(usage.get("usage_observed")),
                    "expires_at": item.get("expires_at"),
                    "status": status,
                    "access_url": item.get("access_url"),
                    "created_at": item.get("created_at") or item.get("starts_at"),
                }
            )
        orders = commerce.list_user_orders(telegram_id, limit=5)
        open_order = next(
            (
                order
                for order in orders
                if order.get("stage")
                not in ("fulfilled", "rejected", "cancelled", "refunded")
            ),
            None,
        )

    priority = {
        "active": 0,
        "activation pending": 1,
        "revocation pending": 2,
        "quota exhausted": 3,
        "expired": 4,
        "revoked": 5,
    }
    entries.sort(
        key=lambda item: (
            priority.get(str(item.get("status")), 6),
            str(item.get("created_at") or ""),
        )
    )
    return {
        "all_items": entries,
        "giveaway": giveaway,
        "usage_available": usage_available,
        "access_available": access_available,
        "open_order": open_order,
        "subscriptions": subscriptions,
    }
