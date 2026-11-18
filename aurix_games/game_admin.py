"""Protected admin boundary for the trial-ready Game Store operations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import os
import re
import stat
from pathlib import Path
import threading
from typing import Any, Callable

from .fazercards_account import (
    SupplierAccountInputError,
    SupplierWalletPolicy,
    WalletFundingService,
    WalletFundingIntentStore,
    parse_supplier_wallet_snapshot,
    parse_supplier_payment_methods,
    safe_payment_url,
)
from .providers.fazercards_commands import FazerCardsCommandClient
from .pricing import PricingPolicy
from .pricing_store import PricingPolicyStore
from .catalog_policy import CatalogPolicyStore, env_catalog_policy_store
from .wallet_monitor import SupplierWalletAlertStore
from .order_reconciliation import SupplierWebhookReconciler
from .order_repository import GameOrderRepository
from .fulfillment_audit import audit_money_integrity
from .mapping_review import mapping_history, review_mapping
from .catalog_inventory import load_catalog_inventory
from .catalog import MAX_PAGE_SIZE, list_products
from .catalog_matching import candidate_match_method
from .supplier_routes import SupplierRouteError, validation_settings
from scripts.approve_fazercards_mapping import (
    MappingApprovalError,
    _read_private as _read_private_mapping,
    _write_private as _write_private_mapping,
    approve_mapping as _approve_mapping_payload,
)


UTC = timezone.utc


def _catalog_refresh_diagnostics(directory: Path, config_path: Path) -> dict[str, Any]:
    """Read the latest private shortlist evidence and expose only mapped status labels."""
    if directory.is_symlink() or not directory.is_dir() or stat.S_IMODE(directory.stat().st_mode) & 0o077:
        return {"status": "unavailable", "checked_at": None, "offers": [], "writes": False}
    latest = max((path for path in directory.iterdir()
                  if path.is_file() and re.fullmatch(r"[0-9]{8}T[0-9]{6}-[a-f0-9]{32}\.json", path.name)),
                 key=lambda path: path.name, default=None)
    if latest is None:
        return {"status": "unavailable", "checked_at": None, "offers": [], "writes": False}
    if latest.is_symlink() or stat.S_IMODE(latest.stat().st_mode) & 0o077 or latest.stat().st_size > 256_000:
        return {"status": "invalid", "checked_at": None, "offers": [], "writes": False}
    try:
        evidence = json.loads(latest.read_text(encoding="utf-8"))
        config = PricingPolicyStore(config_path)._read_config()
        captured = datetime.fromisoformat(evidence["completed_at"].replace("Z", "+00:00"))
        observations = evidence["observations"]
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return {"status": "invalid", "checked_at": None, "offers": [], "writes": False}
    if (evidence.get("scope") != "topup_shortlist" or captured.tzinfo is None or captured.utcoffset() is None
            or not isinstance(observations, list) or len(observations) > 100
            or not isinstance(config.get("mappings"), list)):
        return {"status": "invalid", "checked_at": None, "offers": [], "writes": False}
    mappings = {}
    for row in config["mappings"]:
        if not isinstance(row, dict) or row.get("review_state") != "approved" or row.get("customer_visible") is not True:
            continue
        key = (row.get("family"), row.get("provider_category_id"), row.get("provider_offer_id"))
        if all(isinstance(value, str) for value in key):
            mappings[key] = row
    labels = {
        "eligible": "Eligible at last check",
        "eligible_unknown_stock": "Price eligible · quantity unreported",
        "above_cost_ceiling": "Above approved supplier-cost ceiling",
        "missing_offer": "Supplier did not return this offer",
        "invalid_price": "Supplier price invalid",
        "unknown_stock": "Quantity unreported · not eligible",
        "invalid_stock": "Supplier quantity invalid",
        "out_of_stock": "Out of stock",
    }
    rows = []
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        key = ("topups", observation.get("category_id"), observation.get("offer_id"))
        mapping = mappings.get(key)
        status = observation.get("status")
        observed_at = observation.get("observed_at")
        if (mapping is None or status not in labels or not isinstance(observed_at, str) or len(observed_at) > 80
                or not isinstance(mapping.get("offer_id"), str) or len(mapping["offer_id"]) > 160
                or not isinstance(mapping.get("product_slug"), str)
                or not re.fullmatch(r"[a-z0-9-]{1,80}", mapping["product_slug"])):
            continue
        product_name = mapping.get("product_name", mapping["product_slug"])
        if not isinstance(product_name, str) or len(product_name) > 160:
            product_name = mapping["product_slug"]
        stock = observation.get("stock")
        rows.append({"offer_id": mapping["offer_id"], "product_slug": mapping["product_slug"],
                     "product_name": product_name,
                     "status": status, "label": labels[status], "observed_at": observed_at,
                     "stock_status": "unreported" if stock is None else "available" if type(stock) is int and stock > 0
                     else "out_of_stock" if type(stock) is int and stock == 0 else "invalid"})
    captured = captured.astimezone(UTC)
    age = (datetime.now(UTC) - captured).total_seconds()
    return {"status": "fresh" if 0 <= age <= timedelta(minutes=15).total_seconds() else "stale",
            "checked_at": captured.isoformat(), "age_seconds": max(0, int(age)), "offers": rows[:50], "writes": False}


def _reference_products() -> list[dict[str, Any]]:
    """Load every public reference title using the catalog's bounded pages."""

    products: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = list_products(limit=MAX_PAGE_SIZE, offset=offset)
        products.extend(page)
        if len(page) < MAX_PAGE_SIZE:
            return products
        offset += len(page)


