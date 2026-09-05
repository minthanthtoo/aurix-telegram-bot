"""Transaction-aware persistence for wallet and paid-order approval."""

from __future__ import annotations

import json
import secrets
from typing import Any


class WalletApprovalRepository:
    """SQL boundary for one approval transaction.

    Methods accept the caller's active connection so approval state, wallet
    ledger entries, reservations, subscriptions, jobs, and notifications are
    committed or rolled back as one unit.
    """

    @staticmethod
    def subscription_id_for_order(connection: Any, order_id: str) -> str | None:
        row = connection.execute(
            "SELECT id FROM subscriptions WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        return None if row is None else str(row["id"])

    @staticmethod
    def wallet_reservation(connection: Any, order_id: str) -> Any:
        return connection.execute(
            """SELECT id, amount_minor, currency, status
               FROM wallet_reservations WHERE order_id = ? LIMIT 1""",
            (order_id,),
        ).fetchone()

    @staticmethod
    def plan(connection: Any, plan_code: str) -> Any:
        return connection.execute(
            "SELECT duration_days, quota_bytes, name FROM plans WHERE code = ?",
            (plan_code,),
        ).fetchone()

    @staticmethod
    def ensure_wallet(
        connection: Any,
        *,
        telegram_id: int,
        currency: str,
        now_text: str,
    ) -> None:
        connection.execute(
            """INSERT INTO wallets
               (telegram_id, currency, balance_minor, created_at, updated_at)
               VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
            (telegram_id, currency, now_text, now_text),
        )

    @staticmethod
    def credit_once(
        connection: Any,
        *,
        entry_id: str,
        telegram_id: int,
        amount_minor: int,
        currency: str,
        reference_type: str,
        reference_id: str,
        idempotency_key: str,
        now_text: str,
    ) -> bool:
        existing = connection.execute(
            "SELECT id FROM wallet_ledger WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            return False
        connection.execute(
            """UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ?
               WHERE telegram_id = ?""",
            (amount_minor, now_text, telegram_id),
        )
        connection.execute(
            """INSERT INTO wallet_ledger
               (id, telegram_id, kind, amount_minor, currency, reference_type,
                reference_id, idempotency_key, created_at)
               VALUES (?, ?, 'credit', ?, ?, ?, ?, ?, ?)""",
            (
                entry_id,
                telegram_id,
                amount_minor,
                currency,
                reference_type,
                reference_id,
                idempotency_key,
                now_text,
            ),
        )
        return True

    @staticmethod
    def reserve_once(
        connection: Any,
        *,
        ledger_entry_id: str,
        reservation_id: str,
        telegram_id: int,
        order_id: str,
        amount_minor: int,
        currency: str,
        idempotency_key: str,
        now_text: str,
    ) -> bool:
        existing = connection.execute(
            "SELECT id FROM wallet_ledger WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            return True
        updated = connection.execute(
            """UPDATE wallets SET balance_minor = balance_minor - ?, updated_at = ?
               WHERE telegram_id = ? AND balance_minor >= ?""",
            (amount_minor, now_text, telegram_id, amount_minor),
        )
        if getattr(updated, "rowcount", 1) == 0:
            return False
        connection.execute(
            """INSERT INTO wallet_ledger
               (id, telegram_id, kind, amount_minor, currency, reference_type,
                reference_id, idempotency_key, created_at)
               VALUES (?, ?, 'reserve', ?, ?, 'order', ?, ?, ?)""",
            (
                ledger_entry_id,
                telegram_id,
                amount_minor,
                currency,
                order_id,
                idempotency_key,
                now_text,
            ),
        )
        connection.execute(
            """INSERT INTO wallet_reservations
               (id, telegram_id, order_id, amount_minor, currency, status,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?)""",
            (
                reservation_id,
                telegram_id,
                order_id,
                amount_minor,
                currency,
                now_text,
                now_text,
            ),
        )
        return True

    @staticmethod
    def capture_once(
        connection: Any,
        *,
        ledger_entry_id: str,
        reservation_id: str,
        telegram_id: int,
        order_id: str,
        amount_minor: int,
        currency: str,
        idempotency_key: str,
        now_text: str,
    ) -> None:
        existing = connection.execute(
            "SELECT id FROM wallet_ledger WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """INSERT INTO wallet_ledger
                   (id, telegram_id, kind, amount_minor, currency, reference_type,
                    reference_id, idempotency_key, created_at)
                   VALUES (?, ?, 'capture', ?, ?, 'order', ?, ?, ?)""",
                (
                    ledger_entry_id,
                    telegram_id,
                    amount_minor,
                    currency,
                    order_id,
                    idempotency_key,
                    now_text,
                ),
            )
        connection.execute(
            """INSERT INTO wallet_reservations
               (id, telegram_id, order_id, amount_minor, currency, status,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'captured', ?, ?)
               ON CONFLICT(order_id) DO UPDATE SET
                 status = 'captured', updated_at = excluded.updated_at""",
            (
                reservation_id,
                telegram_id,
                order_id,
                amount_minor,
                currency,
                now_text,
                now_text,
            ),
        )

    @staticmethod
    def mark_order_approved(connection: Any, order_id: str, approved_at: str) -> None:
        connection.execute(
            "UPDATE orders SET status = 'approved', approved_at = ? WHERE id = ?",
            (approved_at, order_id),
        )

    @staticmethod
    def mark_payment_verified(connection: Any, payment_id: str, verified_at: str) -> None:
        connection.execute(
            "UPDATE payments SET status = 'verified', verified_at = ? WHERE id = ?",
            (verified_at, payment_id),
        )

    @staticmethod
    def create_subscription(
        connection: Any,
        *,
        subscription_id: str,
        order_id: str,
        telegram_id: int,
        plan_code: str,
        starts_at: str,
        expires_at: str,
        plan_name: str,
        quota_bytes: int | None,
        duration_days: int,
        server_id: str | None,
    ) -> None:
        connection.execute(
            """INSERT INTO subscriptions
               (id, order_id, telegram_id, plan_code, starts_at, expires_at,
                plan_name, quota_bytes, duration_days, status, server_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (
                subscription_id,
                order_id,
                telegram_id,
                plan_code,
                starts_at,
                expires_at,
                plan_name,
                quota_bytes,
                duration_days,
                server_id,
            ),
        )

    @staticmethod
    def queue_provisioning(
        connection: Any,
        *,
        job_id: str,
        subscription_id: str,
        next_attempt_at: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO provisioning_jobs
               (id, subscription_id, operation, status, next_attempt_at, created_at)
               VALUES (?, ?, 'provision', 'pending', ?, ?)""",
            (job_id, subscription_id, next_attempt_at, created_at),
        )

    @staticmethod
    def queue_notification(
        connection: Any,
        *,
        notification_id: str,
        dedupe_key: str,
        telegram_id: int,
        kind: str,
        text: str,
        now_text: str,
    ) -> None:
        connection.execute(
            """INSERT INTO notifications
               (id, dedupe_key, telegram_id, kind, text, status,
                next_attempt_at, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
               ON CONFLICT(dedupe_key) DO NOTHING""",
            (
                notification_id,
                dedupe_key,
                telegram_id,
                kind,
                text,
                now_text,
                now_text,
            ),
        )

    @staticmethod
    def reject_order(
        connection: Any,
        *,
        order_id: str,
        order: Any,
        admin_id: int,
        rejected_at: str,
        wallet_payment: bool,
    ) -> None:
        connection.execute(
            "UPDATE orders SET status = 'rejected', rejected_at = ? WHERE id = ?",
            (rejected_at, order_id),
        )
        connection.execute(
            "UPDATE payments SET status = 'rejected' WHERE order_id = ? AND status = 'submitted'",
            (order_id,),
        )
        if wallet_payment:
            idem = f"release:{order_id}"
            if connection.execute(
                "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (idem,)
            ).fetchone() is None:
                connection.execute(
                    "UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ? WHERE telegram_id = ?",
                    (order["amount_minor"], rejected_at, order["telegram_id"]),
                )
                connection.execute(
                    """INSERT INTO wallet_ledger
                       (id, telegram_id, kind, amount_minor, currency, reference_type,
                        reference_id, idempotency_key, created_at)
                       VALUES (?, ?, 'release', ?, ?, 'order', ?, ?, ?)""",
                    (
                        f"ledger-{secrets.token_hex(16)}",
                        order["telegram_id"], order["amount_minor"], order["currency"],
                        order_id, idem, rejected_at,
                    ),
                )
                connection.execute(
                    "UPDATE wallet_reservations SET status = 'released', updated_at = ? WHERE order_id = ?",
                    (rejected_at, order_id),
                )
        connection.execute(
            """UPDATE payment_evidence SET reviewer_id = ?, review_notes = 'rejected by admin',
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
                f"notification-{secrets.token_hex(16)}",
                f"payment-rejected:{order_id}", order["telegram_id"],
                "Your AuriX payment/order was rejected. Contact support if you need a review.",
                rejected_at, rejected_at,
            ),
        )

    @staticmethod
    def refund_order(
        connection: Any,
        *,
        order_id: str,
        order: Any,
        amount: int,
        reason: str,
        admin_id: int,
        refunded_at: str,
    ) -> None:
        currency = str(order["currency"]).upper()
        connection.execute(
            """INSERT INTO wallets (telegram_id, currency, balance_minor, created_at, updated_at)
               VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
            (order["telegram_id"], currency, refunded_at, refunded_at),
        )
        wallet = connection.execute(
            "SELECT currency FROM wallets WHERE telegram_id = ?", (order["telegram_id"],)
        ).fetchone()
        if wallet is None or str(wallet["currency"]).upper() != currency:
            raise ValueError("Wallet currency does not match the order")
        idem = f"reversal:{order_id}"
        if connection.execute(
            "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (idem,)
        ).fetchone() is None:
            connection.execute(
                "UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ? WHERE telegram_id = ?",
                (amount, refunded_at, order["telegram_id"]),
            )
            connection.execute(
                """INSERT INTO wallet_ledger
                   (id, telegram_id, kind, amount_minor, currency, reference_type,
                    reference_id, idempotency_key, metadata_json, created_at)
                   VALUES (?, ?, 'reversal', ?, ?, 'order', ?, ?, ?, ?)""",
                (
                    f"ledger-{secrets.token_hex(16)}", order["telegram_id"], amount,
                    currency, order_id, idem,
                    json.dumps({"reason": reason[:500], "admin_id": admin_id}, sort_keys=True),
                    refunded_at,
                ),
            )
        connection.execute(
            "UPDATE payments SET status = 'refunded' WHERE order_id = ? AND status IN ('verified', 'submitted')",
            (order_id,),
        )
        final_status = str(order["status"]) if str(order["status"]) == "approved" else "rejected"
        connection.execute(
            "UPDATE orders SET status = ?, refund_status = 'refunded', rejected_at = COALESCE(rejected_at, ?) WHERE id = ?",
            (final_status, refunded_at, order_id),
        )
        subscription = connection.execute(
            "SELECT id, status FROM subscriptions WHERE order_id = ?", (order_id,)
        ).fetchone()
        if subscription is not None:
            next_status = {"active": "revoked", "pending": "cancelled"}.get(str(subscription["status"]))
            if next_status:
                connection.execute(
                    "UPDATE subscriptions SET status = ? WHERE id = ?",
                    (next_status, subscription["id"]),
                )
            connection.execute(
                """INSERT INTO provisioning_jobs
                   (id, subscription_id, operation, status, next_attempt_at, created_at)
                   VALUES (?, ?, 'revoke', 'pending', ?, ?)
                   ON CONFLICT(subscription_id, operation) DO NOTHING""",
                (f"job-{secrets.token_hex(16)}", subscription["id"], refunded_at, refunded_at),
            )
        connection.execute(
            """INSERT INTO notifications
               (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
               VALUES (?, ?, ?, 'payment_refunded', ?, 'pending', ?, ?)
               ON CONFLICT(dedupe_key) DO NOTHING""",
            (
                f"notification-{secrets.token_hex(16)}",
                f"payment-refund-recorded:{order_id}", order["telegram_id"],
                f"Your AuriX order was refunded with a {amount:,} {currency} wallet credit. Reason: {reason[:300]}",
                refunded_at, refunded_at,
            ),
        )
