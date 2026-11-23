"""Static storefront plus safe live-catalog and protected admin boundaries."""

from __future__ import annotations

import json
import base64
import binascii
from ipaddress import ip_address
import logging
import os
import re
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from typing import Any
from .commerce_settings import env_commerce_settings, commerce_paused, SettingsConflict

from .catalog import (
    CatalogInputError,
    catalog_response,
    navigation_manifest,
    product_detail,
    product_offers,
    reference_discount_events,
    reference_news,
    reference_news_detail,
)
from .customer_api import (
    CustomerApiError,
    CustomerAuthError,
    CustomerIdentityConflict,
    CustomerIdentityService,
    CustomerOrderService,
    CustomerWalletService,
    SESSION_COOKIE_NAME,
    SignedCustomerTokenVerifier,
)
from .customer_ledger import InsufficientFundsError, LedgerConflictError, LedgerInputError, LedgerOperationError
from .customer_checkout import CheckoutInputError, CustomerCheckoutService
from .checkout_journey import CheckoutJourney, GameAccountLookup, read_receiver_qr
from .game_admin import GameAdminAuthError, GameAdminInputError, GameStoreAdminService
from .order_reconciliation import (
    SupplierWebhookReconciler,
    WebhookAuthenticationError,
    WebhookReconciliationError,
    poll_status_is_fresh,
    reconcile_order,
)
from .payment_review import (
    PaymentReviewService, PaymentReviewError, PaymentReviewerAuthError, StaffPaymentReviewer, MAX_RECEIPT_BODY_BYTES,
)
from .order_repository import OrderNotFoundError, OrderRepositoryError, RecipientLookupThrottledError
from .payment_service import PaymentBoundaryError
from .order_state import OrderState
from .providers.fazercards_commands import SupplierCommandError, SupplierWriteDisabledError
from .public_catalog import PublicCatalogStore
from .pricing_store import PricingPolicyStore
from .catalog_policy import env_catalog_policy_store, CatalogPolicyConflict
from .draft_analysis import analyze_draft
from .strategy_guides import (
    DEFAULT_MLBB_SNAPSHOT_PATH,
    StrategyGuideInputError,
    strategy_guide,
    strategy_guides,
)
from .store_features import StoreFeatureStore


WEB_ROOT = Path(__file__).resolve().parents[1] / "web"
RELEASE_MARKER_PATH = Path(__file__).resolve().parents[1] / ".aurix-games-release.json"
UTC = timezone.utc
_PRODUCT_PATH = re.compile(r"^/api/v1/catalog/([a-z0-9-]{1,80})$")
_PRODUCT_OFFERS_PATH = re.compile(r"^/api/v1/catalog/([a-z0-9-]{1,80})/offers$")
_CATALOG_PRODUCT_PATH = re.compile(r"^/api/v1/catalog/products/([a-z0-9-]{1,80})$")
_CATALOG_PRODUCT_OFFERS_PATH = re.compile(r"^/api/v1/catalog/products/([a-z0-9-]{1,80})/offers$")
_ADMIN_PAYMENT_PATH = re.compile(r"^/api/v1/admin/supplier-wallet/payments/([A-Za-z0-9-]{1,128})$")
_ADMIN_VERIFY_PATH = re.compile(r"^/api/v1/admin/supplier-wallet/payments/([A-Za-z0-9-]{1,128})/verify-binance$")
_ADMIN_FUNDING_RESOLUTION_PATH = re.compile(
    r"^/api/v1/admin/supplier-wallet/funding-attempts/([A-Za-z0-9._:-]{1,128})/resolve-absent$"
)
_ADMIN_FUNDING_LINK_PATH = re.compile(
    r"^/api/v1/admin/supplier-wallet/funding-attempts/([A-Za-z0-9._:-]{1,128})/link-invoice$"
)
_CUSTOMER_ORDER_PATH = re.compile(r"^/api/v1/orders/([A-Za-z0-9._:-]{1,128})$")
_CUSTOMER_WALLET_PAYMENT_PATH = re.compile(r"^/api/v1/orders/([A-Za-z0-9._:-]{1,128})/wallet-payment$")
_CUSTOMER_PAYMENT_QR_PATH = re.compile(r"^/api/v1/orders/([A-Za-z0-9._:-]{1,128})/payment-qr$")
_ADMIN_GAME_ORDER_RECONCILE_PATH = re.compile(
    r"^/api/v1/admin/game-orders/([A-Za-z0-9._:-]{1,128})/reconcile$"
)
_REVIEW_ACTION_PATH = re.compile(r"^/api/v1/admin/payment-review/orders/([A-Za-z0-9._:-]{1,128})/revisions/([1-9][0-9]?)/(receipt|decision|resume|refund-complete)$")
_STAFF_ROOT = "/api/v1/store-staff"
_CUSTOMER_RECEIPT_PATH = re.compile(r"^/api/v1/orders/([A-Za-z0-9._:-]{1,128})/payment-evidence$")
_STRATEGY_GUIDE_PATH = re.compile(r"^/api/v1/strategy-guides/([a-z0-9-]{1,80})$")
_REFERENCE_NEWS_DETAIL_PATH = re.compile(r"^/api/v1/reference/events-news/([a-z0-9-]{1,80})$")
_MLBB_LOOKUP_PATH = "/api/v1/games/mlbb/account-check"
_GAME_ACCOUNT_LOOKUP_PATH = re.compile(r"^/api/v1/games/([a-z0-9-]{1,80})/account-check$")


def _mlbb_snapshot_path() -> str | Path | None:
    configured = os.environ.get("AURIX_GAMES_MLBB_GUIDE_SNAPSHOT")
    if configured:
        return configured
    return DEFAULT_MLBB_SNAPSHOT_PATH if DEFAULT_MLBB_SNAPSHOT_PATH.is_file() else None