def _reference_title_candidates(
    category_name: Any, products: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build review-only product-name hints from the supplier category label.

    A title match is not a SKU, region, rights, recipient-schema, or
    fulfillment approval. Keep the complete candidate set and never choose a
    product when more than one reference title matches.
    """

    candidates = []
    for product in products:
        name = product.get("name")
        slug = product.get("slug")
        if not isinstance(name, str) or not isinstance(slug, str):
            continue
        method = candidate_match_method(name, category_name)
        if method is not None:
            candidates.append({
                "product_slug": slug,
                "product_name": name,
                "match_method": method,
            })

    if len(candidates) == 1:
        status = "single_title_candidate"
    elif candidates:
        status = "ambiguous_title_candidates"
    else:
        status = "no_title_candidate"
    return {
        "status": status,
        "candidates": candidates,
        "review_required": True,
        "approval_effect": "none",
    }


class GameAdminAuthError(PermissionError):
    """The request did not carry the configured admin bearer token."""


class GameAdminInputError(ValueError):
    """An admin operation payload is malformed."""


class GameStoreAdminService:
    """Server-side service; supplier credentials never cross this boundary."""

    def __init__(
        self,
        *,
        admin_token: str,
        supplier_client: FazerCardsCommandClient | None,
        wallet_alert_store: SupplierWalletAlertStore,
        wallet_policy: SupplierWalletPolicy | None = None,
        pricing_policy_store: PricingPolicyStore | None = None,
        funding_intent_store: WalletFundingIntentStore | None = None,
        mapping_review_provider: Callable[[], dict[str, Any]] | None = None,
        mapping_history_provider: Callable[[], dict[str, Any]] | None = None,
        catalog_inventory_provider: Callable[[], dict[str, Any]] | None = None,
        catalog_refresh_provider: Callable[[], dict[str, Any]] | None = None,
        mapping_approval_provider: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        treasury_reviewers: dict[str, str] | None = None,
        integrity_paths: tuple[Path, Path, Path] | None = None,
        catalog_policy_store: CatalogPolicyStore | None = None,
    ) -> None:
        if not isinstance(admin_token, str) or not admin_token or "\r" in admin_token or "\n" in admin_token:
            raise ValueError("admin_token must be a non-empty single-line secret")
        self._admin_token = admin_token
        self.catalog_policy_store = catalog_policy_store
        self.integrity_paths = integrity_paths
        self.supplier_client = supplier_client
        self.wallet_alert_store = wallet_alert_store
        self.wallet_policy = wallet_policy or SupplierWalletPolicy()
        self.pricing_policy_store = pricing_policy_store
        self.funding_intent_store = funding_intent_store
        self.mapping_review_provider = mapping_review_provider
        self.mapping_history_provider = mapping_history_provider
        self.catalog_inventory_provider = catalog_inventory_provider
        self.catalog_refresh_provider = catalog_refresh_provider
        self.mapping_approval_provider = mapping_approval_provider
        self._mapping_approval_lock = threading.RLock()
        if treasury_reviewers is not None and not isinstance(treasury_reviewers, dict):
            raise ValueError("invalid named treasury reviewer configuration")
        self.treasury_reviewers = dict(treasury_reviewers or {})
        if (any(not isinstance(actor, str) or not re.fullmatch(r"staff:[A-Za-z0-9._-]{1,64}", actor)
                or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                for actor, digest in self.treasury_reviewers.items())
                or len(set(self.treasury_reviewers.values())) != len(self.treasury_reviewers)):
            raise ValueError("invalid named treasury reviewer configuration")

    def authenticate_treasury(self, token: str | None) -> str:
        if isinstance(token, str) and 32 <= len(token) <= 256:
            digest = hashlib.sha256(token.encode()).hexdigest()
            for actor, expected in self.treasury_reviewers.items():
                if hmac.compare_digest(digest, expected):
                    return actor
        raise GameAdminAuthError("named treasury authorization required")

    def authenticate(self, authorization: str | None) -> None:
        expected = f"Bearer {self._admin_token}"
        if not isinstance(authorization, str) or not hmac.compare_digest(authorization, expected):
            raise GameAdminAuthError("administrator authorization required")

    def money_integrity(self) -> dict[str, Any]:
        if self.integrity_paths is None:
            raise GameAdminInputError("money integrity stores are not configured")
        return audit_money_integrity(*self.integrity_paths)

    def get_catalog_policy(self):
        if self.catalog_policy_store is None:
            raise GameAdminInputError('catalog controls are not configured')
        return self.catalog_policy_store.status()

    def update_catalog_policy(self, payload):
        if self.catalog_policy_store is None:
            raise GameAdminInputError('catalog controls are not configured')
        if not isinstance(payload, dict) or set(payload) != {'policy', 'expected_revision'}:
            raise GameAdminInputError('catalog policy payload is invalid')
        return self.catalog_policy_store.save(payload['policy'], expected_revision=payload['expected_revision'])

    def wallet_status(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now(UTC)
        if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
            raise GameAdminInputError("wallet observation time must be timezone-aware")
        current = current.astimezone(UTC)
        payload = self.supplier_client.get_balance()
        try:
            snapshot = parse_supplier_wallet_snapshot(payload, observed_at=current)
        except SupplierAccountInputError:
            raise GameAdminInputError("supplier balance response was malformed") from None
        observed = self.wallet_alert_store.observe(snapshot, self.wallet_policy, now=current)
        decision = observed.decision
        return {
            "balance": snapshot.as_dict(),
            "low_wallet": {
                "is_low": decision.is_low,
                "should_remind": decision.should_remind,
                "reason": decision.reason,
                "threshold_usd": format(decision.threshold_usd, "f"),
                "suggested_topup_usd": format(decision.suggested_topup_usd, "f"),
                "alert_id": observed.alert_id,
            },
        }

    def create_wallet_invoice(self, payload: dict[str, Any], *, treasury_token: str | None = None) -> dict[str, Any]:
        actor = self.authenticate_treasury(treasury_token)
        if not isinstance(payload, dict) or set(payload) != {"intent_id", "method", "amount_usd"}:
            raise GameAdminInputError("invoice payload must include only the request ID, method and amount")
        if self.funding_intent_store is None:
            raise GameAdminInputError("persistent funding intent storage is required")
        intent_id = payload.get("intent_id")
        method = payload.get("method")
        amount = payload.get("amount_usd")
        service = WalletFundingService(self.supplier_client, intent_store=self.funding_intent_store)
        intent = service.create_intent(intent_id=intent_id, method=method, amount_usd=amount, reviewer=actor)
        return {"intent": intent.as_dict()}

    def funding_reconciliation_status(self) -> dict[str, Any]:
        if self.funding_intent_store is None:
            raise GameAdminInputError("persistent funding intent storage is required")
        attempts = list(self.funding_intent_store.unresolved_attempts())
        return {"unresolved_attempts": attempts, "invoice_creation_blocked": bool(attempts)}

    def resolve_funding_attempt_absent(self, intent_id: str, payload: dict[str, Any], *, treasury_token: str | None = None) -> dict[str, Any]:
        actor = self.authenticate_treasury(treasury_token)
        if self.funding_intent_store is None:
            raise GameAdminInputError("persistent funding intent storage is required")
        if not isinstance(payload, dict) or set(payload) != {
            "provider_reference", "confirmed_absent",
        }:
            raise GameAdminInputError("provider reconciliation fields are invalid")
        try:
            resolution = self.funding_intent_store.resolve_confirmed_absent(
                intent_id,
                provider_reference=payload["provider_reference"],
                reviewer=actor,
                confirmed_absent=payload["confirmed_absent"],
            )
        except SupplierAccountInputError as exc:
            raise GameAdminInputError(str(exc)) from exc
        status = self.funding_reconciliation_status()
        return {"resolution": resolution, **status}

    def recover_funding_invoice(self, intent_id: str, payload: dict[str, Any], *, treasury_token: str | None = None) -> dict[str, Any]:
        actor = self.authenticate_treasury(treasury_token)
        if self.supplier_client is None:
            raise GameAdminInputError("supplier read access is not configured")
        if not isinstance(payload, dict) or set(payload) != {"payment_id", "provider_reference", "confirmed_match"}:
            raise GameAdminInputError("provider invoice resolution fields are invalid")
        service = WalletFundingService(self.supplier_client, intent_store=self.funding_intent_store)
        intent = service.recover_existing(intent_id=intent_id, reviewer=actor, **payload)
        return {"intent": intent.as_dict(), **self.funding_reconciliation_status()}

    def supplier_payment_methods(self) -> dict[str, Any]:
        if self.supplier_client is None:
            raise GameAdminInputError("supplier API key is not configured")
        try:
            methods = parse_supplier_payment_methods(self.supplier_client.get_payment_methods())
        except SupplierAccountInputError as exc:
            raise GameAdminInputError("supplier payment methods were malformed") from exc
        return {"methods": list(methods)}

    def wallet_alerts(self, *, status: str = "pending", limit: int = 20) -> dict[str, Any]:
        return {"alerts": self.wallet_alert_store.list_alerts(status=status, limit=limit)}

    def payment_status(self, payment_id: str) -> dict[str, Any]:
        payload = self.supplier_client.get_payment(payment_id)
        return {"payment": _safe_payment(payload)}

    def subscription_status(self) -> dict[str, Any]:
        """Read a sanitized supplier-plan/trial snapshot for the operator UI."""

        return {"subscription": _safe_subscription(self.supplier_client.get_subscription())}

    def webhook_status(self) -> dict[str, Any]:
        """Read supplier webhook metadata without returning its signing secret."""

        return {"webhook": _safe_webhook(self.supplier_client.get_webhook())}

    def mapping_review_status(self) -> dict[str, Any]:
        """Return aggregate SKU/region/rights readiness without private rows."""

        if self.mapping_review_provider is None:
            return {
                "mapping_review": {
                    "status": "unconfigured",
                    "reason": "mapping_review_not_configured",
                    "writes": False,
                    "supplier_key_used": False,
                }
            }
        try:
            payload = self.mapping_review_provider()
        except (OSError, ValueError, SystemExit):
            payload = {
                "status": "invalid",
                "reason": "mapping_review_invalid",
                "writes": False,
                "supplier_key_used": False,
            }
        return {"mapping_review": _safe_mapping_review(payload)}

    def mapping_history_status(self) -> dict[str, Any]:
        """Return bounded approval events without private rights evidence."""

        if self.mapping_history_provider is None:
            return {
                "mapping_history": {
                    "status": "unconfigured",
                    "count": 0,
                    "events": [],
                    "writes": False,
                }
            }
        try:
            payload = self.mapping_history_provider()
        except (OSError, ValueError, SystemExit):
            payload = {
                "status": "invalid",
                "count": 0,
                "events": [],
                "writes": False,
            }
        return {"mapping_history": _safe_mapping_history(payload)}

    def catalog_inventory(
        self,
        *,
        family: str | None = None,
        query: str = "",
        category: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return exact private API SKU rows through an authenticated boundary."""

        if self.catalog_inventory_provider is None:
            raise GameAdminInputError("catalog inventory is not configured")
        if family is not None and family not in {"topups", "giftcards", "gamekeys", "manual_services"}:
            raise GameAdminInputError("unsupported catalog family")
        for value, label in ((query, "query"), (category, "category")):
            if value is not None and (not isinstance(value, str) or len(value) > 80):
                raise GameAdminInputError(f"{label} is invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise GameAdminInputError("limit is out of range")
        if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= 1_000_000:
            raise GameAdminInputError("offset is out of range")
        payload = self.catalog_inventory_provider()
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise GameAdminInputError("catalog inventory response was malformed")
        normalized_query = query.strip().casefold()
        normalized_category = category.strip().casefold() if isinstance(category, str) else ""
        rows = []
        for item in payload["items"]:
            if not isinstance(item, dict):
                continue
            if family is not None and item.get("family") != family:
                continue
            if normalized_category and normalized_category not in str(item.get("category_id", "")).casefold():
                continue
            if normalized_query:
                haystack = " ".join(
                    str(item.get(key, ""))
                    for key in ("sku", "name", "category_id", "category_name")
                ).casefold()
                if normalized_query not in haystack:
                    continue
            rows.append(item)
        total = len(rows)
        page = [dict(item) for item in rows[offset : offset + limit]]
        reference_products = _reference_products() if page else []
        controls = self.catalog_policy_store.active_policy() if self.catalog_policy_store else None
        for item in page:
            item["mapping_candidate"] = _reference_title_candidates(
                item.get("category_name"), reference_products
            )
            if controls is not None:
                item['catalog_family_enabled'] = item.get('family') in controls['enabled_families']
        next_offset = offset + len(page)
        mapping_status_counts = _inventory_status_counts(rows, "mapping_status")
        mapping_gate_counts = _inventory_status_counts(rows, "mapping_gate")
        projection_status_counts = _inventory_status_counts(rows, "projection_status")
        return {
            "schema_version": 1,
            "status": "private_snapshot",
            "source_sha256": payload.get("source_sha256"),
            "captured_at": payload.get("captured_at"),
            "source_status": payload.get("source_status"),
            "counts": payload.get("counts", {}),
            "category_counts": payload.get("category_counts", {}),
            "mapping_config_status": payload.get("mapping_config_status", "unconfigured"),
            "freshness_status": payload.get("freshness_status", "unknown"),
            "mapping_status_counts": mapping_status_counts,
            "mapping_gate_counts": mapping_gate_counts,
            "projection_status_counts": projection_status_counts,
            "total_count": total,
            "offset": offset,
            "limit": limit,
            "next_offset": next_offset if next_offset < total else None,
            "items": page,
            "reference_products": [
                {"slug": product["slug"], "name": product["name"]}
                for product in reference_products
                if isinstance(product.get("slug"), str) and isinstance(product.get("name"), str)
            ],
            "auxiliary": payload.get("auxiliary", {}),
            "customer_visible": False,
            "checkout_enabled": False,
        }

    def catalog_refresh_status(self) -> dict[str, Any]:
        if self.catalog_refresh_provider is None:
            return {"status": "unavailable", "checked_at": None, "offers": [], "writes": False}
        try:
            payload = self.catalog_refresh_provider()
        except Exception:
            return {"status": "invalid", "checked_at": None, "offers": [], "writes": False}
        if not isinstance(payload, dict) or payload.get("status") not in {"fresh", "stale", "unavailable", "invalid"}:
            return {"status": "invalid", "checked_at": None, "offers": [], "writes": False}
        return payload

    def approve_catalog_mapping(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record one explicitly reviewed row in the private local mapping file."""

        if self.mapping_approval_provider is None:
            raise GameAdminInputError("local mapping approval is not configured")
        if not isinstance(payload, dict):
            raise GameAdminInputError("mapping approval payload must be an object")
        try:
            # Serialize read/modify/replace so simultaneous approvals handled by
            # this process cannot overwrite one another's audit events.
            with self._mapping_approval_lock:
                result = self.mapping_approval_provider(payload)
        except (OSError, MappingApprovalError, ValueError) as exc:
            raise GameAdminInputError(str(exc)) from exc
        if not isinstance(result, dict):
            raise GameAdminInputError("mapping approval result was malformed")
        return {
            "mapping_approval": {
                key: result[key]
                for key in (
                    "status", "provider_key", "offer_id", "product_slug",
                    "fulfillment_kind", "approved_at", "local_config_updated",
                    "supplier_write", "customer_publication",
                )
                if key in result
            }
        }

    def configure_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Configure the supplier order-notification callback through the write gate."""

        if not isinstance(payload, dict):
            raise GameAdminInputError("webhook payload must be an object")
        url = payload.get("url")
        enabled = payload.get("enabled", True)
        if not isinstance(url, str) or not url.strip():
            raise GameAdminInputError("webhook url is required")
        if not isinstance(enabled, bool):
            raise GameAdminInputError("webhook enabled must be boolean")
        result = self.supplier_client.configure_webhook(url=url.strip(), enabled=enabled)
        return {"webhook": _safe_webhook(result), "write_applied": True}

    def delete_webhook(self) -> dict[str, Any]:
        """Remove the supplier callback through the explicit supplier-write gate."""

        result = self.supplier_client.delete_webhook()
        return {"webhook": _safe_webhook(result), "write_applied": True}

    def verify_binance(self, payment_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise GameAdminInputError("verification payload must be an object")
        reference = payload.get("binance_order_id")
        result = self.supplier_client.verify_binance_payment(
            payment_id, binance_order_id=reference
        )
        return {"payment": _safe_payment(result)}

    def get_pricing_policy(self) -> dict[str, Any]:
        if self.pricing_policy_store is None:
            raise GameAdminInputError("pricing policy storage is not configured")
        return self.pricing_policy_store.status()

    def update_pricing_policy(self, payload: dict[str, Any], *, actor: str) -> dict[str, Any]:
        if self.pricing_policy_store is None:
            raise GameAdminInputError("pricing policy storage is not configured")
        if not isinstance(payload, dict):
            raise GameAdminInputError("pricing policy payload must be an object")
        automatic_fx = payload.get("automatic_fx", False)
        if type(automatic_fx) is not bool or payload.get("owner_approval") is not True:
            raise GameAdminInputError("explicit owner approval and valid FX mode are required")
        policy_data = payload.get("policy")
        policy = PricingPolicy.from_complete_mapping(policy_data)
        if policy.supplier_currency != 'USD' or policy.sell_currency != 'MMK':
            raise GameAdminInputError('FazerCards pricing requires USD costs and MMK retail prices')
        # Pricing and mapping approvals share one read/replace configuration file.
        with self._mapping_approval_lock:
            if automatic_fx:
                if set(payload) != {"policy", "automatic_fx", "owner_approval"}:
                    raise GameAdminInputError("automatic FX mode does not accept manual FX evidence")
                saved = self.pricing_policy_store.save_automatic(policy, approved_by=actor)
                return {**saved, "rebuild_required": True}
            if set(payload) not in ({"policy", "approval", "owner_approval"},
                                    {"policy", "approval", "owner_approval", "automatic_fx"}):
                raise GameAdminInputError("FX source, observation time and expiry are required")
            if not isinstance(payload.get("approval"), dict) or set(payload["approval"]) != {
                "fx_source", "fx_observed_at", "valid_until"
            }:
                raise GameAdminInputError("FX source, observation time and expiry are required")
            saved = self.pricing_policy_store.save(policy, approved_by=actor, **payload["approval"])
        return {**saved, "rebuild_required": True}


def build_env_admin_service(*, root: str | Path | None = None) -> GameStoreAdminService | None:
    """Build the protected console when its admin secret is present.

    Without a supplier API key, local snapshot review remains available while
    supplier-backed actions fail closed (the client is deliberately absent).
    """

    admin_token = os.environ.get("AURIX_GAMES_ADMIN_TOKEN", "").strip()
    api_key = os.environ.get("FAZERCARDS_API_KEY", "").strip()
    if not admin_token:
        return None
    base = Path(root or os.environ.get("AURIX_GAMES_BASE_DIR", ".local/fazercards"))
    writes = os.environ.get("FAZERCARDS_ENABLE_WRITES") == "1"
    snapshot_path = Path(
        os.environ.get("FAZERCARDS_CATALOG_SNAPSHOT", str(base / "catalog-20260921.json"))
    )
    config_path = Path(
        os.environ.get("FAZERCARDS_PUBLIC_CATALOG_CONFIG", str(base / "public-catalog-config.json"))
    )
    max_age_minutes = int(os.environ.get("FAZERCARDS_CATALOG_MAX_AGE_MINUTES", "15"))
    policy = SupplierWalletPolicy(
        low_balance_threshold_usd=Decimal(os.environ.get("FAZERCARDS_LOW_BALANCE_USD", "10")),
        suggested_topup_usd=Decimal(os.environ.get("FAZERCARDS_SUGGESTED_TOPUP_USD", "50")),
    )
    def approve_exact_mapping(request: dict[str, Any]) -> dict[str, Any]:
        allowed_fields = {
            "family", "category_id", "sku", "offer_id", "product_slug",
            "fulfillment_kind", "region_rule", "region_label",
            "required_input_labels", "rights_evidence", "reviewer",
            "confirm_approval",
            "validation_category_id", "validation_field_map",
            "validation_region_map", "validation_region_policy",
        }
        if set(request) - allowed_fields:
            raise MappingApprovalError("mapping approval contains unsupported fields")
        family = request.get("family")
        category = request.get("category_id")
        sku = request.get("sku")
        key = (family, category, sku)
        if not all(isinstance(value, str) and value.strip() for value in key):
            raise MappingApprovalError("exact supplier family, category, and SKU are required")
        if request.get("confirm_approval") is not True:
            raise MappingApprovalError("explicit mapping approval confirmation is required")

        # A mapping needs current evidence for its exact category, not an
        # unrelated family or the snapshot's global capture time.
        inventory = load_catalog_inventory(
            snapshot_path,
            mapping_path=config_path,
            max_age_minutes=max_age_minutes,
        )
        exact_rows = [
            item for item in inventory.get("items", [])
            if (item.get("family"), item.get("category_id"), item.get("sku")) == key
        ]
        if len(exact_rows) != 1:
            raise MappingApprovalError("exact supplier SKU must match one current API inventory row")
        source_item = exact_rows[0]
        if source_item.get("freshness_status") != "fresh":
            raise MappingApprovalError("exact supplier category evidence must be fresh before mapping approval")
        if source_item.get("source_category_complete") is not True:
            raise MappingApprovalError("supplier category listing and exact offer detail must be complete before mapping approval")
        if not isinstance(source_item.get("name"), str) or not source_item["name"].strip():
            raise MappingApprovalError("supplier SKU is missing its exact display name")

        products = {
            product["slug"]: product["name"]
            for product in _reference_products()
            if isinstance(product.get("slug"), str) and isinstance(product.get("name"), str)
        }
        product_slug = request.get("product_slug")
        if not isinstance(product_slug, str) or product_slug not in products:
            raise MappingApprovalError("product must be selected from the canonical AuriX catalog")
        family_fulfillment = {
            "topups": "direct_topup",
            "giftcards": "gift_card",
            "gamekeys": "game_key",
            "manual_services": "manual_service",
        }
        fulfillment = request.get("fulfillment_kind")
        if family_fulfillment.get(family) != fulfillment:
            raise MappingApprovalError("fulfillment must match the supplier family")

        region = request.get("region_rule")
        if not isinstance(region, dict) or set(region) - {"allow_all", "dimension", "allowed_values"}:
            raise MappingApprovalError("reviewed account-side region rule is required")
        allow_all = region.get("allow_all")
        if not isinstance(allow_all, bool):
            raise MappingApprovalError("region allow-all choice must be explicit")
        if allow_all:
            if "dimension" in region or "allowed_values" in region:
                raise MappingApprovalError("allow-all region cannot include a dimension or values")
            region_dimension = None
            region_values: list[str] = []
        else:
            region_dimension = region.get("dimension")
            region_values = region.get("allowed_values")
            if not isinstance(region_values, list) or not region_values:
                raise MappingApprovalError("at least one account-side region value is required")
            if not all(isinstance(value, str) and value.strip() for value in region_values):
                raise MappingApprovalError("region values must be non-empty strings")

        labels = request.get("required_input_labels", [])
        if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
            raise MappingApprovalError("recipient field labels must be an array of strings")
        if len(labels) > 12:
            raise MappingApprovalError("no more than 12 recipient field labels may be recorded")
        if any(len(label) > 96 or "\n" in label or "\r" in label for label in labels):
            raise MappingApprovalError("recipient field labels must be single-line and at most 96 characters")

        offer_id = request.get("offer_id")
        if not isinstance(offer_id, str) or not offer_id.strip() or len(offer_id.strip()) > 160:
            raise MappingApprovalError("AuriX offer ID is required")
        approved_at = datetime.now(UTC).isoformat()
        from argparse import Namespace

        args = Namespace(
            confirm_approval=True,
            family=family,
            category=category,
            sku=sku,
            offer_id=offer_id.strip(),
            product_slug=product_slug,
            product_name=products[product_slug],
            fulfillment_kind=fulfillment,
            denomination_label=source_item["name"],
            region_dimension=region_dimension,
            region_value=region_values,
            allow_all_region=allow_all,
            region_label=request.get("region_label"),
            required_input_label=labels,
            rights_evidence=request.get("rights_evidence"),
            reviewer=request.get("reviewer"),
            approved_at=approved_at,
        )
        config = _read_private_mapping(config_path)
        existing_rows = [
            row for row in config["mappings"]
            if tuple(row.get(field) for field in (
                "family", "provider_category_id", "provider_offer_id"
            )) == key
        ]
        if not existing_rows:
            digest = hashlib.sha256("\x1f".join(key).encode("utf-8")).hexdigest()[:24]
            config["mappings"].append({
                "family": family,
                "provider_category_id": category,
                "provider_offer_id": sku,
                "offer_id": f"pending-{digest}",
                "product_slug": "pending-review",
                "product_name": "Pending operator review",
                "fulfillment_kind": fulfillment,
                "denomination_label": source_item["name"],
                "review_state": "blocked",
                "customer_visible": False,
                "supplier_enabled": False,
                "resale_authorized": False,
                "region_rule": {"verified": False, "allow_all": False},
                "region_label": None,
                "region_dimension": None,
                "required_input_labels": [],
            })
        updated = _approve_mapping_payload(config, args)
        approved_row = next(row for row in updated["mappings"] if row["offer_id"] == offer_id.strip())
        for field in ("validation_category_id", "validation_field_map",
                      "validation_region_map", "validation_region_policy"):
            if field in request:
                approved_row[field] = request[field]
        if any(field in request for field in ("validation_category_id", "validation_field_map",
                                              "validation_region_map", "validation_region_policy")):
            try:
                validation_settings(approved_row)
            except SupplierRouteError as exc:
                raise MappingApprovalError(str(exc)) from exc
        _write_private_mapping(config_path, updated)
        return {
            "status": "approved_row",
            "provider_key": list(key),
            "offer_id": offer_id.strip(),
            "product_slug": product_slug,
            "fulfillment_kind": fulfillment,
            "approved_at": approved_at,
            "local_config_updated": True,
            "supplier_write": False,
            "customer_publication": False,
        }

    return GameStoreAdminService(
        admin_token=admin_token,
        catalog_policy_store=env_catalog_policy_store(base),
        integrity_paths=(
            Path(os.environ.get("AURIX_GAMES_ORDER_DATABASE", str(base / "game-orders.sqlite3"))),
            Path(os.environ.get("AURIX_GAMES_LEDGER_DATABASE", str(base / "game-ledger.sqlite3"))),
            Path(os.environ.get("AURIX_GAMES_FULFILLMENT_DATABASE", str(base / "fulfillment.sqlite3"))),
        ),
        treasury_reviewers=json.loads(os.environ.get("AURIX_GAMES_TREASURY_REVIEWERS", "{}")),
        supplier_client=FazerCardsCommandClient(api_key, allow_writes=writes) if api_key else None,
        wallet_alert_store=SupplierWalletAlertStore(base / "wallet-alerts.sqlite3"),
        wallet_policy=policy,
        pricing_policy_store=PricingPolicyStore(config_path),
        funding_intent_store=WalletFundingIntentStore(
            os.environ.get("AURIX_GAMES_FUNDING_INTENTS", str(base / "funding-intents.sqlite3"))
        ),
        mapping_review_provider=lambda: review_mapping(
            snapshot_path=snapshot_path,
            config_path=config_path,
            max_age_minutes=max_age_minutes,
        ),
        mapping_history_provider=lambda: mapping_history(config_path=config_path),
        catalog_inventory_provider=lambda: load_catalog_inventory(
            snapshot_path,
            mapping_path=config_path,
            max_age_minutes=max_age_minutes,
        ),
        catalog_refresh_provider=lambda: _catalog_refresh_diagnostics(
            base / "catalog-refresh-evidence", config_path,
        ),
        mapping_approval_provider=approve_exact_mapping,
    )


def build_env_webhook_reconciler(*, root: str | Path | None = None) -> SupplierWebhookReconciler | None:
    """Build the signed supplier callback boundary when its secret is present."""

    secret = os.environ.get("FAZERCARDS_WEBHOOK_SECRET", "").strip()
    if not secret:
        return None
    base = Path(root or os.environ.get("AURIX_GAMES_BASE_DIR", ".local/fazercards"))
    database = os.environ.get("AURIX_GAMES_ORDER_DATABASE", str(base / "game-orders.sqlite3"))
    return SupplierWebhookReconciler(
        GameOrderRepository(database),
        webhook_secret=secret,
    )


def _safe_payment(payload: dict[str, Any]) -> dict[str, Any]:
    payment = payload.get("payment") if isinstance(payload, dict) else None
    if not isinstance(payment, dict):
        raise GameAdminInputError("supplier payment response was malformed")
    allowed = {
        "id", "network", "amount", "uniqueAmount", "address", "memo", "binanceId",
        "displayName", "status", "verifyAttempts", "paymentUrl", "expiresAt", "completedAt",
        "cancelledAt", "createdAt",
    }
    result = {key: value for key, value in payment.items() if key in allowed and key != "paymentUrl"}
    payment_url = safe_payment_url(payment.get("paymentUrl"))
    if payment_url is not None:
        result["paymentUrl"] = payment_url
    return result


def _safe_subscription(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only plan/expiry metadata; never pass arbitrary provider fields through."""

    if not isinstance(payload, dict):
        raise GameAdminInputError("supplier subscription response was malformed")
    allowed = {
        "subscription", "plan", "planName", "plan_name", "status", "state", "trial",
        "trialEndsAt", "trial_ends_at", "expiresAt", "expires_at", "endsAt", "ends_at",
        "startedAt", "started_at", "renewalAt", "renewal_at", "autoRenew", "auto_renew",
        "remainingDays", "remaining_days", "currency", "price", "amount", "message",
        "subscriptionActive", "planExpiresAt", "planAutoRenew",
    }
    result = {key: value for key, value in payload.items() if key in allowed and _safe_value(value)}
    nested = result.get("subscription")
    if isinstance(nested, dict):
        result["subscription"] = {
            key: value for key, value in nested.items()
            if key in allowed and _safe_value(value)
        }
    if not result:
        raise GameAdminInputError("supplier subscription response was malformed")
    return result


def _safe_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    """Return only callback metadata; secrets and arbitrary provider fields stay out."""

    if not isinstance(payload, dict):
        raise GameAdminInputError("supplier webhook response was malformed")
    allowed = {
        "id", "url", "enabled", "status", "state", "configured", "active",
        "createdAt", "created_at", "updatedAt", "updated_at", "lastDeliveryAt",
        "last_delivery_at", "lastDeliveryStatus", "last_delivery_status",
        "lastErrorAt", "last_error_at", "deliveryCount", "delivery_count",
        "eventTypes", "event_types", "message",
    }
    source = payload.get("webhook") if isinstance(payload.get("webhook"), dict) else payload
    result = {key: value for key, value in source.items() if key in allowed and _safe_value(value)}
    if not result and payload.get("ok") is False:
        raise GameAdminInputError("supplier webhook response was malformed")
    return result


def _safe_mapping_review(payload: dict[str, Any]) -> dict[str, Any]:
    """Expose only aggregate approval state; paths and supplier rows stay private."""

    if not isinstance(payload, dict):
        raise GameAdminInputError("mapping review response was malformed")
    allowed = {
        "status", "reason", "captured_at", "records", "mapping_rows", "mapped", "projected",
        "writes", "supplier_key_used",
    }
    result = {key: value for key, value in payload.items() if key in allowed and _safe_value(value)}
    raw_reasons = payload.get("block_reasons", {})
    if isinstance(raw_reasons, dict):
        result["block_reasons"] = {
            key: value
            for key, value in raw_reasons.items()
            if isinstance(key, str) and len(key) <= 80 and isinstance(value, int) and not isinstance(value, bool)
        }
    if not isinstance(result.get("status"), str) or not isinstance(result.get("reason"), str):
        raise GameAdminInputError("mapping review response was malformed")
    result.setdefault("writes", False)
    result.setdefault("supplier_key_used", False)
    return result


def _safe_mapping_history(payload: dict[str, Any]) -> dict[str, Any]:
    """Sanitize bounded approval history before it crosses the admin API."""

    if not isinstance(payload, dict):
        raise GameAdminInputError("mapping history response was malformed")
    status = payload.get("status")
    if not isinstance(status, str):
        raise GameAdminInputError("mapping history response was malformed")
    count = payload.get("count", 0)
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise GameAdminInputError("mapping history count was malformed")
    raw_events = payload.get("events", [])
    if not isinstance(raw_events, list):
        raise GameAdminInputError("mapping history events were malformed")
    events: list[dict[str, Any]] = []
    for event in raw_events[:100]:
        if not isinstance(event, dict):
            continue
        provider_key = event.get("provider_key")
        if not isinstance(provider_key, list) or len(provider_key) != 3 or not all(
            isinstance(value, str) for value in provider_key
        ):
            continue
        required = ("event_id", "offer_id", "product_slug", "fulfillment_kind", "reviewer", "approved_at")
        if not all(isinstance(event.get(key), str) and len(event[key]) <= 256 for key in required):
            continue
        region_rule = event.get("region_rule", {})
        if not isinstance(region_rule, dict):
            region_rule = {}
        safe_region = {
            key: value
            for key, value in region_rule.items()
            if key in {"verified", "allow_all", "dimension", "allowed_values"}
            and (
                isinstance(value, (bool, str))
                or (
                    isinstance(value, list)
                    and len(value) <= 100
                    and all(isinstance(item, str) and len(item) <= 96 for item in value)
                )
            )
        }
        events.append({
            key: event[key]
            for key in required
        } | {
            "provider_key": list(provider_key),
            "region_rule": safe_region,
            "rights_evidence_recorded": event.get("rights_evidence_recorded") is True,
        })
    result: dict[str, Any] = {
        "status": status,
        "count": count,
        "events": events,
        "writes": False,
    }
    for key in ("queue_status", "last_reviewed_at"):
        if isinstance(payload.get(key), str):
            result[key] = payload[key]
    return result


def _safe_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, dict):
        return all(isinstance(key, str) and len(key) <= 64 for key in value)
    return False


def _inventory_status_counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = row.get(key)
        if isinstance(value, str):
            counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


__all__ = [
    "GameAdminAuthError",
    "GameAdminInputError",
    "GameStoreAdminService",
    "build_env_admin_service",
    "build_env_webhook_reconciler",
]
