"""Validation and commit stages for wallet-funded order approval."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from commerce_models import ApprovalResult, CommerceError, _new_id


@dataclass(frozen=True, slots=True)
class ApprovalContext:
    order: Any
    evidence: Any
    wallet_reservation: Any
    payment: Any
    is_wallet_topup: bool
    wallet_payment: bool


def load_approval_context(
    service: Any, connection: Any, order_id: str
) -> ApprovalContext | ApprovalResult:
    repository = service.wallet_approvals
    order = service.orders.get(connection, order_id)
    if order is None:
        raise CommerceError("Order not found")
    is_wallet_topup = str(order["plan_code"]) == "wallet_topup"
    if not is_wallet_topup:
        service._assert_no_active_promo(connection, int(order["telegram_id"]))
    if order["status"] == "approved":
        if is_wallet_topup:
            return ApprovalResult(order_id, f"wallet:{order['telegram_id']}", "already_credited")
        subscription_id = repository.subscription_id_for_order(connection, order_id)
        if subscription_id is None:
            raise CommerceError("Approved order has no subscription record")
        return ApprovalResult(order_id, subscription_id, "already_approved")
    if order["status"] != "payment_submitted":
        raise CommerceError("Order has no submitted payment for review")
    evidence = service.payments.latest_evidence_for_approval(connection, order_id)
    wallet_reservation = repository.wallet_reservation(connection, order_id)
    if evidence is not None and evidence["review_status"] != "verified":
        if wallet_reservation is None or wallet_reservation["status"] != "reserved":
            raise CommerceError("Receipt must be verified against the receiving account first")
    if (
        evidence is not None
        and service.receipt_storage_required
        and str(evidence["storage_status"] or "") != "stored"
    ):
        raise CommerceError("Receipt image must be stored before approval")
    payment = service.payments.latest_eligible_payment(connection, order_id)
    if payment is None:
        raise CommerceError("Payment record is missing")
    wallet_payment = str(payment["provider"] or "") == "wallet"
    if evidence is not None and payment["status"] != "verified":
        raise CommerceError("Receipt payment has not been verified")
    if wallet_payment and (
        wallet_reservation is None
        or wallet_reservation["status"] != "reserved"
        or int(wallet_reservation["amount_minor"]) < int(order["amount_minor"])
        or str(wallet_reservation["currency"]).upper() != str(order["currency"]).upper()
    ):
        raise CommerceError("Wallet reservation is missing or no longer valid")
    if evidence is None and not wallet_payment and not service.allow_legacy_text_approval:
        raise CommerceError("Verified receipt evidence is required before approval")
    if evidence is not None and wallet_payment:
        raise CommerceError("An order cannot combine a wallet payment and receipt evidence")
    return ApprovalContext(order, evidence, wallet_reservation, payment, is_wallet_topup, wallet_payment)


def approve_wallet_topup(
    service: Any, connection: Any, context: ApprovalContext, *, order_id: str, admin_id: int, starts_at: str
) -> ApprovalResult:
    order = context.order
    evidence = context.evidence
    payment = context.payment
    repository = service.wallet_approvals
    if context.wallet_payment:
        raise CommerceError("A wallet cannot be topped up from the same wallet")
    if evidence is None or evidence["review_status"] != "verified":
        raise CommerceError("Verified receipt evidence is required for a wallet top-up")
    if int(evidence["verified_amount_minor"] or 0) != int(order["amount_minor"]):
        raise CommerceError("Wallet top-up receipt amount must match exactly")
    if str(evidence["verified_currency"] or "").upper() != str(order["currency"]).upper():
        raise CommerceError("Wallet top-up receipt currency does not match")
    payment_id = str(payment["id"])
    repository.ensure_wallet(
        connection,
        telegram_id=int(order["telegram_id"]),
        currency=str(order["currency"]),
        now_text=starts_at,
    )
    repository.credit_once(
        connection,
        entry_id=_new_id(),
        telegram_id=int(order["telegram_id"]),
        amount_minor=int(order["amount_minor"]),
        currency=str(order["currency"]),
        reference_type="payment",
        reference_id=payment_id,
        idempotency_key=f"credit:{payment_id}",
        now_text=starts_at,
    )
    repository.mark_order_approved(connection, order_id, starts_at)
    repository.queue_notification(
        connection,
        notification_id=_new_id(),
        dedupe_key=f"wallet-topup-approved:{order_id}",
        telegram_id=int(order["telegram_id"]),
        kind="wallet_topup_approved",
        text=f"✅ Wallet top-up approved: {int(order['amount_minor']):,} {order['currency']}.",
        now_text=starts_at,
    )
    service._audit(
        connection,
        "wallet_topup_approved",
        "order",
        order_id,
        "admin",
        str(admin_id),
        {"amount_minor": int(order["amount_minor"]), "payment_id": payment_id},
    )
    return ApprovalResult(order_id, f"wallet:{order['telegram_id']}", "wallet_credited")


def approve_paid_order(
    service: Any, connection: Any, context: ApprovalContext, *, order_id: str, admin_id: int, starts_at: str
) -> str:
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