def make_handler(
    *,
    web_root: str | Path = WEB_ROOT,
    public_catalog_path: str | Path | None = None,
    admin_service: GameStoreAdminService | None = None,
    order_reconciler: SupplierWebhookReconciler | None = None,
    customer_order_service: CustomerOrderService | None = None,
    customer_wallet_service: CustomerWalletService | None = None,
    customer_auth: SignedCustomerTokenVerifier | None = None,
    customer_identity_service: CustomerIdentityService | None = None,
    customer_checkout_service: CustomerCheckoutService | None = None,
    checkout_journey: CheckoutJourney | None = None,
    mlbb_lookup: GameAccountLookup | None = None,
    topup_preview: dict[str, Any] | None = None,
    payment_review: PaymentReviewService | None = None,
    receiver_qr_dir: str | Path | None = None,
    store_features: StoreFeatureStore | None = None,
    release_marker_path: str | Path | None = None,
    redirect_live_store: bool = False,
) -> type[SimpleHTTPRequestHandler]:
    """Create the storefront handler with optional live/admin capabilities."""

    # Every private route must use the same revocation boundary as sign-in.
    if customer_identity_service is not None:
        customer_auth = customer_identity_service.verifier
    staff_owner = os.environ.get("AURIX_GAMES_SUPER_ADMIN_CUSTOMER_ID", "").strip()
    qr_directory = receiver_qr_dir
    if qr_directory is None:
        qr_directory = os.environ.get("AURIX_GAMES_RECEIVER_QR_DIR") or None
    if qr_directory is None and checkout_journey is not None:
        qr_directory = checkout_journey.receiver_qr_dir
    if qr_directory is not None:
        qr_directory = Path(qr_directory)
        if not qr_directory.is_absolute():
            raise ValueError("payment QR directory must be absolute")
        try:
            qr_directory = qr_directory.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("payment QR directory is unavailable") from exc
        if not qr_directory.is_dir():
            raise ValueError("payment QR directory is unavailable")
    root = Path(web_root).resolve()
    release_marker = Path(release_marker_path) if release_marker_path is not None else RELEASE_MARKER_PATH
    games_state_dir = Path(os.environ.get('AURIX_GAMES_BASE_DIR', '.local/fazercards'))
    live_store = PublicCatalogStore(
        public_catalog_path
        if public_catalog_path is not None
        else os.environ.get("AURIX_GAMES_PUBLIC_CATALOG", str(games_state_dir / 'public-catalog.json')),
        catalog_policy_store=getattr(admin_service, 'catalog_policy_store', None) or env_catalog_policy_store(),
        pricing_policy_store=getattr(admin_service, 'pricing_policy_store', None) or PricingPolicyStore(
            os.environ.get('FAZERCARDS_PUBLIC_CATALOG_CONFIG', str(
                (Path(public_catalog_path).parent if public_catalog_path is not None else games_state_dir)
                / 'public-catalog-config.json'))),
        discovery_path=os.environ.get(
            "AURIX_GAMES_SUPPLIER_DISCOVERY",
            str(games_state_dir / 'supplier-discovery.json'),
        ),
        topup_preview=topup_preview,
    )

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(root), **kwargs)

        def _lookup_failure(self, error):
            logging.getLogger(__name__).warning(
                "FazerCards recipient validation failed; upstream_status=%s",
                error.http_status if error.http_status is not None else "transport_or_response",
            )
            if error.http_status == 422:
                self._json(400, {"error": "FazerCards could not confirm these account details. Check the Player ID and requested fields."})
            elif error.http_status == 429:
                self._json(429, {"error": "FazerCards is receiving too many account checks. Wait a moment and retry."})
            else:
                self._json(503, {"error": "FazerCards account lookup is temporarily unavailable. Your details are kept; please retry."})

        def _edge_client_ip(self):
            try:
                if not ip_address(self.client_address[0]).is_loopback:
                    return None
                return str(ip_address(self.headers.get("X-AuriX-Client-IP", "")))
            except ValueError:
                return None

        def end_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin-allow-popups")
            if urlsplit(self.path).path.startswith(("/game-store/staff/", "/game-store/admin/")):
                self.send_header("Cache-Control", "private, no-store")
            self.send_header(
                "Content-Security-Policy",
                # shortcut: pin the current Google GIS inline stylesheet; refresh its hash when GIS changes.
                "default-src 'self'; style-src 'self' https://accounts.google.com/gsi/style 'sha256-RU4sU0AaS8IBGZx8XrGt/pa9A5SLA3dQszGeqT5L3Kw=' 'sha256-Cmx01mi2Nog0fYVZmwQe0PjoTDJiF9gQYpWwr/UEVMI='; "
                "script-src 'self' https://accounts.google.com/gsi/client https://telegram.org; "
                "img-src 'self' data:; "
                "connect-src 'self' https://accounts.google.com/gsi/ https://oauth.telegram.org; "
                "frame-src https://accounts.google.com/gsi/ https://oauth.telegram.org; "
                "base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
            )
            super().end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            # Never log query text; it may contain user-entered recipient data
            # if a later route is added incorrectly.
            route = urlsplit(self.path).path
            super().log_message("%s %s", self.command, route)

        def _json(self, status: int, payload: dict[str, Any], *, set_cookie: str | None = None) -> None:
            body = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            if set_cookie is not None:
                self.send_header("Set-Cookie", set_cookie)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _missing_catalog_product(self) -> None:
            unavailable = live_store.controls_active() and live_store.status().get("status") != "fresh"
            if unavailable:
                self._json(503, {"error": "approved catalog is unavailable"})
            else:
                self._json(404, {"error": "product not found"})

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._handle_get()

        def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._handle_get()

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._handle_post()

        def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._handle_put()

        def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._handle_delete()

        def _handle_get(self) -> None:
            parsed = urlsplit(self.path)
            if redirect_live_store and (parsed.path == "/" or parsed.path.startswith("/game-store/")):
                self.send_response(307)
                self.send_header("Location", "https://games.aurix-mart.tech" + ("/game-store/" if parsed.path == "/" else parsed.path))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed.path == "/api/version":
                if parsed.query:
                    self._json(400, {"error": "version check does not accept query parameters"})
                    return
                try:
                    marker = json.loads(release_marker.read_text(encoding="utf-8"))
                    release_id = marker.get("release_id") if isinstance(marker, dict) else None
                    if not isinstance(release_id, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|legacy-[0-9a-f]{64})", release_id):
                        raise ValueError("release marker is invalid")
                except (OSError, ValueError, json.JSONDecodeError):
                    self._json(503, {"error": "release version is unavailable"})
                    return
                self._json(200, {"release_id": release_id, "commit_sha": marker.get("commit_sha")})
                return
            if parsed.path == "/api/v1/admin/commerce-settings":
                try:
                    if admin_service is None:
                        self._json(503, {"error": "Admin settings are unavailable."})
                        return
                    admin_service.authenticate(self.headers.get("Authorization"))
                    self._json(200, env_commerce_settings().status())
                except GameAdminAuthError:
                    self._json(401, {"error": "Administrator authorization required."})
                except ValueError:
                    self._json(503, {"error": "Commerce settings are unavailable."})
                return
            if parsed.path.startswith(_STAFF_ROOT + "/"):
                self._store_staff(parsed)
                return
            if parsed.path.startswith("/api/v1/admin/payment-review/"):
                self._operator_payment_review(parsed)
                return
            if _CUSTOMER_RECEIPT_PATH.fullmatch(parsed.path):
                self._payment_evidence(parsed, upload=False)
                return
            if parsed.path == "/api/v1/checkout/config":
                rails = sorted(checkout_journey.payment_receivers) if checkout_journey and not commerce_paused("payments_enabled") else []
                self._json(200, {"account_check_enabled": mlbb_lookup is not None and not commerce_paused("account_checks_enabled"),
                    "recipient_review_enabled": checkout_journey is not None and not commerce_paused("account_checks_enabled") and not commerce_paused("checkout_enabled"),
                    "payments_enabled": bool(rails), "payment_rails": rails})
                return
            if parsed.path == "/api/v1/games/mlbb/zones":
                self._json(200, {"zones": mlbb_lookup.zones if mlbb_lookup else []})
                return
            if len(parsed.query) > 500:
                self._json(400, {"error": "query string is too long"})
                return
            if parsed.path in {"/api/v1/store/notices", "/api/v1/store/leaderboard", "/api/v1/leaderboard/profile"}:
                if parsed.query:
                    self._json(400, {"error": "store feature reads do not accept query parameters"})
                    return
                if store_features is None:
                    self._json(503, {"error": "store notices and leaderboard are unavailable"})
                    return
                try:
                    if parsed.path == "/api/v1/store/notices":
                        self._json(200, {"notices": store_features.public_notices()})
                        return
                    if parsed.path == "/api/v1/store/leaderboard":
                        self._json(200, store_features.leaderboard())
                        return
                    if customer_auth is None:
                        self._json(503, {"error": "customer sign-in is unavailable"})
                        return
                    principal = customer_auth.verify(
                        self.headers.get("Authorization"), cookie_header=self.headers.get("Cookie")
                    )
                    self._json(200, store_features.leaderboard_profile(principal.customer_id))
                except CustomerAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except Exception:
                    self._json(503, {"error": "store feature data is unavailable"})
                return
            if parsed.path == "/api/v1/admin/store-notices":
                if parsed.query:
                    self._json(400, {"error": "notice list does not accept query parameters"})
                    return
                if admin_service is None or store_features is None:
                    self._json(503, {"error": "store notice administration is unavailable"})
                    return
                try:
                    admin_service.authenticate(self.headers.get("Authorization"))
                    self._json(200, {"notices": store_features.admin_notices()})
                except GameAdminAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except Exception:
                    self._json(503, {"error": "store notices could not be loaded"})
                return
            if Path(unquote(parsed.path)).name.casefold() == "test_token.txt":
                self.send_error(404)
                return
            if parsed.path == "/api/v1/auth/config":
                if parsed.query:
                    self._json(400, {"error": "authentication config does not accept query parameters"})
                    return
                self._json(
                    200,
                    customer_identity_service.config_payload()
                    if customer_identity_service is not None
                    else {"providers": {"telegram": {"enabled": False}, "google": {"enabled": False}}},
                )
                return
            if parsed.path == "/api/v1/auth/session":
                if parsed.query:
                    self._json(400, {"error": "customer session does not accept query parameters"})
                    return
                if customer_auth is None or customer_identity_service is None:
                    self._json(200, {"authenticated": False, "user": None})
                    return
                try:
                    principal = customer_auth.verify(
                        self.headers.get("Authorization"), cookie_header=self.headers.get("Cookie")
                    )
                    self._json(200, {"authenticated": True, "user": {
                        "customer_id": principal.customer_id,
                        "providers": customer_identity_service.identity_store.providers_for(principal.customer_id),
                        "staff_role": customer_identity_service.identity_store.staff_role(principal.customer_id, staff_owner),
                    }})
                except CustomerAuthError:
                    self._json(200, {"authenticated": False, "user": None})
                except Exception:
                    self._json(503, {"error": "customer session check is unavailable"})
                return
            if parsed.path == "/api/healthz":
                if parsed.query:
                    self._json(400, {"error": "health check does not accept query parameters"})
                    return
                checkout_enabled = os.environ.get("AURIX_GAMES_CUSTOMER_CHECKOUT_ENABLED", "0").strip() == "1"
                self._json(200, {"status": "ok", "service": "aurix-games", "checkout_enabled": checkout_enabled})
                return
            if parsed.path == "/api/readyz":
                if parsed.query:
                    self._json(400, {"error": "readiness check does not accept query parameters"})
                    return
                try:
                    catalog = live_store.response(limit=50)
                    settings = env_commerce_settings().status()["settings"]
                    products = catalog.get("products", []) if isinstance(catalog, dict) else []
                    checks = {
                        "approved_catalog": isinstance(catalog, dict) and catalog.get("catalog_controls_active") is True,
                        "purchasable_offer": any(item.get("purchase_enabled") is True for item in products if isinstance(item, dict)),
                        "customer_auth": customer_auth is not None,
                        "customer_orders": customer_order_service is not None and customer_checkout_service is not None,
                        "recipient_lookup": mlbb_lookup is not None,
                        "payment_receivers": bool(checkout_journey and checkout_journey.payment_receivers),
                        "payment_review": bool(payment_review and getattr(payment_review, "reviewers", {})),
                        "wallet_ledger": customer_wallet_service is not None,
                        "wallet_checkout": bool(checkout_journey and checkout_journey.wallet_payment_service),
                        "supplier_reconciliation": order_reconciler is not None or poll_status_is_fresh(
                            os.environ.get(
                                "AURIX_GAMES_RECONCILIATION_STATUS_FILE",
                                str(Path(os.environ.get("AURIX_GAMES_BASE_DIR", ".local/fazercards"))
                                    / "supplier-reconciliation-status.json"),
                            )
                        ),
                        "commerce_controls": all(settings.get(flag) is True for flag in (
                            "checkout_enabled", "account_checks_enabled", "payments_enabled", "supplier_orders_enabled"
                        )),
                    }
                except Exception:
                    checks = {"approved_catalog": False}
                ready = all(checks.values())
                self._json(200 if ready else 503, {
                    "status": "ready" if ready else "not_ready",
                    "service": "aurix-games",
                    "checks": checks,
                })
                return
            if parsed.path == "/api/v1/admin/supplier-wallet/status":
                if admin_service is None:
                    self._json(503, {"error": "admin operations are not configured"})
                    return
                try:
                    admin_service.authenticate(self.headers.get("Authorization"))
                    self._json(200, admin_service.wallet_status())
                except GameAdminAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except Exception as exc:
                    self._json(502, {"error": "supplier wallet status failed"})
                return
            if parsed.path == "/api/v1/admin/supplier-wallet/funding-attempts":
                if admin_service is None:
                    self._json(503, {"error": "admin operations are not configured"})
                    return
                if parsed.query:
                    self._json(400, {"error": "funding attempts do not accept query parameters"})
                    return
                try:
                    admin_service.authenticate(self.headers.get("Authorization"))
                    self._json(200, admin_service.funding_reconciliation_status())
                except GameAdminAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except Exception:
                    self._json(502, {"error": "funding reconciliation state could not be loaded"})
                return
            if parsed.path == "/api/v1/admin/money-integrity":
                if admin_service is None:
                    self._json(503, {"error": "admin operations are not configured"})
                    return
                try:
                    admin_service.authenticate(self.headers.get("Authorization"))
                    if parsed.query:
                        self._json(400, {"error": "money integrity does not accept query parameters"})
                        return
                    self._json(200, admin_service.money_integrity())
                except GameAdminAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except Exception:
                    self._json(503, {"error": "money integrity unavailable; check store configuration and schema"})
                return
            if parsed.path == "/api/v1/admin/game-orders/attention":
                if admin_service is None or customer_order_service is None:
                    self._json(503, {"error": "game order operations are not configured"})
                    return
                if parsed.query:
                    self._json(400, {"error": "game order attention does not accept query parameters"})
                    return
                try:
                    admin_service.authenticate(self.headers.get("Authorization"))
                    repository = customer_order_service.repository
                    rows = []
                    supplier_read_available = getattr(admin_service, "supplier_client", None) is not None
                    pollable_states = {
                        OrderState.SUBMITTED, OrderState.PROCESSING, OrderState.UNKNOWN,
                        OrderState.RECONCILIATION, OrderState.FAILED, OrderState.DELIVERED,
                    }
                    for state in (OrderState.UNKNOWN, OrderState.RECONCILIATION,
                                  OrderState.REVIEW_REFUND, OrderState.FAILED):
                        for order in repository.list_by_state(state, limit=100):
                            updated_at = order.events[-1].at if order.events else None
                            if order.state not in pollable_states:
                                reconcile_reason = "not_pollable"
                            elif not order.provider_order_id:
                                reconcile_reason = "supplier_attempt_missing"
                            elif not supplier_read_available:
                                reconcile_reason = "supplier_read_not_configured"
                            else:
                                reconcile_reason = None
                            rows.append({
                                "order_id": order.order_id,
                                "state": order.state.value,
                                "product_slug": order.snapshot.product_slug,
                                "amount_minor": order.snapshot.amount_minor,
                                "currency": order.snapshot.currency,
                                "updated_at": None if updated_at is None else updated_at.isoformat(),
                                "reconcile_available": reconcile_reason is None,
                                "reconcile_unavailable_reason": reconcile_reason,
                            })
                    rows.sort(key=lambda row: row["updated_at"] or "")
                    self._json(200, {"orders": rows[:200], "truncated": len(rows) > 200})
                except GameAdminAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except Exception:
                    self._json(502, {"error": "game order attention could not be loaded"})
                return
            if parsed.path == "/api/v1/admin/supplier-wallet/payment-methods":
                if admin_service is None:
                    self._json(503, {"error": "admin operations are not configured"})
                    return
                if parsed.query:
                    self._json(400, {"error": "payment-method lookup does not accept query parameters"})
                    return
                try:
                    admin_service.authenticate(self.headers.get("Authorization"))
                    self._json(200, admin_service.supplier_payment_methods())
                except GameAdminAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except GameAdminInputError as exc:
                    self._json(502, {"error": str(exc)})
                except Exception:
                    self._json(502, {"error": "supplier payment methods failed"})
                return
            if parsed.path == "/api/v1/admin/supplier-wallet/alerts":
                if admin_service is None:
                    self._json(503, {"error": "admin operations are not configured"})
                    return
                parameters = parse_qs(parsed.query, keep_blank_values=True)
                if set(parameters) - {"status", "limit"}:
                    self._json(400, {"error": "unsupported query parameter"})
                    return
                try:
                    admin_service.authenticate(self.headers.get("Authorization"))
                    status = _one(parameters, "status") or "pending"
                    limit_text = _one(parameters, "limit")
                    limit = 20 if limit_text is None else int(limit_text)
                    self._json(200, admin_service.wallet_alerts(status=status, limit=limit))
                except GameAdminAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except (GameAdminInputError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
                except Exception as exc:
                    self._json(502, {"error": "supplier wallet alerts failed"})
                return
            if parsed.path in {"/api/v1/checkout/review", "/api/v1/checkout/confirm", "/api/v1/checkout/payment"}:
                self._json(405, {"error": "use POST for checkout review"})
                return
            if parsed.path == "/api/v1/orders":
                if customer_order_service is None or customer_auth is None:
                    self._json(503, {"error": "customer order history is not configured"})
                    return
                if parsed.query:
                    self._json(400, {"error": "order history does not accept query parameters"})
                    return
                try:
                    principal = customer_auth.verify(
                        self.headers.get("Authorization"), cookie_header=self.headers.get("Cookie")
                    )
                    self._json(200, {"orders": customer_order_service.list_orders(principal)})
                except CustomerAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except CustomerApiError as exc:
                    self._json(404, {"error": str(exc)})
                except Exception as exc:
                    self._json(500, {"error": "customer order history failed"})
                return
            if parsed.path == "/api/v1/wallet":
                if customer_wallet_service is None or customer_auth is None:
                    self._json(503, {"error": "customer wallet is not configured"})
                    return
                if parsed.query:
                    self._json(400, {"error": "wallet does not accept query parameters"})
                    return
                try:
                    principal = customer_auth.verify(
                        self.headers.get("Authorization"), cookie_header=self.headers.get("Cookie")
                    )
                    self._json(200, {"wallet": customer_wallet_service.balance(principal)})
                except CustomerAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except (LedgerInputError, LedgerOperationError) as exc:
                    self._json(500, {"error": str(exc)})
                except Exception as exc:
                    self._json(500, {"error": "customer wallet read failed"})
                return
            if parsed.path == "/api/v1/top-up-history":
                if customer_wallet_service is None or customer_auth is None:
                    self._json(503, {"error": "customer wallet is not configured"})
                    return
                parameters = parse_qs(parsed.query, keep_blank_values=True)
                if set(parameters) - {"limit", "offset"}:
                    self._json(400, {"error": "unsupported query parameter"})
                    return
                try:
                    principal = customer_auth.verify(
                        self.headers.get("Authorization"), cookie_header=self.headers.get("Cookie")
                    )
                    limit_text = _one(parameters, "limit")
                    offset_text = _one(parameters, "offset")
                    limit = 50 if limit_text is None else int(limit_text)
                    offset = 0 if offset_text is None else int(offset_text)
                    self._json(
                        200,
                        customer_wallet_service.top_up_history(principal, limit=limit, offset=offset),
                    )
                except CustomerAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except (LedgerInputError, LedgerOperationError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
                except Exception as exc:
                    self._json(500, {"error": "top-up history failed"})
                return
            customer_order_match = _CUSTOMER_ORDER_PATH.fullmatch(parsed.path)
            if customer_order_match:
                if customer_order_service is None or customer_auth is None:
                    self._json(503, {"error": "customer order history is not configured"})
                    return
                if parsed.query:
                    self._json(400, {"error": "order detail does not accept query parameters"})
                    return
                try:
                    principal = customer_auth.verify(
                        self.headers.get("Authorization"), cookie_header=self.headers.get("Cookie")
                    )
                    self._json(
                        200,
                        customer_order_service.get_order(principal, customer_order_match.group(1), payment_review=payment_review),
                    )
                except CustomerAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except CustomerApiError as exc:
                    self._json(404, {"error": str(exc)})
                except Exception as exc:
                    self._json(500, {"error": "customer order detail failed"})
                return
            customer_qr_match = _CUSTOMER_PAYMENT_QR_PATH.fullmatch(parsed.path)
            if customer_qr_match:
                if customer_order_service is None or customer_auth is None:
                    self._json(503, {"error": "customer order history is not configured"})
                    return
                if qr_directory is None:
                    self._json(404, {"error": "payment QR is unavailable"})
                    return
                if parsed.query:
                    self._json(400, {"error": "payment QR does not accept query parameters"})
                    return
                try:
                    principal = customer_auth.verify(
                        self.headers.get("Authorization"), cookie_header=self.headers.get("Cookie")
                    )
                    order = customer_order_service.get_order(principal, customer_qr_match.group(1))
                    payment = order.get("payment")
                    if (order.get("state") != "awaiting_payment" or not isinstance(payment, dict)
                            or payment.get("expired") or not isinstance(payment.get("qr_asset"), str)
                            or not isinstance(payment.get("qr_sha256"), str)):
                        self._json(404, {"error": "payment QR is unavailable"})
                        return
                    image = read_receiver_qr(
                        qr_directory, payment["qr_asset"], expected_sha256=payment["qr_sha256"]
                    )
                    if image is None:
                        self._json(404, {"error": "payment QR is unavailable"})
                        return
                    mime_type, image_bytes = image
                    self.send_response(200)
                    self.send_header("Content-Type", mime_type)
                    self.send_header("Cache-Control", "private, no-store")
                    self.send_header("Content-Length", str(len(image_bytes)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(image_bytes)
                except CustomerAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except CustomerApiError:
                    self._json(404, {"error": "payment QR is unavailable"})
                except Exception:
                    self._json(502, {"error": "payment QR could not be loaded"})
                return
            if parsed.path == '/api/v1/admin/catalog-policy':
                if admin_service is None:
                    self._json(503, {'error': 'admin operations are not configured'})
                    return
                try:
                    admin_service.authenticate(self.headers.get('Authorization'))
                    if parsed.query:
                        raise ValueError('catalog policy does not accept query parameters')
                    self._json(200, admin_service.get_catalog_policy())
                except GameAdminAuthError:
                    self._json(401, {'error': 'administrator authorization required'})
                except ValueError:
                    self._json(400, {'error': 'invalid catalog policy request'})
                except Exception:
                    self._json(503, {'error': 'catalog policy is unavailable'})
                return
            if parsed.path == "/api/v1/admin/pricing-policy":
                if admin_service is None:
                    self._json(503, {"error": "admin operations are not configured"})
                    return
                try:
                    admin_service.authenticate(self.headers.get("Authorization"))
                    self._json(200, admin_service.get_pricing_policy())
                except GameAdminAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except Exception as exc:
                    self._json(502, {"error": "pricing policy could not be loaded"})
                return
            if parsed.path == "/api/v1/admin/live-catalog/status":
                if admin_service is None:
                    self._json(503, {"error": "admin operations are not configured"})
                    return
                try:
                    admin_service.authenticate(self.headers.get("Authorization"))
                    self._json(200, live_store.status())
                except GameAdminAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except Exception as exc:
                    self._json(502, {"error": "live catalog status failed"})
                return
            if parsed.path == "/api/v1/admin/catalog-refresh-status":
                if admin_service is None:
                    self._json(503, {"error": "admin operations are not configured"})
                    return
                try:
                    admin_service.authenticate(self.headers.get("Authorization"))
                    if parsed.query:
                        raise ValueError("catalog refresh status does not accept query parameters")
                    self._json(200, admin_service.catalog_refresh_status())
                except GameAdminAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except ValueError:
                    self._json(400, {"error": "invalid catalog refresh status request"})
                except Exception:
                    self._json(502, {"error": "catalog refresh status failed"})
                return
            if parsed.path == "/api/v1/admin/mapping-review":
                if admin_service is None:
                    self._json(503, {"error": "admin operations are not configured"})
                    return
                if parsed.query:
                    self._json(400, {"error": "mapping review does not accept query parameters"})
                    return
                try:
                    admin_service.authenticate(self.headers.get("Authorization"))
                    self._json(200, admin_service.mapping_review_status())
                except GameAdminAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except GameAdminInputError as exc:
                    self._json(502, {"error": str(exc)})
                except Exception as exc:
                    self._json(502, {"error": "mapping review failed"})
                return
            if parsed.path == "/api/v1/admin/mapping-history":
                if admin_service is None:
                    self._json(503, {"error": "admin operations are not configured"})
                    return
                if parsed.query:
                    self._json(400, {"error": "mapping history does not accept query parameters"})
                    return
                try:
                    admin_service.authenticate(self.headers.get("Authorization"))
                    self._json(200, admin_service.mapping_history_status())
                except GameAdminAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except GameAdminInputError as exc:
                    self._json(502, {"error": str(exc)})
                except Exception as exc:
                    self._json(502, {"error": "mapping history failed"})
                return
            if parsed.path == "/api/v1/admin/catalog-inventory":
                if admin_service is None:
                    self._json(503, {"error": "admin operations are not configured"})
                    return
                parameters = parse_qs(parsed.query, keep_blank_values=True)
                if set(parameters) - {"family", "category", "q", "limit", "offset"}:
                    self._json(400, {"error": "unsupported query parameter"})
                    return
                try:
                    admin_service.authenticate(self.headers.get("Authorization"))
                    family = _one(parameters, "family")
                    category = _one(parameters, "category")
                    query = _one(parameters, "q") or ""
                    limit_text = _one(parameters, "limit")
                    offset_text = _one(parameters, "offset")
                    limit = 50 if limit_text is None else int(limit_text)
                    offset = 0 if offset_text is None else int(offset_text)
                    self._json(
                        200,
                        admin_service.catalog_inventory(
                            family=family,
                            category=category,
                            query=query,
                            limit=limit,
                            offset=offset,
                        ),
                    )
                except GameAdminAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except (GameAdminInputError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
                except Exception as exc:
                    self._json(502, {"error": "catalog inventory failed"})
                return
            if parsed.path == "/api/v1/admin/subscription/status":
                if admin_service is None:
                    self._json(503, {"error": "admin operations are not configured"})
                    return
                try:
                    admin_service.authenticate(self.headers.get("Authorization"))
                    self._json(200, admin_service.subscription_status())
                except GameAdminAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except Exception as exc:
                    self._json(502, {"error": "supplier subscription status failed"})
                return
            if parsed.path == "/api/v1/admin/supplier-webhook":
                if admin_service is None:
                    self._json(503, {"error": "admin operations are not configured"})
                    return
                if parsed.query:
                    self._json(400, {"error": "supplier webhook status does not accept query parameters"})
                    return
                try:
                    admin_service.authenticate(self.headers.get("Authorization"))
                    self._json(200, admin_service.webhook_status())
                except GameAdminAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except GameAdminInputError as exc:
                    self._json(502, {"error": str(exc)})
                except Exception as exc:
                    self._json(502, {"error": "supplier webhook status failed"})
                return
            payment_match = _ADMIN_PAYMENT_PATH.fullmatch(parsed.path)
            if payment_match:
                if admin_service is None:
                    self._json(503, {"error": "admin operations are not configured"})
                    return
                try:
                    admin_service.authenticate(self.headers.get("Authorization"))
                    self._json(200, admin_service.payment_status(payment_match.group(1)))
                except GameAdminAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except (GameAdminInputError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
                except Exception as exc:
                    self._json(502, {"error": "supplier payment status failed"})
                return
            if parsed.path == "/api/v1/catalog":
                parameters = parse_qs(parsed.query, keep_blank_values=True)
                if set(parameters) - {"category", "q", "limit", "offset"}:
                    self._json(400, {"error": "unsupported query parameter"})
                    return
                try:
                    category = _one(parameters, "category")
                    query = _one(parameters, "q") or ""
                    limit_text = _one(parameters, "limit")
                    offset_text = _one(parameters, "offset")
                    limit = 50 if limit_text is None else int(limit_text)
                    offset = 0 if offset_text is None else int(offset_text)
                    if live_store.controls_active() or live_store.supplier_discovery_available():
                        payload = live_store.response(category=category, query=query, limit=limit, offset=offset)
                        if payload is None:
                            self._json(503, {'error': 'approved catalog is unavailable'})
                            return
                    else:
                        payload = catalog_response(category=category, query=query, limit=limit, offset=offset,
                                                   topup_preview=topup_preview)
                except (CatalogInputError, ValueError) as exc:
                    self._json(400, {"error": str(exc) or "invalid query"})
                    return
                self._json(200, payload)
                return

            # Keep the resource-oriented catalog paths as aliases of the
            # existing catalog contract, including its shared checkout gate.
            if parsed.path == "/api/v1/catalog/categories":
                if parsed.query:
                    self._json(400, {"error": "categories does not accept query parameters"})
                    return
                use_live_store = live_store.controls_active() or live_store.supplier_discovery_available()
                payload = live_store.response(limit=1) if use_live_store else catalog_response(limit=1)
                if payload is None:
                    self._json(503, {'error': 'approved catalog is unavailable'})
                    return
                self._json(
                    200,
                    {
                        "schema_version": payload["schema_version"],
                        "status": payload["status"],
                        "checkout_enabled": os.environ.get("AURIX_GAMES_CUSTOMER_CHECKOUT_ENABLED", "0").strip() == "1" if payload.get("checkout_enabled") is False else payload.get("checkout_enabled", False),
                        "categories": payload["categories"],
                    },
                )
                return

            if parsed.path == "/api/v1/catalog/products":
                parameters = parse_qs(parsed.query, keep_blank_values=True)
                if set(parameters) - {"category", "q", "search", "limit", "offset", "cursor"}:
                    self._json(400, {"error": "unsupported query parameter"})
                    return
                try:
                    category = _one(parameters, "category")
                    query_value = _one(parameters, "q")
                    search_value = _one(parameters, "search")
                    if query_value is not None and search_value is not None and query_value != search_value:
                        raise CatalogInputError("q and search must match when both are supplied")
                    query = search_value if search_value is not None else (query_value or "")
                    limit_text = _one(parameters, "limit")
                    offset_text = _one(parameters, "offset")
                    cursor_text = _one(parameters, "cursor")
                    if offset_text is not None and cursor_text is not None and offset_text != cursor_text:
                        raise CatalogInputError("offset and cursor must match when both are supplied")
                    offset_text = cursor_text if cursor_text is not None else offset_text
                    limit = 50 if limit_text is None else int(limit_text)
                    offset = 0 if offset_text is None else int(offset_text)
                    if live_store.controls_active() or live_store.supplier_discovery_available():
                        payload = live_store.response(category=category, query=query, limit=limit, offset=offset)
                        if payload is None:
                            self._json(503, {'error': 'approved catalog is unavailable'})
                            return
                    else:
                        payload = catalog_response(category=category, query=query, limit=limit, offset=offset,
                                                   topup_preview=topup_preview)
                    payload["next_cursor"] = (
                        str(payload["next_offset"]) if payload["next_offset"] is not None else None
                    )
                except (CatalogInputError, ValueError) as exc:
                    self._json(400, {"error": str(exc) or "invalid query"})
                    return
                self._json(200, payload)
                return

            if parsed.path == "/api/v1/catalog/discovery":
                if parsed.query:
                    self._json(400, {"error": "catalog discovery does not accept query parameters"})
                    return
                payload = live_store.discovery()
                if payload is None:
                    self._json(503, {"error": "recent game list is unavailable"})
                    return
                self._json(200, payload)
                return

            if parsed.path == "/api/v1/live-catalog":
                parameters = parse_qs(parsed.query, keep_blank_values=True)
                if set(parameters) - {"category", "q", "limit", "offset"}:
                    self._json(400, {"error": "unsupported query parameter"})
                    return
                try:
                    category = _one(parameters, "category")
                    query = _one(parameters, "q") or ""
                    limit = 50 if _one(parameters, "limit") is None else int(_one(parameters, "limit"))
                    offset = 0 if _one(parameters, "offset") is None else int(_one(parameters, "offset"))
                    if not 1 <= limit <= 50 or not 0 <= offset <= 10_000:
                        raise ValueError("limit or offset is out of range")
                    payload = live_store.response(query=query, category=category, limit=limit, offset=offset)
                except (ValueError, TypeError) as exc:
                    self._json(400, {"error": str(exc) or "invalid query"})
                    return
                if payload is None:
                    self._json(503, {"error": "live catalog is not available"})
                    return
                self._json(200, payload)
                return

            if parsed.path == "/api/v1/navigation":
                if parsed.query:
                    self._json(400, {"error": "navigation does not accept query parameters"})
                    return
                self._json(200, navigation_manifest())
                return

            if parsed.path == "/api/v1/reference/events-news":
                if parsed.query:
                    self._json(400, {"error": "reference news index does not accept query parameters"})
                    return
                self._json(200, reference_news())
                return

            if parsed.path == "/api/v1/reference/discount-events":
                if parsed.query:
                    self._json(400, {"error": "reference discount events does not accept query parameters"})
                    return
                self._json(200, reference_discount_events())
                return

            reference_news_match = _REFERENCE_NEWS_DETAIL_PATH.fullmatch(parsed.path)
            if reference_news_match:
                if parsed.query:
                    self._json(400, {"error": "reference news detail does not accept query parameters"})
                    return
                try:
                    payload = reference_news_detail(reference_news_match.group(1))
                except CatalogInputError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                if payload is None:
                    self._json(404, {"error": "reference news item not found"})
                    return
                self._json(200, payload)
                return

            if parsed.path == "/api/v1/strategy-guides":
                if parsed.query:
                    self._json(400, {"error": "strategy guide index does not accept query parameters"})
                    return
                try:
                    guides = strategy_guides(
                        mlbb_snapshot_path=_mlbb_snapshot_path()
                    )
                except StrategyGuideInputError as exc:
                    self._json(503, {"error": str(exc)})
                    return
                self._json(
                    200,
                    {
                        "schema_version": 1,
                        "status": "reference",
                        "guides": [
                            {
                                "slug": guide["slug"],
                                "game": guide["game"],
                                "locale": guide["locale"],
                                "status": guide["status"],
                                "freshness": guide["freshness"],
                                "player_lookup": guide["player_lookup"],
                            }
                            for guide in guides
                        ],
                    },
                )
                return

            strategy_match = _STRATEGY_GUIDE_PATH.fullmatch(parsed.path)
            if strategy_match:
                if parsed.query:
                    self._json(400, {"error": "strategy guide detail does not accept query parameters"})
                    return
                try:
                    guide = strategy_guide(
                        strategy_match.group(1),
                        mlbb_snapshot_path=_mlbb_snapshot_path(),
                    )
                except StrategyGuideInputError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                if guide is None:
                    self._json(404, {"error": "strategy guide not found"})
                    return
                self._json(200, guide)
                return

            offers_match = _PRODUCT_OFFERS_PATH.fullmatch(parsed.path)
            if offers_match:
                parameters = parse_qs(parsed.query, keep_blank_values=True)
                if set(parameters) - {"region"}:
                    self._json(400, {"error": "unsupported query parameter"})
                    return
                try:
                    region = _one(parameters, "region")
                    payload = live_store.product_offers(offers_match.group(1), region=region)
                    if payload is None and not live_store.controls_active():
                        payload = product_offers(offers_match.group(1), region=region, topup_preview=topup_preview)
                except (CatalogInputError, ValueError) as exc:
                    self._json(400, {"error": str(exc) or "invalid query"})
                    return
                if payload is None:
                    self._missing_catalog_product()
                    return
                self._json(200, payload)
                return

            catalog_offers_match = _CATALOG_PRODUCT_OFFERS_PATH.fullmatch(parsed.path)
            if catalog_offers_match:
                parameters = parse_qs(parsed.query, keep_blank_values=True)
                if set(parameters) - {"region"}:
                    self._json(400, {"error": "unsupported query parameter"})
                    return
                try:
                    region = _one(parameters, "region")
                    payload = live_store.product_offers(
                        catalog_offers_match.group(1),
                        region=region,
                    )
                    if payload is None and not live_store.controls_active():
                        payload = product_offers(
                            catalog_offers_match.group(1),
                            region=region,
                            topup_preview=topup_preview,
                        )
                except (CatalogInputError, ValueError) as exc:
                    self._json(400, {"error": str(exc) or "invalid query"})
                    return
                if payload is None:
                    self._missing_catalog_product()
                    return
                self._json(200, payload)
                return

            catalog_product_match = _CATALOG_PRODUCT_PATH.fullmatch(parsed.path)
            if catalog_product_match:
                if parsed.query:
                    self._json(400, {"error": "product detail does not accept query parameters"})
                    return
                slug = catalog_product_match.group(1)
                detail = live_store.product_detail(slug)
                if (detail is None and not live_store.controls_active()
                        and not live_store.supplier_slug_exists(slug)):
                    detail = product_detail(slug, topup_preview=topup_preview)
                if detail is None:
                    self._missing_catalog_product()
                    return
                self._json(200, detail)
                return

            match = _PRODUCT_PATH.fullmatch(parsed.path)
            if match:
                slug = match.group(1)
                detail = live_store.product_detail(slug)
                if (detail is None and not live_store.controls_active()
                        and not live_store.supplier_slug_exists(slug)):
                    detail = product_detail(slug, topup_preview=topup_preview)
                if detail is None:
                    self._missing_catalog_product()
                    return
                self._json(200, detail)
                return

            if self.command == "HEAD":
                super().do_HEAD()
            else:
                super().do_GET()

        def _handle_post(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/api/v1/admin/store-notices":
                if parsed.query:
                    self._json(400, {"error": "notice creation does not accept query parameters"})
                    return
                if admin_service is None or store_features is None:
                    self._json(503, {"error": "store notice administration is unavailable"})
                    return
                try:
                    admin_service.authenticate(self.headers.get("Authorization"))
                    self._json(201, {"notice": store_features.save_notice(self._read_json_body())})
                except GameAdminAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except (ValueError, TypeError) as exc:
                    self._json(400, {"error": str(exc)})
                except Exception:
                    self._json(503, {"error": "notice could not be published"})
                return
            if parsed.path == "/api/v1/admin/commerce-settings":
                try:
                    if admin_service is None:
                        self._json(503, {"error": "Admin settings are unavailable."})
                        return
                    admin_service.authenticate(self.headers.get("Authorization"))
                    if parsed.query:
                        raise ValueError("Settings do not accept query parameters.")
                    body = self._read_json_body()
                    if set(body) != {"settings", "expected_revision"}:
                        raise ValueError("Invalid commerce settings request.")
                    self._json(200, env_commerce_settings().save(
                        body["settings"], expected_revision=body["expected_revision"], actor="admin"))
                except GameAdminAuthError:
                    self._json(401, {"error": "Administrator authorization required."})
                except SettingsConflict as exc:
                    self._json(409, {"error": str(exc)})
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                return
            if parsed.path.startswith(_STAFF_ROOT + "/"):
                self._store_staff(parsed)
                return
            if parsed.path == _MLBB_LOOKUP_PATH:
                lookup_product = "mobile-legends"
            else:
                lookup_match = _GAME_ACCOUNT_LOOKUP_PATH.fullmatch(parsed.path)
                lookup_product = lookup_match.group(1) if lookup_match else None
            if lookup_product is not None:
                if mlbb_lookup is None or commerce_paused("account_checks_enabled"):
                    self._json(503, {"error": "Game account check is unavailable on this server."})
                    return
                if parsed.query or not self._same_origin_request():
                    self._json(403, {"error": "Same-origin account check required."})
                    return
                try:
                    if mlbb_lookup.local_guest:
                        host = self.headers.get("Host", "")
                        if (self.server.server_address[0] != "127.0.0.1"
                                or self.client_address[0] != "127.0.0.1"
                                or host != f"127.0.0.1:{self.server.server_port}"):
                            raise CustomerAuthError("Local account check required.")
                        actor = "local-mlbb-preview"
                        source_identity = None
                    else:
                        actor = None
                        source_identity = self._edge_client_ip()
                    self._json(200, mlbb_lookup.check_product(
                        lookup_product, actor, self._read_json_body(), now=datetime.now(UTC),
                        source_identity=source_identity))
                except CustomerAuthError:
                    self._json(403, {"error": "This preview account check is available only on this device."})
                except RecipientLookupThrottledError:
                    self._json(429, {"error": "Too many account checks were made. Please wait up to a minute, then retry."})
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                except SupplierCommandError as exc:
                    self._lookup_failure(exc)
                except (OrderRepositoryError, OSError):
                    self._json(503, {"error": "Game account check is temporarily unavailable. Try again shortly."})
                return
            if parsed.path.startswith("/api/v1/admin/payment-review/"):
                self._operator_payment_review(parsed)
                return
            if _CUSTOMER_RECEIPT_PATH.fullmatch(parsed.path):
                self._payment_evidence(parsed, upload=True)
                return
            wallet_payment_match = _CUSTOMER_WALLET_PAYMENT_PATH.fullmatch(parsed.path)
            if wallet_payment_match:
                if checkout_journey is None or customer_auth is None or customer_order_service is None:
                    self._json(503, {"error": "wallet checkout is not configured"})
                    return
                if parsed.query or (not self.headers.get("Authorization") and not self._same_origin_request()):
                    self._json(403, {"error": "same-origin wallet payment required"})
                    return
                try:
                    principal = customer_auth.verify(
                        self.headers.get("Authorization"), cookie_header=self.headers.get("Cookie")
                    )
                    payload = self._read_json_body()
                    if not isinstance(payload, dict) or set(payload) != {"confirmed"} or payload["confirmed"] is not True:
                        self._json(400, {"error": "explicit wallet payment confirmation is required"})
                        return
                    self._json(200, checkout_journey.pay_with_wallet(
                        principal, wallet_payment_match.group(1), now=datetime.now(UTC)
                    ))
                except CustomerAuthError:
                    self._json(401, {"error": "customer sign-in required"})
                except InsufficientFundsError:
                    try:
                        order = checkout_journey.checkout.repository.get(
                            wallet_payment_match.group(1), customer_id=principal.customer_id
                        )
                        wallet = customer_wallet_service.balance(principal) if customer_wallet_service else {}
                        available = wallet.get("available_minor", 0)
                        required = order.snapshot.amount_minor
                        self._json(409, {"error": "wallet balance is below the order total",
                            "required_minor": required, "available_minor": available,
                            "shortfall_minor": max(0, required - available), "currency": order.snapshot.currency})
                    except Exception:
                        self._json(409, {"error": "wallet balance is below the order total"})
                except (LedgerConflictError, PaymentBoundaryError):
                    self._json(409, {"error": "wallet payment conflicts with the current order state"})
                except CheckoutInputError as exc:
                    self._json(400, {"error": str(exc)})
                except (LedgerOperationError, OSError):
                    self._json(503, {"error": "wallet payment is temporarily unavailable"})
                except Exception:
                    self._json(503, {"error": "wallet payment could not be completed"})
                return
            if parsed.path in {"/api/v1/checkout/review", "/api/v1/checkout/confirm", "/api/v1/checkout/payment"}:
                if checkout_journey is None or customer_auth is None:
                    self._json(503, {"error": "recipient checkout is not configured"})
                    return
                if parsed.query or (not self.headers.get("Authorization") and not self._same_origin_request()):
                    self._json(403, {"error": "same-origin checkout request required"})
                    return
                try:
                    principal = customer_auth.verify(self.headers.get("Authorization"), cookie_header=self.headers.get("Cookie"))
                    action = {"review": checkout_journey.review, "confirm": checkout_journey.confirm,
                              "payment": checkout_journey.payment}[parsed.path.rsplit("/", 1)[-1]]
                    self._json(200, action(principal, self._read_json_body(), now=datetime.now(UTC)))
                except CustomerAuthError:
                    self._json(401, {"error": "customer sign-in required"})
                except CheckoutInputError as exc:
                    self._json(400, {"error": str(exc)})
                except SupplierCommandError as exc:
                    self._lookup_failure(exc)
                except Exception:
                    self._json(503, {"error": "checkout review is unavailable"})
                return
            if parsed.path.startswith("/api/v1/auth/"):
                self._handle_customer_auth(parsed)
                return
            if parsed.path == "/api/v1/strategy-guides/mlbb/draft-analysis":
                if parsed.query:
                    self._json(400, {"error": "draft analysis does not accept query parameters"})
                    return
                if not self._same_origin_request():
                    self._json(403, {"error": "same-origin analysis request required"})
                    return
                try:
                    guide = strategy_guide("mlbb", mlbb_snapshot_path=_mlbb_snapshot_path())
                    self._json(200, analyze_draft(self._read_json_body(), guide))
                except (StrategyGuideInputError, ValueError):
                    self._json(400, {"error": "invalid draft or unavailable roster"})
                except Exception:
                    self._json(503, {"error": "draft analysis is unavailable"})
                return
            if parsed.path == "/api/v1/orders":
                if customer_checkout_service is None or customer_auth is None:
                    self._json(501, {"error": "customer checkout is not configured"})
                    return
                if not self.headers.get("Authorization") and not self._same_origin_request():
                    self._json(403, {"error": "same-origin checkout request required"})
                    return
                if parsed.query:
                    self._json(400, {"error": "checkout does not accept query parameters"})
                    return
                try:
                    principal = customer_auth.verify(
                        self.headers.get("Authorization"), cookie_header=self.headers.get("Cookie")
                    )
                    idempotency_key = self.headers.get("Idempotency-Key", "")
                    result = customer_checkout_service.prepare(
                        principal,
                        self._read_json_body(),
                        idempotency_key=idempotency_key,
                        now=datetime.now(UTC),
                    )
                    self._json(202, result.as_dict())
                except CustomerAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except CheckoutInputError as exc:
                    self._json(400, {"error": str(exc)})
                except Exception:
                    self._json(500, {"error": "customer checkout preparation failed"})
                return
            if parsed.path == "/api/v1/webhooks/fazercards":
                if order_reconciler is None:
                    self._json(503, {"error": "supplier webhook is not configured"})
                    return
                try:
                    body = self._read_raw_body()
                    event = order_reconciler.receive(
                        body,
                        self.headers.get("X-Webhook-Signature", ""),
                        now=datetime.now(UTC),
                    )
                    self._json(200, {"accepted": True, "event_id": event.event_id})
                except WebhookAuthenticationError as exc:
                    self._json(401, {"error": str(exc)})
                except (SupplierCommandError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
                except Exception as exc:
                    # The supplier retries non-2xx responses; do not leak
                    # database/provider details into a signed callback.
                    self._json(500, {"error": "supplier webhook processing failed"})
                return
            if not parsed.path.startswith("/api/v1/admin/"):
                self._json(501, {"error": "order and payment writes are not available"})
                return
            if admin_service is None:
                self._json(503, {"error": "admin operations are not configured"})
                return
            try:
                admin_service.authenticate(self.headers.get("Authorization"))
                payload = self._read_json_body()
                verify_match = _ADMIN_VERIFY_PATH.fullmatch(parsed.path)
                if verify_match:
                    admin_service.authenticate_treasury(self.headers.get("X-Aurix-Treasury-Token"))
                    self._json(
                        200,
                        admin_service.verify_binance(verify_match.group(1), payload),
                    )
                    return
                funding_resolution_match = _ADMIN_FUNDING_RESOLUTION_PATH.fullmatch(parsed.path)
                funding_link_match = _ADMIN_FUNDING_LINK_PATH.fullmatch(parsed.path)
                if funding_link_match:
                    if parsed.query:
                        self._json(400, {"error": "funding resolution does not accept query parameters"})
                        return
                    self._json(200, admin_service.recover_funding_invoice(funding_link_match.group(1), payload,
                        treasury_token=self.headers.get("X-Aurix-Treasury-Token")))
                    return
                if funding_resolution_match:
                    if parsed.query:
                        self._json(400, {"error": "funding resolution does not accept query parameters"})
                        return
                    self._json(200, admin_service.resolve_funding_attempt_absent(
                        funding_resolution_match.group(1), payload,
                        treasury_token=self.headers.get("X-Aurix-Treasury-Token")))
                    return
                reconcile_match = _ADMIN_GAME_ORDER_RECONCILE_PATH.fullmatch(parsed.path)
                if reconcile_match:
                    if parsed.query:
                        self._json(400, {"error": "supplier reconciliation does not accept query parameters"})
                        return
                    if payload:
                        self._json(400, {"error": "supplier reconciliation does not accept a request body"})
                        return
                    if customer_order_service is None:
                        self._json(503, {"error": "game order storage is not configured"})
                        return
                    supplier_client = getattr(admin_service, "supplier_client", None)
                    if supplier_client is None:
                        self._json(503, {"error": "supplier read access is not configured"})
                        return
                    repository = customer_order_service.repository
                    order_id = reconcile_match.group(1)
                    try:
                        result = reconcile_order(
                            repository,
                            supplier_client,
                            order_id,
                            now=datetime.now(UTC),
                        )
                    except OrderNotFoundError:
                        self._json(404, {"error": "game order not found"})
                        return
                    except WebhookReconciliationError:
                        self._json(409, {"error": "supplier order is not safe to reconcile in its current state"})
                        return
                    except SupplierCommandError:
                        self._json(502, {"error": "supplier status could not be checked; local order state was retained"})
                        return
                    self._json(200, {"supplier_writes": False, "outcome": result.as_dict()})
                    return
                if parsed.path == "/api/v1/admin/supplier-wallet/invoice":
                    self._json(201, admin_service.create_wallet_invoice(payload,
                        treasury_token=self.headers.get("X-Aurix-Treasury-Token")))
                    return
                if parsed.path == "/api/v1/admin/mapping-approval":
                    if parsed.query:
                        self._json(400, {"error": "mapping approval does not accept query parameters"})
                        return
                    self._json(201, admin_service.approve_catalog_mapping(payload))
                    return
                self._json(404, {"error": "admin route not found"})
            except GameAdminAuthError as exc:
                self._json(401, {"error": str(exc)})
            except SupplierWriteDisabledError:
                self._json(503, {"error": "supplier wallet writes are disabled"})
            except OrderNotFoundError:
                self._json(404, {"error": "game order not found"})
            except (GameAdminInputError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                # Do not leak provider bodies, tokens, or filesystem details.
                self._json(502, {"error": "supplier admin operation failed"})

        def _handle_customer_auth(self, parsed: Any) -> None:
            if parsed.query:
                self._json(400, {"error": "authentication route does not accept query parameters"})
                return
            if not self._same_origin_request():
                self._json(403, {"error": "same-origin authentication request required"})
                return
            if parsed.path == "/api/v1/auth/logout":
                try:
                    if customer_identity_service is not None:
                        customer_identity_service.logout(
                            self.headers.get("Authorization"), cookie_header=self.headers.get("Cookie")
                        )
                except Exception:
                    self._json(503, {"error": "sign-out could not be completed; try again"})
                    return
                self._json(200, {"authenticated": False}, set_cookie=self._customer_cookie(None))
                return
            if parsed.path not in {
                "/api/v1/auth/google/challenge", "/api/v1/auth/telegram", "/api/v1/auth/google",
                "/api/v1/auth/link/telegram", "/api/v1/auth/link/google",
            }:
                self._json(404, {"error": "authentication route not found"})
                return
            if customer_identity_service is None:
                self._json(503, {"error": "customer sign-in is not configured"})
                return
            try:
                if parsed.path == "/api/v1/auth/google/challenge":
                    self._read_json_body()
                    self._json(200, {"nonce": customer_identity_service.issue_google_challenge()})
                    return
                if parsed.path == "/api/v1/auth/telegram":
                    token, principal = customer_identity_service.login_telegram(self._read_json_body())
                elif parsed.path == "/api/v1/auth/google":
                    payload = self._read_json_body()
                    token, principal = customer_identity_service.login_google(
                        payload.get("credential"), payload.get("nonce")
                    )
                else:
                    if customer_auth is None:
                        self._json(503, {"error": "customer identity is not configured"})
                        return
                    current = customer_auth.verify(
                        self.headers.get("Authorization"), cookie_header=self.headers.get("Cookie")
                    )
                    payload = self._read_json_body()
                    if payload.get("customer_id") != current.customer_id:
                        self._json(409, {"error": "Your store account changed. Open Account and start linking again."})
                        return
                    proof = payload.get("proof")
                    if not isinstance(proof, dict):
                        raise ValueError("Sign-in proof is required")
                    if parsed.path.endswith("/telegram"):
                        _, principal = customer_identity_service.login_telegram(
                            proof, link_customer_id=current.customer_id
                        )
                    else:
                        _, principal = customer_identity_service.login_google(
                            proof.get("credential"), proof.get("nonce"),
                            link_customer_id=current.customer_id,
                        )
                    token = None
                self._json(
                    200,
                    {"authenticated": True, "user": {
                        "customer_id": principal.customer_id,
                        "providers": customer_identity_service.identity_store.providers_for(principal.customer_id),
                    }},
                    set_cookie=self._customer_cookie(token) if token else None,
                )
            except CustomerIdentityConflict as exc:
                self._json(409, {"error": str(exc)})
            except CustomerAuthError as exc:
                self._json(401, {"error": str(exc)})
            except (CustomerApiError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception:
                self._json(502, {"error": "customer sign-in could not be completed"})

        def _same_origin_request(self) -> bool:
            origin = self.headers.get("Origin", "")
            host = self.headers.get("Host", "").strip().lower()
            try:
                parsed = urlsplit(origin)
            except ValueError:
                return False
            return parsed.scheme in {"http", "https"} and bool(host) and parsed.netloc.lower() == host

        def _customer_cookie(self, token: str | None) -> str:
            parts = [f"{SESSION_COOKIE_NAME}={token or ''}", "Path=/", "HttpOnly", "SameSite=Lax"]
            if customer_identity_service is not None and customer_identity_service.cookie_secure:
                parts.append("Secure")
            if token:
                parts.append(f"Max-Age={customer_identity_service.session_seconds}")
            else:
                parts.append("Max-Age=0")
            return "; ".join(parts)

        def _handle_put(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path.startswith(_STAFF_ROOT + "/"):
                self._store_staff(parsed)
                return
            if parsed.path == "/api/v1/leaderboard/profile":
                if parsed.query or not self._same_origin_request():
                    self._json(403, {"error": "same-origin profile update required"})
                    return
                if customer_auth is None or store_features is None:
                    self._json(503, {"error": "leaderboard settings are unavailable"})
                    return
                try:
                    principal = customer_auth.verify(
                        self.headers.get("Authorization"), cookie_header=self.headers.get("Cookie")
                    )
                    self._json(200, store_features.save_leaderboard_profile(
                        principal.customer_id, self._read_json_body()))
                except CustomerAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except (ValueError, TypeError) as exc:
                    self._json(400, {"error": str(exc)})
                except Exception:
                    self._json(503, {"error": "leaderboard settings could not be saved"})
                return
            notice_match = re.fullmatch(r"/api/v1/admin/store-notices/([A-Za-z0-9_-]{12,40})", parsed.path)
            if notice_match:
                if parsed.query:
                    self._json(400, {"error": "notice update does not accept query parameters"})
                    return
                if admin_service is None or store_features is None:
                    self._json(503, {"error": "store notice administration is unavailable"})
                    return
                try:
                    admin_service.authenticate(self.headers.get("Authorization"))
                    self._json(200, {"notice": store_features.save_notice(
                        self._read_json_body(), notice_id=notice_match.group(1))})
                except GameAdminAuthError as exc:
                    self._json(401, {"error": str(exc)})
                except (ValueError, TypeError) as exc:
                    self._json(400, {"error": str(exc)})
                except Exception:
                    self._json(503, {"error": "notice could not be updated"})
                return
            if parsed.path not in {"/api/v1/admin/pricing-policy", "/api/v1/admin/supplier-webhook", '/api/v1/admin/catalog-policy'}:
                self._json(501, {"error": "unsupported write route"})
                return
            if parsed.path == "/api/v1/admin/supplier-webhook" and parsed.query:
                self._json(400, {"error": "supplier webhook configuration does not accept query parameters"})
                return
            if admin_service is None:
                self._json(503, {"error": "admin operations are not configured"})
                return
            try:
                admin_service.authenticate(self.headers.get("Authorization"))
                if parsed.query:
                    raise ValueError('admin configuration does not accept query parameters')
                payload = self._read_json_body(maximum=128 * 1024) if parsed.path == '/api/v1/admin/catalog-policy' else self._read_json_body()
                if parsed.path == '/api/v1/admin/catalog-policy':
                    self._json(200, admin_service.update_catalog_policy(payload))
                    return
                if parsed.path == "/api/v1/admin/supplier-webhook":
                    self._json(200, admin_service.configure_webhook(payload))
                    return
                actor = admin_service.authenticate_treasury(self.headers.get("X-Aurix-Treasury-Token"))
                self._json(200, admin_service.update_pricing_policy(payload, actor=actor))
            except GameAdminAuthError as exc:
                self._json(401, {"error": str(exc)})
            except CatalogPolicyConflict as exc:
                self._json(409, {'error': str(exc)})
            except SupplierWriteDisabledError:
                self._json(503, {"error": "supplier webhook writes are disabled"})
            except (GameAdminInputError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                self._json(502, {"error": "supplier admin operation could not be saved"})

        def _handle_delete(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path.startswith(_STAFF_ROOT + "/"):
                self._store_staff(parsed)
                return
            if parsed.path != "/api/v1/admin/supplier-webhook":
                self._json(501, {"error": "unsupported delete route"})
                return
            if parsed.query:
                self._json(400, {"error": "supplier webhook delete does not accept query parameters"})
                return
            if admin_service is None:
                self._json(503, {"error": "admin operations are not configured"})
                return
            try:
                admin_service.authenticate(self.headers.get("Authorization"))
                self._json(200, admin_service.delete_webhook())
            except GameAdminAuthError as exc:
                self._json(401, {"error": str(exc)})
            except SupplierWriteDisabledError:
                self._json(503, {"error": "supplier webhook writes are disabled"})
            except (GameAdminInputError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                self._json(502, {"error": "supplier webhook could not be deleted"})

        def _store_staff(self, parsed) -> None:
            route = parsed.path.removeprefix(_STAFF_ROOT)
            if parsed.query and not route.startswith("/admin/"):
                self._json(400, {"error": "Query parameters are not supported."})
                return
            if self.command in {"POST", "PUT", "DELETE"} and not self._same_origin_request():
                self._json(403, {"error": "Same-origin staff request required."})
                return
            try:
                if customer_identity_service is None:
                    raise CustomerAuthError("Store sign-in is unavailable.")
                principal = customer_identity_service.verifier.verify(cookie_header=self.headers.get("Cookie"))
                identities = customer_identity_service.identity_store
                role = identities.staff_role(principal.customer_id, staff_owner)
                if role is None:
                    self._json(403, {"error": "This store account has no admin access."})
                    return
                if route.startswith("/admin/"):
                    if role != "super_admin":
                        self._json(403, {"error": "Only the super admin can use the operator console."})
                        return
                    if admin_service is None or not getattr(admin_service, "_admin_token", None):
                        self._json(503, {"error": "Operator services are unavailable."})
                        return
                    # Reuse the established operator routes only after the signed Store
                    # session has passed the super-admin check; never expose this token.
                    old_path = self.path
                    original_authorizations = self.headers.get_all("Authorization", [])
                    if "Authorization" in self.headers:
                        del self.headers["Authorization"]
                    self.headers.add_header("Authorization", f"Bearer {admin_service._admin_token}")
                    self.path = "/api/v1" + route + (f"?{parsed.query}" if parsed.query else "")
                    try:
                        dispatch = {
                            "GET": self._handle_get,
                            "HEAD": self._handle_get,
                            "POST": self._handle_post,
                            "PUT": self._handle_put,
                            "DELETE": self._handle_delete,
                        }.get(self.command)
                        if dispatch is None:
                            self._json(405, {"error": "Method not allowed."})
                        else:
                            dispatch()
                    finally:
                        if "Authorization" in self.headers:
                            del self.headers["Authorization"]
                        for value in original_authorizations:
                            self.headers.add_header("Authorization", value)
                        self.path = old_path
                    return
                if route == "/commerce-settings":
                    if role != "super_admin":
                        self._json(403, {"error": "Only the super admin can change commerce settings."})
                    elif self.command in {"GET", "HEAD"}:
                        self._json(200, env_commerce_settings().status())
                    elif self.command == "POST":
                        body = self._read_json_body()
                        if set(body) != {"settings", "expected_revision"}:
                            raise ValueError("Invalid commerce settings request.")
                        self._json(200, env_commerce_settings().save(
                            body["settings"], expected_revision=body["expected_revision"],
                            actor=f"account:{principal.customer_id}"))
                    else:
                        self._json(405, {"error": "Method not allowed."})
                    return
                if route == "/session" and self.command in {"GET", "HEAD"}:
                    self._json(200, {
                        "role": role,
                        "review_available": payment_review is not None,
                        "accepting_payments": bool(checkout_journey and checkout_journey.payment_receivers),
                    })
                    return
                if route == "/admins":
                    if role != "super_admin":
                        self._json(403, {"error": "Only the super admin can manage admin access."})
                    elif self.command in {"GET", "HEAD"}:
                        self._json(200, {"admins": identities.staff_admins()})
                    else:
                        body = self._read_json_body()
                        if set(body) != {"customer_id", "grant"} or type(body["grant"]) is not bool:
                            raise CustomerApiError("Choose grant or revoke and enter the store account ID.")
                        changed = identities.set_staff_admin(staff_owner, body["customer_id"],
                                                            grant=body["grant"], now=datetime.now(UTC))
                        self._json(200, {"changed": changed})
                    return
                if route.startswith("/payment-review/"):
                    credential = StaffPaymentReviewer(f"account:{principal.customer_id}")
                    self._operator_payment_review(parsed._replace(path="/api/v1/admin" + route), token=credential)
                    return
                self._json(404, {"error": "Staff route not found."})
            except CustomerAuthError:
                self._json(401, {"error": "Sign in to the Game Store first."})
            except SettingsConflict as exc:
                self._json(409, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                self._json(400, {"error": str(exc)})

        def _operator_payment_review(self, parsed, *, token=None) -> None:
            if payment_review is None:
                self._json(503, {"error": "payment review is not configured"})
                return
            authorization = self.headers.get("Authorization", "")
            if token is None:
                token = authorization[7:] if authorization.startswith("Bearer ") else ""
            try:
                payment_review.authenticate(token)
                if parsed.query:
                    raise ValueError("query parameters are not supported")
                if parsed.path == "/api/v1/admin/payment-review/recent" and self.command in {"GET", "HEAD"}:
                    self._json(200, {"orders": payment_review.recent_orders(token=token)})
                    return
                history = re.fullmatch(
                    r"/api/v1/admin/payment-review/orders/([A-Za-z0-9._:-]{1,128})/history", parsed.path
                )
                if history and self.command in {"GET", "HEAD"}:
                    self._json(200, payment_review.order_history(history[1], token=token))
                    return
                bank_match = re.fullmatch(r'/api/v1/admin/payment-review/orders/([A-Za-z0-9._:-]{1,128})/bank-refund(?:/(authorize|complete))?', parsed.path)
                if bank_match:
                    from .bank_refund import SettledBankRefundService
                    service = SettledBankRefundService(payment_review)
                    order_id, action = bank_match.groups()
                    if action is None and self.command in {'GET','HEAD'}:
                        self._json(200, service.status(order_id, token=token))
                    elif action and self.command == 'POST':
                        payload = self._read_json_body()
                        self._json(200, getattr(service, action)(order_id, token=token, payload=payload, now=datetime.now(UTC)))
                    else:
                        self._json(405, {'error':'use GET for status or POST for refund decisions'})
                    return
                if parsed.path == "/api/v1/admin/payment-review/queue":
                    if self.command not in {"GET", "HEAD"}:
                        self._json(405, {"error": "use GET for the review queue"})
                    else:
                        self._json(200, payment_review.queue(token))
                    return
                match = _REVIEW_ACTION_PATH.fullmatch(parsed.path)
                if not match:
                    self._json(404, {"error": "review route not found"})
                    return
                order_id, revision, action = match.groups()
                revision = int(revision)
                if action == "receipt":
                    if self.command not in {"GET", "HEAD"}:
                        self._json(405, {"error": "use GET to download evidence"})
                        return
                    content = payment_review.receipt(order_id, token=token, revision=revision)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", f'attachment; filename="receipt-r{revision}.bin"')
                    self.send_header("Cache-Control", "private, no-store")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(content)
                    return
                if self.command != "POST":
                    self._json(405, {"error": "use POST for reviewer decisions"})
                    return
                payload = self._read_json_body()
                if action == "resume":
                    if payload:
                        raise ValueError("resume does not accept new instructions")
                    result = payment_review.resume(order_id, token=token, revision=revision, now=datetime.now(UTC))
                elif action == "refund-complete":
                    if set(payload) != {"refund_transaction_id", "refunded_at", "reason"} or not isinstance(payload.get("refunded_at"), str):
                        raise ValueError("invalid refund completion fields")
                    result = payment_review.complete_external_refund(order_id, token=token,
                        revision=revision, refund_transaction_id=payload["refund_transaction_id"],
                        refunded_at=datetime.fromisoformat(payload["refunded_at"]), reason=payload["reason"],
                        now=datetime.now(UTC))
                else:
                    if set(payload) not in ({"approve", "reason"}, {"approve", "reason", "verified_transfer"},
                                            {"approve", "reason", "external_refund"}):
                        raise ValueError("invalid decision fields")
                    if "verified_transfer" in payload:
                        transfer = payload["verified_transfer"]
                        if not isinstance(transfer, dict) or not isinstance(transfer.get("occurred_at"), str):
                            raise ValueError("verified transfer timestamp required")
                        transfer["occurred_at"] = datetime.fromisoformat(transfer["occurred_at"])
                    if "external_refund" in payload:
                        transfer = payload["external_refund"]
                        if not isinstance(transfer, dict) or not isinstance(transfer.get("occurred_at"), str):
                            raise ValueError("verified incoming transfer timestamp required")
                        transfer["occurred_at"] = datetime.fromisoformat(transfer["occurred_at"])
                    result = payment_review.decide(order_id, token=token, revision=revision,
                        now=datetime.now(UTC), **payload)
                self._json(200, result)
            except PaymentReviewerAuthError:
                if isinstance(token, StaffPaymentReviewer):
                    self._json(403, {"error": "Only the reviewer who saved this decision can resume it."})
                else:
                    self._json(401, {"error": "named reviewer sign-in required"})
            except OrderNotFoundError:
                self._json(404, {"error": "order not found"})
            except (PaymentReviewError, PaymentBoundaryError, LedgerConflictError, LedgerInputError, LedgerOperationError):
                self._json(409, {"error": "review cannot advance; refresh the case and inspect merchant records before retrying"})
            except (ValueError, TypeError):
                self._json(400, {"error": "invalid review request"})
            except Exception:
                self._json(503, {"error": "review result is uncertain; refresh and resume the saved decision"})

        def _payment_evidence(self, parsed, *, upload: bool) -> None:
            if payment_review is None or customer_auth is None:
                self._json(503, {"error": "payment evidence is not configured"})
                return
            if upload and payment_review.evidence_cipher is None:
                self._json(503, {"error": "receipt storage is not configured"})
                return
            if parsed.query or (upload and not self.headers.get("Authorization") and not self._same_origin_request()):
                self._json(403, {"error": "same-origin evidence request required"})
                return
            try:
                principal = customer_auth.verify(self.headers.get("Authorization"), cookie_header=self.headers.get("Cookie"))
                order_id = _CUSTOMER_RECEIPT_PATH.fullmatch(parsed.path).group(1)
                # Check ownership before reading or decoding the upload.
                payment_review.repository.get(order_id, customer_id=principal.customer_id)
                if upload:
                    if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                        raise ValueError("JSON upload required")
                    payload = json.loads(self._read_raw_body(maximum=MAX_RECEIPT_BODY_BYTES))
                    if not isinstance(payload, dict) or set(payload) not in (
                        {"transaction_reference", "receipt_base64"}, {"transaction_reference", "receipt_base64", "expected_revision"}):
                        raise ValueError("invalid evidence fields")
                    encoded = payload["receipt_base64"]
                    if not isinstance(encoded, str):
                        raise ValueError("invalid receipt encoding")
                    receipt = base64.b64decode(encoded, validate=True)
                    payment_review.submit_receipt(order_id, customer_id=principal.customer_id,
                        transaction_reference=payload["transaction_reference"], receipt=receipt, now=datetime.now(UTC),
                        expected_revision=payload.get("expected_revision"))
                self._json(200, payment_review.customer_status(order_id, customer_id=principal.customer_id))
            except CustomerAuthError:
                self._json(401, {"error": "customer sign-in required"})
            except OrderNotFoundError:
                self._json(404, {"error": "order not found"})
            except (ValueError, binascii.Error):
                self._json(400, {"error": "invalid or conflicting payment evidence; contact support if already submitted"})
            except Exception:
                self._json(503, {"error": "payment evidence is unavailable"})

        def _read_json_body(self, *, maximum: int = 16 * 1024) -> dict[str, Any]:
            raw = self._read_raw_body(maximum=maximum)
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError("request body must be valid JSON") from None
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
            return payload

        def _read_raw_body(self, *, maximum: int = 16 * 1024) -> bytes:
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length or "0")
            except ValueError:
                raise ValueError("invalid request body length") from None
            if length <= 0 or length > maximum:
                raise ValueError("request body is too large or empty")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("request body was truncated")
            return raw

    return Handler


def _one(parameters: dict[str, list[str]], key: str) -> str | None:
    values = parameters.get(key)
    if values is None:
        return None
    if len(values) != 1:
        raise CatalogInputError(f"{key} must be supplied once")
    return values[0]


def create_server(
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    public_catalog_path: str | Path | None = None,
    admin_service: GameStoreAdminService | None = None,
    order_reconciler: SupplierWebhookReconciler | None = None,
    customer_order_service: CustomerOrderService | None = None,
    customer_wallet_service: CustomerWalletService | None = None,
    customer_auth: SignedCustomerTokenVerifier | None = None,
    customer_identity_service: CustomerIdentityService | None = None,
    customer_checkout_service: CustomerCheckoutService | None = None,
    checkout_journey: CheckoutJourney | None = None,
    mlbb_lookup: GameAccountLookup | None = None,
    topup_preview: dict[str, Any] | None = None,
    payment_review: PaymentReviewService | None = None,
    store_features: StoreFeatureStore | None = None,
    redirect_live_store: bool = False,
) -> ThreadingHTTPServer:
    """Create a development server; callers must opt in to non-loopback bind."""

    if redirect_live_store and host != "127.0.0.1":
        raise ValueError("Live store navigation is available only on loopback.")
    return ThreadingHTTPServer(
        (host, int(port)),
        make_handler(
            public_catalog_path=public_catalog_path,
            admin_service=admin_service,
            order_reconciler=order_reconciler,
            customer_order_service=customer_order_service,
            customer_wallet_service=customer_wallet_service,
            customer_auth=customer_auth,
            customer_identity_service=customer_identity_service,
            customer_checkout_service=customer_checkout_service,
            checkout_journey=checkout_journey,
            mlbb_lookup=mlbb_lookup,
            topup_preview=topup_preview,
            payment_review=payment_review,
            store_features=store_features,
            redirect_live_store=redirect_live_store,
        ),
    )
