"""Games-owned order state machine with no external side effects.

The module is deliberately persistence- and provider-independent. It gives the
future service one auditable transition vocabulary before payment/supplier
adapters are enabled. Every transition returns a new immutable order value.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import re
from typing import Any, Mapping


UTC = timezone.utc
_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")


class OrderState(str, Enum):
    DRAFT = "draft"
    AWAITING_PAYMENT = "awaiting_payment"
    PAID_RESERVED = "paid_reserved"
    FULFILLMENT_QUEUED = "fulfillment_queued"
    SUBMITTED = "submitted"
    PROCESSING = "processing"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNKNOWN = "unknown"
    RECONCILIATION = "reconciliation"
    REVIEW_REFUND = "review_refund"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"


class OrderTransitionError(ValueError):
    """A state transition or immutable snapshot is invalid."""


_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.DRAFT: frozenset({OrderState.AWAITING_PAYMENT, OrderState.CANCELLED}),
    OrderState.AWAITING_PAYMENT: frozenset({OrderState.PAID_RESERVED, OrderState.CANCELLED}),
    OrderState.PAID_RESERVED: frozenset({OrderState.FULFILLMENT_QUEUED, OrderState.REVIEW_REFUND}),
    OrderState.FULFILLMENT_QUEUED: frozenset({OrderState.SUBMITTED, OrderState.UNKNOWN, OrderState.REVIEW_REFUND}),
    # A supplier webhook may report completion before a local polling worker
    # observes an intermediate `processing` response.  The direct edge is
    # explicit and audited; it is not a blind retry.
    OrderState.SUBMITTED: frozenset({OrderState.PROCESSING, OrderState.DELIVERED, OrderState.UNKNOWN, OrderState.FAILED, OrderState.RECONCILIATION}),
    OrderState.PROCESSING: frozenset({OrderState.DELIVERED, OrderState.FAILED, OrderState.UNKNOWN, OrderState.RECONCILIATION}),
    # A provider refund or late failure after local delivery must enter
    # reconciliation for an auditable review; it must not silently rewind to
    # processing or charge a customer a second time.
    OrderState.DELIVERED: frozenset({OrderState.RECONCILIATION}),
    OrderState.FAILED: frozenset({OrderState.REVIEW_REFUND, OrderState.RECONCILIATION}),
    OrderState.UNKNOWN: frozenset({OrderState.RECONCILIATION}),
    # Reconciliation may resolve the original attempt or route to review; it
    # never silently loops back to SUBMITTED. A separately reviewed retry
    # would need an explicit new fulfillment attempt/idempotency decision.
    OrderState.RECONCILIATION: frozenset(
        {OrderState.PROCESSING, OrderState.DELIVERED, OrderState.FAILED, OrderState.REVIEW_REFUND}
    ),
    OrderState.REVIEW_REFUND: frozenset({OrderState.REFUNDED}),
    OrderState.REFUNDED: frozenset(),
    OrderState.CANCELLED: frozenset(),
}


@dataclass(frozen=True)
class OrderSnapshot:
    """Frozen customer-facing order facts; no supplier IDs or raw credentials."""

    offer_id: str
    product_slug: str
    fulfillment_kind: str
    amount_minor: int
    currency: str
    recipient_fingerprint: str

    def __post_init__(self) -> None:
        _require_id(self.offer_id, "offer_id")
        _require_id(self.product_slug, "product_slug")
        _require_id(self.fulfillment_kind, "fulfillment_kind")
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise OrderTransitionError("amount_minor must be an integer")
        if self.amount_minor < 0:
            raise OrderTransitionError("amount_minor cannot be negative")
        if not isinstance(self.currency, str) or not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise OrderTransitionError("currency must be an uppercase 3-letter code")
        _require_id(self.recipient_fingerprint, "recipient_fingerprint")

    def canonical_json(self) -> str:
        return json.dumps(
            {
                "amount_minor": self.amount_minor,
                "currency": self.currency,
                "fulfillment_kind": self.fulfillment_kind,
                "offer_id": self.offer_id,
                "product_slug": self.product_slug,
                "recipient_fingerprint": self.recipient_fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class OrderEvent:
    at: datetime
    from_state: OrderState
    to_state: OrderState
    reason: str


@dataclass(frozen=True)
class GameOrder:
    order_id: str
    customer_id: str
    customer_idempotency_key: str
    provider_idempotency_key: str
    snapshot: OrderSnapshot
    state: OrderState = OrderState.DRAFT
    provider_order_id: str | None = None
    provider_status: str | None = None
    events: tuple[OrderEvent, ...] = ()

    def __post_init__(self) -> None:
        for value, label in (
            (self.order_id, "order_id"),
            (self.customer_id, "customer_id"),
            (self.customer_idempotency_key, "customer_idempotency_key"),
            (self.provider_idempotency_key, "provider_idempotency_key"),
        ):
            _require_id(value, label)
        if not isinstance(self.state, OrderState):
            raise OrderTransitionError("state is invalid")
        if self.provider_order_id is not None:
            _require_id(self.provider_order_id, "provider_order_id")


def create_order(
    *,
    order_id: str,
    customer_id: str,
    customer_idempotency_key: str,
    snapshot: OrderSnapshot,
) -> GameOrder:
    """Create a DRAFT order and derive a stable provider idempotency key.

    The provider key is derived from the AuriX order ID and customer key; the
    function does not submit anything or claim that a provider accepted it.
    """

    _require_id(order_id, "order_id")
    _require_id(customer_id, "customer_id")
    _require_id(customer_idempotency_key, "customer_idempotency_key")
    provider_key = "aurix-game:" + hashlib.sha256(
        f"{order_id}\0{customer_idempotency_key}".encode("utf-8")
    ).hexdigest()
    return GameOrder(
        order_id=order_id,
        customer_id=customer_id,
        customer_idempotency_key=customer_idempotency_key,
        provider_idempotency_key=provider_key,
        snapshot=snapshot,
    )


def transition_order(
    order: GameOrder,
    target: OrderState,
    *,
    at: datetime,
    reason: str,
    provider_order_id: str | None = None,
    provider_status: str | None = None,
) -> GameOrder:
    """Apply one legal transition and append an immutable audit event."""

    if not isinstance(order, GameOrder) or not isinstance(target, OrderState):
        raise OrderTransitionError("order or target state is invalid")
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise OrderTransitionError("transition time must be timezone-aware")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 240:
        raise OrderTransitionError("transition reason is invalid")
    if target not in _TRANSITIONS[order.state]:
        raise OrderTransitionError(f"illegal transition: {order.state.value} -> {target.value}")
    if provider_order_id is not None:
        _require_id(provider_order_id, "provider_order_id")
        if order.provider_order_id is not None and provider_order_id != order.provider_order_id:
            raise OrderTransitionError("accepted supplier order identity cannot change")
    event = OrderEvent(
        at=at.astimezone(UTC),
        from_state=order.state,
        to_state=target,
        reason=reason.strip(),
    )
    return replace(
        order,
        state=target,
        provider_order_id=provider_order_id or order.provider_order_id,
        provider_status=provider_status if provider_status is not None else order.provider_status,
        events=order.events + (event,),
    )


def idempotency_fingerprint(order: GameOrder) -> str:
    """Return a stable digest for auditing duplicate create-order requests."""

    payload = "\0".join(
        (
            order.customer_id,
            order.customer_idempotency_key,
            order.snapshot.canonical_json(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_id(value: object, label: str) -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise OrderTransitionError(f"{label} is invalid")


__all__ = [
    "GameOrder",
    "OrderEvent",
    "OrderSnapshot",
    "OrderState",
    "OrderTransitionError",
    "create_order",
    "idempotency_fingerprint",
    "transition_order",
]
