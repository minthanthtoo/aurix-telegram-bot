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
