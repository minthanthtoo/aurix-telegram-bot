"""Authenticated AuriX VPN web portal and Telegram Mini App server."""

from __future__ import annotations

import io
import json
import mimetypes
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .commerce import CommerceError
from .connectivity import ConnectivityError
from .device_api import DeviceAPIService, ManifestSigner, create_device_wsgi_app
from .entitlements import OutlineError
from .runtime import RuntimeServices, build_runtime_services
from telegram_web_app import TelegramWebAppAuthError, VerifiedTelegramUser, verify_init_data
from .vpn_dashboard import collect_customer_vpn_state


# The product package is below the repository/container root; the browser
# assets remain top-level so each subdomain has an explicit web directory.
STATIC_ROOT = Path(__file__).resolve().parents[1] / "web" / "vpn-app"
ADMIN_STATIC_ROOT = Path(__file__).resolve().parents[1] / "web" / "vpn-admin"
MAX_JSON_BYTES = 16 * 1024
ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
PROMO_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")


class AdminAuthorizationError(PermissionError):
    """The signed Telegram identity is valid but is not an operator."""


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _safe_order(order: dict[str, Any]) -> dict[str, Any]:
    """Return customer-safe order fields, excluding internal joins/metadata."""
    allowed = {
        "id",
        "plan_code",
        "plan_name",
        "amount_minor",
        "currency",
        "status",
        "refund_status",
        "created_at",
        "order_type",
        "selected_payment_provider",
        "payment_status",
        "receipt_status",
        "subscription_status",
        "expires_at",
        "provisioning_status",
        "revocation_status",
        "wallet_reservation_status",
        "stage",
        "requested_endpoint_id",
        "requested_protocol",
    }
    return {key: order[key] for key in allowed if key in order}


def _safe_key(entry: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "outline_key_id",
        "protocol",
        "endpoint_id",
        "key_type",
        "tier",
        "plan_code",
        "used_bytes",
        "quota_bytes",
        "remaining_bytes",
        "usage_observed",
        "expires_at",
        "status",
        "access_blocked",
        "created_at",
    }
    result = {key: entry[key] for key in allowed if key in entry}
    # A key is deliberately returned only for an active entry already owned by
    # the verified Telegram identity. It is never returned for ended keys.
    access_url = entry.get("access_url")
    if entry.get("status") == "active" and isinstance(access_url, str):
        scheme = urlsplit(access_url).scheme.lower()
        if scheme in {"ss", "ssconf", "vless", "hysteria2", "hy2", "trojan", "vmess", "wireguard", "wg"}:
            result["access_url"] = access_url
    return result


def _safe_subscription(item: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "subscription_id",
        "plan_code",
        "plan_name",
        "status",
        "expires_at",
        "starts_at",
        "outline_key_id",
        "endpoint_id",
        "quota_bytes",
        "key_status",
        "created_at",
        "quota_reason",
        "preferred_protocol",
    }
    return {key: item[key] for key in allowed if key in item}


def _safe_endpoint(item: dict[str, Any]) -> dict[str, Any]:
    """Expose only customer-facing endpoint metadata, never infrastructure secrets."""
    allowed = {
        "id",
        "code",
        "region",
        "protocol",
        "state",
        "healthy",
        "eligible",
        "active_assignments",
        "max_active_keys",
        "last_healthy_at",
        "management_latency_ms",
        "last_probe_at",
    }
    return {key: item[key] for key in allowed if key in item}


def _safe_protocol_observation(item: dict[str, Any]) -> dict[str, Any]:
    """Expose protocol evidence fields without trusting arbitrary details."""
    allowed = {
        "observation_id",
        "profile_id",
        "endpoint_id",
        "protocol",
        "signal",
        "status",
        "latency_ms",
        "observed_at",
        "expires_at",
        "source",
        "created_at",
    }
    result = {key: item[key] for key in allowed if key in item}
    detail_keys = {
        "error",
        "reason",
        "network_bucket",
        "sample_count",
        "active_users",
        "status_code",
        "quota_enforced",
        "restart_persisted",
        "session_termination",
        "client_path",
    }
    raw_details = item.get("details")
    if isinstance(raw_details, dict):
        result["details"] = {
            str(key): value
            for key, value in raw_details.items()
            if str(key).strip().lower() in detail_keys
            and isinstance(value, (bool, int, float, str))
        }
    else:
        result["details"] = {}
    return result


def _safe_protocol_readiness(item: dict[str, Any]) -> dict[str, Any]:
    """Expose effective promotion gates without operator or provider secrets."""
    allowed = {
        "endpoint_id",
        "protocol",
        "profile_id",
        "profile_status",
        "required_signals",
        "required_capabilities",
        "minimum_required_signals",
        "minimum_required_capabilities",
        "fresh_healthy_signals",
        "missing_signals",
        "missing_evidence",
        "missing_capabilities",
        "latest_healthy_at",
        "promotable",
        "reasons",
    }
    result = {key: item[key] for key in allowed if key in item}
    result["reasons"] = [str(value)[:256] for value in item.get("reasons") or []]
    return result


def _safe_giveaway(giveaway: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "exists",
        "code",
        "campaign_state",
        "winner",
        "winner_number",
        "winner_limit",
        "remaining_slots",
        "duration_days",
        "quota_bytes",
        "access_lock_active",
        "status",
        "expires_at",
    }
    return {key: giveaway[key] for key in allowed if key in giveaway}


