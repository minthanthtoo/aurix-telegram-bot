"""Paid-order subscription, wallet, and provisioning settlement."""

from __future__ import annotations

from typing import Any

from commerce_models import _new_id
from commerce_service_wallet_paid_approval_steps import (
    persist_paid_subscription,
    prepare_subscription_terms,
    settle_paid_funding,
)


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
    payment = context.payment
    repository = service.wallet_approvals
    terms = prepare_subscription_terms(repository, connection, order, starts_at)
    persist_paid_subscription(
        repository,
        connection,
        order=order,
        payment=payment,
        order_id=order_id,
        starts_at=starts_at,
        terms=terms,
    )
    payment_id = str(payment["id"])
    settle_paid_funding(
        repository,
        connection,
        context=context,
        order=order,
        order_id=order_id,
        payment_id=payment_id,
        starts_at=starts_at,
    )
    repository.queue_provisioning(
        connection,
        job_id=_new_id(),
        subscription_id=terms.subscription_id,
        next_attempt_at=terms.effective_start.isoformat(),
        created_at=terms.effective_start.isoformat(),
    )
    service._audit(
        connection,
        "order_approved",
        "order",
        order_id,
        "admin",
        str(admin_id),
        {"subscription_id": terms.subscription_id},
    )
    return terms.subscription_id
