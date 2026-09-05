"""Order lifecycle and payment submission use cases."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any
from commerce_models import UTC
from commerce_models import CommerceError
from commerce_models import OrderResult
from commerce_models import Plan
from commerce_models import _new_id
from commerce_models import _normalize_reference
from commerce_models import _now_text
from commerce_repositories import _PostgresConnection
from receipt_fingerprint import NEAR_DUPLICATE_DISTANCE
from receipt_fingerprint import fingerprint_distance
from receipt_fingerprint import receipt_perceptual_hash
from commerce_service_support import LOCAL_PAYMENT_METHODS


def create_wallet_topup(
    self,
    telegram_id: int,
    first_name: str,
    amount_minor: int,
    now: datetime | None = None,
    username: str | None = None,
) -> OrderResult:
    """Create a receipt-backed deposit that credits wallet balance only."""
    try:
        amount_minor = int(amount_minor)
    except (TypeError, ValueError) as exc:
        raise CommerceError("Top-up amount must be a whole number of MMK") from exc
    if not 1_000 <= amount_minor <= 1_000_000:
        raise CommerceError("Wallet top-up must be between 1,000 and 1,000,000 MMK")
    plan = Plan("wallet_topup", "Wallet Top-up", amount_minor, "MMK", None, 1)
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
        existing = self.orders.find_open_for_user(connection, telegram_id)
        if existing is not None:
            existing_plan = Plan(
                str(existing["plan_code"]),
                str(existing["plan_name"] or existing["plan_code"]),
                int(existing["amount_minor"]),
                str(existing["currency"]),
                existing["quota_bytes_snapshot"],
                int(existing["duration_days_snapshot"] or 1),
            )
            return OrderResult(
                str(existing["id"]),
                existing_plan,
                str(existing["status"]),
                False,
                str(existing["plan_code"]) != "wallet_topup"
                or int(existing["amount_minor"]) != amount_minor,
            )
        connection.execute(
            """INSERT INTO orders
               (id, telegram_id, plan_code, amount_minor, currency, plan_name,
                quota_bytes_snapshot, duration_days_snapshot, status, created_at)
               VALUES (?, ?, 'wallet_topup', ?, 'MMK', 'Wallet Top-up',
                       NULL, 1, 'awaiting_payment', ?)""",
            (order_id, telegram_id, amount_minor, created_at),
        )
        self._audit(
            connection,
            "wallet_topup_created",
            "order",
            order_id,
            "customer",
            str(telegram_id),
            {"amount_minor": amount_minor, "currency": "MMK"},
        )
        self._queue_staff_notification(
            connection,
            "order_created",
            order_id,
            "💰 WALLET TOP-UP\n\n"
            f"Order: #{order_id[:8]}\nCustomer: tg:{telegram_id}\n"
            f"Amount: {amount_minor:,} MMK\n\nStatus: waiting for receipt",
            created_at,
        )
    return OrderResult(order_id, plan, "awaiting_payment")

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
        self._assert_no_active_promo(connection, telegram_id)
        existing = self.orders.find_open_for_user(connection, telegram_id)
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
        registered = connection.execute(
            "SELECT COUNT(*) AS n FROM outline_servers WHERE enabled = 1"
        ).fetchone()["n"]
        server_id = (
            self._select_server_for_plan(
                connection, plan.code, created_at, telegram_id=telegram_id
            )
            if int(registered)
            else None
        )
        reserved_until = (
            ((now or datetime.now(UTC)).astimezone(UTC) + timedelta(hours=24)).isoformat()
            if server_id
            else None
        )
        connection.execute(
            """INSERT INTO orders
               (id, telegram_id, plan_code, amount_minor, currency, plan_name,
                quota_bytes_snapshot, duration_days_snapshot, status, created_at,
                server_id, capacity_reserved_until)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_payment', ?, ?, ?)""",
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
                server_id,
                reserved_until,
            ),
        )
        self._audit(
            connection,
            "order_created",
            "order",
            order_id,
            "customer",
            str(telegram_id),
            {"plan_code": plan.code, "amount_minor": plan.price_minor, "server_id": server_id},
        )
        self._queue_staff_notification(
            connection,
            "order_created",
            order_id,
            "🛒 NEW ORDER\n\n"
            f"Order: #{order_id[:8]}\n"
            f"Customer: tg:{telegram_id}\n"
            f"Plan: {plan.name}\n"
            f"Amount: {plan.price_minor:,} {plan.currency}\n\n"
            "Status: waiting for payment",
            created_at,
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
        self._assert_no_active_promo(connection, telegram_id)
        existing = self.orders.find_open_for_user(connection, telegram_id)
        if existing is None:
            raise CommerceError("No open order is available to replace")
        self._lock_order(connection, str(existing["id"]))
        if expected_order_id and str(existing["id"]) != str(expected_order_id):
            raise CommerceError("The open order changed; refresh and try again")
        if existing["plan_code"] == plan.code:
            return OrderResult(str(existing["id"]), plan, str(existing["status"]), False)
        payment_count, evidence_count = self.payments.activity_counts(
            connection, str(existing["id"])
        )
        if evidence_count or payment_count:
            raise CommerceError(
                "This order has payment activity and cannot be replaced; ask staff to review it"
            )
        connection.execute(
            "UPDATE orders SET status = 'cancelled', rejected_at = ? WHERE id = ?",
            (created_at, existing["id"]),
        )
        registered = connection.execute(
            "SELECT COUNT(*) AS n FROM outline_servers WHERE enabled = 1"
        ).fetchone()["n"]
        server_id = (
            self._select_server_for_plan(
                connection, plan.code, created_at, telegram_id=telegram_id
            )
            if int(registered)
            else None
        )
        reserved_until = (
            ((now or datetime.now(UTC)).astimezone(UTC) + timedelta(hours=24)).isoformat()
            if server_id
            else None
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
                quota_bytes_snapshot, duration_days_snapshot, status, created_at,
                server_id, capacity_reserved_until)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_payment', ?, ?, ?)""",
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
                server_id,
                reserved_until,
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
        order = self.orders.get_owned(connection, order_id, telegram_id)
        if order is None:
            raise CommerceError("Order not found")
        if order["status"] in ("cancelled", "rejected"):
            return "already_cancelled"
        if order["status"] == "approved":
            raise CommerceError("An approved order cannot be cancelled")
        payment_count, evidence_count = self.payments.activity_counts(connection, order_id)
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

def choose_payment_method(
    self, telegram_id: int, order_id: str, payment_method: str
) -> dict[str, Any]:
    """Attach an explicit local transfer rail to an open customer order."""
    method = str(payment_method).strip().lower()
    if method not in LOCAL_PAYMENT_METHODS:
        raise CommerceError("That payment method is unavailable")
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        self._lock_order(connection, order_id)
        order = self.orders.get(connection, order_id)
        if order is None or int(order["telegram_id"]) != int(telegram_id):
            raise CommerceError("Order not found")
        if order["status"] != "awaiting_payment":
            raise CommerceError("The payment method can no longer be changed")
        if self.payments.has_evidence(connection, order_id):
            raise CommerceError("A receipt is already attached to this order")
        connection.execute(
            "UPDATE orders SET payment_method = ? WHERE id = ?", (method, order_id)
        )
        self._audit(
            connection,
            "payment_method_selected",
            "order",
            order_id,
            "customer",
            str(telegram_id),
            {"payment_method": method},
        )
    result = self.order_detail(order_id, telegram_id)
    if result is None:  # pragma: no cover - transaction just verified ownership
        raise CommerceError("Order not found")
    return result

def submit_payment(
    self,
    telegram_id: int,
    order_id: str,
    provider: str,
    provider_reference: str,
    now: datetime | None = None,
) -> str:
    # Provider identity is canonicalized alongside the reference so the
    # database uniqueness constraint, legacy rows, and application checks
    # all agree on values such as ``Manual`` vs `` manual ``.
    provider = _normalize_reference(provider)[:64]
    provider_reference = provider_reference.strip()[:128]
    normalized_reference = _normalize_reference(provider_reference)
    if not provider or not provider_reference:
        raise CommerceError("Payment provider and reference are required")
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        self._lock_order(connection, order_id)
        order = self.orders.get_owned(connection, order_id, telegram_id)
        if order is None or order["telegram_id"] != telegram_id:
            raise CommerceError("Order not found")
        if str(order["plan_code"]) != "wallet_topup":
            self._assert_no_active_promo(connection, telegram_id)
        if order["status"] == "approved":
            raise CommerceError("Order is already approved")
        if order["status"] not in ("awaiting_payment", "payment_submitted"):
            raise CommerceError("Order is not open for payment")
        existing = self.payments.latest_payment(connection, order_id)
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
        row = self.orders.find_open_for_user(connection, telegram_id)
    return dict(row) if row is not None else None

def open_order_ids_for_user(self, telegram_id: int, limit: int = 20) -> list[str]:
    """Return open order IDs for uncaptioned receipt routing.

    A customer may intentionally purchase multiple keys.  The Telegram
    transport uses this list to refuse ambiguous uncaptioned screenshots;
    an order-specific Upload Receipt button or an explicit caption then
    selects the intended order.
    """
    with self.database.connect() as connection:
        rows = connection.execute(
            """SELECT id FROM orders
               WHERE telegram_id = ?
                 AND status IN ('awaiting_payment', 'payment_submitted')
                 AND COALESCE(refund_status, 'none') != 'refunded'
               ORDER BY created_at LIMIT ?""",
            (telegram_id, max(1, min(int(limit), 100))),
        ).fetchall()
    return [str(row["id"]) for row in rows]

def receipt_duplicate_status(
    self,
    telegram_id: int,
    order_id: str,
    image_bytes: bytes,
    file_unique_id: str | None = None,
    provider: str | None = None,
) -> str:
    """Check immutable and near-duplicate identity before vision work.

    Exact SHA-256/Telegram identity remains a hard duplicate check. A
    perceptual match is a conservative cross-order fraud signal for
    resized/re-encoded screenshots; a match is held out of model spend and
    sent to manual review, never treated as proof of a payment.
    """
    digest = hashlib.sha256(image_bytes).hexdigest()
    phash = receipt_perceptual_hash(image_bytes)
    expected_provider = str(provider or "").strip().lower()
    with self.database.connect() as connection:
        order = self.orders.get_payment_context(connection, order_id)
        if order is None or int(order["telegram_id"]) != int(telegram_id):
            raise CommerceError("Order not found")
        if not expected_provider:
            expected_provider = str(order["payment_method"] or "").strip().lower()
        exact_rows = self.payments.find_exact_duplicate(connection, digest, file_unique_id)
    if any(str(row["order_id"]) != str(order_id) for row in exact_rows):
        return "different_order"
    if exact_rows:
        return "same_order"
    if phash:
        with self.database.connect() as connection:
            prior_rows = self.payments.prior_phash_rows(connection)
        for row in prior_rows:
            if expected_provider and str(row["provider"] or "").strip().lower() != expected_provider:
                continue
            distance = fingerprint_distance(phash, str(row["image_phash"] or ""))
            if distance is not None and distance <= NEAR_DUPLICATE_DISTANCE:
                return "possible_duplicate"
    return "new"
