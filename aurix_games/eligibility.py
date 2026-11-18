"""Fail-closed sale eligibility rules for Games catalog offers.

This module evaluates already-normalized AuriX evidence. It does not infer
publisher rules from supplier labels and performs no network, payment, or
supplier-order operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hmac


UTC = timezone.utc
DEFAULT_CATALOG_MAX_AGE = timedelta(minutes=15)
DEFAULT_VALIDATION_MAX_AGE = timedelta(minutes=2)


class RegionDimension(str, Enum):
    """The account-side meaning of a normalized region token."""

    GAME_SERVER = "game_server"
    STORE_COUNTRY = "store_country"
    ACTIVATION_COUNTRY = "activation_country"


SUPPORTED_FULFILLMENT_KINDS = frozenset(
    {"direct_topup", "gift_card", "game_key", "steam_topup", "steam_gift", "manual_service"}
)


@dataclass(frozen=True)
class RegionValue:
    dimension: RegionDimension
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, RegionDimension):
            raise ValueError("region dimension is unsupported")
        normalized = _region_token(self.value)
        if normalized is None:
            raise ValueError("region value is required")
        object.__setattr__(self, "value", normalized)


@dataclass(frozen=True)
class RegionRule:
    """A reviewed allow-all or allow-list mapped to recipient-side regions.

    Supplier labels are deliberately not accepted as `dimension`; an operator
    must first map current supplier/publisher evidence into an account-side
    dimension and approve that mapping.
    """

    verified: bool = False
    allow_all: bool = False
    dimension: RegionDimension | None = None
    allowed_values: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.verified, bool) or not isinstance(self.allow_all, bool):
            raise ValueError("region rule flags must be boolean")
        if self.dimension is not None and not isinstance(self.dimension, RegionDimension):
            raise ValueError("region dimension is unsupported")
        if self.allowed_values is not None:
            normalized = frozenset(
                token
                for raw in self.allowed_values
                if (token := _region_token(raw)) is not None
            )
            if len(normalized) != len(self.allowed_values):
                raise ValueError("region allow-list contains an empty value")
            object.__setattr__(self, "allowed_values", normalized)
        if self.allow_all and self.allowed_values is not None:
            raise ValueError("region rule cannot combine allow-all with an allow-list")
        if not self.allow_all and self.allowed_values is not None and self.dimension is None:
            raise ValueError("region allow-list requires an account-side dimension")


@dataclass(frozen=True)
class OfferEligibilityEvidence:
    """AuriX review and freshness evidence for one normalized offer."""

    fulfillment_kind: str
    review_state: str
    supplier_enabled: bool | None
    available: bool | None
    resale_authorized: bool | None
    catalog_observed_at: datetime | None
    required_input_fields: frozenset[str] = frozenset()
    region_rule: RegionRule = RegionRule()
    recipient_validation_required: bool = False


@dataclass(frozen=True)
class RecipientEligibilityEvidence:
    """Minimum recipient evidence needed for a pre-order eligibility check."""

    fulfillment_kind: str
    supplied_field_names: frozenset[str] = frozenset()
    region: RegionValue | None = None
    validation_valid: bool | None = None
    validation_checked_at: datetime | None = None
    recipient_binding: str | None = None
    validation_binding: str | None = None


@dataclass(frozen=True)
class EligibilityDecision:
    eligible: bool
    reasons: tuple[str, ...]


def evaluate_offer_eligibility(
    offer: OfferEligibilityEvidence,
    recipient: RecipientEligibilityEvidence,
    *,
    now: datetime,
    catalog_max_age: timedelta = DEFAULT_CATALOG_MAX_AGE,
    validation_max_age: timedelta = DEFAULT_VALIDATION_MAX_AGE,
) -> EligibilityDecision:
    """Return a deterministic decision; every missing/ambiguous gate denies.

    The caller must still enforce customer authorization, price confirmation,
    fraud/payment rules, and the final provider response. The function is a
    pre-submit eligibility guard only.
    """

    _require_aware_datetime(now, "now")
    if catalog_max_age < timedelta(0) or validation_max_age < timedelta(0):
        raise ValueError("maximum age cannot be negative")
    current = now.astimezone(UTC)
    reasons: list[str] = []

    if offer.review_state != "approved":
        reasons.append("offer_not_approved")
    if offer.supplier_enabled is not True:
        reasons.append("supplier_not_enabled")
    if offer.available is not True:
        reasons.append("offer_availability_unknown" if offer.available is None else "offer_unavailable")
    if offer.resale_authorized is not True:
        reasons.append("resale_rights_unverified")
    if offer.fulfillment_kind != recipient.fulfillment_kind:
        reasons.append("fulfillment_kind_mismatch")
    if (
        offer.fulfillment_kind not in SUPPORTED_FULFILLMENT_KINDS
        or recipient.fulfillment_kind not in SUPPORTED_FULFILLMENT_KINDS
    ):
        reasons.append("fulfillment_kind_unsupported")

    if not isinstance(offer.catalog_observed_at, datetime):
        reasons.append("catalog_observation_missing")
    else:
        catalog_time = _aware_time_or_none(offer.catalog_observed_at)
        if catalog_time is None or catalog_time > current:
            reasons.append("catalog_observation_invalid")
        elif current - catalog_time > catalog_max_age:
            reasons.append("catalog_observation_stale")

    missing_fields = offer.required_input_fields - recipient.supplied_field_names
    if missing_fields:
        reasons.append("recipient_fields_missing")

    if offer.recipient_validation_required:
        if recipient.validation_valid is not True:
            reasons.append("recipient_not_validated")
        if not _nonempty_binding(recipient.recipient_binding) or not _nonempty_binding(
            recipient.validation_binding
        ):
            reasons.append("recipient_validation_binding_missing")
        elif not hmac.compare_digest(
            recipient.recipient_binding.encode("utf-8"), recipient.validation_binding.encode("utf-8")
        ):
            reasons.append("recipient_validation_binding_mismatch")
        if not isinstance(recipient.validation_checked_at, datetime):
            reasons.append("recipient_validation_time_missing")
        else:
            validation_time = _aware_time_or_none(recipient.validation_checked_at)
            if validation_time is None or validation_time > current:
                reasons.append("recipient_validation_time_invalid")
            elif current - validation_time > validation_max_age:
                reasons.append("recipient_validation_stale")

    _append_region_reasons(offer.region_rule, recipient.region, reasons)
    return EligibilityDecision(eligible=not reasons, reasons=tuple(reasons))


def _append_region_reasons(
    rule: RegionRule, recipient_region: RegionValue | None, reasons: list[str]
) -> None:
    if not rule.verified:
        reasons.append("region_policy_unverified")
        return
    if rule.allow_all:
        return
    if rule.dimension is None or rule.allowed_values is None:
        reasons.append("region_policy_incomplete")
        return
    if recipient_region is None:
        reasons.append("recipient_region_missing")
        return
    if recipient_region.dimension != rule.dimension:
        reasons.append("region_dimension_mismatch")
        return
    if recipient_region.value not in rule.allowed_values:
        reasons.append("recipient_region_not_allowed")


def _region_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned.casefold()


def _nonempty_binding(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _require_aware_datetime(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or _aware_time_or_none(value) is None:
        raise ValueError(f"{label} must be timezone-aware")


def _aware_time_or_none(value: datetime) -> datetime | None:
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


__all__ = [
    "DEFAULT_CATALOG_MAX_AGE",
    "DEFAULT_VALIDATION_MAX_AGE",
    "EligibilityDecision",
    "OfferEligibilityEvidence",
    "RecipientEligibilityEvidence",
    "RegionDimension",
    "RegionRule",
    "RegionValue",
    "SUPPORTED_FULFILLMENT_KINDS",
    "evaluate_offer_eligibility",
]
