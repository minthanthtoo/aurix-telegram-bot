"""Authenticated, payment-boundary checkout preparation for Games.

This service creates an ``AWAITING_PAYMENT`` order only from an injected,
operator-approved offer resolver. Prices, supplier IDs, and fulfillment data
are never accepted from the browser as authoritative. Recipient fields are
fingerprinted for the order snapshot and stored separately in the encrypted
private vault for a later paid fulfillment worker.
"""

from __future__ import annotations
from .commerce_settings import commerce_paused, env_commerce_settings

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, Mapping, Protocol
import uuid

from .catalog_projection import ApprovedOfferInput
from .customer_api import CustomerApiConfig, CustomerPrincipal, build_env_customer_api
from .eligibility import (
    RegionDimension,
    RegionRule,
    RegionValue,
    RecipientEligibilityEvidence,
    SUPPORTED_FULFILLMENT_KINDS,
)
from .order_repository import GameOrderRepository
from .order_service import OrderPreparationError, PreparedOrder, prepare_order
from .order_state import GameOrder, OrderState
from .public_catalog import PublicCatalogStore
from .pricing_store import PricingPolicyStore
from .catalog_policy import env_catalog_policy_store
from .recipient_vault import RecipientVault, RecipientVaultError, RecipientVaultInputError
from .supplier_routes import (
    PrivateSupplierRoute,
    PrivateSupplierRouteStore,
    SupplierRouteError,
    supplier_order_payload,
)


UTC = timezone.utc


class CheckoutInputError(ValueError):
    """A checkout request is malformed or not safe to prepare."""


class CheckoutOfferResolver(Protocol):
    def resolve(self, offer_id: str) -> ApprovedOfferInput | None: ...


@dataclass(frozen=True)
class MappingOfferResolver:
    """Small injected resolver useful for a private approved mapping store."""

    offers: Mapping[str, ApprovedOfferInput]

    def resolve(self, offer_id: str) -> ApprovedOfferInput | None:
        value = self.offers.get(offer_id)
        return value if isinstance(value, ApprovedOfferInput) else None


