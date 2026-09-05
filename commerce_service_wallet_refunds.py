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
        connection.execute(
            "UPDATE orders SET status = 'rejected', rejected_at = ? WHERE id = ?",
            (rejected_at, order_id),
        )
        connection.execute(
            "UPDATE payments SET status = 'rejected' WHERE order_id = ? AND status = 'submitted'",
            (order_id,),
        )
        wallet_payment = self.payments.latest_payment(connection, order_id)
        if wallet_payment is not None and wallet_payment["provider"] == "wallet":
            amount_row = connection.execute(
                "SELECT amount_minor, currency, telegram_id FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            release_idem = f"release:{order_id}"
            if (
                amount_row is not None
                and connection.execute(
                    "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (release_idem,)
                ).fetchone()
                is None
            ):
                connection.execute(
                    "UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ? WHERE telegram_id = ?",
                    (amount_row["amount_minor"], rejected_at, amount_row["telegram_id"]),
                )
                connection.execute(
                    """INSERT INTO wallet_ledger
                       (id, telegram_id, kind, amount_minor, currency, reference_type,
                        reference_id, idempotency_key, created_at)
                       VALUES (?, ?, 'release', ?, ?, 'order', ?, ?, ?)""",
                    (
                        _new_id(),
                        amount_row["telegram_id"],
                        amount_row["amount_minor"],
                        amount_row["currency"],
                        order_id,
                        release_idem,
                        rejected_at,
                    ),
                )
                connection.execute(
                    "UPDATE wallet_reservations SET status = 'released', updated_at = ? WHERE order_id = ?",
                    (rejected_at, order_id),
                )
        connection.execute(
            """UPDATE payment_evidence
               SET reviewer_id = ?, review_notes = 'rejected by admin',
                   review_status = 'rejected', reviewed_at = ?
               WHERE order_id = ? AND reviewer_id IS NULL""",
            (admin_id, rejected_at, order_id),
        )
        connection.execute(
            """INSERT INTO notifications
               (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
               VALUES (?, ?, ?, 'payment_rejected', ?, 'pending', ?, ?)
               ON CONFLICT(dedupe_key) DO NOTHING""",
            (
                _new_id(),
                f"payment-rejected:{order_id}",
                order["telegram_id"],
                "Your AuriX payment/order was rejected. Contact support if you need a review.",
                rejected_at,
                rejected_at,
            ),
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
        currency = str(order["currency"]).upper()
        now_text = refunded_at
        connection.execute(
            """INSERT INTO wallets (telegram_id, currency, balance_minor, created_at, updated_at)
               VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
            (order["telegram_id"], currency, now_text, now_text),
        )
        wallet = connection.execute(
            "SELECT currency FROM wallets WHERE telegram_id = ?",
            (order["telegram_id"],),
        ).fetchone()
        if wallet is None or str(wallet["currency"]).upper() != currency:
            raise CommerceError("Wallet currency does not match the order")
        idem = f"reversal:{order_id}"
        existing_reversal = connection.execute(
            "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (idem,)
        ).fetchone()
        if existing_reversal is None:
            connection.execute(
                "UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ? WHERE telegram_id = ?",
                (amount, now_text, order["telegram_id"]),
            )
            connection.execute(
                """INSERT INTO wallet_ledger
                   (id, telegram_id, kind, amount_minor, currency, reference_type,
                    reference_id, idempotency_key, metadata_json, created_at)
                   VALUES (?, ?, 'reversal', ?, ?, 'order', ?, ?, ?, ?)""",
                (
                    _new_id(),
                    order["telegram_id"],
                    amount,
                    currency,
                    order_id,
                    idem,
                    json.dumps(
                        {"reason": (reason or "")[:500], "admin_id": admin_id}, sort_keys=True
                    ),
                    now_text,
                ),
            )
        connection.execute(
            "UPDATE payments SET status = 'refunded' WHERE order_id = ? AND status IN ('verified', 'submitted')",
            (order_id,),
        )
        final_order_status = str(order["status"])
        if final_order_status != "approved":
            final_order_status = "rejected"
        connection.execute(
            "UPDATE orders SET status = ?, refund_status = 'refunded', rejected_at = COALESCE(rejected_at, ?) WHERE id = ?",
            (final_order_status, now_text, order_id),
        )
        subscription = connection.execute(
            "SELECT id, status FROM subscriptions WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if subscription is not None:
            if subscription["status"] == "active":
                connection.execute(
                    "UPDATE subscriptions SET status = 'revoked' WHERE id = ?",
                    (subscription["id"],),
                )
            elif subscription["status"] == "pending":
                connection.execute(
                    "UPDATE subscriptions SET status = 'cancelled' WHERE id = ?",
                    (subscription["id"],),
                )
            connection.execute(
                """INSERT INTO provisioning_jobs
                   (id, subscription_id, operation, status, next_attempt_at, created_at)
                   VALUES (?, ?, 'revoke', 'pending', ?, ?)
                   ON CONFLICT(subscription_id, operation) DO NOTHING""",
                (_new_id(), subscription["id"], now_text, now_text),
            )
        connection.execute(
            """INSERT INTO notifications
               (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
               VALUES (?, ?, ?, 'payment_refunded', ?, 'pending', ?, ?)
               ON CONFLICT(dedupe_key) DO NOTHING""",
            (
                _new_id(),
                f"payment-refund-recorded:{order_id}",
                order["telegram_id"],
                f"Your AuriX order was refunded with a {amount:,} {currency} wallet credit. Reason: {(reason or 'admin refund')[:300]}",
                now_text,
                now_text,
            ),
        )
        self._audit(
            connection,
            "order_refunded",
            "order",
            order_id,
            "admin",
            str(admin_id),
            {"amount_minor": amount, "currency": currency, "reason": (reason or "")[:500]},
        )
    return "refunded"
