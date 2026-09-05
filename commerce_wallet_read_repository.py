"""Persistence reads for wallet balances and commerce consistency checks."""

from __future__ import annotations

from typing import Any


class WalletReadRepository:
    """SQL boundary for customer wallet reads and invariant reporting."""

    @staticmethod
    def user_exists(connection: Any, telegram_id: int) -> bool:
        return (
            connection.execute(
                "SELECT telegram_id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            is not None
        )

    @staticmethod
    def wallet_currency(connection: Any, telegram_id: int) -> Any:
        return connection.execute(
            "SELECT currency FROM wallets WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()

    @staticmethod
    def ensure_wallet(
        connection: Any, *, telegram_id: int, currency: str, now_text: str
    ) -> None:
        connection.execute(
            """INSERT INTO wallets (telegram_id, currency, balance_minor, created_at, updated_at)
               VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
            (telegram_id, currency, now_text, now_text),
        )

    @staticmethod
    def balance(connection: Any, telegram_id: int) -> Any:
        return connection.execute(
            "SELECT balance_minor FROM wallets WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()

    @staticmethod
    def history(connection: Any, telegram_id: int, currency: str, limit: int) -> list[Any]:
        return connection.execute(
            """SELECT kind, amount_minor, currency, reference_type, reference_id,
                      created_at
               FROM wallet_ledger
               WHERE telegram_id = ? AND currency = ?
               ORDER BY created_at DESC LIMIT ?""",
            (telegram_id, currency, limit),
        ).fetchall()

    @staticmethod
    def _table_exists(connection: Any, name: str) -> bool:
        if connection.__class__.__name__ == "_PostgresConnection":
            row = connection.execute(
                "SELECT to_regclass(?) AS table_name", (f"public.{name}",)
            ).fetchone()
            return bool(row and row["table_name"])
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
            ).fetchone()
            is not None
        )

    @classmethod
    def consistency_report(cls, connection: Any, review_cutoff: str) -> dict[str, int]:
        def count(sql: str, params: tuple[Any, ...] = ()) -> int:
            return int(connection.execute(sql, params).fetchone()["n"])

        values = {
            "duplicate_open_orders": count(
                """SELECT COUNT(*) AS n FROM (
                     SELECT telegram_id FROM orders
                     WHERE status IN ('awaiting_payment', 'payment_submitted')
                     GROUP BY telegram_id HAVING COUNT(*) > 1)"""
            ),
            "duplicate_payment_references": count(
                """SELECT COUNT(*) AS n FROM (
                     SELECT lower(provider), normalized_reference
                     FROM payments WHERE normalized_reference <> ''
                     GROUP BY lower(provider), normalized_reference
                     HAVING COUNT(*) > 1)"""
            ),
            "approved_missing_subscription": count(
                """SELECT COUNT(*) AS n FROM orders o
                   LEFT JOIN subscriptions s ON s.order_id = o.id
                   WHERE o.status = 'approved' AND o.plan_code != 'wallet_topup'
                     AND s.id IS NULL"""
            ),
            "approved_missing_provision_job": count(
                """SELECT COUNT(*) AS n FROM subscriptions s
                   JOIN orders o ON o.id = s.order_id
                   WHERE o.status = 'approved'
                     AND NOT EXISTS (SELECT 1 FROM provisioning_jobs j
                                     WHERE j.subscription_id = s.id AND j.operation = 'provision')"""
            ),
            "pending_receipts": count(
                "SELECT COUNT(*) AS n FROM payment_evidence WHERE review_status = 'pending'"
            ),
            "stale_receipts": count(
                """SELECT COUNT(*) AS n FROM payment_evidence
                   WHERE review_status = 'pending' AND submitted_at <= ?""",
                (review_cutoff,),
            ),
            "pending_receipt_uploads": count(
                "SELECT COUNT(*) AS n FROM payment_evidence WHERE storage_status = 'pending'"
            ),
            "failed_receipt_uploads": count(
                "SELECT COUNT(*) AS n FROM payment_evidence WHERE storage_status = 'failed'"
            ),
            "failed_jobs": count(
                "SELECT COUNT(*) AS n FROM provisioning_jobs WHERE status = 'failed'"
            ),
            "pending_revocations": count(
                "SELECT COUNT(*) AS n FROM provisioning_jobs WHERE operation = 'revoke' AND status IN ('pending', 'running')"
            ),
            "failed_revocations": count(
                "SELECT COUNT(*) AS n FROM provisioning_jobs WHERE operation = 'revoke' AND status = 'failed'"
            ),
            "failed_activations": count(
                "SELECT COUNT(*) AS n FROM provisioning_jobs WHERE operation = 'provision' AND status = 'failed'"
            ),
            "dead_notifications": count(
                "SELECT COUNT(*) AS n FROM notifications WHERE dead_lettered_at IS NOT NULL"
            ),
        }
        repair_counts = {"pending": 0, "failed": 0, "manual": 0}
        if cls._table_exists(connection, "managed_key_repair_jobs"):
            for status in repair_counts:
                repair_counts[status] = count(
                    "SELECT COUNT(*) AS n FROM managed_key_repair_jobs WHERE status = ?",
                    (status,) if status != "pending" else ("pending",),
                )
                if status == "pending":
                    repair_counts[status] += count(
                        "SELECT COUNT(*) AS n FROM managed_key_repair_jobs WHERE status = 'running'"
                    )
        managed_key_missing = 0
        if cls._table_exists(connection, "outline_remote_keys"):
            managed_key_missing = count(
                "SELECT COUNT(*) AS n FROM outline_remote_keys WHERE managed = 1 AND status = 'missing'"
            )
        wallet_mismatches = 0
        wallets = connection.execute(
            "SELECT telegram_id, currency, balance_minor FROM wallets"
        ).fetchall()
        for wallet in wallets:
            ledger = connection.execute(
                """SELECT COALESCE(SUM(CASE WHEN kind IN ('credit', 'release', 'reversal')
                                            THEN amount_minor
                                            WHEN kind = 'reserve' THEN -amount_minor
                                            ELSE 0 END), 0) AS projected
                   FROM wallet_ledger WHERE telegram_id = ? AND currency = ?""",
                (wallet["telegram_id"], wallet["currency"]),
            ).fetchone()
            if int(ledger["projected"] or 0) != int(wallet["balance_minor"]):
                wallet_mismatches += 1
        values.update(
            {
                "managed_key_missing": managed_key_missing,
                "managed_key_repairs_pending": repair_counts["pending"],
                "managed_key_repairs_failed": repair_counts["failed"],
                "managed_key_repairs_manual": repair_counts["manual"],
                "wallet_balance_mismatches": wallet_mismatches,
            }
        )
        return values

    @staticmethod
    def pending_orders(connection: Any, limit: int) -> list[Any]:
        return connection.execute(
            """SELECT o.id, o.telegram_id, o.plan_code, o.amount_minor, o.currency,
                      o.status, o.created_at,
                      (SELECT p.provider FROM payments p WHERE p.order_id = o.id
                       ORDER BY p.submitted_at DESC LIMIT 1) AS provider,
                      (SELECT p.provider_reference FROM payments p WHERE p.order_id = o.id
                       ORDER BY p.submitted_at DESC LIMIT 1) AS provider_reference,
                      (SELECT e.review_status FROM payment_evidence e WHERE e.order_id = o.id
                       ORDER BY e.submitted_at DESC LIMIT 1) AS receipt_status,
                      (SELECT r.status FROM wallet_reservations r WHERE r.order_id = o.id
                       LIMIT 1) AS wallet_reservation_status
               FROM orders o
               WHERE o.status IN ('awaiting_payment', 'payment_submitted')
                 AND COALESCE(o.refund_status, 'none') != 'refunded'
               ORDER BY o.created_at LIMIT ?""",
            (limit,),
        ).fetchall()
