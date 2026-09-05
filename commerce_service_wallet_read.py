"""Wallet reads and consistency reporting."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from commerce_models import ApprovalResult, CommerceError, UTC, _new_id, _normalize_reference, _now_text

def wallet_balance(self, telegram_id: int, currency: str = "MMK") -> int:
    now_text = _now_text()
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        user = connection.execute(
            "SELECT telegram_id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        if user is None:
            self._ensure_user(connection, telegram_id, "")
        existing_wallet = connection.execute(
            "SELECT currency FROM wallets WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if (
            existing_wallet is not None
            and str(existing_wallet["currency"]).upper() != currency.upper()
        ):
            raise CommerceError("A user wallet has one supported currency")
        connection.execute(
            """INSERT INTO wallets (telegram_id, currency, balance_minor, created_at, updated_at)
               VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
            (telegram_id, currency, now_text, now_text),
        )
        row = connection.execute(
            "SELECT balance_minor FROM wallets WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return int(row["balance_minor"] if row else 0)
def wallet_history(
    self, telegram_id: int, limit: int = 20, currency: str = "MMK"
) -> list[dict[str, Any]]:
    """Return immutable wallet events for the owner, newest first."""
    with self.database.connect() as connection:
        rows = connection.execute(
            """SELECT kind, amount_minor, currency, reference_type, reference_id,
                      created_at
               FROM wallet_ledger
               WHERE telegram_id = ? AND currency = ?
               ORDER BY created_at DESC LIMIT ?""",
            (telegram_id, currency.upper(), max(1, min(limit, 100))),
        ).fetchall()
    return [dict(row) for row in rows]
def consistency_report(
    self,
    now: datetime | None = None,
    review_sla: timedelta = timedelta(hours=24),
) -> dict[str, int]:
    """Read-only invariant scan for admin operations and deployment checks."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    review_cutoff = _now_text(current - review_sla)
    with self.database.connect() as connection:
        duplicate_open = connection.execute(
            """SELECT COUNT(*) AS n FROM (
                 SELECT telegram_id FROM orders
                 WHERE status IN ('awaiting_payment', 'payment_submitted')
                 GROUP BY telegram_id HAVING COUNT(*) > 1)"""
        ).fetchone()["n"]
        duplicate_payment_references = connection.execute(
            """SELECT COUNT(*) AS n FROM (
                 SELECT lower(provider), normalized_reference
                 FROM payments
                 WHERE normalized_reference <> ''
                 GROUP BY lower(provider), normalized_reference
                 HAVING COUNT(*) > 1)"""
        ).fetchone()["n"]
        approved_missing_subscription = connection.execute(
            """SELECT COUNT(*) AS n FROM orders o
               LEFT JOIN subscriptions s ON s.order_id = o.id
               WHERE o.status = 'approved' AND o.plan_code != 'wallet_topup'
                 AND s.id IS NULL"""
        ).fetchone()["n"]
        approved_missing_job = connection.execute(
            """SELECT COUNT(*) AS n FROM subscriptions s
               JOIN orders o ON o.id = s.order_id
               WHERE o.status = 'approved'
                 AND NOT EXISTS (SELECT 1 FROM provisioning_jobs j
                                 WHERE j.subscription_id = s.id AND j.operation = 'provision')"""
        ).fetchone()["n"]
        pending_reviews = connection.execute(
            "SELECT COUNT(*) AS n FROM payment_evidence WHERE review_status = 'pending'"
        ).fetchone()["n"]
        stale_reviews = connection.execute(
            """SELECT COUNT(*) AS n FROM payment_evidence
               WHERE review_status = 'pending' AND submitted_at <= ?""",
            (review_cutoff,),
        ).fetchone()["n"]
        pending_receipt_uploads = connection.execute(
            "SELECT COUNT(*) AS n FROM payment_evidence WHERE storage_status = 'pending'"
        ).fetchone()["n"]
        failed_receipt_uploads = connection.execute(
            "SELECT COUNT(*) AS n FROM payment_evidence WHERE storage_status = 'failed'"
        ).fetchone()["n"]
        failed_jobs = connection.execute(
            "SELECT COUNT(*) AS n FROM provisioning_jobs WHERE status = 'failed'"
        ).fetchone()["n"]
        pending_revocations = connection.execute(
            "SELECT COUNT(*) AS n FROM provisioning_jobs WHERE operation = 'revoke' AND status IN ('pending', 'running')"
        ).fetchone()["n"]
        failed_revocations = connection.execute(
            "SELECT COUNT(*) AS n FROM provisioning_jobs WHERE operation = 'revoke' AND status = 'failed'"
        ).fetchone()["n"]
        failed_activations = connection.execute(
            "SELECT COUNT(*) AS n FROM provisioning_jobs WHERE operation = 'provision' AND status = 'failed'"
        ).fetchone()["n"]
        dead_notifications = connection.execute(
            "SELECT COUNT(*) AS n FROM notifications WHERE dead_lettered_at IS NOT NULL"
        ).fetchone()["n"]
        managed_key_repairs_pending = 0
        managed_key_repairs_failed = 0
        managed_key_repairs_manual = 0
        managed_key_missing = 0
        if self._table_exists(connection, "managed_key_repair_jobs"):
            managed_key_repairs_pending = connection.execute(
                "SELECT COUNT(*) AS n FROM managed_key_repair_jobs WHERE status IN ('pending', 'running')"
            ).fetchone()["n"]
            managed_key_repairs_failed = connection.execute(
                "SELECT COUNT(*) AS n FROM managed_key_repair_jobs WHERE status = 'failed'"
            ).fetchone()["n"]
            managed_key_repairs_manual = connection.execute(
                "SELECT COUNT(*) AS n FROM managed_key_repair_jobs WHERE status = 'manual'"
            ).fetchone()["n"]
        if self._table_exists(connection, "outline_remote_keys"):
            managed_key_missing = connection.execute(
                """SELECT COUNT(*) AS n FROM outline_remote_keys
                    WHERE managed = 1 AND status = 'missing'"""
            ).fetchone()["n"]
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
    return {
        "duplicate_open_orders": int(duplicate_open),
        "duplicate_payment_references": int(duplicate_payment_references),
        "approved_missing_subscription": int(approved_missing_subscription),
        "approved_missing_provision_job": int(approved_missing_job),
        "pending_receipts": int(pending_reviews),
        "stale_receipts": int(stale_reviews),
        "pending_receipt_uploads": int(pending_receipt_uploads),
        "failed_receipt_uploads": int(failed_receipt_uploads),
        "failed_jobs": int(failed_jobs),
        "pending_revocations": int(pending_revocations),
        "failed_revocations": int(failed_revocations),
        "failed_activations": int(failed_activations),
        "dead_notifications": int(dead_notifications),
        "managed_key_missing": int(managed_key_missing),
        "managed_key_repairs_pending": int(managed_key_repairs_pending),
        "managed_key_repairs_failed": int(managed_key_repairs_failed),
        "managed_key_repairs_manual": int(managed_key_repairs_manual),
        "wallet_balance_mismatches": int(wallet_mismatches),
    }
def list_pending_orders(self, limit: int = 20) -> list[dict[str, Any]]:
    with self.database.connect() as connection:
        rows = connection.execute(
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
            (max(1, min(limit, 100)),),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["stage"] = self._order_stage(item)
        result.append(item)
    return result
