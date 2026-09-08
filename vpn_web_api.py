"""Authenticated AuriX VPN web portal and Telegram Mini App server."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from commerce import CommerceError
from entitlements import OutlineError
from runtime import RuntimeServices, build_runtime_services
from telegram_web_app import TelegramWebAppAuthError, VerifiedTelegramUser, verify_init_data
from vpn_dashboard import collect_customer_vpn_state


STATIC_ROOT = Path(__file__).resolve().parent / "web" / "vpn-app"
MAX_JSON_BYTES = 16 * 1024
ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
PROMO_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")


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
    }
    return {key: order[key] for key in allowed if key in order}


def _safe_key(entry: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "outline_key_id",
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
        "created_at",
    }
    result = {key: entry[key] for key in allowed if key in entry}
    # A key is deliberately returned only for an active entry already owned by
    # the verified Telegram identity. It is never returned for ended keys.
    access_url = entry.get("access_url")
    if entry.get("status") == "active" and isinstance(access_url, str):
        scheme = urlsplit(access_url).scheme.lower()
        if scheme in {"ss", "ssconf"}:
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
    }
    return {key: item[key] for key in allowed if key in item}


def _safe_endpoint(item: dict[str, Any]) -> dict[str, Any]:
    """Expose only customer-facing endpoint metadata, never infrastructure secrets."""
    allowed = {
        "id",
        "code",
        "region",
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
    ):
        self.runtime = runtime
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

    def authenticate(self, init_data: str | None) -> VerifiedTelegramUser:
        return verify_init_data(
            init_data or "",
            self.runtime.token,
            max_age_seconds=self.max_init_data_age,
        )

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

    def servers_payload(self, plan_code: str | None = None) -> dict[str, Any]:
        registry = getattr(self.runtime, "connectivity", None)
        directory = []
        if registry is not None:
            list_customer_endpoints = getattr(registry, "list_customer_endpoints", None)
            if callable(list_customer_endpoints):
                directory = [_safe_endpoint(item) for item in list_customer_endpoints(plan_code)]
        return {
            "servers": directory,
            "plan_code": str(plan_code or "").strip() or None,
            "selection_policy": "A selected server is rechecked at payment approval and provisioning.",
            "latency_note": "Displayed latency is the latest Outline control-plane check, not a user-device ping.",
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
    ) -> dict[str, Any]:
        if not isinstance(plan_code, str) or len(plan_code) > 64:
            raise CommerceError("Choose a valid plan")
        result = self.runtime.commerce.create_order(
            user.telegram_id,
            user.first_name,
            plan_code.strip(),
            username=user.username,
            requested_endpoint_id=requested_endpoint_id,
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


def make_handler(application: AuriXVpnWebApplication, static_root: Path = STATIC_ROOT):
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

        def _route_api(self, method: str, path: str) -> None:
            if path == "/api/healthz" and method == "GET":
                self._write(200, {"ok": True, "service": "aurix-vpn-web"})
                return
            if path == "/api/plans" and method == "GET":
                self._write(200, application.plans_payload(), no_store=False)
                return
            user = self._user()
            if path == "/api/servers" and method == "GET":
                query = parse_qs(urlsplit(self.path).query)
                plan_values = query.get("plan_code") or []
                plan_code = str(plan_values[0])[:64] if plan_values else None
                self._write(200, application.servers_payload(plan_code))
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

        def _serve_static(self, path: str) -> None:
            relative = "index.html" if path in ("/", "/app", "/app/") else path.lstrip("/")
            target = (static_root / relative).resolve()
            root = static_root.resolve()
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

        def _dispatch(self, method: str) -> None:
            path = urlsplit(self.path).path
            try:
                if path.startswith("/api/"):
                    self._route_api(method, path)
                    return
                if method == "GET":
                    self._serve_static(path)
                    return
                self._error(405, "Method not allowed")
            except TelegramWebAppAuthError as exc:
                self._error(401, str(exc))
            except CommerceError as exc:
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


def create_server(application: AuriXVpnWebApplication, *, port: int, static_root: Path = STATIC_ROOT):
    if not 1 <= int(port) <= 65_535:
        raise ValueError("PORT must be between 1 and 65535")
    return ThreadingHTTPServer(("0.0.0.0", int(port)), make_handler(application, static_root))


def main() -> int:
    try:
        port = int(os.environ.get("PORT", "10000"))
        max_age = int(os.environ.get("AURIX_WEB_APP_INIT_DATA_MAX_AGE", "86400"))
    except ValueError:
        print("PORT and AURIX_WEB_APP_INIT_DATA_MAX_AGE must be integers", file=sys.stderr)
        return 2
    try:
        runtime = build_runtime_services(
            validate_telegram=True,
            check_outline=False,
            reconcile=False,
            configure_bootstrap=False,
        )
        application = AuriXVpnWebApplication(
            runtime,
            max_init_data_age=max_age,
            telegram_url=os.environ.get("AURIX_TELEGRAM_URL", ""),
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
