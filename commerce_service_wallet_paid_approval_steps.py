"""Subscription terms and wallet funding phases for paid-order approval."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from commerce_models import CommerceError, _new_id


@dataclass(frozen=True, slots=True)
class PaidSubscriptionTerms:
    subscription_id: str
    effective_start: datetime
    expires_at: str
    plan_name: str
    quota_bytes: int | None
    duration_days: int


def prepare_subscription_terms(
    repository: Any, connection: Any, order: Any, starts_at: str
) -> PaidSubscriptionTerms:
    """Resolve immutable plan snapshots and subscription timing."""
    plan = repository.plan(connection, str(order["plan_code"]))
    if plan is None:
        raise CommerceError("Plan record is missing")
    duration_days = int(order["duration_days_snapshot"] or plan["duration_days"])
    effective_start = datetime.fromisoformat(starts_at)
    return PaidSubscriptionTerms(
        subscription_id=_new_id(),
        effective_start=effective_start,
        expires_at=(effective_start + timedelta(days=duration_days)).isoformat(),
        plan_name=str(order["plan_name"] or plan["name"]),
        quota_bytes=(
            order["quota_bytes_snapshot"]
            if order["quota_bytes_snapshot"] is not None
            else plan["quota_bytes"]
        ),
        duration_days=duration_days,
    )


def persist_paid_subscription(
    repository: Any,
    connection: Any,
    *,
    order: Any,
    payment: Any,
    order_id: str,
    starts_at: str,
    terms: PaidSubscriptionTerms,
) -> None:
    """Mark the payment/order and create the pending subscription."""
    if payment["status"] == "submitted":
        repository.mark_payment_verified(connection, str(payment["id"]), starts_at)
    repository.mark_order_approved(connection, order_id, starts_at)
    repository.create_subscription(
        connection,
        subscription_id=terms.subscription_id,
        order_id=order_id,
        telegram_id=int(order["telegram_id"]),
        plan_code=str(order["plan_code"]),
        starts_at=terms.effective_start.isoformat(),
        expires_at=terms.expires_at,
        plan_name=terms.plan_name,
        quota_bytes=terms.quota_bytes,
        duration_days=terms.duration_days,
        server_id=order["server_id"] if "server_id" in order.keys() else None,
    )


def settle_paid_funding(
    repository: Any,
    connection: Any,
    *,
    context: Any,
    order: Any,
    order_id: str,
    payment_id: str,
    starts_at: str,
) -> None:
    """Credit verified funds when needed, then capture the order reservation."""
    repository.ensure_wallet(
        connection,
        telegram_id=int(order["telegram_id"]),
        currency=str(order["currency"]),
        now_text=starts_at,
    )
    if not context.wallet_payment:
        repository.credit_once(
            connection,
            entry_id=_new_id(),
            telegram_id=int(order["telegram_id"]),
            amount_minor=_verified_credit_amount(context, order),
            currency=str(order["currency"]),
            reference_type="payment",
            reference_id=payment_id,
            idempotency_key=f"credit:{payment_id}",
            now_text=starts_at,
        )
        if not repository.reserve_once(
            connection,
            ledger_entry_id=_new_id(),
            reservation_id=_new_id(),
            telegram_id=int(order["telegram_id"]),
            order_id=order_id,
            amount_minor=int(order["amount_minor"]),
            currency=str(order["currency"]),
            idempotency_key=f"reserve:{order_id}",
            now_text=starts_at,
        ):
            raise CommerceError("Verified payment credit is insufficient for this order")
    repository.capture_once(
        connection,
        ledger_entry_id=_new_id(),
        reservation_id=_new_id(),
        telegram_id=int(order["telegram_id"]),
        order_id=order_id,
        amount_minor=int(order["amount_minor"]),
        currency=str(order["currency"]),
        idempotency_key=f"capture:{order_id}",
        now_text=starts_at,
    )


def _verified_credit_amount(context: Any, order: Any) -> int:
    """Return the verified receipt amount or the order amount for wallet payments."""
    evidence = context.evidence
    if evidence is None:
        return int(order["amount_minor"])
    if str(evidence["verified_currency"]).upper() != str(order["currency"]).upper():
        raise CommerceError("Verified receipt currency does not match the order")
    return int(evidence["verified_amount_minor"])
