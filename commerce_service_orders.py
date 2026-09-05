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
        self.orders.lock_user(connection, telegram_id)
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
        self.orders.insert_wallet_topup(
            connection,
            order_id=order_id,
            telegram_id=telegram_id,
            amount_minor=amount_minor,
            created_at=created_at,
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
        self.orders.lock_user(connection, telegram_id)
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
        registered = self.orders.enabled_server_count(connection)
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
        self.orders.insert_order(
            connection,
            order_id=order_id,
            telegram_id=telegram_id,
            plan_code=plan.code,
            amount_minor=plan.price_minor,
            currency=plan.currency,
            plan_name=plan.name,
            quota_bytes=plan.quota_bytes,
            duration_days=plan.duration_days,
            created_at=created_at,
            server_id=server_id,
            reserved_until=reserved_until,
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
        self.orders.lock_user(connection, telegram_id)
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
        self.orders.cancel(connection, str(existing["id"]), created_at)
        registered = self.orders.enabled_server_count(connection)
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
        self.orders.insert_order(
            connection,
            order_id=new_order_id,
            telegram_id=telegram_id,
            plan_code=plan.code,
            amount_minor=plan.price_minor,
            currency=plan.currency,
            plan_name=plan.name,
            quota_bytes=plan.quota_bytes,
            duration_days=plan.duration_days,
            created_at=created_at,
            server_id=server_id,
            reserved_until=reserved_until,
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
        self.orders.cancel(connection, order_id, cancelled_at)
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
        rows = self.orders.expired_open_orders(connection, cutoff)
        for row in rows:
            self.orders.cancel(connection, str(row["id"]), _now_text(current))
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
        rows = self.orders.expired_wallet_reservations(connection, cutoff)
        for row in rows:
            idem = f"release:{row['order_id']}"
            if not self.orders.wallet_ledger_exists(connection, idem):
                self.orders.credit_wallet(
                    connection,
                    telegram_id=int(row["telegram_id"]),
                    amount_minor=int(row["amount_minor"]),
                    updated_at=_now_text(current),
                )
                self.orders.insert_wallet_ledger(
                    connection,
                    ledger_id=_new_id(),
                    telegram_id=int(row["telegram_id"]),
                    amount_minor=int(row["amount_minor"]),
                    currency=str(row["currency"]),
                    order_id=str(row["order_id"]),
                    idempotency_key=idem,
                    created_at=_now_text(current),
                )
            self.orders.release_wallet_reservation(
                connection, str(row["order_id"]), _now_text(current)
            )
            self.orders.reject_wallet_payment(connection, str(row["order_id"]))
            self.orders.cancel_submitted_order(
                connection, str(row["order_id"]), _now_text(current)
            )
            self.orders.insert_notification(
                connection,
                notification_id=_new_id(),
                dedupe_key=f"wallet-reservation-expired:{row['order_id']}",
                telegram_id=int(row["telegram_id"]),
                text="Your wallet payment hold expired before approval; the funds were returned to your wallet.",
                now_text=_now_text(current),
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
        rows = self.orders.list_user_orders(
            connection, telegram_id, max(1, min(limit, 50))
        )
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
        users = self.orders.open_order_users(connection)
        for user in users:
            rows = self.orders.open_orders_for_user(connection, int(user["telegram_id"]))
            protected = [row for row in rows if int(row["payments"]) or int(row["evidence"])]
            keeper_id = (protected[0] if protected else rows[0])["id"]
            if len(protected) > 1:
                manual_conflicts += 1
            for row in rows:
                if row["id"] == keeper_id:
                    continue
                if int(row["payments"]) or int(row["evidence"]):
                    continue
                self.orders.cancel_duplicate(connection, str(row["id"]))
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
        row = self.orders.detail(connection, order_id)
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
        self.orders.update_payment_method(connection, order_id, method)
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
            self.orders.insert_payment(
                connection,
                payment_id=_new_id(),
                order_id=order_id,
                provider=provider,
                provider_reference=provider_reference,
                normalized_reference=normalized_reference,
                submitted_at=_now_text(now),
            )
        except Exception as exc:
            if self.database.is_integrity_error(exc):
                raise CommerceError("Payment reference has already been submitted") from exc
            raise
        self.orders.mark_payment_submitted(connection, order_id)
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
        rows = self.orders.open_order_ids(
            connection, telegram_id, max(1, min(int(limit), 100))
        )
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