class AuriXVpnWebApplication:
    """Application facade kept independent from HTTP transport for testing."""

    def __init__(
        self,
        runtime: RuntimeServices,
        *,
        max_init_data_age: int = 86_400,
        telegram_url: str = "",
        device_api: DeviceAPIService | None = None,
    ):
        self.runtime = runtime
        self.device_api = device_api
        self.device_wsgi_app = create_device_wsgi_app(device_api) if device_api else None
        self.max_init_data_age = max(60, int(max_init_data_age))
        parsed_telegram_url = urlsplit(telegram_url.strip())
        self.telegram_url = (
            telegram_url.strip()
            if parsed_telegram_url.scheme == "https"
            and parsed_telegram_url.hostname in {"t.me", "telegram.me"}
            else ""
        )
        try:
            self.trial_ids = {
                int(value.strip())
                for value in os.environ.get("TRIAL_TELEGRAM_IDS", "").split(",")
                if value.strip()
            }
        except ValueError as exc:
            raise ValueError("TRIAL_TELEGRAM_IDS must contain comma-separated numeric IDs") from exc
        try:
            self.admin_ids = {
                int(value.strip())
                for value in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(",")
                if value.strip()
            }
        except ValueError as exc:
            raise ValueError("ADMIN_TELEGRAM_IDS must contain comma-separated numeric IDs") from exc

    def authenticate(self, init_data: str | None) -> VerifiedTelegramUser:
        return verify_init_data(
            init_data or "",
            self.runtime.token,
            max_age_seconds=self.max_init_data_age,
        )

    def authenticate_admin(self, init_data: str | None) -> VerifiedTelegramUser:
        user = self.authenticate(init_data)
        if user.telegram_id not in self.admin_ids:
            raise AdminAuthorizationError("administrator access required")
        return user

    def plans_payload(self) -> dict[str, Any]:
        plans = []
        for plan in self.runtime.commerce.plans():
            plans.append(
                {
                    "code": plan.code,
                    "name": plan.name,
                    "price_minor": int(plan.price_minor),
                    "currency": plan.currency,
                    "quota_bytes": plan.quota_bytes,
                    "duration_days": int(plan.duration_days),
                }
            )
        providers = []
        for code, label in self.runtime.commerce.PAYMENT_PROVIDERS.items():
            env_code = {"kpay": "KPAY", "wavepay": "WAVEPAY", "ayapay": "AYAPAY", "uabpay": "UABPAY", "cbpay": "CBPAY"}[code]
            providers.append(
                {
                    "code": code,
                    "name": label,
                    "configured": bool(os.environ.get(f"PAYMENT_QR_{env_code}", "").strip()),
                }
            )
        return {
            "product": "aurix-vpn",
            "name": "AuriX VPN",
            "plans": plans,
            "payment_providers": providers,
            "text_reference_payment_enabled": bool(
                getattr(self.runtime, "allow_text_payment", False)
            ),
            "telegram_url": self.telegram_url or None,
            "client_downloads": {
                "android": "https://play.google.com/store/apps/details?id=org.outline.android.client",
                "ios": "https://apps.apple.com/app/outline-app/id1356177741",
                "macos": "https://apps.apple.com/app/outline-secure-internet-access/id1356178125",
                "windows": "https://s3.amazonaws.com/outline-releases/client/windows/stable/Outline-Client.exe",
                "linux": "https://support.getoutline.org/client/getting-started/install-linux/",
            },
        }

    def servers_payload(
        self, plan_code: str | None = None, protocol: str = "outline"
    ) -> dict[str, Any]:
        registry = getattr(self.runtime, "connectivity", None)
        selected_protocol = str(protocol or "outline").strip().lower()
        directory = []
        if registry is not None:
            list_customer_endpoints = getattr(registry, "list_customer_endpoints", None)
            if callable(list_customer_endpoints):
                catalog_protocols = self.protocols_payload(plan_code)["protocols"]
                catalog_entry = next(
                    (
                        item
                        for item in catalog_protocols
                        if str(item.get("protocol") or "").strip().lower() == selected_protocol
                    ),
                    None,
                )
                if selected_protocol != "outline" and (
                    not isinstance(catalog_entry, dict)
                    or int(catalog_entry.get("eligible_servers") or 0) <= 0
                ):
                    endpoint_rows = []
                else:
                    try:
                        endpoint_rows = list_customer_endpoints(plan_code, selected_protocol)
                    except TypeError:
                        # Keep compatibility with a pre-protocol registry
                        # only for the default Outline directory. Managed
                        # transports must never inherit Outline rows.
                        endpoint_rows = (
                            list_customer_endpoints(plan_code)
                            if selected_protocol == "outline"
                            else []
                        )
                directory = [_safe_endpoint(item) for item in endpoint_rows]
        return {
            "servers": directory,
            "plan_code": str(plan_code or "").strip() or None,
            "protocol": selected_protocol,
            "selection_policy": "A selected server is rechecked at payment approval and provisioning.",
            "latency_note": "Displayed latency is the latest Outline control-plane check, not a user-device ping.",
        }

    def protocols_payload(self, plan_code: str | None = None) -> dict[str, Any]:
        """Return customer-selectable protocols without exposing fleet secrets.

        A protocol is customer-visible only when an endpoint profile is enabled,
        its adapter is registered, and a deployment-owned route binding exists.
        Candidate profiles and transports that are merely present in the roadmap
        stay out of the purchase UI.
        """
        registry = getattr(self.runtime, "connectivity", None)
        profile_method = getattr(registry, "list_protocol_profiles", None)
        commerce = getattr(self.runtime, "commerce", None)
        adapter_registry = getattr(commerce, "adapter_registry", None)
        registered_check = getattr(adapter_registry, "is_registered", None)
        managed_bindings = getattr(commerce, "managed_route_bindings", None)
        routes_method = getattr(managed_bindings, "routes", None)
        bound_routes: set[tuple[str, str]] = set()
        if callable(routes_method):
            for route in routes_method() or []:
                if not isinstance(route, dict):
                    continue
                endpoint_id = str(route.get("endpoint_id") or "").strip()
                protocol = str(route.get("protocol") or "").strip().lower()
                if endpoint_id and protocol:
                    bound_routes.add((endpoint_id, protocol))
        protocols: set[str] = set()
        if callable(profile_method):
            try:
                profiles = profile_method(enabled_only=True)
            except TypeError:
                profiles = profile_method()
            for profile in profiles or []:
                if not isinstance(profile, dict):
                    continue
                protocol = str(profile.get("protocol") or "").strip().lower()
                if not protocol:
                    continue
                if protocol != "outline" and not callable(registered_check):
                    continue
                if callable(registered_check) and not registered_check(protocol):
                    continue
                if protocol != "outline" and not any(
                    bound_protocol == protocol for _endpoint_id, bound_protocol in bound_routes
                ):
                    continue
                protocols.add(protocol)
        # Preserve the legacy Outline customer experience for older injected
        # registries that do not expose protocol profiles yet.
        if not callable(profile_method) or not protocols:
            protocols.add("outline")

        result: list[dict[str, Any]] = []
        for protocol in sorted(protocols, key=lambda value: (value != "outline", value)):
            list_endpoints = getattr(registry, "list_customer_endpoints", None)
            if callable(list_endpoints):
                try:
                    endpoint_rows = list_endpoints(plan_code, protocol)
                except TypeError:
                    endpoint_rows = list_endpoints(plan_code)
            else:
                endpoint_rows = []
            if protocol != "outline":
                endpoint_rows = [
                    item for item in endpoint_rows or []
                    if isinstance(item, dict)
                    and (str(item.get("id") or ""), protocol) in bound_routes
                ]
            endpoints = [
                _safe_endpoint(item)
                for item in endpoint_rows or []
                if isinstance(item, dict)
            ]
            result.append(
                {
                    "protocol": protocol,
                    "name": protocol.replace("_", " ").title(),
                    "servers": len(endpoints),
                    "eligible_servers": sum(1 for item in endpoints if item.get("eligible")),
                    "default": protocol == "outline",
                }
            )
        return {
            "protocols": result,
            "plan_code": str(plan_code or "").strip() or None,
            "selection_policy": "Only enabled endpoint profiles with registered adapters and managed route bindings can be purchased.",
        }

    def dashboard(self, user: VerifiedTelegramUser) -> dict[str, Any]:
        state = collect_customer_vpn_state(
            self.runtime.claim_service, self.runtime.commerce, user.telegram_id
        )
        orders = self.runtime.commerce.list_user_orders(user.telegram_id, limit=20)
        active_endpoints = {
            str(item.get("endpoint_id"))
            for item in state["all_items"]
            if item.get("status") == "active" and item.get("endpoint_id")
        }
        servers = self.servers_payload()
        current_servers = [
            item for item in servers["servers"] if str(item.get("id")) in active_endpoints
        ]
        paid_active = self._has_active_paid_access(state["subscriptions"])
        promo_locked = bool(state["giveaway"].get("access_lock_active"))
        return {
            "user": {
                "telegram_id": user.telegram_id,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "username": user.username,
                "language_code": user.language_code,
            },
            "keys": [_safe_key(item) for item in state["all_items"]],
            "subscriptions": [_safe_subscription(item) for item in state["subscriptions"]],
            "orders": [_safe_order(order) for order in orders],
            "giveaway": _safe_giveaway(state["giveaway"]),
            "usage_available": bool(state["usage_available"]),
            "access_available": bool(state["access_available"]),
            "open_order": _safe_order(state["open_order"]) if state.get("open_order") else None,
            "servers": current_servers,
            "claim_capabilities": {
                "daily": not paid_active and not promo_locked,
                "trial": (
                    not paid_active
                    and not promo_locked
                    and (not self.trial_ids or user.telegram_id in self.trial_ids)
                ),
                "promo": True,
            },
        }

    @staticmethod
    def _has_active_paid_access(subscriptions: list[dict[str, Any]]) -> bool:
        for item in subscriptions:
            if item.get("status") != "active" or item.get("key_status") != "active":
                continue
            try:
                expires_at = datetime.fromisoformat(str(item.get("expires_at"))).astimezone(UTC)
                if expires_at > datetime.now(UTC):
                    return True
            except (TypeError, ValueError):
                continue
        return False

    @staticmethod
    def _admin_limit(value: str | None, default: int = 100) -> int:
        try:
            return max(1, min(int(value or default), 200))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _endpoint_health_is_fresh(value: Any) -> bool:
        """Match the connectivity registry's bounded health freshness rule."""
        try:
            max_age = max(
                30, int(os.environ.get("AURIX_ENDPOINT_HEALTH_MAX_AGE_SECONDS", "900"))
            )
            observed_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=UTC)
            return observed_at.astimezone(UTC) >= datetime.now(UTC) - timedelta(seconds=max_age)
        except (TypeError, ValueError, OverflowError):
            return False

    def admin_fleet(self) -> list[dict[str, Any]]:
        """Return operator-safe endpoint metadata without probing providers."""
        registry = getattr(self.runtime, "connectivity", None)
        list_endpoints = getattr(registry, "list_endpoints", None)
        raw = list_endpoints() if callable(list_endpoints) else []
        safe: list[dict[str, Any]] = []
        for item in raw:
            safe.append(
                {
                    "id": item.get("id"),
                    "code": item.get("code"),
                    "provider": item.get("provider"),
                    "region": item.get("region"),
                    "state": item.get("state"),
                    "accepts_new_assignments": bool(item.get("accepts_new_assignments")),
                    "outline_version": item.get("outline_version"),
                    "max_active_keys": item.get("max_active_keys"),
                    "reserved_transfer_bytes": item.get("reserved_transfer_bytes"),
                    "active_assignments": item.get("active_assignments", 0),
                    "last_healthy_at": item.get("last_healthy_at"),
                    "healthy": self._endpoint_health_is_fresh(item.get("last_healthy_at")),
                    "protocols": item.get("protocols", []),
                }
            )
        return safe

    def admin_endpoint(self, endpoint_id: str) -> dict[str, Any] | None:
        """Return one endpoint's safe operational detail for the admin console."""
        registry = getattr(self.runtime, "connectivity", None)
        endpoint_method = getattr(registry, "endpoint", None)
        if callable(endpoint_method):
            try:
                raw = endpoint_method(str(endpoint_id))
            except ConnectivityError:
                raw = None
        else:
            raw = None
        if raw is None:
            raw = next(
                (item for item in self.admin_fleet() if str(item.get("id")) == str(endpoint_id)),
                None,
            )
        if raw is None:
            return None
        safe_endpoint = {
            key: raw.get(key)
            for key in (
                "id", "code", "provider", "region", "state",
                "accepts_new_assignments", "outline_version", "max_active_keys",
                "reserved_transfer_bytes", "active_assignments", "last_healthy_at",
            )
            if key in raw
        }
        safe_endpoint["healthy"] = self._endpoint_health_is_fresh(raw.get("last_healthy_at"))
        profile_method = getattr(registry, "list_protocol_profiles", None)
        profiles: list[dict[str, Any]] = []
        if callable(profile_method):
            profiles = [
                item
                for item in profile_method(str(endpoint_id)) or []
                if isinstance(item, dict)
            ]
            safe_endpoint["protocols"] = profiles
        protocol_readiness: list[dict[str, Any]] = []
        requirements_method = getattr(registry, "protocol_promotion_requirements", None)
        readiness_method = getattr(registry, "protocol_profile_promotion_readiness", None)
        if callable(requirements_method) and callable(readiness_method):
            for profile in profiles:
                protocol = str(profile.get("protocol") or "").strip().lower()
                if not protocol or protocol == "outline":
                    continue
                try:
                    requirements = requirements_method(protocol)
                    preview = readiness_method(
                        str(endpoint_id),
                        protocol,
                        required_signals=tuple(requirements.get("signals") or ()),
                        required_capabilities=tuple(requirements.get("capabilities") or ()),
                    )
                except (ConnectivityError, TypeError, ValueError):
                    continue
                if isinstance(preview, dict):
                    protocol_readiness.append(_safe_protocol_readiness(preview))
        observation_method = getattr(registry, "list_protocol_observations", None)
        observations: list[dict[str, Any]] = []
        if callable(observation_method):
            observations = [
                _safe_protocol_observation(item)
                for item in observation_method(str(endpoint_id), limit=200)
                if isinstance(item, dict)
            ]
        inventory_reconciliation: dict[str, Any] = {
            "endpoint_id": str(endpoint_id),
            "present_keys": 0,
            "managed_present": 0,
            "unmanaged_present": 0,
            "historical_keys": 0,
            "latest_observed_at": None,
            "protocols": [],
            "status": "unavailable",
        }
        inventory_method = getattr(registry, "inventory_reconciliation", None)
        if callable(inventory_method):
            try:
                raw_inventory = inventory_method(str(endpoint_id))
            except Exception:
                raw_inventory = None
            if isinstance(raw_inventory, dict):
                inventory_reconciliation.update(
                    {
                        key: raw_inventory.get(key)
                        for key in (
                            "endpoint_id", "present_keys", "managed_present",
                            "unmanaged_present", "historical_keys", "latest_observed_at",
                            "status",
                        )
                        if key in raw_inventory
                    }
                )
                for key in ("present_keys", "managed_present", "unmanaged_present", "historical_keys"):
                    try:
                        inventory_reconciliation[key] = max(0, int(inventory_reconciliation[key] or 0))
                    except (TypeError, ValueError):
                        inventory_reconciliation[key] = 0
                inventory_reconciliation["status"] = str(
                    inventory_reconciliation.get("status") or "unavailable"
                )[:32]
                if inventory_reconciliation.get("latest_observed_at") is not None:
                    inventory_reconciliation["latest_observed_at"] = str(
                        inventory_reconciliation["latest_observed_at"]
                    )[:64]
                protocol_rows = raw_inventory.get("protocols")
                if isinstance(protocol_rows, list):
                    safe_protocols: list[dict[str, Any]] = []
                    for item in protocol_rows:
                        if not isinstance(item, dict):
                            continue
                        safe_item = {
                            "protocol": str(item.get("protocol") or "")[:64],
                            "present_keys": 0,
                            "managed_present": 0,
                            "unmanaged_present": 0,
                            "historical_keys": 0,
                            "latest_observed_at": None,
                            "status": str(item.get("status") or "unavailable")[:32],
                        }
                        for key in (
                            "present_keys", "managed_present", "unmanaged_present", "historical_keys"
                        ):
                            try:
                                safe_item[key] = max(0, int(item.get(key) or 0))
                            except (TypeError, ValueError):
                                safe_item[key] = 0
                        if item.get("latest_observed_at") is not None:
                            safe_item["latest_observed_at"] = str(item["latest_observed_at"])[:64]
                        if safe_item["protocol"]:
                            safe_protocols.append(safe_item)
                    inventory_reconciliation["protocols"] = safe_protocols
        database = getattr(self.runtime, "commerce_database", None)
        assignments: list[dict[str, Any]] = []
        if database is not None:
            with database.connect() as connection:
                rows = connection.execute(
                    """SELECT id, endpoint_id, subscription_id, free_key_id,
                              plan_code, protocol, status, reason,
                              reserved_quota_bytes, assigned_at, released_at
                           FROM endpoint_assignments
                          WHERE endpoint_id = ?
                          ORDER BY assigned_at DESC LIMIT 200""",
                    (str(endpoint_id),),
                ).fetchall()
            assignments = [dict(row) for row in rows]
        capacity_by_plan: list[dict[str, Any]] = []
        capacity_method = getattr(self.runtime.commerce, "endpoint_plan_capacity", None)
        if callable(capacity_method):
            try:
                capacity_by_plan = [
                    {
                        key: item.get(key)
                        for key in (
                            "plan_code", "enabled", "max_active_assignments",
                            "active_assignments",
                        )
                        if key in item
                    }
                    for item in capacity_method(str(endpoint_id))
                    if isinstance(item, dict)
                ]
            except CommerceError:
                capacity_by_plan = []
        identity = getattr(self.runtime.commerce, "identity", None)
        generations = []
        method = getattr(identity, "admin_generations", None)
        if callable(method):
            generations = [
                item
                for item in method(limit=200)
                if str(item.get("endpoint_id")) == str(endpoint_id)
            ]
        return {
            "endpoint": safe_endpoint,
            "assignments": assignments,
            "capacity_by_plan": capacity_by_plan,
            "credentials": generations,
            "protocol_observations": observations,
            "protocol_readiness": protocol_readiness,
            "inventory_reconciliation": inventory_reconciliation,
        }

    def admin_usage_snapshot(self) -> dict[str, Any]:
        """Expose maintenance snapshot health without contacting providers."""
        registry = getattr(self.runtime, "connectivity", None)
        reader = getattr(registry, "cached_usage_metrics", None)
        if not callable(reader):
            return {
                "status": "unavailable",
                "latest_observed_at": None,
                "failed_endpoint_count": 0,
                "snapshot_max_age_seconds": None,
            }
        try:
            payload = reader()
        except Exception:
            return {
                "status": "unavailable",
                "latest_observed_at": None,
                "failed_endpoint_count": 0,
                "snapshot_max_age_seconds": None,
            }
        if not isinstance(payload, dict):
            return {
                "status": "unavailable",
                "latest_observed_at": None,
                "failed_endpoint_count": 0,
                "snapshot_max_age_seconds": None,
            }
        errors = payload.get("errors")
        errors = errors if isinstance(errors, dict) else {}
        failed_endpoint_count = sum(1 for key in errors if str(key) != "snapshot")
        latest = payload.get("latest_observed_at")
        status = (
            "unavailable"
            if not latest
            else "healthy"
            if not errors
            else "degraded"
        )
        return {
            "status": status,
            "latest_observed_at": latest,
            "failed_endpoint_count": failed_endpoint_count,
            "snapshot_max_age_seconds": payload.get("snapshot_max_age_seconds"),
        }

    def admin_summary(self) -> dict[str, Any]:
        """Read-only overview for the AuriX Control Center."""
        commerce = self.runtime.commerce
        identity = getattr(commerce, "identity", None)
        counts = identity.admin_counts() if identity and callable(getattr(identity, "admin_counts", None)) else {}
        consistency = (
            commerce.consistency_report()
            if callable(getattr(commerce, "consistency_report", None))
            else {}
        )
        registry = getattr(commerce, "adapter_registry", None)
        catalog = (
            registry.protocol_catalog()
            if registry and callable(getattr(registry, "protocol_catalog", None))
            else []
        )
        readiness = (
            registry.protocol_readiness()
            if registry and callable(getattr(registry, "protocol_readiness", None))
            else catalog
        )
        controller = getattr(commerce, "fleet_controller", None)
        recommendation_method = getattr(controller, "scale_out_recommendation", None)
        if callable(recommendation_method):
            try:
                scale_out = recommendation_method(
                    plan_code=os.environ.get("AURIX_SCALE_PLAN_CODE") or None,
                    protocol="outline",
                    region=os.environ.get("AURIX_SCALE_REGION") or None,
                )
            except ConnectivityError as exc:
                scale_out = {"status": "unavailable", "reason": str(exc)}
        else:
            scale_out = {"status": "unavailable", "reason": "scale controller is not configured"}
        fleet = self.admin_fleet()
        usage_snapshot = self.admin_usage_snapshot()
        max_active_devices = getattr(self.device_api, "max_active_devices", None)
        device_policy = {
            "status": (
                "bounded"
                if self.device_api is not None and max_active_devices is not None
                else "unbounded"
                if self.device_api is not None
                else "unconfigured"
            ),
            "max_active_devices": max_active_devices,
        }
        return {
            "product": "aurix-control-center",
            "management_mode": "read-only",
            "control_plane": "durable-state",
            "counts": counts,
            "consistency": consistency,
            "protocols": catalog,
            "protocol_readiness": readiness,
            "scale_out": scale_out,
            "usage_snapshot": usage_snapshot,
            "device_policy": device_policy,
            "fleet": {
                "endpoints": len(fleet),
                "healthy": sum(1 for item in fleet if item.get("healthy")),
            },
            "safety": {
                "provider_mutations_from_web": False,
                "secrets_in_payloads": False,
                "remote_probe_on_page_load": False,
            },
        }

    def admin_accounts(self, query: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        identity = getattr(self.runtime.commerce, "identity", None)
        method = getattr(identity, "admin_accounts", None)
        return method(query=query or "", limit=limit) if callable(method) else []

    def admin_account(self, account_id: str) -> dict[str, Any] | None:
        identity = getattr(self.runtime.commerce, "identity", None)
        method = getattr(identity, "admin_account", None)
        return method(account_id) if callable(method) else None

    def admin_devices(
        self, query: str | None = None, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        identity = getattr(self.runtime.commerce, "identity", None)
        method = getattr(identity, "admin_devices", None)
        return method(query=query or "", status=status, limit=limit) if callable(method) else []

    def admin_credentials(
        self, protocol: str | None = None, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        identity = getattr(self.runtime.commerce, "identity", None)
        method = getattr(identity, "admin_generations", None)
        return (
            method(protocol=protocol, status=status, limit=limit)
            if callable(method)
            else []
        )

    def admin_failover(self, limit: int = 100) -> list[dict[str, Any]]:
        failover = getattr(self.runtime.commerce, "failover", None)
        method = getattr(failover, "decisions", None)
        if not callable(method):
            return []
        decisions = method(limit=limit)
        safe: list[dict[str, Any]] = []
        for decision in decisions:
            item = {
                key: decision[key]
                for key in (
                    "decision_id",
                    "source_endpoint_id",
                    "target_endpoint_id",
                    "trigger",
                    "network_bucket",
                    "state",
                    "attempts",
                    "next_attempt_at",
                    "locked_at",
                    "policy_version",
                    "policy_enabled",
                    "policy_failure_threshold",
                    "policy_recovery_threshold",
                    "policy_cooldown_seconds",
                    "policy_standby_lease_bytes",
                    "policy_max_attempts",
                    "policy_created_at",
                    "created_at",
                    "updated_at",
                    "completed_at",
                )
                if key in decision
            }
            # Provider/adapter exception text can contain remote identifiers or
            # request details. Keep only its bounded class for browser triage.
            if decision.get("last_error"):
                item["error_type"] = str(decision["last_error"]).split(":", 1)[0][:64]
            else:
                item["error_type"] = None
            safe.append(item)
        return safe

    def admin_failover_decision(self, decision_id: str) -> dict[str, Any] | None:
        """Return one redacted, historically explainable failover decision."""
        failover = getattr(self.runtime.commerce, "failover", None)
        method = getattr(failover, "decision_explanation", None)
        if not callable(method):
            return None
        decision = method(str(decision_id))
        if not decision:
            return None
        allowed = {
            "decision_id",
            "source_endpoint_id",
            "target_endpoint_id",
            "trigger",
            "network_bucket",
            "state",
            "attempts",
            "policy_version",
            "policy_enabled",
            "policy_failure_threshold",
            "policy_recovery_threshold",
            "policy_cooldown_seconds",
            "policy_standby_lease_bytes",
            "policy_max_attempts",
            "policy_created_at",
            "policy_snapshot_available",
        }
        return {key: decision[key] for key in allowed if key in decision}

    def admin_operations(self, limit: int = 100) -> dict[str, Any]:
        commerce = self.runtime.commerce
        database = getattr(self.runtime, "commerce_database", None)
        failover = getattr(commerce, "failover", None)
        jobs = (
            commerce.failed_jobs(limit=limit, include_nonterminal=True)
            if callable(getattr(commerce, "failed_jobs", None))
            else []
        )
        pending = (
            commerce.list_pending_orders(limit=limit)
            if callable(getattr(commerce, "list_pending_orders", None))
            else []
        )
        # Deliberately select fields instead of forwarding payment references,
        # receipt paths, or provider metadata from the commerce repository.
        pending_safe = [
            {
                key: item.get(key)
                for key in (
                    "id", "plan_code", "amount_minor", "currency", "status",
                    "created_at", "order_type", "receipt_status", "stage",
                    "wallet_reservation_status", "requested_endpoint_id",
                    "requested_protocol",
                )
                if key in item
            }
            for item in pending
        ]
        infrastructure_jobs: list[dict[str, Any]] = []
        safety_controls = []
        controls_method = getattr(failover, "safety_controls", None)
        if database is not None and callable(controls_method):
            safety_controls = controls_method()
        if database is not None:
            with database.connect() as connection:
                rows = connection.execute(
                    """SELECT id, operation, endpoint_id, status, attempts,
                              next_attempt_at, created_at, completed_at, last_error
                         FROM infrastructure_jobs
                        ORDER BY created_at DESC LIMIT ?""",
                    (max(1, min(int(limit), 200)),),
                ).fetchall()
            infrastructure_jobs = [
                {
                    "job_id": row["id"],
                    "operation": row["operation"],
                    "endpoint_id": row["endpoint_id"],
                    "status": row["status"],
                    "attempts": row["attempts"],
                    "next_attempt_at": row["next_attempt_at"],
                    "created_at": row["created_at"],
                    "completed_at": row["completed_at"],
                    # Provider exception text is not a safe browser payload;
                    # preserve only the bounded exception class for triage.
                    "error_type": (
                        str(row["last_error"]).split(":", 1)[0][:64]
                        if row["last_error"]
                        else None
                    ),
                }
                for row in rows
            ]
        return {
            "jobs": jobs,
            "infrastructure_jobs": infrastructure_jobs,
            "pending_orders": pending_safe,
            "safety_controls": safety_controls,
            "consistency": commerce.consistency_report(),
        }

    def admin_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        database = getattr(self.runtime, "commerce_database", None)
        if database is None:
            return []
        with database.connect() as connection:
            rows = connection.execute(
                """SELECT actor_type, actor_id, action, target_type, target_id, created_at
                     FROM audit_events ORDER BY created_at DESC LIMIT ?""",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def _claim_allowed(self, user: VerifiedTelegramUser, *, trial: bool = False) -> None:
        subscriptions = self.runtime.commerce.user_vpns(user.telegram_id)
        if self._has_active_paid_access(subscriptions):
            raise CommerceError("Your paid VPN access is active; free claims resume after it ends")
        giveaway = self.runtime.claim_service.giveaway_status(user.telegram_id)
        if giveaway.get("access_lock_active"):
            raise CommerceError("Your promo VPN access is active; regular free claims are paused")
        if trial and self.trial_ids and user.telegram_id not in self.trial_ids:
            raise CommerceError("Monthly trial access is not enabled for this account")

    @staticmethod
    def _claim_payload(result: Any) -> dict[str, Any]:
        return {
            "issued": bool(result.access_url),
            "expires_at": result.expires_at.isoformat() if result.expires_at else None,
            "next_claim_at": result.next_claim_at.isoformat() if result.next_claim_at else None,
            "denied_reason": result.denied_reason,
        }

    def claim_daily(self, user: VerifiedTelegramUser) -> dict[str, Any]:
        self._claim_allowed(user)
        result = self.runtime.claim_service.claim(
            user.telegram_id, user.first_name, username=user.username
        )
        return self._claim_payload(result)

    def claim_trial(self, user: VerifiedTelegramUser) -> dict[str, Any]:
        self._claim_allowed(user, trial=True)
        result = self.runtime.claim_service.claim_trial(
            user.telegram_id, user.first_name, username=user.username
        )
        return self._claim_payload(result)

    def claim_promo(self, user: VerifiedTelegramUser, code: str) -> dict[str, Any]:
        normalized = str(code or "").strip().upper()
        if not PROMO_CODE_PATTERN.fullmatch(normalized):
            raise CommerceError("Enter a valid promo code")
        result = self.runtime.claim_service.claim_giveaway(
            user.telegram_id,
            user.first_name,
            username=user.username,
            code=normalized,
        )
        return {
            "outcome": result.outcome,
            "code": result.code,
            "quota_bytes": result.quota_bytes,
            "duration_days": result.duration_days,
            "expires_at": result.expires_at.isoformat() if result.expires_at else None,
            "winner_number": result.winner_number,
            "remaining_slots": result.remaining_slots,
            "reason": result.reason,
        }

    @staticmethod
    def _require_order_id(order_id: str) -> str:
        if not ORDER_ID_PATTERN.fullmatch(order_id):
            raise CommerceError("Order not found")
        return order_id

    def create_order(
        self,
        user: VerifiedTelegramUser,
        plan_code: str,
        requested_endpoint_id: str | None = None,
        requested_protocol: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(plan_code, str) or len(plan_code) > 64:
            raise CommerceError("Choose a valid plan")
        result = self.runtime.commerce.create_order(
            user.telegram_id,
            user.first_name,
            plan_code.strip(),
            username=user.username,
            requested_endpoint_id=requested_endpoint_id,
            requested_protocol=requested_protocol,
        )
        order = self.runtime.commerce.order_detail(result.order_id, user.telegram_id)
        return {
            "order": _safe_order(order or {"id": result.order_id, "status": result.status}),
            "created": bool(result.created),
            "plan_conflict": bool(result.plan_conflict),
        }

    def order_detail(self, user: VerifiedTelegramUser, order_id: str) -> dict[str, Any]:
        order = self.runtime.commerce.order_detail(self._require_order_id(order_id), user.telegram_id)
        if order is None:
            raise CommerceError("Order not found")
        return {"order": _safe_order(order)}

    def submit_payment(
        self, user: VerifiedTelegramUser, order_id: str, provider: str, reference: str
    ) -> dict[str, Any]:
        if not bool(getattr(self.runtime, "allow_text_payment", False)):
            raise CommerceError(
                "Payment references are accepted through the Telegram receipt flow only"
            )
        order_id = self._require_order_id(order_id)
        provider = str(provider or "").strip().lower()[:32]
        reference = str(reference or "").strip()[:128]
        if provider not in self.runtime.commerce.PAYMENT_PROVIDERS:
            raise CommerceError("Unsupported payment method")
        if not reference:
            raise CommerceError("Payment reference is required")
        self.runtime.commerce.select_payment_provider(user.telegram_id, order_id, provider)
        status = self.runtime.commerce.submit_payment(
            user.telegram_id,
            order_id,
            self.runtime.commerce.PAYMENT_PROVIDERS[provider],
            reference,
        )
        return {"status": status, **self.order_detail(user, order_id)}


def make_handler(
    application: AuriXVpnWebApplication,
    static_root: Path = STATIC_ROOT,
    admin_static_root: Path = ADMIN_STATIC_ROOT,
):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AuriXVPN/1.0"

        def _write(self, status: int, payload: dict[str, Any], *, no_store: bool = True) -> None:
            body = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if no_store else "public, max-age=300")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, message: str) -> None:
            self._write(status, {"error": message})

        def _device_api(self, method: str) -> None:
            app = application.device_wsgi_app
            if app is None:
                self._error(404, "Not found")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > 128 * 1024:
                self._error(413, "Request body is invalid")
                return
            body = self.rfile.read(length) if length else b""
            path_info, _, query = self.path.partition("?")
            environ: dict[str, Any] = {
                "REQUEST_METHOD": method,
                "PATH_INFO": path_info,
                "QUERY_STRING": query,
                "CONTENT_LENGTH": str(length),
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "REMOTE_ADDR": self.client_address[0],
                "SERVER_NAME": self.server.server_address[0],
                "SERVER_PORT": str(self.server.server_address[1]),
                "SERVER_PROTOCOL": self.request_version,
                "wsgi.url_scheme": "https",
                "wsgi.input": io.BytesIO(body),
            }
            for key, value in self.headers.items():
                header = "HTTP_" + key.upper().replace("-", "_")
                if header not in {"HTTP_CONTENT_LENGTH", "HTTP_CONTENT_TYPE"}:
                    environ[header] = value
            response_status = "500 Internal Server Error"
            response_headers: list[tuple[str, str]] = []
            response_parts: list[bytes] = []

            def start_response(status: str, headers: list[tuple[str, str]], *_: Any) -> None:
                nonlocal response_status, response_headers
                response_status = status
                response_headers = headers

            response_parts = list(app(environ, start_response))
            response_body = b"".join(
                part if isinstance(part, bytes) else str(part).encode("utf-8")
                for part in response_parts
            )
            try:
                status_code = int(response_status.split(" ", 1)[0])
            except (ValueError, IndexError):
                status_code = 500
            self.send_response(status_code)
            has_length = False
            for header, value in response_headers:
                if header.lower() == "content-length":
                    has_length = True
                self.send_header(header, value)
            if not has_length:
                self.send_header("Content-Length", str(len(response_body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(response_body)

        def _read_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("Request body is invalid") from exc
            if length <= 0 or length > MAX_JSON_BYTES:
                raise ValueError("Request body is invalid")
            try:
                value = json.loads(self.rfile.read(length))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("Request body is invalid") from exc
            if not isinstance(value, dict):
                raise ValueError("Request body is invalid")
            return value

        def _user(self) -> VerifiedTelegramUser:
            return application.authenticate(self.headers.get("X-Telegram-Init-Data"))

        def _admin_user(self) -> VerifiedTelegramUser:
            return application.authenticate_admin(self.headers.get("X-Telegram-Init-Data"))

        def _route_admin(self, method: str, path: str) -> None:
            if method != "GET":
                self._error(405, "Method not allowed")
                return
            query = parse_qs(urlsplit(self.path).query)
            limit = application._admin_limit((query.get("limit") or [None])[0])
            if path == "/api/admin/summary":
                self._write(200, application.admin_summary())
                return
            if path == "/api/admin/fleet":
                self._write(200, {"endpoints": application.admin_fleet()})
                return
            fleet_prefix = "/api/admin/fleet/"
            if path.startswith(fleet_prefix):
                detail = application.admin_endpoint(unquote(path[len(fleet_prefix) :]))
                if detail is None:
                    self._error(404, "Endpoint not found")
                else:
                    self._write(200, detail)
                return
            if path == "/api/admin/accounts":
                self._write(
                    200,
                    {
                        "accounts": application.admin_accounts(
                            (query.get("q") or [""])[0], limit
                        )
                    },
                )
                return
            account_prefix = "/api/admin/accounts/"
            if path.startswith(account_prefix):
                account = application.admin_account(unquote(path[len(account_prefix) :]))
                if account is None:
                    self._error(404, "Account not found")
                else:
                    self._write(200, {"account": account})
                return
            if path == "/api/admin/credentials":
                self._write(
                    200,
                    {
                        "credentials": application.admin_credentials(
                            (query.get("protocol") or [None])[0],
                            (query.get("status") or [None])[0],
                            limit,
                        )
                    },
                )
                return
            if path == "/api/admin/devices":
                self._write(
                    200,
                    {
                        "devices": application.admin_devices(
                            (query.get("q") or [""])[0],
                            (query.get("status") or [None])[0],
                            limit,
                        )
                    },
                )
                return
            if path == "/api/admin/failover":
                self._write(200, {"decisions": application.admin_failover(limit)})
                return
            failover_prefix = "/api/admin/failover/"
            if path.startswith(failover_prefix):
                decision = application.admin_failover_decision(
                    unquote(path[len(failover_prefix) :])
                )
                if decision is None:
                    self._error(404, "Failover decision not found")
                else:
                    self._write(200, {"decision": decision})
                return
            if path == "/api/admin/operations":
                self._write(200, application.admin_operations(limit))
                return
            if path == "/api/admin/audit":
                self._write(200, {"events": application.admin_audit(limit)})
                return
            self._error(404, "Not found")

        def _route_api(self, method: str, path: str) -> None:
            if path == "/api/healthz" and method == "GET":
                self._write(200, {"ok": True, "service": "aurix-vpn-web"})
                return
            if path == "/api/plans" and method == "GET":
                self._write(200, application.plans_payload(), no_store=False)
                return
            if path == "/api/admin" or path.startswith("/api/admin/"):
                self._admin_user()
                self._route_admin(method, path)
                return
            user = self._user()
            if path == "/api/servers" and method == "GET":
                query = parse_qs(urlsplit(self.path).query)
                plan_values = query.get("plan_code") or []
                plan_code = str(plan_values[0])[:64] if plan_values else None
                protocol_values = query.get("protocol") or []
                protocol = str(protocol_values[0])[:64] if protocol_values else "outline"
                self._write(200, application.servers_payload(plan_code, protocol))
                return
            if path == "/api/protocols" and method == "GET":
                query = parse_qs(urlsplit(self.path).query)
                plan_values = query.get("plan_code") or []
                plan_code = str(plan_values[0])[:64] if plan_values else None
                self._write(200, application.protocols_payload(plan_code))
                return
            if path in ("/api/me", "/api/dashboard") and method == "GET":
                self._write(200, application.dashboard(user))
                return
            if path == "/api/orders" and method == "GET":
                orders = application.runtime.commerce.list_user_orders(user.telegram_id, limit=50)
                self._write(200, {"orders": [_safe_order(order) for order in orders]})
                return
            if path == "/api/orders" and method == "POST":
                body = self._read_json()
                payload = application.create_order(
                    user,
                    str(body.get("plan_code") or ""),
                    str(body.get("endpoint_id") or "").strip() or None,
                    str(body.get("protocol") or "").strip() or None,
                )
                self._write(
                    201 if payload["created"] else 200,
                    payload,
                )
                return
            if path == "/api/claims/daily" and method == "POST":
                payload = application.claim_daily(user)
                self._write(201 if payload["issued"] else 200, payload)
                return
            if path == "/api/claims/trial" and method == "POST":
                payload = application.claim_trial(user)
                self._write(201 if payload["issued"] else 200, payload)
                return
            if path == "/api/claims/promo" and method == "POST":
                body = self._read_json()
                payload = application.claim_promo(user, str(body.get("code") or ""))
                self._write(201 if payload["outcome"] == "won" else 200, payload)
                return
            prefix = "/api/orders/"
            if path.startswith(prefix):
                tail = path[len(prefix) :]
                parts = tail.split("/")
                if len(parts) == 1 and method == "GET":
                    self._write(200, application.order_detail(user, parts[0]))
                    return
                if len(parts) == 2 and parts[1] == "payment" and method == "POST":
                    body = self._read_json()
                    self._write(
                        200,
                        application.submit_payment(
                            user,
                            parts[0],
                            str(body.get("provider") or ""),
                            str(body.get("reference") or ""),
                        ),
                    )
                    return
            self._error(404, "Not found")

        def _serve_static_from(self, path: str, root_path: Path, prefix: str) -> None:
            if (not prefix and path in ("/", "/app", "/app/")) or path in (prefix, prefix + "/"):
                relative = "index.html"
            else:
                relative = path[len(prefix) :].lstrip("/")
            root = root_path.resolve()
            target = (root / relative).resolve()
            if root not in target.parents and target != root:
                self._error(404, "Not found")
                return
            if not target.is_file():
                self._error(404, "Not found")
                return
            body = target.read_bytes()
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' https://telegram.org; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; base-uri 'none'; "
                "frame-ancestors https://web.telegram.org https://*.telegram.org",
            )
            self.end_headers()
            self.wfile.write(body)

        def _serve_static(self, path: str) -> None:
            self._serve_static_from(path, static_root, "")

        def _serve_admin_static(self, path: str) -> None:
            self._serve_static_from(path, admin_static_root, "/admin")

        def _dispatch(self, method: str) -> None:
            path = urlsplit(self.path).path
            try:
                if path.startswith("/v1/devices/"):
                    self._device_api(method)
                    return
                if path.startswith("/api/"):
                    self._route_api(method, path)
                    return
                if path == "/admin" or path.startswith("/admin/"):
                    if method == "GET":
                        self._serve_admin_static(path)
                    else:
                        self._error(405, "Method not allowed")
                    return
                if method == "GET":
                    self._serve_static(path)
                    return
                self._error(405, "Method not allowed")
            except TelegramWebAppAuthError as exc:
                self._error(401, str(exc))
            except AdminAuthorizationError as exc:
                self._error(403, str(exc))
            except CommerceError as exc:
                self._error(400, str(exc))
            except ConnectivityError as exc:
                # Customer endpoint validation is performed by the shared
                # connectivity registry. Treat rejected/stale selections as
                # client-visible VPN errors instead of leaking them as 500s.
                self._error(400, str(exc))
            except OutlineError:
                self._error(503, "VPN provisioning is temporarily unavailable; try again shortly")
            except ValueError as exc:
                self._error(400, str(exc))
            except Exception as exc:
                print(f"vpn web request error: {type(exc).__name__}", file=sys.stderr)
                self._error(500, "The AuriX VPN service is temporarily unavailable")

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._dispatch("POST")

        def log_message(self, format: str, *args: Any) -> None:
            # Never write query strings or authentication headers to logs.
            sys.stderr.write("aurix vpn web request\n")

    return Handler


def create_server(
    application: AuriXVpnWebApplication,
    *,
    port: int,
    static_root: Path = STATIC_ROOT,
    admin_static_root: Path = ADMIN_STATIC_ROOT,
):
    if not 1 <= int(port) <= 65_535:
        raise ValueError("PORT must be between 1 and 65535")
    return ThreadingHTTPServer(
        ("0.0.0.0", int(port)),
        make_handler(application, static_root, admin_static_root),
    )


def _optional_positive_int(name: str, *, maximum: int = 1000) -> int | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 1 <= result <= maximum:
        raise ValueError(f"{name} is outside the allowed range")
    return result


def build_device_api(
    runtime: Any, *, max_active_devices: int | None = None
) -> DeviceAPIService | None:
    """Build the optional managed-device service for either web entrypoint."""
    manifest_seed = os.environ.get("AURIX_DEVICE_MANIFEST_PRIVATE_KEY", "").strip()
    if not manifest_seed:
        return None
    signer = ManifestSigner.from_base64_seed(
        manifest_seed,
        key_id=os.environ.get("AURIX_DEVICE_MANIFEST_KEY_ID", "aurix-manifest-1"),
    )
    return DeviceAPIService(
        runtime.commerce_database,
        identity=runtime.commerce.identity,
        manifest_signer=signer,
        route_provider=runtime.commerce.identity.routes_for_account,
        secret_decryptor=runtime.commerce._decrypt_access_url,
        max_active_devices=max_active_devices,
    )


def main() -> int:
    try:
        port = int(os.environ.get("PORT", "10000"))
        max_age = int(os.environ.get("AURIX_WEB_APP_INIT_DATA_MAX_AGE", "86400"))
        max_active_devices = _optional_positive_int("AURIX_MAX_ACTIVE_DEVICES")
    except ValueError:
        print(
            "PORT, AURIX_WEB_APP_INIT_DATA_MAX_AGE, and AURIX_MAX_ACTIVE_DEVICES must be valid integers",
            file=sys.stderr,
        )
        return 2
    try:
        runtime = build_runtime_services(
            validate_telegram=True,
            check_outline=False,
            reconcile=False,
            configure_bootstrap=False,
        )
        device_api = build_device_api(runtime, max_active_devices=max_active_devices)
        application = AuriXVpnWebApplication(
            runtime,
            max_init_data_age=max_age,
            telegram_url=os.environ.get("AURIX_TELEGRAM_URL", ""),
            device_api=device_api,
        )
        server = create_server(application, port=port)
    except Exception as exc:
        print(f"AuriX VPN web startup failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"AuriX VPN web listening on :{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
