"""Paid-order subscription, wallet, and provisioning settlement."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from commerce_models import CommerceError, _new_id


def settle_paid_order(
    service: Any,
    connection: Any,
    context: Any,
    *,
    order_id: str,
    admin_id: int,
    starts_at: str,
) -> str:
    """Persist a paid subscription and atomically reserve its provisioning."""
    order = context.order
    evidence = context.evidence
    payment = context.payment
    repository = service.wallet_approvals
    plan = repository.plan(connection, str(order["plan_code"]))
    if plan is None:
        raise CommerceError("Plan record is missing")
    duration_days = int(order["duration_days_snapshot"] or plan["duration_days"])
    plan_name = str(order["plan_name"] or plan["name"])
    quota_bytes = (
        order["quota_bytes_snapshot"]
        if order["quota_bytes_snapshot"] is not None
        else plan["quota_bytes"]
    )
    effective_start = datetime.fromisoformat(starts_at)
    expires_at = (effective_start + timedelta(days=duration_days)).isoformat()
    subscription_id = _new_id()
    if payment["status"] == "submitted":
        repository.mark_payment_verified(connection, str(payment["id"]), starts_at)
    repository.mark_order_approved(connection, order_id, starts_at)
    repository.create_subscription(
        connection,
        subscription_id=subscription_id,
        order_id=order_id,
        telegram_id=int(order["telegram_id"]),
        plan_code=str(order["plan_code"]),
        starts_at=effective_start.isoformat(),
        expires_at=expires_at,
        plan_name=plan_name,
        quota_bytes=quota_bytes,
        duration_days=duration_days,
        server_id=order["server_id"] if "server_id" in order.keys() else None,
    )
    repository.ensure_wallet(
        connection,
        telegram_id=int(order["telegram_id"]),
        currency=str(order["currency"]),
        now_text=starts_at,
    )
    payment_id = str(payment["id"])
    credit_amount = int(order["amount_minor"])
    if evidence is not None:
        if str(evidence["verified_currency"]).upper() != str(order["currency"]).upper():
            raise CommerceError("Verified receipt currency does not match the order")
        credit_amount = int(evidence["verified_amount_minor"])
    if not context.wallet_payment:
        repository.credit_once(
            connection,
            entry_id=_new_id(),
            telegram_id=int(order["telegram_id"]),
            amount_minor=credit_amount,
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
    repository.queue_provisioning(
        connection,
        job_id=_new_id(),
        subscription_id=subscription_id,
        next_attempt_at=effective_start.isoformat(),
        created_at=effective_start.isoformat(),
    )
    service._audit(
        connection,
        "order_approved",
        "order",
        order_id,
        "admin",
        str(admin_id),
        {"subscription_id": subscription_id},
    )
    return subscription_id
