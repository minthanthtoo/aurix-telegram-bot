"""Order rejection, refund, and compensation workflows."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from commerce_models import ApprovalResult, CommerceError, UTC, _new_id, _normalize_reference, _now_text

def reject_order(self, order_id: str, admin_id: int, now: datetime | None = None) -> str:
    rejected_at = _now_text(now)
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        self._lock_order(connection, order_id)
        order = self.orders.get(connection, order_id)
        if order is None:
            raise CommerceError("Order not found")
        if order["status"] == "rejected":
            return "already_rejected"
        if order["status"] == "approved":
            raise CommerceError("Approved order cannot be rejected here")
        verified_payment = self.payments.verified_payment(connection, order_id)
        verified_receipt = self.payments.verified_evidence(connection, order_id)
        if verified_payment is not None or verified_receipt is not None:
            raise CommerceError("Verified payment must be refunded instead of rejected")
        wallet_payment = self.payments.latest_payment(connection, order_id)
        self.wallet_approvals.reject_order(
            connection,
            order_id=order_id,
            order=order,
            admin_id=admin_id,
            rejected_at=rejected_at,
            wallet_payment=wallet_payment is not None and wallet_payment["provider"] == "wallet",
        )
        self._audit(
            connection,
            "order_rejected",
            "order",
            order_id,
            "admin",
            str(admin_id),
        )
        self._queue_staff_notification(
            connection,
            "rejected",
            f"order-{order_id}",
            "❌ ORDER REJECTED\n\n"
            f"Order: #{order_id[:8]}\n"
            f"Customer: tg:{order['telegram_id']}\n"
            f"Plan: {order['plan_name'] or order['plan_code']}\n"
            f"Amount: {int(order['amount_minor']):,} {order['currency']}\n"
            f"Reviewed by: tg:{admin_id}",
            rejected_at,
        )
    return "rejected"
def refund_order(
    self,
    order_id: str,
    admin_id: int,
    reason: str = "refunded by admin",
    now: datetime | None = None,
) -> str:
    """Record an idempotent wallet compensation and close paid access.

    This does not claim that an external bank transfer was reversed.  It
    credits the customer's AuriX wallet as a compensating ledger event;
    the operator remains responsible for any off-platform payout.
    """
    refunded_at = _now_text(now)
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        self._lock_order(connection, order_id)
        order = self.orders.get(connection, order_id)
        if order is None:
            raise CommerceError("Order not found")
        if str(order["plan_code"]) == "wallet_topup":
            raise CommerceError(
                "Wallet top-up refunds require manual off-platform reconciliation"
            )
        if str(order["refund_status"] or "none") == "refunded":
            return "already_refunded"
        payment = self.payments.latest_eligible_payment(connection, order_id)
        if payment is None or payment["status"] != "verified":
            raise CommerceError("Only a verified payment can be refunded")
        verified_evidence = self.payments.verified_evidence_amount(connection, order_id)
        amount = int(order["amount_minor"])
        if str(payment["provider"] or "").lower() != "wallet" and verified_evidence is not None:
            amount = max(amount, int(verified_evidence["verified_amount_minor"] or amount))
        now_text = refunded_at
        try:
            self.wallet_approvals.refund_order(
                connection,
                order_id=order_id,
                order=order,
                amount=amount,
                reason=reason or "admin refund",
                admin_id=admin_id,
                refunded_at=now_text,
            )
        except ValueError as exc:
            raise CommerceError(str(exc)) from exc
        self._audit(
            connection,
            "order_refunded",
            "order",
            order_id,
            "admin",
            str(admin_id),
            {"amount_minor": amount, "currency": str(order["currency"]).upper(), "reason": (reason or "")[:500]},
        )
    return "refunded"
