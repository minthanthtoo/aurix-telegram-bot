"""Build a customer-safe live projection from a reviewed supplier snapshot.

The builder is intentionally separate from the FazerCards importer.  A fresh
supplier row is not automatically sellable: an operator mapping must provide
an AuriX-owned offer id, rights, region rule, and availability.  Prices are
recomputed on every build from the current supplier cost and policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from .catalog_projection import ApprovedOfferInput, PublicOffer, project_public_offers
from .eligibility import RegionDimension, RegionRule
from .pricing import PriceBreakdown, PricingInputError, PricingPolicy, calculate_price
from .catalog_policy import allows_offer


class LiveCatalogInputError(ValueError):
    """A supplier row or operator mapping is malformed."""


@dataclass(frozen=True)
class SupplierOfferRecord:
    family: str
    provider_category_id: str
    provider_offer_id: str
    display_name: str
    supplier_cost_usd: Decimal
    observed_at: datetime
    available: bool


@dataclass(frozen=True)
class OfferMapping:
    """AuriX review and rights record; no supplier cost is stored here."""

    offer_id: str
    product_slug: str
    product_name: str
    fulfillment_kind: str
    denomination_label: str
    review_state: str = "approved"
    customer_visible: bool = True
    supplier_enabled: bool = True
    resale_authorized: bool = True
    region_rule: RegionRule = RegionRule()
    region_label: str | None = None
    region_dimension: RegionDimension | None = None
    required_input_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class PricedLiveOffer:
    public_offer: PublicOffer
    breakdown: PriceBreakdown


def build_priced_offers(
    records: Iterable[SupplierOfferRecord],
    mappings: Mapping[tuple[str, str, str], OfferMapping],
    *,
    policy: PricingPolicy,
    now: datetime,
    max_age: timedelta,
    catalog_policy: dict[str, Any] | None = None,
) -> tuple[PricedLiveOffer, ...]:
    """Reprice and project only explicitly mapped, currently available rows."""

    if policy.supplier_currency != 'USD' or policy.sell_currency != 'MMK':
        raise LiveCatalogInputError('FazerCards pricing requires USD costs and MMK retail prices')

    approved: list[ApprovedOfferInput] = []
    breakdowns: dict[str, PriceBreakdown] = {}
    for record in records:
        _validate_record(record, now=now)
        key = (record.family, record.provider_category_id, record.provider_offer_id)
        mapping = mappings.get(key)
        if mapping is None:
            continue
        if catalog_policy is not None and not allows_offer(catalog_policy, {
            'fulfillment_kind': mapping.fulfillment_kind,
            'product_slug': mapping.product_slug, 'offer_id': mapping.offer_id,
        }):
            continue
        if not record.available:
            continue
        try:
            breakdown = calculate_price(record.supplier_cost_usd, policy)
        except PricingInputError as exc:
            raise LiveCatalogInputError("pricing policy could not price a mapped offer") from exc
        approved.append(
            ApprovedOfferInput(
                offer_id=_text(mapping.offer_id, "offer_id", 160),
                product_slug=_text(mapping.product_slug, "product_slug", 160),
                product_name=_text(mapping.product_name, "product_name", 160),
                fulfillment_kind=mapping.fulfillment_kind,
                denomination_label=_text(mapping.denomination_label, "denomination_label", 160),
                retail_amount_minor=breakdown.final_price_minor,
                currency=policy.sell_currency,
                observed_at=record.observed_at,
                review_state=mapping.review_state,
                customer_visible=mapping.customer_visible,
                supplier_enabled=mapping.supplier_enabled,
                available=mapping.supplier_enabled and record.available,
                resale_authorized=mapping.resale_authorized,
                region_rule=mapping.region_rule,
                region_label=mapping.region_label,
                region_dimension=mapping.region_dimension,
                required_input_labels=mapping.required_input_labels,
            )
        )
        breakdowns[mapping.offer_id] = breakdown
    projected = project_public_offers(approved, now=now, max_age=max_age)
    return tuple(PricedLiveOffer(public, breakdowns[public.offer_id]) for public in projected)


def _validate_record(record: SupplierOfferRecord, *, now: datetime) -> None:
    if not isinstance(record, SupplierOfferRecord):
        raise LiveCatalogInputError("supplier record has unsupported type")
    for label in ("family", "provider_category_id", "provider_offer_id", "display_name"):
        _text(getattr(record, label), label, 256)
    if isinstance(record.supplier_cost_usd, bool):
        raise LiveCatalogInputError("supplier cost is invalid")
    try:
        cost = record.supplier_cost_usd if isinstance(record.supplier_cost_usd, Decimal) else Decimal(str(record.supplier_cost_usd))
    except (InvalidOperation, TypeError, ValueError):
        raise LiveCatalogInputError("supplier cost is invalid") from None
    if not cost.is_finite() or cost < 0:
        raise LiveCatalogInputError("supplier cost is invalid")
    if not isinstance(record.available, bool):
        raise LiveCatalogInputError("supplier availability is invalid")
    if not isinstance(record.observed_at, datetime) or record.observed_at.tzinfo is None or record.observed_at.utcoffset() is None:
        raise LiveCatalogInputError("supplier observation time must be timezone-aware")
    if record.observed_at > now:
        raise LiveCatalogInputError("supplier observation cannot be from the future")


def _text(value: Any, label: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise LiveCatalogInputError(f"{label} is invalid")
    return value.strip()


__all__ = [
    "LiveCatalogInputError",
    "OfferMapping",
    "PricedLiveOffer",
    "SupplierOfferRecord",
    "build_priced_offers",
]
