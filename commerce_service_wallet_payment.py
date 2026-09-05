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
        connection.execute(
            """INSERT INTO wallets (telegram_id, currency, balance_minor, created_at, updated_at)
               VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
            (telegram_id, currency, now_text, now_text),
        )
        existing = connection.execute(
            "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (idem,)
        ).fetchone()
        if existing is not None:
            return "already_credited"
        connection.execute(
            "UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ? WHERE telegram_id = ?",
            (amount_minor, now_text, telegram_id),
        )
        connection.execute(
            """INSERT INTO wallet_ledger
               (id, telegram_id, kind, amount_minor, currency, reference_type,
                reference_id, idempotency_key, created_at)
               VALUES (?, ?, 'credit', ?, ?, 'payment', ?, ?, ?)""",
            (_new_id(), telegram_id, amount_minor, currency, reference_id, idem, now_text),
        )
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
        evidence = connection.execute(
            "SELECT review_status FROM payment_evidence WHERE order_id = ? ORDER BY submitted_at DESC LIMIT 1",
            (order_id,),
        ).fetchone()
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
        connection.execute(
            """INSERT INTO wallets (telegram_id, currency, balance_minor, created_at, updated_at)
               VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
            (telegram_id, order["currency"], now_text, now_text),
        )
        idem = f"reserve:{order_id}"
        existing = connection.execute(
            "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (idem,)
        ).fetchone()
        if existing is not None:
            return "already_reserved"
        updated = connection.execute(
            """UPDATE wallets SET balance_minor = balance_minor - ?, updated_at = ?
               WHERE telegram_id = ? AND balance_minor >= ?""",
            (order["amount_minor"], now_text, telegram_id, order["amount_minor"]),
        )
        if getattr(updated, "rowcount", 1) == 0:
            raise CommerceError("Insufficient wallet balance")
        connection.execute(
            """INSERT INTO wallet_ledger
               (id, telegram_id, kind, amount_minor, currency, reference_type,
                reference_id, idempotency_key, created_at)
               VALUES (?, ?, 'reserve', ?, ?, 'order', ?, ?, ?)""",
            (
                _new_id(),
                telegram_id,
                order["amount_minor"],
                order["currency"],
                order_id,
                idem,
                now_text,
            ),
        )
        connection.execute(
            """INSERT INTO wallet_reservations
               (id, telegram_id, order_id, amount_minor, currency, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?)""",
            (
                _new_id(),
                telegram_id,
                order_id,
                order["amount_minor"],
                order["currency"],
                now_text,
                now_text,
            ),
        )
        connection.execute(
            """INSERT INTO payments
               (id, order_id, provider, provider_reference, normalized_reference, status, submitted_at)
               VALUES (?, ?, 'wallet', ?, ?, 'submitted', ?)""",
            (
                _new_id(),
                order_id,
                f"wallet:{order_id}",
                _normalize_reference(f"wallet:{order_id}"),
                now_text,
            ),
        )
        connection.execute(
            "UPDATE orders SET status = 'payment_submitted', payment_method = 'wallet' WHERE id = ?",
            (order_id,),
        )
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