@dataclass(frozen=True)
class PublicCatalogOfferResolver:
    """Resolve only a fresh, approved public projection for private checkout.

    Global offers can be resolved directly from the projection. Regional
    offers deliberately require an operator-supplied recipient-side mapping;
    a supplier label such as ``SEA`` is not evidence that a customer's game
    account uses that region.
    """

    store: PublicCatalogStore
    regional_values: Mapping[str, tuple[str, ...] | list[str] | frozenset[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.store, PublicCatalogStore):
            raise CheckoutInputError("public catalog store is invalid")
        if not isinstance(self.regional_values, Mapping):
            raise CheckoutInputError("regional values mapping is invalid")
        normalized: dict[str, frozenset[str]] = {}
        for raw_offer_id, raw_values in self.regional_values.items():
            if not isinstance(raw_offer_id, str) or not raw_offer_id.strip():
                raise CheckoutInputError("regional offer id is invalid")
            if isinstance(raw_values, (str, bytes)) or not isinstance(raw_values, (list, tuple, frozenset, set)):
                raise CheckoutInputError("regional values must be a list of strings")
            values = frozenset(item.strip() for item in raw_values if isinstance(item, str) and item.strip())
            if not values or len(values) != len(raw_values):
                raise CheckoutInputError("regional values must be non-empty strings")
            normalized[raw_offer_id.strip()] = values
        object.__setattr__(self, "regional_values", normalized)

    def resolve(self, offer_id: str) -> ApprovedOfferInput | None:
        offer = self.store.offer(offer_id)
        if offer is None:
            return None
        observed_at = offer.pop("_observed_at", None)
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None or observed_at.utcoffset() is None:
            return None
        region = offer.get("region")
        if region is None or not isinstance(region, Mapping):
            return None
        region_type = region.get("type")
        region_label = region.get("label")
        if not isinstance(region_type, str) or not isinstance(region_label, str) or not region_label.strip():
            return None
        if region_type == "global":
            region_rule = RegionRule(verified=True, allow_all=True)
            region_dimension = None
        else:
            try:
                region_dimension = RegionDimension(region_type)
            except ValueError:
                return None
            values = self.regional_values.get(offer["offer_id"])
            if not values:
                return None
            region_rule = RegionRule(
                verified=True,
                allow_all=False,
                dimension=region_dimension,
                allowed_values=frozenset(values),
            )
        try:
            fulfillment_kind = offer["fulfillment_kind"]
            required_labels = tuple(offer.get("required_input_labels", []))
            if fulfillment_kind not in SUPPORTED_FULFILLMENT_KINDS:
                return None
            return ApprovedOfferInput(
                offer_id=offer["offer_id"],
                product_slug=offer["product_slug"],
                product_name=offer["product_name"],
                fulfillment_kind=fulfillment_kind,
                denomination_label=offer["denomination_label"],
                retail_amount_minor=offer["retail_amount_minor"],
                currency=offer["currency"],
                observed_at=observed_at,
                review_state="approved",
                customer_visible=True,
                supplier_enabled=True,
                available=True,
                resale_authorized=True,
                region_rule=region_rule,
                region_label=region_label,
                region_dimension=region_dimension,
                required_input_labels=required_labels,
            )
        except (KeyError, TypeError, ValueError):
            return None


def build_env_customer_checkout(
    *,
    root: str | Path | None = None,
    customer_api: CustomerApiConfig | None = None,
    for_fulfillment: bool = False,
) -> CustomerCheckoutService | None:
    """Build authenticated checkout only when the operator opts in.

    This wires the fresh public projection to order preparation and encrypted
    recipient storage. It intentionally does not charge a wallet or call a
    supplier; every prepared order remains ``AWAITING_PAYMENT``.
    """

    if not for_fulfillment and os.environ.get("AURIX_GAMES_CUSTOMER_CHECKOUT_ENABLED", "0").strip() != "1":
        return None
    configured_api = customer_api or build_env_customer_api(root=root)
    if configured_api is None:
        raise CheckoutInputError("customer checkout requires customer auth configuration")
    base = Path(root or os.environ.get("AURIX_GAMES_BASE_DIR", ".local/fazercards"))
    public_path = os.environ.get(
        "AURIX_GAMES_PUBLIC_CATALOG",
        str(base / "public-catalog.json"),
    )
    mapping_config_path = os.environ.get(
        "FAZERCARDS_PUBLIC_CATALOG_CONFIG",
        str(base / "public-catalog-config.json"),
    )
    shop2topup_mapping_path = os.environ.get(
        "SHOP2TOPUP_MAPPING_CONFIG",
        str(base / "shop2topup-mapping-config.json"),
    )
    vault_path = os.environ.get(
        "AURIX_GAMES_RECIPIENT_VAULT_DATABASE",
        str(base / "game-recipient-vault.sqlite3"),
    )
    vault_key = os.environ.get("AURIX_GAMES_RECIPIENT_VAULT_KEY", "")
    fingerprint_secret = os.environ.get("AURIX_GAMES_CHECKOUT_FINGERPRINT_SECRET", "")
    if not vault_key or not fingerprint_secret:
        raise CheckoutInputError(
            "customer checkout requires recipient vault and fingerprint secrets"
        )
    regional_values = _regional_values_from_env()
    resolver = PublicCatalogOfferResolver(
        # Merchandising changes gate new checkout, not existing paid delivery obligations.
        PublicCatalogStore(
            public_path,
            catalog_policy_store=None if for_fulfillment else env_catalog_policy_store(base),
            pricing_policy_store=None if for_fulfillment else PricingPolicyStore(mapping_config_path),
        ),
        regional_values=regional_values,
    )
    return CustomerCheckoutService(
        configured_api.service.repository,
        offer_resolver=resolver,
        recipient_vault=RecipientVault(vault_path, encryption_key=vault_key),
        fingerprint_secret=fingerprint_secret,
        supplier_route_store=PrivateSupplierRouteStore(
            mapping_config_path, additional_paths=(shop2topup_mapping_path,)
        ),
    )


def _regional_values_from_env() -> dict[str, list[str]]:
    raw = os.environ.get("AURIX_GAMES_CHECKOUT_REGION_VALUES_JSON", "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CheckoutInputError("checkout regional values JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise CheckoutInputError("checkout regional values must be an object")
    result: dict[str, list[str]] = {}
    for offer_id, values in payload.items():
        if not isinstance(offer_id, str) or not offer_id.strip() or not isinstance(values, list):
            raise CheckoutInputError("checkout regional values are invalid")
        if not values or any(not isinstance(value, str) or not value.strip() for value in values):
            raise CheckoutInputError("checkout regional values are invalid")
        result[offer_id.strip()] = [value.strip() for value in values]
    return result


@dataclass(frozen=True)
class CheckoutResult:
    order: GameOrder
    prepared: PreparedOrder

    def as_dict(self) -> dict[str, Any]:
        snapshot = self.order.snapshot
        return {
            "order": {
                "order_id": self.order.order_id,
                "state": self.order.state.value,
                "product_slug": snapshot.product_slug,
                "offer_id": snapshot.offer_id,
                "fulfillment_kind": snapshot.fulfillment_kind,
                "amount_minor": snapshot.amount_minor,
                "currency": snapshot.currency,
            },
            "payment": {
                "state": "required",
                "amount_minor": snapshot.amount_minor,
                "currency": snapshot.currency,
            },
            "eligibility": {"eligible": self.prepared.decision.eligible},
        }


class CustomerCheckoutService:
    """Prepare owner-scoped orders without charging or contacting a supplier."""

    def __init__(
        self,
        repository: GameOrderRepository,
        *,
        offer_resolver: CheckoutOfferResolver,
        recipient_vault: RecipientVault,
        fingerprint_secret: bytes | str,
        supplier_route_store: PrivateSupplierRouteStore | None = None,
    ) -> None:
        if not hasattr(offer_resolver, "resolve"):
            raise CheckoutInputError("offer resolver is invalid")
        try:
            secret = fingerprint_secret.encode("utf-8") if isinstance(fingerprint_secret, str) else fingerprint_secret
        except UnicodeError as exc:
            raise CheckoutInputError("fingerprint secret is invalid") from exc
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise CheckoutInputError("fingerprint secret must be at least 32 bytes")
        self.repository = repository
        self.offer_resolver = offer_resolver
        self.recipient_vault = recipient_vault
        self._fingerprint_secret = secret
        if supplier_route_store is not None and not isinstance(supplier_route_store, PrivateSupplierRouteStore):
            raise CheckoutInputError("private supplier route store is invalid")
        self.supplier_route_store = supplier_route_store

    def claim_recipient_lookup(self, customer_id: str, *, now: datetime) -> None:
        """Share a privacy-preserving lookup rate limit through order storage."""
        self.repository.claim_recipient_lookup(
            customer_id,
            now=now,
            fingerprint_secret=self._fingerprint_secret,
        )

    def _supplier_route(self, offer: ApprovedOfferInput) -> PrivateSupplierRoute | None:
        supplier_route: PrivateSupplierRoute | None = None
        if self.supplier_route_store is not None:
            try:
                supplier_route = self.supplier_route_store.resolve(
                    offer_id=offer.offer_id,
                    product_slug=offer.product_slug,
                    fulfillment_kind=offer.fulfillment_kind,
                    region_rule=offer.region_rule,
                    region_label=offer.region_label,
                    denomination_label=offer.denomination_label,
                    required_input_labels=offer.required_input_labels,
                )
            except SupplierRouteError as exc:
                raise CheckoutInputError("offer supplier fulfillment route is unavailable") from exc
        return supplier_route

    def recipient_validation_binding(
        self, principal: CustomerPrincipal, payload: Mapping[str, Any], *, now: datetime
    ) -> str:
        """Bind a trusted lookup to this customer, recipient and approved offer.

        Internal lookup callers must obtain this before lookup and attach it
        only to a successful provider result. Never expose a proof-minting API.
        """
        _validate_principal(principal, now=now)
        if not isinstance(payload, Mapping):
            raise CheckoutInputError("checkout payload must be an object")
        offer = self.offer_resolver.resolve(_id(payload.get("offer_id"), "offer_id"))
        if offer is None:
            raise CheckoutInputError("offer is unavailable")
        return self._recipient_binding(
            principal, offer, _fields(payload.get("fields")), _region(payload.get("region")),
            self._supplier_route(offer),
        )

    def _recipient_binding(
        self, principal: CustomerPrincipal, offer: ApprovedOfferInput,
        fields: Mapping[str, str], region: RegionValue | None,
        supplier_route: PrivateSupplierRoute | None,
    ) -> str:
        policy = offer.region_rule
        bound = {
            "version": 2,
            "customer_id": principal.customer_id,
            "issuer": principal.issuer,
            "offer_id": offer.offer_id,
            "product_slug": offer.product_slug,
            "fulfillment_kind": offer.fulfillment_kind,
            "amount_minor": offer.retail_amount_minor,
            "currency": offer.currency,
            "required_fields": sorted(offer.required_input_labels),
            "supplier_route": supplier_route.as_vault_dict() if supplier_route else None,
            "region_policy": {
                "verified": policy.verified,
                "allow_all": policy.allow_all,
                "dimension": policy.dimension.value if policy.dimension else None,
                "allowed_values": sorted(policy.allowed_values) if policy.allowed_values is not None else None,
            },
            "fields": dict(fields),
            "region": _region_dict(region),
        }
        return hmac.new(
            self._fingerprint_secret, _canonical(bound).encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def prepare(
        self,
        principal: CustomerPrincipal,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
        now: datetime,
        validation: Mapping[str, Any] | None = None,
    ) -> CheckoutResult:
        _validate_principal(principal, now=now)
        if commerce_paused("checkout_enabled"):
            raise CheckoutInputError("New orders are paused in store settings.")
        if not isinstance(payload, Mapping):
            raise CheckoutInputError("checkout payload must be an object")
        offer_id = _id(payload.get("offer_id"), "offer_id")
        client_key = _id(idempotency_key, "idempotency_key")
        offer = self.offer_resolver.resolve(offer_id)
        if offer is None:
            raise CheckoutInputError("offer is unavailable")
        maximum = env_commerce_settings().status()["settings"]["maximum_order_mmk"]
        if maximum is not None and offer.currency == "MMK" and offer.retail_amount_minor > maximum:
            raise CheckoutInputError("This package exceeds the store order limit.")
        supplier_route = self._supplier_route(offer)
        fields = _fields(payload.get("fields"))
        region = _region(payload.get("region"))
        if payload.get("validation") is not None:
            raise CheckoutInputError("validation evidence must come from the trusted server")
        validated = _validation(validation, now=now)
        fingerprint = self._recipient_binding(principal, offer, fields, region, supplier_route)
        recipient = RecipientEligibilityEvidence(
            fulfillment_kind=offer.fulfillment_kind,
            supplied_field_names=frozenset(fields),
            region=region,
            validation_valid=validated["valid"] if validated else None,
            validation_checked_at=validated["checked_at"] if validated else None,
            recipient_binding=fingerprint,
            validation_binding=validated["binding"] if validated else None,
        )
        try:
            prepared = prepare_order(
                self.repository,
                order_id="game-" + uuid.uuid4().hex,
                customer_id=principal.customer_id,
                customer_idempotency_key=client_key,
                offer=offer,
                recipient=recipient,
                recipient_fingerprint=fingerprint,
                now=now,
            )
        except OrderPreparationError as exc:
            raise CheckoutInputError("offer or recipient failed eligibility checks") from exc
        vault_payload = {
            "offer_id": offer.offer_id,
            "fulfillment_kind": offer.fulfillment_kind,
            "fields": fields,
            "region": _region_dict(region),
        }
        if supplier_route is not None:
            vault_payload["supplier_route"] = supplier_route.as_vault_dict()
        try:
            self.recipient_vault.put(
                order_id=prepared.order.order_id,
                customer_id=principal.customer_id,
                payload=vault_payload,
                now=now,
            )
        except (RecipientVaultError, RecipientVaultInputError) as exc:
            raise CheckoutInputError("private recipient payload could not be stored") from exc
        return CheckoutResult(prepared.order, prepared)

    def fulfillment_payload(self, *, customer_id: str, order_id: str) -> dict[str, Any]:
        """Read private fulfillment inputs for an internal paid worker only."""

        order = self.repository.get(order_id, customer_id=customer_id)
        if order.state not in {
            OrderState.PAID_RESERVED,
            OrderState.FULFILLMENT_QUEUED,
            OrderState.SUBMITTED,
            OrderState.PROCESSING,
        }:
            raise CheckoutInputError("order is not eligible for fulfillment payload access")
        try:
            private_payload = self.recipient_vault.get(order_id=order.order_id, customer_id=customer_id)
        except RecipientVaultError as exc:
            raise CheckoutInputError("private recipient payload is unavailable") from exc
        if self.supplier_route_store is None:
            return private_payload
        if (
            private_payload.get("offer_id") != order.snapshot.offer_id
            or private_payload.get("fulfillment_kind") != order.snapshot.fulfillment_kind
        ):
            raise CheckoutInputError("private fulfillment payload does not match the frozen order")
        route_value = private_payload.get("supplier_route")
        if not isinstance(route_value, dict):
            raise CheckoutInputError("private supplier route is unavailable")
        try:
            route = self.supplier_route_store.resolve(
                offer_id=order.snapshot.offer_id,
                product_slug=order.snapshot.product_slug,
                fulfillment_kind=order.snapshot.fulfillment_kind,
            )
        except SupplierRouteError as exc:
            raise CheckoutInputError("approved supplier route is no longer available") from exc
        if route_value != route.as_vault_dict():
            raise CheckoutInputError("private supplier route changed after order preparation")
        try:
            return supplier_order_payload(
                route=route,
                fulfillment_kind=order.snapshot.fulfillment_kind,
                fields=private_payload.get("fields", {}),
            )
        except SupplierRouteError as exc:
            raise CheckoutInputError("private fulfillment inputs are invalid") from exc


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise CheckoutInputError(f"{label} is invalid")
    if any(char in value for char in "\r\n"):
        raise CheckoutInputError(f"{label} is invalid")
    return value.strip()


def _validate_principal(value: object, *, now: datetime) -> None:
    if not isinstance(value, CustomerPrincipal):
        raise CheckoutInputError("customer principal is invalid")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise CheckoutInputError("now must be timezone-aware")
    customer_id = value.customer_id
    issuer = value.issuer
    expires_at = value.expires_at
    if (
        not isinstance(customer_id, str)
        or not customer_id.strip()
        or customer_id.strip() != customer_id
        or len(customer_id) > 128
        or any(char in customer_id for char in "\r\n")
        or not isinstance(issuer, str)
        or not issuer.strip()
        or issuer.strip() != issuer
        or len(issuer) > 80
        or any(char in issuer for char in "\r\n")
        or not isinstance(expires_at, datetime)
        or expires_at.tzinfo is None
        or expires_at.utcoffset() is None
        or expires_at.astimezone(UTC) <= now.astimezone(UTC)
    ):
        raise CheckoutInputError("customer principal is invalid or expired")


def _fields(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise CheckoutInputError("fields must be a non-empty object")
    if len(value) > 32:
        raise CheckoutInputError("too many recipient fields")
    result: dict[str, str] = {}
    for key, item in value.items():
        normalized_key = _id(key, "field name")
        if not isinstance(item, str) or not item.strip() or len(item) > 2048:
            raise CheckoutInputError("recipient field value is invalid")
        if any(char in item for char in "\r\n"):
            raise CheckoutInputError("recipient field value is invalid")
        result[normalized_key] = item.strip()
    return result


def _region(value: object) -> RegionValue | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise CheckoutInputError("region must be an object")
    try:
        dimension = RegionDimension(value.get("dimension"))
        return RegionValue(dimension, value.get("value"))
    except (TypeError, ValueError) as exc:
        raise CheckoutInputError("region is invalid") from exc


def _validation(value: object, *, now: datetime) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or value.get("valid") is not True:
        raise CheckoutInputError("validation evidence is invalid")
    checked_at_raw = value.get("checked_at")
    binding = value.get("binding")
    if not isinstance(checked_at_raw, str) or not isinstance(binding, str) or not binding.strip():
        raise CheckoutInputError("validation evidence is invalid")
    try:
        checked_at = datetime.fromisoformat(checked_at_raw)
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            raise ValueError("validation timestamp must include a timezone")
        checked_at = checked_at.astimezone(UTC)
    except (TypeError, ValueError):
        raise CheckoutInputError("validation evidence is invalid") from None
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise CheckoutInputError("now must be timezone-aware")
    if checked_at > now.astimezone(UTC):
        raise CheckoutInputError("validation evidence is from the future")
    return {"valid": True, "checked_at": checked_at, "binding": binding.strip()}


def _region_dict(value: RegionValue | None) -> dict[str, str] | None:
    if value is None:
        return None
    return {"dimension": value.dimension.value, "value": value.value}


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "CheckoutInputError",
    "CheckoutOfferResolver",
    "CheckoutResult",
    "CustomerCheckoutService",
    "MappingOfferResolver",
    "PublicCatalogOfferResolver",
    "build_env_customer_checkout",
]
