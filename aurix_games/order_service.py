"""Prepare Games orders up to the payment boundary.

This service deliberately stops at ``AWAITING_PAYMENT``. It composes the
reviewed catalog/recipient eligibility gate with the durable order repository,
but has no payment connector and no supplier-order capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hmac

from .catalog_projection import ApprovedOfferInput
from .eligibility import (
    DEFAULT_CATALOG_MAX_AGE,
    DEFAULT_VALIDATION_MAX_AGE,
    EligibilityDecision,
    OfferEligibilityEvidence,
    RecipientEligibilityEvidence,
    evaluate_offer_eligibility,
)
from .order_repository import GameOrderRepository
from .order_state import (
    GameOrder,
    OrderSnapshot,
    OrderState,
    OrderTransitionError,
    create_order,
)


class OrderPreparationError(ValueError):
    """The order cannot safely cross the pre-payment boundary."""

    def __init__(self, message: str, *, decision: EligibilityDecision | None = None) -> None:
        super().__init__(message)
        self.decision = decision


@dataclass(frozen=True)
class PreparedOrder:
    order: GameOrder
    decision: EligibilityDecision


def prepare_order(
    repository: GameOrderRepository,
    *,
    order_id: str,
    customer_id: str,
    customer_idempotency_key: str,
    offer: ApprovedOfferInput,
    recipient: RecipientEligibilityEvidence,
    recipient_fingerprint: str,
    now: datetime,
    catalog_max_age: timedelta = DEFAULT_CATALOG_MAX_AGE,
    validation_max_age: timedelta = DEFAULT_VALIDATION_MAX_AGE,
) -> PreparedOrder:
    """Validate and persist an order awaiting payment; never perform effects."""

    if not isinstance(recipient_fingerprint, str) or not recipient_fingerprint.strip():
        raise OrderPreparationError("recipient fingerprint is required")
    evidence = OfferEligibilityEvidence(
        fulfillment_kind=offer.fulfillment_kind,
        review_state=offer.review_state,
        supplier_enabled=offer.supplier_enabled,
        available=offer.available,
        resale_authorized=offer.resale_authorized,
        catalog_observed_at=offer.observed_at,
        required_input_fields=frozenset(offer.required_input_labels),
        region_rule=offer.region_rule,
        recipient_validation_required=offer.fulfillment_kind == "direct_topup"
        or recipient.validation_valid is not None
        or recipient.validation_checked_at is not None,
    )
    if evidence.recipient_validation_required and (
        not isinstance(recipient.recipient_binding, str)
        or not hmac.compare_digest(
            recipient.recipient_binding.encode("utf-8"), recipient_fingerprint.strip().encode("utf-8")
        )
    ):
        raise OrderPreparationError("recipient validation does not match the order fingerprint")
    decision = evaluate_offer_eligibility(
        evidence,
        recipient,
        now=now,
        catalog_max_age=catalog_max_age,
        validation_max_age=validation_max_age,
    )
    if not decision.eligible:
        raise OrderPreparationError(
            "offer or recipient failed eligibility checks",
            decision=decision,
        )
    try:
        snapshot = OrderSnapshot(
            offer_id=offer.offer_id,
            product_slug=offer.product_slug,
            fulfillment_kind=offer.fulfillment_kind,
            amount_minor=offer.retail_amount_minor,
            currency=offer.currency,
            recipient_fingerprint=recipient_fingerprint.strip(),
        )
        draft = create_order(
            order_id=order_id,
            customer_id=customer_id,
            customer_idempotency_key=customer_idempotency_key,
            snapshot=snapshot,
        )
        saved = repository.create(draft, created_at=now)
        if saved.state == OrderState.DRAFT:
            saved = repository.transition(
                saved.order_id,
                OrderState.AWAITING_PAYMENT,
                at=now,
                reason="eligibility passed; awaiting customer payment",
            )
        elif saved.state != OrderState.AWAITING_PAYMENT:
            # An idempotent retry of an order already beyond this boundary must
            # replay its current state, never rewind or create a second order.
            return PreparedOrder(saved, decision)
        return PreparedOrder(saved, decision)
    except (OrderTransitionError, ValueError) as exc:
        raise OrderPreparationError("order snapshot or persistence validation failed") from exc


__all__ = ["OrderPreparationError", "PreparedOrder", "prepare_order"]
