"""Persistence queries for the paid order aggregate.

The repository deliberately accepts an existing connection. Application services
still own transaction boundaries while this module owns order row lookup SQL and
its parameter conventions.
"""

from __future__ import annotations

from typing import Any


class OrderRepository:
    """Small, transaction-neutral order query boundary."""

    @staticmethod
    def get(connection: Any, order_id: str) -> Any:
        return connection.execute(
            "SELECT * FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()

    @staticmethod
    def get_owned(connection: Any, order_id: str, telegram_id: int) -> Any:
        return connection.execute(
            "SELECT * FROM orders WHERE id = ? AND telegram_id = ?",
            (order_id, telegram_id),
        ).fetchone()

    @staticmethod
    def find_open_for_user(connection: Any, telegram_id: int) -> Any:
        return connection.execute(
            """SELECT * FROM orders
               WHERE telegram_id = ?
                 AND status IN ('awaiting_payment', 'payment_submitted')
                 AND COALESCE(refund_status, 'none') != 'refunded'
               ORDER BY created_at LIMIT 1""",
            (telegram_id,),
        ).fetchone()

    @staticmethod
    def get_payment_context(connection: Any, order_id: str) -> Any:
        return connection.execute(
            "SELECT telegram_id, payment_method FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()

    @staticmethod
    def lock_user(connection: Any, telegram_id: int) -> None:
        if connection.__class__.__name__ == "_PostgresConnection":
            connection.execute(
                "SELECT telegram_id FROM users WHERE telegram_id = ? FOR UPDATE",
                (telegram_id,),
            ).fetchone()

    @staticmethod
    def enabled_server_count(connection: Any) -> int:
        return int(
            connection.execute(
                "SELECT COUNT(*) AS n FROM outline_servers WHERE enabled = 1"
            ).fetchone()["n"]
        )

    @staticmethod
    def insert_wallet_topup(
        connection: Any, *, order_id: str, telegram_id: int, amount_minor: int, created_at: str
    ) -> None:
        connection.execute(
            """INSERT INTO orders
               (id, telegram_id, plan_code, amount_minor, currency, plan_name,
                quota_bytes_snapshot, duration_days_snapshot, status, created_at)
               VALUES (?, ?, 'wallet_topup', ?, 'MMK', 'Wallet Top-up',
                       NULL, 1, 'awaiting_payment', ?)""",
            (order_id, telegram_id, amount_minor, created_at),
        )

    @staticmethod
    def insert_order(
        connection: Any,
        *,
        order_id: str,
        telegram_id: int,
        plan_code: str,
        amount_minor: int,
        currency: str,
        plan_name: str,
        quota_bytes: int | None,
        duration_days: int,
        created_at: str,
        server_id: str | None,
        reserved_until: str | None,
    ) -> None:
        connection.execute(
            """INSERT INTO orders
               (id, telegram_id, plan_code, amount_minor, currency, plan_name,
                quota_bytes_snapshot, duration_days_snapshot, status, created_at,
                server_id, capacity_reserved_until)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_payment', ?, ?, ?)""",
            (
                order_id,
                telegram_id,
                plan_code,
                amount_minor,
                currency,
                plan_name,
                quota_bytes,
                duration_days,
                created_at,
                server_id,
                reserved_until,
            ),
        )

    @staticmethod
    def cancel(connection: Any, order_id: str, cancelled_at: str) -> None:
        connection.execute(
            "UPDATE orders SET status = 'cancelled', rejected_at = ? WHERE id = ?",
            (cancelled_at, order_id),
        )

    @staticmethod
    def expired_open_orders(connection: Any, cutoff: str) -> list[Any]:
        return connection.execute(
            """SELECT o.id FROM orders o
               WHERE o.status = 'awaiting_payment' AND o.created_at <= ?
                 AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.id)
                 AND NOT EXISTS (SELECT 1 FROM payment_evidence e WHERE e.order_id = o.id)""",
            (cutoff,),
        ).fetchall()

    @staticmethod
    def expired_wallet_reservations(connection: Any, cutoff: str) -> list[Any]:
        return connection.execute(
            """SELECT r.order_id, r.telegram_id, r.amount_minor, r.currency
               FROM wallet_reservations r JOIN orders o ON o.id = r.order_id
               WHERE r.status = 'reserved' AND r.created_at <= ?
                 AND o.status = 'payment_submitted'""",
            (cutoff,),
        ).fetchall()

    @staticmethod
    def wallet_ledger_exists(connection: Any, idempotency_key: str) -> bool:
        return (
            connection.execute(
                "SELECT id FROM wallet_ledger WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def credit_wallet(
        connection: Any, *, telegram_id: int, amount_minor: int, updated_at: str
    ) -> None:
        connection.execute(
            "UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ? WHERE telegram_id = ?",
            (amount_minor, updated_at, telegram_id),
        )

    @staticmethod
    def insert_wallet_ledger(
        connection: Any,
        *,
        ledger_id: str,
        telegram_id: int,
        amount_minor: int,
        currency: str,
        order_id: str,
        idempotency_key: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO wallet_ledger
               (id, telegram_id, kind, amount_minor, currency, reference_type,
                reference_id, idempotency_key, created_at)
               VALUES (?, ?, 'release', ?, ?, 'order', ?, ?, ?)""",
            (
                ledger_id,
                telegram_id,
                amount_minor,
                currency,
                order_id,
                idempotency_key,
                created_at,
            ),
        )

    @staticmethod
    def release_wallet_reservation(connection: Any, order_id: str, updated_at: str) -> None:
        connection.execute(
            "UPDATE wallet_reservations SET status = 'released', updated_at = ? WHERE order_id = ?",
            (updated_at, order_id),
        )

    @staticmethod
    def reject_wallet_payment(connection: Any, order_id: str) -> None:
        connection.execute(
            "UPDATE payments SET status = 'rejected' WHERE order_id = ? AND provider = 'wallet' AND status = 'submitted'",
            (order_id,),
        )

    @staticmethod
    def cancel_submitted_order(connection: Any, order_id: str, rejected_at: str) -> None:
        connection.execute(
            "UPDATE orders SET status = 'cancelled', rejected_at = ? WHERE id = ? AND status = 'payment_submitted'",
            (rejected_at, order_id),
        )

    @staticmethod
    def insert_notification(
        connection: Any,
        *,
        notification_id: str,
        dedupe_key: str,
        telegram_id: int,
        text: str,
        now_text: str,
    ) -> None:
        connection.execute(
            """INSERT INTO notifications
               (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
               VALUES (?, ?, ?, 'wallet_reservation_expired', ?, 'pending', ?, ?)
               ON CONFLICT(dedupe_key) DO NOTHING""",
            (notification_id, dedupe_key, telegram_id, text, now_text, now_text),
        )

    @staticmethod
    def open_order_users(connection: Any) -> list[Any]:
        return connection.execute(
            """SELECT telegram_id FROM orders
               WHERE status IN ('awaiting_payment', 'payment_submitted')
               GROUP BY telegram_id HAVING COUNT(*) > 1"""
        ).fetchall()

    @staticmethod
    def open_orders_for_user(connection: Any, telegram_id: int) -> list[Any]:
        return connection.execute(
            """SELECT o.id, o.created_at,
                      (SELECT COUNT(*) FROM payments p WHERE p.order_id = o.id) AS payments,
                      (SELECT COUNT(*) FROM payment_evidence e WHERE e.order_id = o.id) AS evidence
               FROM orders o WHERE o.telegram_id = ?
                 AND o.status IN ('awaiting_payment', 'payment_submitted')
               ORDER BY o.created_at""",
            (telegram_id,),
        ).fetchall()

    @staticmethod
    def cancel_duplicate(connection: Any, order_id: str) -> None:
        connection.execute(
            "UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,)
        )

    @staticmethod
    def list_user_orders(connection: Any, telegram_id: int, limit: int) -> list[Any]:
        return connection.execute(
            """SELECT o.id, o.plan_code, o.plan_name, o.amount_minor, o.currency,
                      o.status, o.refund_status, o.created_at,
                      (SELECT p.status FROM payments p WHERE p.order_id = o.id
                       ORDER BY p.submitted_at DESC LIMIT 1) AS payment_status,
                      (SELECT e.review_status FROM payment_evidence e WHERE e.order_id = o.id
                       ORDER BY e.submitted_at DESC LIMIT 1) AS receipt_status,
                      (SELECT s.status FROM subscriptions s WHERE s.order_id = o.id
                       LIMIT 1) AS subscription_status,
                      (SELECT s.expires_at FROM subscriptions s WHERE s.order_id = o.id
                       LIMIT 1) AS expires_at,
                      (SELECT j.status FROM provisioning_jobs j JOIN subscriptions s
                       ON s.id = j.subscription_id WHERE s.order_id = o.id
                       AND j.operation = 'provision' LIMIT 1) AS provisioning_status,
                      (SELECT j.status FROM provisioning_jobs j JOIN subscriptions s
                       ON s.id = j.subscription_id WHERE s.order_id = o.id
                       AND j.operation = 'revoke' LIMIT 1) AS revocation_status,
                      (SELECT r.status FROM wallet_reservations r WHERE r.order_id = o.id
                       LIMIT 1) AS wallet_reservation_status
               FROM orders o WHERE o.telegram_id = ?
               ORDER BY o.created_at DESC LIMIT ?""",
            (telegram_id, limit),
        ).fetchall()

    @staticmethod
    def detail(connection: Any, order_id: str) -> Any:
        return connection.execute(
            """SELECT o.*,
                      (SELECT p.status FROM payments p WHERE p.order_id = o.id
                       ORDER BY p.submitted_at DESC LIMIT 1) AS payment_status,
                      (SELECT p.provider FROM payments p WHERE p.order_id = o.id
                       ORDER BY p.submitted_at DESC LIMIT 1) AS payment_provider,
                      (SELECT e.review_status FROM payment_evidence e WHERE e.order_id = o.id
                       ORDER BY e.submitted_at DESC LIMIT 1) AS receipt_status,
                      (SELECT e.id FROM payment_evidence e WHERE e.order_id = o.id
                       ORDER BY e.submitted_at DESC LIMIT 1) AS evidence_id,
                      (SELECT s.status FROM subscriptions s WHERE s.order_id = o.id
                       LIMIT 1) AS subscription_status,
                      (SELECT s.expires_at FROM subscriptions s WHERE s.order_id = o.id
                       LIMIT 1) AS expires_at,
                      (SELECT j.status FROM provisioning_jobs j JOIN subscriptions s
                       ON s.id = j.subscription_id WHERE s.order_id = o.id
                       AND j.operation = 'provision' LIMIT 1) AS provisioning_status,
                      (SELECT j.status FROM provisioning_jobs j JOIN subscriptions s
                       ON s.id = j.subscription_id WHERE s.order_id = o.id
                       AND j.operation = 'revoke' LIMIT 1) AS revocation_status,
                      (SELECT r.status FROM wallet_reservations r WHERE r.order_id = o.id
                       LIMIT 1) AS wallet_reservation_status
               FROM orders o WHERE o.id = ?""",
            (order_id,),
        ).fetchone()

    @staticmethod
    def update_payment_method(connection: Any, order_id: str, method: str) -> None:
        connection.execute(
            "UPDATE orders SET payment_method = ? WHERE id = ?", (method, order_id)
        )

    @staticmethod
    def insert_payment(
        connection: Any,
        *,
        payment_id: str,
        order_id: str,
        provider: str,
        provider_reference: str,
        normalized_reference: str,
        submitted_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO payments
               (id, order_id, provider, provider_reference, normalized_reference, status, submitted_at)
               VALUES (?, ?, ?, ?, ?, 'submitted', ?)""",
            (
                payment_id,
                order_id,
                provider,
                provider_reference,
                normalized_reference,
                submitted_at,
            ),
        )

    @staticmethod
    def mark_payment_submitted(connection: Any, order_id: str) -> None:
        connection.execute(
            "UPDATE orders SET status = 'payment_submitted' WHERE id = ?", (order_id,)
        )

    @staticmethod
    def open_order_ids(connection: Any, telegram_id: int, limit: int) -> list[Any]:
        return connection.execute(
            """SELECT id FROM orders
               WHERE telegram_id = ?
                 AND status IN ('awaiting_payment', 'payment_submitted')
                 AND COALESCE(refund_status, 'none') != 'refunded'
               ORDER BY created_at LIMIT ?""",
            (telegram_id, limit),
        ).fetchall()
