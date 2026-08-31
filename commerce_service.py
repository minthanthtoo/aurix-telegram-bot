"""Paid-order, wallet, provisioning, and notification application service."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from commerce_models import (
    UTC,
    ApprovalResult,
    CommerceError,
    OrderResult,
    Plan,
    _new_id,
    _normalize_reference,
    _now_text,
)
from commerce_repositories import _PostgresConnection
from ports import OutlineGateway, ReceiptStorageGateway
from repositories import RepositoryDatabase
from commerce_worker import CommerceWorkerMixin
from supabase_storage import NullReceiptStorage


class CommerceService(CommerceWorkerMixin):
    """Commerce state machine and idempotent Outline job processor."""

    def __init__(
        self,
        database: RepositoryDatabase,
        outline: OutlineGateway,
        access_url_key: bytes | str,
        allow_legacy_text_approval: bool = False,
        receipt_storage: ReceiptStorageGateway | None = None,
        receipt_storage_required: bool = False,
    ):
        self.database = database
        self.outline = outline
        # Kept only for controlled migration tests. Public deployments must
        # require verified screenshot evidence or a wallet reservation.
        self.allow_legacy_text_approval = bool(allow_legacy_text_approval)
        self.receipt_storage = receipt_storage or NullReceiptStorage()
        self.receipt_storage_required = bool(receipt_storage_required)
        try:
            self.access_url_cipher = Fernet(access_url_key)
        except (TypeError, ValueError) as exc:
            raise ValueError("AURIX_ACCESS_URL_KEY must be a Fernet key") from exc

    def _encrypt_access_url(self, access_url: str) -> str:
        return self.access_url_cipher.encrypt(access_url.encode()).decode()

    def _decrypt_access_url(self, encrypted: str | None) -> str | None:
        if not encrypted:
            return None
        try:
            return self.access_url_cipher.decrypt(encrypted.encode()).decode()
        except (InvalidToken, UnicodeDecodeError, ValueError):
            return None

    @staticmethod
    def _receipt_storage_extension(mime_type: str) -> str:
        normalized = str(mime_type or "").lower().split(";", 1)[0].strip()
        return {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "image/gif": "gif",
        }.get(normalized, "bin")

    @staticmethod
    def _receipt_storage_path(order_id: str, evidence_id: str, mime_type: str) -> str:
        # Order/evidence IDs are generated UUIDs. Keep this defensive because
        # old/imported order IDs may contain unexpected characters.
        safe_order = re.sub(r"[^A-Za-z0-9_-]+", "-", str(order_id)).strip("-_")[:96]
        safe_evidence = re.sub(r"[^A-Za-z0-9_-]+", "-", str(evidence_id)).strip("-_")[:96]
        extension = CommerceService._receipt_storage_extension(mime_type)
        return f"orders/{safe_order or 'unknown'}/{safe_evidence or _new_id()}.{extension}"

    def _storage_is_configured(self) -> bool:
        return bool(getattr(self.receipt_storage, "configured", False))

    def _storage_bucket(self) -> str | None:
        bucket = getattr(self.receipt_storage, "bucket", None)
        return str(bucket) if bucket else None

    @staticmethod
    def _lock_order(connection: Any, order_id: str) -> None:
        """Serialize aggregate mutations on PostgreSQL as well as SQLite.

        SQLite already serializes writers. PostgreSQL needs an explicit row
        lock because payment, receipt, approval and refund requests can arrive
        concurrently from Telegram retries or two administrators.
        """
        if isinstance(connection, _PostgresConnection):
            connection.execute(
                "SELECT id FROM orders WHERE id = ? FOR UPDATE", (order_id,)
            ).fetchone()

    @staticmethod
    def _assert_not_giveaway_winner(connection: Any, telegram_id: int) -> None:
        if not isinstance(connection, _PostgresConnection):
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'giveaway_claims'"
            ).fetchone()
            if table is None:
                return
        winner = connection.execute(
            "SELECT 1 FROM giveaway_claims WHERE telegram_id = ? LIMIT 1",
            (telegram_id,),
        ).fetchone()
        if winner is not None:
            raise CommerceError(
                "Your 100 GiB giveaway win is your final AuriX entitlement; "
                "additional free or paid plans are disabled for this account."
            )

    def initialize(self) -> None:
        self.database.initialize()

    def plans(self) -> list[Plan]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT code, name, price_minor, currency, quota_bytes, duration_days
                   FROM plans WHERE active = 1 ORDER BY price_minor"""
            ).fetchall()
        return [Plan(**dict(row)) for row in rows]

    def get_plan(self, code: str) -> Plan:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT code, name, price_minor, currency, quota_bytes, duration_days
                   FROM plans WHERE code = ? AND active = 1""",
                (code,),
            ).fetchone()
        if row is None:
            raise CommerceError("Unknown or inactive plan")
        return Plan(**dict(row))

    @staticmethod
    def _ensure_user(
        connection: sqlite3.Connection,
        telegram_id: int,
        first_name: str,
        username: str | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO users (telegram_id, first_name, username, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                   first_name = excluded.first_name,
                   username = COALESCE(excluded.username, users.username)""",
            (
                telegram_id,
                first_name[:128],
                (username or "").lstrip("@")[:64] or None,
                _now_text(),
            ),
        )

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        action: str,
        target_type: str,
        target_id: str,
        actor_type: str,
        actor_id: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO audit_events
               (actor_type, actor_id, action, target_type, target_id, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                actor_type,
                actor_id,
                action,
                target_type,
                target_id,
                json.dumps(metadata or {}, sort_keys=True),
                _now_text(),
            ),
        )

    def create_order(
        self,
        telegram_id: int,
        first_name: str,
        plan_code: str,
        now: datetime | None = None,
        username: str | None = None,
    ) -> OrderResult:
        plan = self.get_plan(plan_code)
        order_id = _new_id()
        created_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._ensure_user(connection, telegram_id, first_name, username)
            if isinstance(connection, _PostgresConnection):
                connection.execute(
                    "SELECT telegram_id FROM users WHERE telegram_id = ? FOR UPDATE",
                    (telegram_id,),
                ).fetchone()
            self._assert_not_giveaway_winner(connection, telegram_id)
            existing = connection.execute(
                """SELECT * FROM orders
                   WHERE telegram_id = ?
                     AND status IN ('awaiting_payment', 'payment_submitted')
                     AND COALESCE(refund_status, 'none') != 'refunded'
                   ORDER BY created_at LIMIT 1""",
                (telegram_id,),
            ).fetchone()
            if existing is not None:
                existing_plan = Plan(
                    code=str(existing["plan_code"]),
                    name=str(existing["plan_name"] or plan.name),
                    price_minor=int(existing["amount_minor"]),
                    currency=str(existing["currency"]),
                    quota_bytes=(
                        existing["quota_bytes_snapshot"]
                        if existing["quota_bytes_snapshot"] is not None
                        else plan.quota_bytes
                    ),
                    duration_days=int(existing["duration_days_snapshot"] or plan.duration_days),
                )
                return OrderResult(
                    str(existing["id"]),
                    existing_plan,
                    str(existing["status"]),
                    False,
                    existing_plan.code != plan.code,
                )
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, plan_name,
                    quota_bytes_snapshot, duration_days_snapshot, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_payment', ?)""",
                (
                    order_id,
                    telegram_id,
                    plan.code,
                    plan.price_minor,
                    plan.currency,
                    plan.name,
                    plan.quota_bytes,
                    plan.duration_days,
                    created_at,
                ),
            )
            self._audit(
                connection,
                "order_created",
                "order",
                order_id,
                "customer",
                str(telegram_id),
                {"plan_code": plan.code, "amount_minor": plan.price_minor},
            )
        return OrderResult(order_id, plan, "awaiting_payment")

    def replace_open_order(
        self,
        telegram_id: int,
        first_name: str,
        plan_code: str,
        now: datetime | None = None,
        username: str | None = None,
        expected_order_id: str | None = None,
    ) -> OrderResult:
        """Replace an untouched open order with a different plan."""
        plan = self.get_plan(plan_code)
        created_at = _now_text(now)
        new_order_id = _new_id()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._ensure_user(connection, telegram_id, first_name, username)
            if isinstance(connection, _PostgresConnection):
                connection.execute(
                    "SELECT telegram_id FROM users WHERE telegram_id = ? FOR UPDATE",
                    (telegram_id,),
                ).fetchone()
            self._assert_not_giveaway_winner(connection, telegram_id)
            existing = connection.execute(
                """SELECT * FROM orders
                   WHERE telegram_id = ? AND status IN ('awaiting_payment', 'payment_submitted')
                     AND COALESCE(refund_status, 'none') != 'refunded'
                   ORDER BY created_at LIMIT 1""",
                (telegram_id,),
            ).fetchone()
            if existing is None:
                raise CommerceError("No open order is available to replace")
            self._lock_order(connection, str(existing["id"]))
            if expected_order_id and str(existing["id"]) != str(expected_order_id):
                raise CommerceError("The open order changed; refresh and try again")
            if existing["plan_code"] == plan.code:
                return OrderResult(str(existing["id"]), plan, str(existing["status"]), False)
            evidence_count = connection.execute(
                "SELECT COUNT(*) AS n FROM payment_evidence WHERE order_id = ?",
                (existing["id"],),
            ).fetchone()["n"]
            payment_count = connection.execute(
                "SELECT COUNT(*) AS n FROM payments WHERE order_id = ?",
                (existing["id"],),
            ).fetchone()["n"]
            if evidence_count or payment_count:
                raise CommerceError(
                    "This order has payment activity and cannot be replaced; ask staff to review it"
                )
            connection.execute(
                "UPDATE orders SET status = 'cancelled', rejected_at = ? WHERE id = ?",
                (created_at, existing["id"]),
            )
            self._audit(
                connection,
                "order_replaced",
                "order",
                str(existing["id"]),
                "customer",
                str(telegram_id),
                {"new_order_id": new_order_id, "plan_code": plan.code},
            )
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, plan_name,
                    quota_bytes_snapshot, duration_days_snapshot, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_payment', ?)""",
                (
                    new_order_id,
                    telegram_id,
                    plan.code,
                    plan.price_minor,
                    plan.currency,
                    plan.name,
                    plan.quota_bytes,
                    plan.duration_days,
                    created_at,
                ),
            )
            self._audit(
                connection,
                "order_created",
                "order",
                new_order_id,
                "customer",
                str(telegram_id),
                {"plan_code": plan.code, "replaces_order_id": str(existing["id"])},
            )
        return OrderResult(new_order_id, plan, "awaiting_payment")

    def cancel_order(self, telegram_id: int, order_id: str, now: datetime | None = None) -> str:
        """Cancel an empty customer order, or release a wallet reservation."""
        cancelled_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute(
                "SELECT * FROM orders WHERE id = ? AND telegram_id = ?",
                (order_id, telegram_id),
            ).fetchone()
            if order is None:
                raise CommerceError("Order not found")
            if order["status"] in ("cancelled", "rejected"):
                return "already_cancelled"
            if order["status"] == "approved":
                raise CommerceError("An approved order cannot be cancelled")
            evidence_count = connection.execute(
                "SELECT COUNT(*) AS n FROM payment_evidence WHERE order_id = ?",
                (order_id,),
            ).fetchone()["n"]
            payment_count = connection.execute(
                "SELECT COUNT(*) AS n FROM payments WHERE order_id = ?",
                (order_id,),
            ).fetchone()["n"]
            if evidence_count or payment_count:
                raise CommerceError(
                    "This order has payment activity; ask staff to reject or refund it"
                )
            connection.execute(
                "UPDATE orders SET status = 'cancelled', rejected_at = ? WHERE id = ?",
                (cancelled_at, order_id),
            )
            self._audit(
                connection,
                "order_cancelled",
                "order",
                order_id,
                "customer",
                str(telegram_id),
            )
        return "cancelled"

    def expire_open_orders(
        self,
        now: datetime | None = None,
        awaiting_ttl: timedelta = timedelta(hours=24),
    ) -> int:
        """Close only untouched unpaid orders past the customer-facing TTL."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        cutoff = _now_text(current - awaiting_ttl)
        closed = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            rows = connection.execute(
                """SELECT o.id FROM orders o
                   WHERE o.status = 'awaiting_payment' AND o.created_at <= ?
                     AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.id)
                     AND NOT EXISTS (SELECT 1 FROM payment_evidence e WHERE e.order_id = o.id)""",
                (cutoff,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE orders SET status = 'cancelled', rejected_at = ? WHERE id = ?",
                    (_now_text(current), row["id"]),
                )
                self._audit(
                    connection,
                    "order_expired",
                    "order",
                    str(row["id"]),
                    "system",
                    None,
                )
                closed += 1
        return closed

    def release_expired_wallet_reservations(
        self,
        now: datetime | None = None,
        reservation_ttl: timedelta = timedelta(hours=24),
    ) -> int:
        """Release wallet holds that were never approved by staff."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        cutoff = _now_text(current - reservation_ttl)
        released = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            rows = connection.execute(
                """SELECT r.order_id, r.telegram_id, r.amount_minor, r.currency
                   FROM wallet_reservations r JOIN orders o ON o.id = r.order_id
                   WHERE r.status = 'reserved' AND r.created_at <= ?
                     AND o.status = 'payment_submitted'""",
                (cutoff,),
            ).fetchall()
            for row in rows:
                idem = f"release:{row['order_id']}"
                if (
                    connection.execute(
                        "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (idem,)
                    ).fetchone()
                    is None
                ):
                    connection.execute(
                        "UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ? WHERE telegram_id = ?",
                        (row["amount_minor"], _now_text(current), row["telegram_id"]),
                    )
                    connection.execute(
                        """INSERT INTO wallet_ledger
                           (id, telegram_id, kind, amount_minor, currency, reference_type,
                            reference_id, idempotency_key, created_at)
                           VALUES (?, ?, 'release', ?, ?, 'order', ?, ?, ?)""",
                        (
                            _new_id(),
                            row["telegram_id"],
                            row["amount_minor"],
                            row["currency"],
                            row["order_id"],
                            idem,
                            _now_text(current),
                        ),
                    )
                connection.execute(
                    "UPDATE wallet_reservations SET status = 'released', updated_at = ? WHERE order_id = ?",
                    (_now_text(current), row["order_id"]),
                )
                connection.execute(
                    "UPDATE payments SET status = 'rejected' WHERE order_id = ? AND provider = 'wallet' AND status = 'submitted'",
                    (row["order_id"],),
                )
                connection.execute(
                    "UPDATE orders SET status = 'cancelled', rejected_at = ? WHERE id = ? AND status = 'payment_submitted'",
                    (_now_text(current), row["order_id"]),
                )
                connection.execute(
                    """INSERT INTO notifications
                       (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
                       VALUES (?, ?, ?, 'wallet_reservation_expired', ?, 'pending', ?, ?)
                       ON CONFLICT(dedupe_key) DO NOTHING""",
                    (
                        _new_id(),
                        f"wallet-reservation-expired:{row['order_id']}",
                        row["telegram_id"],
                        "Your wallet payment hold expired before approval; the funds were returned to your wallet.",
                        _now_text(current),
                        _now_text(current),
                    ),
                )
                self._audit(
                    connection,
                    "wallet_reservation_expired",
                    "order",
                    row["order_id"],
                    "system",
                    None,
                )
                released += 1
        return released

    @staticmethod
    def _order_stage(order: dict[str, Any]) -> str:
        """Derive one customer-facing stage from order/payment/evidence state."""
        status = str(order.get("status") or "")
        subscription = str(order.get("subscription_status") or "")
        payment = str(order.get("payment_status") or "")
        receipt = str(order.get("receipt_status") or "")
        reservation = str(order.get("wallet_reservation_status") or "")
        if str(order.get("refund_status") or "none") == "refunded" or payment == "refunded":
            return "refunded"
        provision = str(order.get("provisioning_status") or "")
        revoke = str(order.get("revocation_status") or "")
        if revoke in ("pending", "running"):
            return "revocation_pending"
        if revoke == "failed":
            return "revocation_failed"
        if status == "approved":
            if provision == "failed":
                return "activation_failed"
            if subscription == "active":
                return "fulfilled"
            if subscription == "pending":
                return "activation_pending"
            return "approved"
        if status in ("rejected", "cancelled"):
            return status
        if receipt == "verified" or payment == "verified":
            return "payment_verified"
        if reservation == "reserved":
            return "wallet_reserved"
        if receipt == "pending" or payment == "submitted":
            return "review_pending"
        return "awaiting_payment"

    def list_user_orders(self, telegram_id: int, limit: int = 10) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
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
                (telegram_id, max(1, min(limit, 50))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["stage"] = self._order_stage(item)
            result.append(item)
        return result

    def reconcile_duplicate_open_orders(self) -> dict[str, int]:
        """Cancel only empty historical duplicates, preserving review evidence."""
        cancelled = 0
        manual_conflicts = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            users = connection.execute(
                """SELECT telegram_id FROM orders
                   WHERE status IN ('awaiting_payment', 'payment_submitted')
                   GROUP BY telegram_id HAVING COUNT(*) > 1"""
            ).fetchall()
            for user in users:
                rows = connection.execute(
                    """SELECT o.id, o.created_at,
                              (SELECT COUNT(*) FROM payments p WHERE p.order_id = o.id) AS payments,
                              (SELECT COUNT(*) FROM payment_evidence e WHERE e.order_id = o.id) AS evidence
                       FROM orders o WHERE o.telegram_id = ?
                         AND o.status IN ('awaiting_payment', 'payment_submitted')
                       ORDER BY o.created_at""",
                    (user["telegram_id"],),
                ).fetchall()
                protected = [row for row in rows if int(row["payments"]) or int(row["evidence"])]
                keeper_id = (protected[0] if protected else rows[0])["id"]
                if len(protected) > 1:
                    manual_conflicts += 1
                for row in rows:
                    if row["id"] == keeper_id:
                        continue
                    if int(row["payments"]) or int(row["evidence"]):
                        continue
                    connection.execute(
                        "UPDATE orders SET status = 'cancelled' WHERE id = ?",
                        (row["id"],),
                    )
                    self._audit(
                        connection,
                        "duplicate_empty_order_cancelled",
                        "order",
                        str(row["id"]),
                        "system",
                        None,
                        {"kept_order_id": str(keeper_id)},
                    )
                    cancelled += 1
        return {"cancelled": cancelled, "manual_conflicts": manual_conflicts}

    def order_detail(
        self, order_id: str, requester_id: int, is_admin: bool = False
    ) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
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
                           ON s.id = j.subscription_id
                           WHERE s.order_id = o.id AND j.operation = 'provision'
                           LIMIT 1) AS provisioning_status,
                          (SELECT j.status FROM provisioning_jobs j JOIN subscriptions s
                           ON s.id = j.subscription_id
                           WHERE s.order_id = o.id AND j.operation = 'revoke'
                           LIMIT 1) AS revocation_status,
                          (SELECT r.status FROM wallet_reservations r WHERE r.order_id = o.id
                           LIMIT 1) AS wallet_reservation_status
                   FROM orders o WHERE o.id = ?""",
                (order_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if not is_admin and int(result["telegram_id"]) != int(requester_id):
            return None
        result["stage"] = self._order_stage(result)
        return result

    def submit_payment(
        self,
        telegram_id: int,
        order_id: str,
        provider: str,
        provider_reference: str,
        now: datetime | None = None,
    ) -> str:
        provider = provider.strip()[:64]
        provider_reference = provider_reference.strip()[:128]
        normalized_reference = _normalize_reference(provider_reference)
        if not provider or not provider_reference:
            raise CommerceError("Payment provider and reference are required")
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if order is None or order["telegram_id"] != telegram_id:
                raise CommerceError("Order not found")
            self._assert_not_giveaway_winner(connection, telegram_id)
            if order["status"] == "approved":
                raise CommerceError("Order is already approved")
            if order["status"] not in ("awaiting_payment", "payment_submitted"):
                raise CommerceError("Order is not open for payment")
            existing = connection.execute(
                """SELECT provider, provider_reference FROM payments WHERE order_id = ?
                   ORDER BY submitted_at DESC LIMIT 1""",
                (order_id,),
            ).fetchone()
            if existing is not None:
                if (
                    _normalize_reference(existing["provider"]) == _normalize_reference(provider)
                    and _normalize_reference(existing["provider_reference"]) == normalized_reference
                ):
                    return "already_submitted"
                raise CommerceError("A payment reference is already attached to this order")
            try:
                connection.execute(
                    """INSERT INTO payments
                       (id, order_id, provider, provider_reference, normalized_reference, status, submitted_at)
                       VALUES (?, ?, ?, ?, ?, 'submitted', ?)""",
                    (
                        _new_id(),
                        order_id,
                        provider,
                        provider_reference,
                        normalized_reference,
                        _now_text(now),
                    ),
                )
            except Exception as exc:
                if self.database.is_integrity_error(exc):
                    raise CommerceError("Payment reference has already been submitted") from exc
                raise
            connection.execute(
                "UPDATE orders SET status = 'payment_submitted' WHERE id = ?",
                (order_id,),
            )
            self._audit(
                connection,
                "payment_submitted",
                "order",
                order_id,
                "customer",
                str(telegram_id),
                {"provider": provider},
            )
        return "submitted"

    def pending_order_for_user(self, telegram_id: int) -> dict[str, Any] | None:
        """Return the oldest open order so a receipt can be sent without text."""
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT * FROM orders
                   WHERE telegram_id = ? AND status IN ('awaiting_payment', 'payment_submitted')
                     AND COALESCE(refund_status, 'none') != 'refunded'
                   ORDER BY created_at LIMIT 1""",
                (telegram_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def submit_receipt(
        self,
        telegram_id: int,
        order_id: str,
        provider: str,
        file_id: str,
        file_unique_id: str | None,
        image_bytes: bytes,
        mime_type: str,
        extraction: dict[str, Any] | None = None,
        now: datetime | None = None,
        telegram_media_type: str = "photo",
    ) -> dict[str, Any]:
        """Persist receipt metadata and upload the raw image out-of-band.

        The database transaction creates an upload-pending evidence row, then
        the object is uploaded without holding a database connection open. A
        second short transaction marks the object stored and moves the order to
        payment review. This keeps network latency out of the database lock and
        makes a lost response safely retryable using the same immutable path.
        """
        if not isinstance(file_id, str) or not file_id.strip():
            raise CommerceError("Receipt file id is missing")
        if not image_bytes or len(image_bytes) > 20 * 1024 * 1024:
            raise CommerceError("Receipt image is empty or too large")
        if telegram_media_type not in ("photo", "document"):
            raise CommerceError("Receipt media type is invalid")
        digest = hashlib.sha256(image_bytes).hexdigest()
        extraction = extraction if isinstance(extraction, dict) else None
        tx_id = extraction.get("transaction_id") if extraction else None
        provider_name = str((extraction or {}).get("provider") or provider).strip()[:64]
        tx_candidate = str(tx_id).strip()[:128] if tx_id else ""
        status = "parsed" if tx_id else "needs_review"
        submitted_at = _now_text(now)
        storage_configured = self._storage_is_configured()
        if self.receipt_storage_required and not storage_configured:
            raise CommerceError("Receipt storage is not configured")
        storage_status = "pending" if storage_configured else "not_configured"
        storage_bucket = self._storage_bucket() if storage_configured else None
        evidence_id: str
        storage_path: str | None = None
        is_new = False
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if order is None or order["telegram_id"] != telegram_id:
                raise CommerceError("Order not found")
            self._assert_not_giveaway_winner(connection, telegram_id)
            if order["status"] == "approved":
                raise CommerceError("Order is already approved")
            if order["status"] not in ("awaiting_payment", "payment_submitted"):
                raise CommerceError("Order is not open for a receipt")
            existing = connection.execute(
                """SELECT id, extraction_json, extraction_status, review_status,
                          storage_bucket, storage_path, storage_status
                   FROM payment_evidence
                   WHERE order_id = ? AND image_sha256 = ?""",
                (order_id, digest),
            ).fetchone()
            if existing is not None:
                parsed = json.loads(existing["extraction_json"] or "{}")
                result = parsed if isinstance(parsed, dict) else {}
                result["evidence_id"] = existing["id"]
                result["extraction_status"] = existing["extraction_status"]
                result["review_status"] = existing["review_status"]
                result["image_sha256"] = digest
                result["storage_status"] = existing["storage_status"] or "not_configured"
                result["storage_path"] = existing["storage_path"]
                storage_ready = (
                    result["storage_status"] == "stored"
                    if storage_configured
                    else result["storage_status"] in ("stored", "not_configured")
                )
                if storage_ready:
                    # A prior process may have committed the evidence row but
                    # lost the response before moving the order state. Repair
                    # that narrow inconsistency on an idempotent retry.
                    if order["status"] == "awaiting_payment":
                        connection.execute(
                            "UPDATE orders SET status = 'payment_submitted' WHERE id = ?",
                            (order_id,),
                        )
                        self._audit(
                            connection,
                            "receipt_state_recovered",
                            "order",
                            order_id,
                            "customer",
                            str(telegram_id),
                            {"evidence_id": existing["id"]},
                        )
                    return result
                evidence_id = str(existing["id"])
                storage_path = str(existing["storage_path"] or "") or self._receipt_storage_path(
                    order_id, evidence_id, mime_type
                )
                connection.execute(
                    """UPDATE payment_evidence
                       SET storage_bucket = ?, storage_path = ?, storage_status = 'pending',
                           storage_error = NULL
                       WHERE id = ?""",
                    (storage_bucket, storage_path, evidence_id),
                )
            else:
                latest = connection.execute(
                    """SELECT review_status FROM payment_evidence
                       WHERE order_id = ? ORDER BY submitted_at DESC LIMIT 1""",
                    (order_id,),
                ).fetchone()
                if latest is not None and str(latest["review_status"] or "pending") != "rejected":
                    raise CommerceError(
                        "A receipt is already awaiting review; wait for staff feedback"
                    )
                payment_rows = connection.execute(
                    "SELECT provider, status FROM payments WHERE order_id = ?",
                    (order_id,),
                ).fetchall()
                if any(str(item["provider"] or "").lower() == "wallet" for item in payment_rows):
                    raise CommerceError(
                        "This order already uses wallet payment; receipt payment cannot be combined"
                    )
                if any(
                    str(item["status"] or "") in ("submitted", "verified", "refunded")
                    for item in payment_rows
                ):
                    raise CommerceError("A payment is already attached to this order")
                # Keep model output as evidence only. Detect a repeated
                # candidate across screenshots without creating an
                # authoritative payment row.
                if tx_candidate:
                    prior_evidence = connection.execute(
                        "SELECT provider, extraction_json FROM payment_evidence WHERE order_id != ?",
                        (order_id,),
                    ).fetchall()
                    for prior in prior_evidence:
                        try:
                            prior_extraction = json.loads(prior["extraction_json"] or "{}")
                        except json.JSONDecodeError:
                            prior_extraction = {}
                        prior_tx = (
                            prior_extraction.get("transaction_id")
                            if isinstance(prior_extraction, dict)
                            else None
                        )
                        if _normalize_reference(str(prior["provider"])) == _normalize_reference(
                            provider_name or "manual"
                        ) and _normalize_reference(str(prior_tx or "")) == _normalize_reference(
                            tx_candidate
                        ):
                            status = "needs_review"
                            flagged = dict(extraction or {})
                            flagged["flags"] = sorted(
                                set(flagged.get("flags") or [])
                                | {"duplicate_transaction_candidate"}
                            )
                            extraction = flagged
                            break
                evidence_id = _new_id()
                storage_path = (
                    self._receipt_storage_path(order_id, evidence_id, mime_type)
                    if storage_configured
                    else None
                )
                connection.execute(
                    """INSERT INTO payment_evidence
                       (id, order_id, telegram_id, provider, telegram_file_id,
                        telegram_file_unique_id, telegram_media_type, image_sha256,
                        mime_type, byte_size, storage_bucket, storage_path,
                        storage_status, extraction_json, extraction_status,
                        submitted_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        evidence_id,
                        order_id,
                        telegram_id,
                        provider_name,
                        file_id[:256],
                        file_unique_id[:256] if file_unique_id else None,
                        telegram_media_type,
                        digest,
                        mime_type[:64],
                        len(image_bytes),
                        storage_bucket,
                        storage_path,
                        storage_status,
                        json.dumps(extraction or {}, sort_keys=True),
                        status,
                        submitted_at,
                    ),
                )
                is_new = True

        if storage_configured:
            assert storage_path is not None
            try:
                uploaded_path = self.receipt_storage.upload(storage_path, image_bytes, mime_type)
                uploaded_path = str(uploaded_path or "").strip()
                if not uploaded_path:
                    raise RuntimeError("Receipt storage returned an empty object path")
            except Exception as exc:
                # Preserve the row so a retry can reuse the same object path.
                try:
                    with self.database.connect() as connection:
                        connection.execute(
                            """UPDATE payment_evidence
                               SET storage_status = 'failed', storage_error = ?
                               WHERE id = ?""",
                            (type(exc).__name__[:128], evidence_id),
                        )
                except Exception:
                    pass
                raise CommerceError("Receipt image could not be saved. Please try again.") from exc
            try:
                storage_path = uploaded_path
                with self.database.connect() as connection:
                    self.database.begin_write(connection)
                    connection.execute(
                        """UPDATE payment_evidence
                           SET storage_bucket = ?, storage_path = ?, storage_status = 'stored',
                               storage_error = NULL, stored_at = ?
                           WHERE id = ?""",
                        (storage_bucket, str(uploaded_path), submitted_at, evidence_id),
                    )
                    connection.execute(
                        "UPDATE orders SET status = 'payment_submitted' WHERE id = ?",
                        (order_id,),
                    )
                    self._audit(
                        connection,
                        "receipt_submitted" if is_new else "receipt_storage_recovered",
                        "order",
                        order_id,
                        "customer",
                        str(telegram_id),
                        {"evidence_id": evidence_id, "extraction_status": status},
                    )
            except Exception:
                # Do not leave a billable orphan if the final metadata commit
                # fails. Deletion is best-effort and the row remains retryable.
                try:
                    self.receipt_storage.delete(storage_path)
                except Exception:
                    pass
                raise
        else:
            # A receipt is a payment submission even when OCR/LLM extraction
            # failed. Approval still requires a human verification decision.
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                connection.execute(
                    "UPDATE orders SET status = 'payment_submitted' WHERE id = ?",
                    (order_id,),
                )
                if is_new:
                    self._audit(
                        connection,
                        "receipt_submitted",
                        "order",
                        order_id,
                        "customer",
                        str(telegram_id),
                        {"evidence_id": evidence_id, "extraction_status": status},
                    )
        result = dict(extraction or {})
        result["evidence_id"] = evidence_id
        result["image_sha256"] = digest
        result["extraction_status"] = status
        result["storage_status"] = "stored" if storage_configured else "not_configured"
        result["storage_path"] = storage_path
        return result

    def list_pending_receipts(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT e.id, e.order_id, e.telegram_id, e.provider, e.image_sha256,
                          e.byte_size, e.storage_bucket, e.storage_path, e.storage_status,
                          e.extraction_json, e.extraction_status, e.submitted_at,
                          o.plan_code, o.amount_minor, o.currency
                   FROM payment_evidence e JOIN orders o ON o.id = e.order_id
                   WHERE e.review_status = 'pending'
                     AND e.storage_status IN ('stored', 'not_configured')
                   ORDER BY e.submitted_at LIMIT ?""",
                (max(1, min(limit, 100)),),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            try:
                item["extraction"] = json.loads(item.pop("extraction_json") or "{}")
            except json.JSONDecodeError:
                item["extraction"] = {}
            results.append(item)
        return results

    def get_receipt(self, evidence_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT e.*, o.plan_code, o.amount_minor, o.currency, o.status AS order_status
                   FROM payment_evidence e JOIN orders o ON o.id = e.order_id
                   WHERE e.id = ?""",
                (evidence_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["extraction"] = json.loads(result.pop("extraction_json") or "{}")
        except json.JSONDecodeError:
            result["extraction"] = {}
        return result

    def verify_receipt(
        self,
        evidence_id: str,
        admin_id: int,
        provider_reference: str,
        verified_amount_minor: int,
        currency: str = "MMK",
        now: datetime | None = None,
    ) -> str:
        """Record a human verification against the receiving account.

        LLM extraction is deliberately excluded from this trust boundary.  The
        reviewer must supply the transaction ID and amount observed in the
        actual receiving account before an order can be approved.
        """
        provider_reference = provider_reference.strip()[:128]
        currency = currency.strip().upper()[:16]
        try:
            verified_amount_minor = int(verified_amount_minor)
        except (TypeError, ValueError) as exc:
            raise CommerceError("Verified payment amount must be an integer") from exc
        if not provider_reference:
            raise CommerceError("Verified transaction ID is required")
        if verified_amount_minor <= 0:
            raise CommerceError("Verified payment amount must be positive")
        reviewed_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            evidence = connection.execute(
                """SELECT e.*, o.amount_minor, o.currency, o.status AS order_status
                   FROM payment_evidence e JOIN orders o ON o.id = e.order_id
                   WHERE e.id = ?""",
                (evidence_id,),
            ).fetchone()
            if evidence is None:
                raise CommerceError("Receipt evidence not found")
            self._lock_order(connection, str(evidence["order_id"]))
            if evidence["order_status"] == "approved":
                return evidence["order_id"]
            if self.receipt_storage_required and str(evidence["storage_status"] or "") != "stored":
                raise CommerceError("Receipt image must be stored before verification")
            if evidence["review_status"] == "verified":
                if (
                    str(evidence["verified_provider_reference"] or "").strip().casefold()
                    == provider_reference.casefold()
                    and int(evidence["verified_amount_minor"] or 0) == verified_amount_minor
                    and str(evidence["verified_currency"] or "").upper() == currency
                ):
                    return evidence["order_id"]
                raise CommerceError("Receipt verification is already recorded")
            if evidence["review_status"] == "rejected":
                raise CommerceError("Receipt was rejected; submit a new screenshot")
            if evidence["order_status"] not in ("awaiting_payment", "payment_submitted"):
                raise CommerceError("Order is not open for receipt verification")
            if currency != str(evidence["currency"]).upper():
                raise CommerceError("Verified payment currency does not match the order")
            if verified_amount_minor < int(evidence["amount_minor"]):
                raise CommerceError("Verified payment amount is below the order total")
            payment = connection.execute(
                """SELECT id FROM payments
                   WHERE order_id = ? AND status IN ('submitted', 'verified')
                   ORDER BY submitted_at DESC LIMIT 1""",
                (evidence["order_id"],),
            ).fetchone()
            provider = str(evidence["provider"] or "manual")[:64]
            normalized_provider = _normalize_reference(provider)
            normalized_reference = _normalize_reference(provider_reference)
            conflicts = connection.execute(
                """SELECT order_id, provider, normalized_reference FROM payments
                   WHERE status IN ('submitted', 'verified')
                     AND normalized_reference = ? AND order_id != ?""",
                (normalized_reference, evidence["order_id"]),
            ).fetchall()
            if any(
                _normalize_reference(str(item["provider"])) == normalized_provider
                for item in conflicts
            ):
                raise CommerceError(
                    "This transaction ID has already been submitted for another order"
                )
            try:
                if payment is None:
                    payment_id = _new_id()
                    connection.execute(
                        """INSERT INTO payments
                           (id, order_id, provider, provider_reference, normalized_reference,
                            status, submitted_at, verified_at)
                           VALUES (?, ?, ?, ?, ?, 'verified', ?, ?)""",
                        (
                            payment_id,
                            evidence["order_id"],
                            provider,
                            provider_reference,
                            normalized_reference,
                            reviewed_at,
                            reviewed_at,
                        ),
                    )
                else:
                    payment_id = payment["id"]
                    connection.execute(
                        """UPDATE payments
                           SET provider = ?, provider_reference = ?, normalized_reference = ?,
                               status = 'verified', verified_at = ?
                           WHERE id = ?""",
                        (
                            provider,
                            provider_reference,
                            normalized_reference,
                            reviewed_at,
                            payment_id,
                        ),
                    )
            except Exception as exc:
                if self.database.is_integrity_error(exc):
                    raise CommerceError("This transaction ID has already been verified") from exc
                raise
            connection.execute(
                """UPDATE payment_evidence
                   SET reviewer_id = ?, review_notes = 'verified against receiving account',
                       review_status = 'verified', verified_provider_reference = ?,
                       verified_amount_minor = ?, verified_currency = ?, reviewed_at = ?
                   WHERE id = ?""",
                (
                    admin_id,
                    provider_reference,
                    verified_amount_minor,
                    currency,
                    reviewed_at,
                    evidence_id,
                ),
            )
            connection.execute(
                "UPDATE orders SET status = 'payment_submitted' WHERE id = ?",
                (evidence["order_id"],),
            )
            self._audit(
                connection,
                "receipt_verified",
                "payment_evidence",
                evidence_id,
                "admin",
                str(admin_id),
                {"amount_minor": verified_amount_minor, "currency": currency},
            )
        return str(evidence["order_id"])

    def reject_receipt(
        self,
        evidence_id: str,
        admin_id: int,
        notes: str = "rejected by admin",
        now: datetime | None = None,
    ) -> str:
        reviewed_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            evidence = connection.execute(
                "SELECT id, order_id, review_status FROM payment_evidence WHERE id = ?",
                (evidence_id,),
            ).fetchone()
            if evidence is None:
                raise CommerceError("Receipt evidence not found")
            self._lock_order(connection, str(evidence["order_id"]))
            if evidence["review_status"] == "verified":
                raise CommerceError("Verified receipt cannot be rejected")
            if evidence["review_status"] == "rejected":
                return str(evidence["order_id"])
            connection.execute(
                """UPDATE payment_evidence SET reviewer_id = ?, review_notes = ?,
                           review_status = 'rejected', reviewed_at = ? WHERE id = ?""",
                (admin_id, (notes or "rejected by admin")[:500], reviewed_at, evidence_id),
            )
            connection.execute(
                "UPDATE payments SET status = 'rejected' WHERE order_id = ? AND status = 'submitted'",
                (evidence["order_id"],),
            )
            self._audit(
                connection,
                "receipt_rejected",
                "payment_evidence",
                evidence_id,
                "admin",
                str(admin_id),
                {"notes": (notes or "")[:500]},
            )
        return str(evidence["order_id"])

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
            approved_missing_subscription = connection.execute(
                """SELECT COUNT(*) AS n FROM orders o
                   LEFT JOIN subscriptions s ON s.order_id = o.id
                   WHERE o.status = 'approved' AND s.id IS NULL"""
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
            "wallet_balance_mismatches": int(wallet_mismatches),
        }

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
            order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if order is None or order["telegram_id"] != telegram_id:
                raise CommerceError("Order not found")
            self._assert_not_giveaway_winner(connection, telegram_id)
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
            payments = connection.execute(
                "SELECT provider, status FROM payments WHERE order_id = ?",
                (order_id,),
            ).fetchall()
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
                "UPDATE orders SET status = 'payment_submitted' WHERE id = ?", (order_id,)
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

    def approve_order(
        self,
        order_id: str,
        admin_id: int,
        now: datetime | None = None,
    ) -> ApprovalResult:
        starts_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if order is None:
                raise CommerceError("Order not found")
            self._assert_not_giveaway_winner(connection, int(order["telegram_id"]))
            if order["status"] == "approved":
                subscription = connection.execute(
                    "SELECT id FROM subscriptions WHERE order_id = ?", (order_id,)
                ).fetchone()
                if subscription is None:
                    raise CommerceError("Approved order has no subscription record")
                return ApprovalResult(order_id, subscription["id"], "already_approved")
            if order["status"] != "payment_submitted":
                raise CommerceError("Order has no submitted payment for review")
            evidence = connection.execute(
                """SELECT review_status, verified_amount_minor, verified_currency,
                          storage_status
                   FROM payment_evidence WHERE order_id = ?
                   ORDER BY submitted_at DESC LIMIT 1""",
                (order_id,),
            ).fetchone()
            wallet_reservation = connection.execute(
                """SELECT id, amount_minor, currency, status
                   FROM wallet_reservations WHERE order_id = ? LIMIT 1""",
                (order_id,),
            ).fetchone()
            if evidence is not None and evidence["review_status"] != "verified":
                if wallet_reservation is None or wallet_reservation["status"] != "reserved":
                    raise CommerceError(
                        "Receipt must be verified against the receiving account first"
                    )
            if (
                evidence is not None
                and self.receipt_storage_required
                and str(evidence["storage_status"] or "") != "stored"
            ):
                raise CommerceError("Receipt image must be stored before approval")
            payment = connection.execute(
                """SELECT id, provider, status FROM payments WHERE order_id = ?
                   AND status IN ('submitted', 'verified')
                   ORDER BY submitted_at DESC LIMIT 1""",
                (order_id,),
            ).fetchone()
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
            if evidence is None and not wallet_payment and not self.allow_legacy_text_approval:
                raise CommerceError("Verified receipt evidence is required before approval")
            if evidence is not None and wallet_payment:
                raise CommerceError("An order cannot combine a wallet payment and receipt evidence")
            plan = connection.execute(
                "SELECT duration_days, quota_bytes, name FROM plans WHERE code = ?",
                (order["plan_code"],),
            ).fetchone()
            if plan is None:
                raise CommerceError("Plan record is missing")
            duration_days = int(order["duration_days_snapshot"] or plan["duration_days"])
            plan_name = str(order["plan_name"] or plan["name"])
            quota_bytes = (
                order["quota_bytes_snapshot"]
                if order["quota_bytes_snapshot"] is not None
                else plan["quota_bytes"]
            )
            # Each approved paid order represents an independent entitlement
            # and may provision its own key. A customer can therefore buy
            # multiple plans/devices at once; renewal is not serialized behind
            # an existing subscription.
            effective_start = datetime.fromisoformat(starts_at)
            expires_at = (effective_start + timedelta(days=duration_days)).isoformat()
            subscription_id = _new_id()
            if payment["status"] == "submitted":
                connection.execute(
                    """UPDATE payments SET status = 'verified', verified_at = ?
                       WHERE id = ?""",
                    (starts_at, payment["id"]),
                )
            connection.execute(
                """UPDATE orders SET status = 'approved', approved_at = ? WHERE id = ?""",
                (starts_at, order_id),
            )
            connection.execute(
                """INSERT INTO subscriptions
                   (id, order_id, telegram_id, plan_code, starts_at, expires_at,
                    plan_name, quota_bytes, duration_days, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    subscription_id,
                    order_id,
                    order["telegram_id"],
                    order["plan_code"],
                    effective_start.isoformat(),
                    expires_at,
                    plan_name,
                    quota_bytes,
                    duration_days,
                ),
            )
            # Record money movement as immutable ledger events. External
            # deposits are credited and immediately reserved/captured. Wallet
            # payments already have a reservation; approval only captures it.
            wallet_now = starts_at
            connection.execute(
                """INSERT INTO wallets (telegram_id, currency, balance_minor, created_at, updated_at)
                   VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
                (order["telegram_id"], order["currency"], wallet_now, wallet_now),
            )
            payment_id = payment["id"]
            credit_amount = int(order["amount_minor"])
            if evidence is not None:
                if str(evidence["verified_currency"]).upper() != str(order["currency"]).upper():
                    raise CommerceError("Verified receipt currency does not match the order")
                credit_amount = int(evidence["verified_amount_minor"])
            if not wallet_payment:
                credit_idem = f"credit:{payment_id}"
                credit_exists = connection.execute(
                    "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (credit_idem,)
                ).fetchone()
                if credit_exists is None:
                    connection.execute(
                        "UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ? WHERE telegram_id = ?",
                        (credit_amount, wallet_now, order["telegram_id"]),
                    )
                    connection.execute(
                        """INSERT INTO wallet_ledger
                           (id, telegram_id, kind, amount_minor, currency, reference_type,
                            reference_id, idempotency_key, created_at)
                           VALUES (?, ?, 'credit', ?, ?, 'payment', ?, ?, ?)""",
                        (
                            _new_id(),
                            order["telegram_id"],
                            credit_amount,
                            order["currency"],
                            payment_id,
                            credit_idem,
                            wallet_now,
                        ),
                    )
                reserve_idem = f"reserve:{order_id}"
                reserve_exists = connection.execute(
                    "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (reserve_idem,)
                ).fetchone()
                if reserve_exists is None:
                    updated = connection.execute(
                        "UPDATE wallets SET balance_minor = balance_minor - ?, updated_at = ? WHERE telegram_id = ? AND balance_minor >= ?",
                        (
                            order["amount_minor"],
                            wallet_now,
                            order["telegram_id"],
                            order["amount_minor"],
                        ),
                    )
                    if getattr(updated, "rowcount", 1) == 0:
                        raise CommerceError(
                            "Verified payment credit is insufficient for this order"
                        )
                    connection.execute(
                        """INSERT INTO wallet_ledger
                           (id, telegram_id, kind, amount_minor, currency, reference_type,
                            reference_id, idempotency_key, created_at)
                           VALUES (?, ?, 'reserve', ?, ?, 'order', ?, ?, ?)""",
                        (
                            _new_id(),
                            order["telegram_id"],
                            order["amount_minor"],
                            order["currency"],
                            order_id,
                            reserve_idem,
                            wallet_now,
                        ),
                    )
                    connection.execute(
                        """INSERT INTO wallet_reservations
                           (id, telegram_id, order_id, amount_minor, currency, status, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?)""",
                        (
                            _new_id(),
                            order["telegram_id"],
                            order_id,
                            order["amount_minor"],
                            order["currency"],
                            wallet_now,
                            wallet_now,
                        ),
                    )
            capture_idem = f"capture:{order_id}"
            if (
                connection.execute(
                    "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (capture_idem,)
                ).fetchone()
                is None
            ):
                # Capture is a state transition; the reserve already reduced
                # available balance, so capture must not deduct again.
                connection.execute(
                    """INSERT INTO wallet_ledger
                       (id, telegram_id, kind, amount_minor, currency, reference_type,
                        reference_id, idempotency_key, created_at)
                       VALUES (?, ?, 'capture', ?, ?, 'order', ?, ?, ?)""",
                    (
                        _new_id(),
                        order["telegram_id"],
                        order["amount_minor"],
                        order["currency"],
                        order_id,
                        capture_idem,
                        wallet_now,
                    ),
                )
            connection.execute(
                """INSERT INTO wallet_reservations
                   (id, telegram_id, order_id, amount_minor, currency, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'captured', ?, ?)
                   ON CONFLICT(order_id) DO UPDATE SET status = 'captured', updated_at = excluded.updated_at""",
                (
                    _new_id(),
                    order["telegram_id"],
                    order_id,
                    order["amount_minor"],
                    order["currency"],
                    wallet_now,
                    wallet_now,
                ),
            )
            connection.execute(
                """INSERT INTO provisioning_jobs
                   (id, subscription_id, operation, status, next_attempt_at, created_at)
                   VALUES (?, ?, 'provision', 'pending', ?, ?)""",
                (
                    _new_id(),
                    subscription_id,
                    effective_start.isoformat(),
                    effective_start.isoformat(),
                ),
            )
            self._audit(
                connection,
                "order_approved",
                "order",
                order_id,
                "admin",
                str(admin_id),
                {"subscription_id": subscription_id},
            )
        return ApprovalResult(order_id, subscription_id, "approved")

    def reject_order(self, order_id: str, admin_id: int, now: datetime | None = None) -> str:
        rejected_at = _now_text(now)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            self._lock_order(connection, order_id)
            order = connection.execute(
                "SELECT status, telegram_id FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if order is None:
                raise CommerceError("Order not found")
            if order["status"] == "rejected":
                return "already_rejected"
            if order["status"] == "approved":
                raise CommerceError("Approved order cannot be rejected here")
            verified_payment = connection.execute(
                "SELECT id FROM payments WHERE order_id = ? AND status = 'verified' LIMIT 1",
                (order_id,),
            ).fetchone()
            verified_receipt = connection.execute(
                "SELECT id FROM payment_evidence WHERE order_id = ? AND review_status = 'verified' LIMIT 1",
                (order_id,),
            ).fetchone()
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
            wallet_payment = connection.execute(
                "SELECT provider FROM payments WHERE order_id = ? ORDER BY submitted_at DESC LIMIT 1",
                (order_id,),
            ).fetchone()
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
            order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if order is None:
                raise CommerceError("Order not found")
            if str(order["refund_status"] or "none") == "refunded":
                return "already_refunded"
            payment = connection.execute(
                """SELECT id, provider, status FROM payments
                   WHERE order_id = ? AND status IN ('verified', 'submitted')
                   ORDER BY submitted_at DESC LIMIT 1""",
                (order_id,),
            ).fetchone()
            if payment is None or payment["status"] != "verified":
                raise CommerceError("Only a verified payment can be refunded")
            verified_evidence = connection.execute(
                """SELECT verified_amount_minor, verified_currency FROM payment_evidence
                   WHERE order_id = ? AND review_status = 'verified'
                   ORDER BY reviewed_at DESC LIMIT 1""",
                (order_id,),
            ).fetchone()
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

    def user_usage(self, telegram_id: int, usage_by_key: dict[str, Any]) -> list[dict[str, Any]]:
        """Return paid key usage belonging to one Telegram user."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT s.plan_code, s.plan_name, s.status AS subscription_status, s.expires_at, s.starts_at,
                          k.outline_key_id, k.quota_bytes, k.status,
                          k.last_usage_bytes, k.quota_reason, k.created_at,
                          (SELECT j.status FROM provisioning_jobs j WHERE j.subscription_id = s.id
                           AND j.operation = 'revoke' LIMIT 1) AS revocation_status
                   FROM subscriptions s
                   JOIN paid_vpn_keys k ON k.subscription_id = s.id
                   WHERE s.telegram_id = ?
                     AND (k.status IN ('active', 'revoke_failed') OR k.quota_reason = 'quota')
                   ORDER BY k.created_at DESC LIMIT 10""",
                (telegram_id,),
            ).fetchall()
        result = []
        for row in rows:
            key_id = str(row["outline_key_id"])
            observed = key_id in usage_by_key
            raw_used = usage_by_key.get(key_id, row["last_usage_bytes"] or 0)
            try:
                used = max(0, int(raw_used or 0))
            except (TypeError, ValueError):
                used = max(0, int(row["last_usage_bytes"] or 0))
                observed = False
            quota = int(row["quota_bytes"] or 0)
            if quota <= 0:
                continue
            result.append(
                {
                    "tier": row["plan_name"] or row["plan_code"],
                    "used_bytes": used,
                    "quota_bytes": quota,
                    "remaining_bytes": max(0, quota - used),
                    "usage_observed": observed,
                    "expires_at": row["expires_at"],
                    "status": "quota exhausted"
                    if row["quota_reason"] == "quota"
                    else (
                        "revocation failed"
                        if row["revocation_status"] == "failed"
                        else (
                            "revocation pending"
                            if row["subscription_status"] != "active" and row["status"] == "active"
                            else (
                                "revocation pending"
                                if row["status"] == "revoke_failed"
                                else row["status"]
                            )
                        )
                    ),
                    "created_at": row["created_at"],
                }
            )
        return result

    def user_vpns(self, telegram_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """Return all of a user's paid entitlements without exposing secrets.

        A customer may own multiple active keys (for devices or parallel
        plans). Access URLs are decrypted only for active, non-expired keys.
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT s.id AS subscription_id, s.plan_code, s.status, s.expires_at,
                          s.starts_at, k.access_url, k.quota_bytes, k.status AS key_status
                   FROM subscriptions s LEFT JOIN paid_vpn_keys k ON k.subscription_id = s.id
                   WHERE s.telegram_id = ? AND s.status IN ('pending', 'active', 'expired', 'revoked')
                   ORDER BY CASE s.status WHEN 'active' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                            s.starts_at DESC LIMIT ?""",
                (telegram_id, max(1, min(int(limit), 100))),
            ).fetchall()
        now_text = _now_text()
        results = []
        for row in rows:
            result = dict(row)
            result["access_url"] = self._decrypt_access_url(result.get("access_url"))
            if (
                result.get("status") != "active"
                or result.get("key_status") != "active"
                or str(result.get("expires_at") or "") <= now_text
            ):
                result["access_url"] = None
            results.append(result)
        return results

    def user_vpn(self, telegram_id: int) -> dict[str, Any] | None:
        """Backward-compatible latest/most relevant paid entitlement view."""
        subscriptions = self.user_vpns(telegram_id, limit=1)
        return subscriptions[0] if subscriptions else None
