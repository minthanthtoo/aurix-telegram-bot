"""Wallet credit and order reservation workflows."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from commerce_models import ApprovalResult, CommerceError, UTC, _new_id, _normalize_reference, _now_text

def credit_wallet(
    self,
    telegram_id: int,
    amount_minor: int,
    reference_id: str,
    admin_id: int,
    currency: str = "MMK",
) -> str:
    if amount_minor <= 0:
        raise CommerceError("Wallet credit must be positive")
    now_text = _now_text()
    idem = f"credit:{reference_id}"
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        self._ensure_user(connection, telegram_id, "")
        self.wallet_approvals.ensure_wallet(
            connection, telegram_id=telegram_id, currency=currency, now_text=now_text
        )
        credited = self.wallet_approvals.credit_once(
            connection,
            entry_id=_new_id(),
            telegram_id=telegram_id,
            amount_minor=amount_minor,
            currency=currency,
            reference_type="payment",
            reference_id=reference_id,
            idempotency_key=idem,
            now_text=now_text,
        )
        if not credited:
            return "already_credited"
        self._audit(
            connection,
            "wallet_credited",
            "user",
            str(telegram_id),
            "admin",
            str(admin_id),
            {"amount_minor": amount_minor, "reference_id": reference_id},
        )
    return "credited"
def pay_order_with_wallet(
    self, telegram_id: int, order_id: str, now: datetime | None = None
) -> str:
    """Reserve wallet funds and submit an idempotent wallet payment."""
    now_text = _now_text(now)
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        self._lock_order(connection, order_id)
        order = self.orders.get_owned(connection, order_id, telegram_id)
        if order is None:
            raise CommerceError("Order not found")
        if str(order["plan_code"]) == "wallet_topup":
            raise CommerceError("A wallet cannot be topped up from the same wallet")
        self._assert_no_active_promo(connection, telegram_id)
        if order["status"] == "approved":
            return "already_approved"
        if order["status"] not in ("awaiting_payment", "payment_submitted"):
            raise CommerceError("Order is not open for wallet payment")
        evidence = self.payments.latest_evidence_review(connection, order_id)
        if evidence is not None and str(evidence["review_status"] or "pending") != "rejected":
            raise CommerceError(
                "This order already has a receipt; wallet payment cannot be combined"
            )
        payments = self.payments.payments_for_order(connection, order_id)
        if any(
            str(item["provider"] or "").lower() != "wallet"
            and str(item["status"] or "") in ("submitted", "verified")
            for item in payments
        ):
            raise CommerceError("A receipt payment is already attached to this order")
        self._ensure_user(connection, telegram_id, "")
        idem = f"reserve:{order_id}"
        already_reserved = self.wallet_approvals.ledger_entry_exists(connection, idem)
        reserved = self.wallet_approvals.reserve_once(
            connection,
            ledger_entry_id=_new_id(),
            reservation_id=_new_id(),
            telegram_id=telegram_id,
            order_id=order_id,
            amount_minor=int(order["amount_minor"]),
            currency=str(order["currency"]),
            idempotency_key=idem,
            now_text=now_text,
        )
        if not reserved:
            raise CommerceError("Insufficient wallet balance")
        if already_reserved:
            return "already_reserved"
        self.orders.insert_payment(
            connection,
            payment_id=_new_id(),
            order_id=order_id,
            provider="wallet",
            provider_reference=f"wallet:{order_id}",
            normalized_reference=_normalize_reference(f"wallet:{order_id}"),
            submitted_at=now_text,
        )
        self.orders.mark_wallet_payment_submitted(connection, order_id)
        self._audit(
            connection,
            "wallet_reserved",
            "order",
            order_id,
            "customer",
            str(telegram_id),
            {"amount_minor": order["amount_minor"]},
        )
    return "reserved"
