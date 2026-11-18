"""Customer-safe projection of reviewed Games offers.

The private supplier snapshot is intentionally not a storefront catalog. This
module is the narrow boundary an operator can use after approving a mapping,
retail price, and region policy. It accepts only AuriX-owned presentation
fields and never copies supplier identifiers, costs, or raw provider payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .eligibility import RegionDimension, RegionRule


UTC = timezone.utc
DEFAULT_PROJECTION_MAX_AGE = timedelta(minutes=15)
_FULFILLMENT_KINDS = frozenset(
    {"direct_topup", "gift_card", "game_key", "steam_topup", "steam_gift", "manual_service"}
)


class ProjectionInputError(ValueError):
    """An operator mapping is malformed or unsafe to expose."""


@dataclass(frozen=True)
class ApprovedOfferInput:
    """AuriX review record, with no supplier identifiers or supplier costs."""

    offer_id: str
    product_slug: str
    product_name: str
    fulfillment_kind: str
    denomination_label: str
    retail_amount_minor: int
    currency: str
    observed_at: datetime
    review_state: str = "approved"
    customer_visible: bool = True
    supplier_enabled: bool = True
    available: bool = True
    resale_authorized: bool = True
    region_rule: RegionRule = RegionRule()
    region_label: str | None = None
    region_dimension: RegionDimension | None = None
    required_input_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class PublicOffer:
    """Customer-safe offer representation; all IDs are AuriX-owned."""

    offer_id: str
    product_slug: str
    product_name: str
    fulfillment_kind: str
    denomination_label: str
    retail_amount_minor: int
    currency: str
    region: dict[str, str] | None
    required_input_labels: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "offer_id": self.offer_id,
            "product_slug": self.product_slug,
            "product_name": self.product_name,
            "fulfillment_kind": self.fulfillment_kind,
            "denomination_label": self.denomination_label,
            "retail_amount_minor": self.retail_amount_minor,
            "currency": self.currency,
            "region": None if self.region is None else dict(self.region),
            "required_input_labels": list(self.required_input_labels),
        }


def project_public_offers(
    offers: Iterable[ApprovedOfferInput],
    *,
    now: datetime,
    max_age: timedelta = DEFAULT_PROJECTION_MAX_AGE,
) -> tuple[PublicOffer, ...]:
    """Return only fresh, explicitly approved and customer-visible offers.

    Any malformed record raises rather than being partially exposed. Records
    with unknown rights, availability, supplier state, region policy, or stale
    observations are omitted. This is a projection gate, not an order gate;
    checkout must still run recipient-aware eligibility immediately before
    payment/provider submission.
    """

    _require_aware(now, "now")
    if max_age < timedelta(0):
        raise ProjectionInputError("maximum age cannot be negative")
    rows: list[PublicOffer] = []
    seen: set[str] = set()
    for offer in offers:
        if not _validate_input(offer, now=now, max_age=max_age):
            continue
        if not (
            offer.review_state == "approved"
            and offer.customer_visible
            and offer.supplier_enabled
            and offer.available
            and offer.resale_authorized
            and offer.region_rule.verified
        ):
            continue
        if offer.offer_id in seen:
            raise ProjectionInputError("duplicate AuriX offer id")
        seen.add(offer.offer_id)
        region = None
        if offer.region_rule.allow_all:
            region = {"type": "global", "label": offer.region_label or "Global"}
        else:
            if offer.region_dimension is None or offer.region_label is None:
                raise ProjectionInputError("approved regional offer is missing its AuriX region")
            region = {
                "type": offer.region_dimension.value,
                "label": offer.region_label,
            }
        rows.append(
            PublicOffer(
                offer_id=offer.offer_id,
                product_slug=offer.product_slug,
                product_name=offer.product_name,
                fulfillment_kind=offer.fulfillment_kind,
                denomination_label=offer.denomination_label,
                retail_amount_minor=offer.retail_amount_minor,
                currency=offer.currency,
                region=region,
                required_input_labels=offer.required_input_labels,
            )
        )
    return tuple(rows)


def _validate_input(offer: ApprovedOfferInput, *, now: datetime, max_age: timedelta) -> bool:
    if not isinstance(offer, ApprovedOfferInput):
        raise ProjectionInputError("offer record has unsupported type")
    for label, value in (
        ("offer id", offer.offer_id),
        ("product slug", offer.product_slug),
        ("product name", offer.product_name),
        ("denomination label", offer.denomination_label),
        ("currency", offer.currency),
    ):
        if not isinstance(value, str) or not value.strip() or len(value) > 160:
            raise ProjectionInputError(f"{label} is invalid")
    if offer.offer_id.strip() != offer.offer_id:
        raise ProjectionInputError("offer id has surrounding whitespace")
    if offer.fulfillment_kind not in _FULFILLMENT_KINDS:
        raise ProjectionInputError("fulfillment kind is unsupported")
    if isinstance(offer.retail_amount_minor, bool) or not isinstance(offer.retail_amount_minor, int):
        raise ProjectionInputError("retail amount must be an integer")
    if offer.retail_amount_minor < 0:
        raise ProjectionInputError("retail amount cannot be negative")
    if not offer.currency.isupper() or len(offer.currency) != 3:
        raise ProjectionInputError("currency must be an ISO-style uppercase code")
    observed = _aware_or_none(offer.observed_at)
    if observed is None or observed > now.astimezone(UTC):
        raise ProjectionInputError("offer observation time is invalid")
    if now.astimezone(UTC) - observed > max_age:
        return False
    if not isinstance(offer.region_rule, RegionRule):
        raise ProjectionInputError("region rule is invalid")
    if offer.region_rule.allow_all and offer.region_dimension is not None:
        raise ProjectionInputError("global offer must not carry a region dimension")
    if offer.region_label is not None and (
        not isinstance(offer.region_label, str) or not offer.region_label.strip()
    ):
        raise ProjectionInputError("region label is invalid")
    if offer.region_dimension is not None and not isinstance(offer.region_dimension, RegionDimension):
        raise ProjectionInputError("region dimension is invalid")
    if not isinstance(offer.required_input_labels, tuple) or not all(
        isinstance(label, str) and label.strip() for label in offer.required_input_labels
    ):
        raise ProjectionInputError("required input labels are invalid")
    return True


def _require_aware(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or _aware_or_none(value) is None:
        raise ProjectionInputError(f"{label} must be timezone-aware")


def _aware_or_none(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


__all__ = [
    "ApprovedOfferInput",
    "DEFAULT_PROJECTION_MAX_AGE",
    "ProjectionInputError",
    "PublicOffer",
    "project_public_offers",
]
