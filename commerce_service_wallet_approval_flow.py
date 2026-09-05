"""Wallet-funded order approval workflow."""

from __future__ import annotations

from datetime import datetime, timedelta

from commerce_models import (
    ApprovalResult,
    CommerceError,
    _new_id,
    _now_text,
)


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
        order = self.orders.get(connection, order_id)
        if order is None:
            raise CommerceError("Order not found")
        is_wallet_topup = str(order["plan_code"]) == "wallet_topup"
        if not is_wallet_topup:
            self._assert_no_active_promo(connection, int(order["telegram_id"]))
        if order["status"] == "approved":
            if is_wallet_topup:
                return ApprovalResult(
                    order_id, f"wallet:{order['telegram_id']}", "already_credited"
                )
            subscription = connection.execute(
                "SELECT id FROM subscriptions WHERE order_id = ?", (order_id,)
            ).fetchone()
            if subscription is None:
                raise CommerceError("Approved order has no subscription record")
            return ApprovalResult(order_id, subscription["id"], "already_approved")
        if order["status"] != "payment_submitted":
            raise CommerceError("Order has no submitted payment for review")
        evidence = self.payments.latest_evidence_for_approval(connection, order_id)
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
        payment = self.payments.latest_eligible_payment(connection, order_id)
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
        if is_wallet_topup:
            if wallet_payment:
                raise CommerceError("A wallet cannot be topped up from the same wallet")
            if evidence is None or evidence["review_status"] != "verified":
                raise CommerceError("Verified receipt evidence is required for a wallet top-up")
            if int(evidence["verified_amount_minor"] or 0) != int(order["amount_minor"]):
                raise CommerceError("Wallet top-up receipt amount must match exactly")
            if str(evidence["verified_currency"] or "").upper() != str(
                order["currency"]
            ).upper():
                raise CommerceError("Wallet top-up receipt currency does not match")
            payment_id = str(payment["id"])
            credit_idem = f"credit:{payment_id}"
            connection.execute(
                """INSERT INTO wallets
                   (telegram_id, currency, balance_minor, created_at, updated_at)
                   VALUES (?, ?, 0, ?, ?) ON CONFLICT(telegram_id) DO NOTHING""",
                (order["telegram_id"], order["currency"], starts_at, starts_at),
            )
            if connection.execute(
                "SELECT id FROM wallet_ledger WHERE idempotency_key = ?", (credit_idem,)
            ).fetchone() is None:
                connection.execute(
                    """UPDATE wallets SET balance_minor = balance_minor + ?, updated_at = ?
                       WHERE telegram_id = ?""",
                    (order["amount_minor"], starts_at, order["telegram_id"]),
                )
                connection.execute(
                    """INSERT INTO wallet_ledger
                       (id, telegram_id, kind, amount_minor, currency, reference_type,
                        reference_id, idempotency_key, created_at)
                       VALUES (?, ?, 'credit', ?, ?, 'payment', ?, ?, ?)""",
                    (
                        _new_id(),
                        order["telegram_id"],
                        order["amount_minor"],
                        order["currency"],
                        payment_id,
                        credit_idem,
                        starts_at,
                    ),
                )
            connection.execute(
                "UPDATE orders SET status = 'approved', approved_at = ? WHERE id = ?",
                (starts_at, order_id),
            )
            connection.execute(
                """INSERT INTO notifications
                   (id, dedupe_key, telegram_id, kind, text, status,
                    next_attempt_at, created_at)
                   VALUES (?, ?, ?, 'wallet_topup_approved', ?, 'pending', ?, ?)
                   ON CONFLICT(dedupe_key) DO NOTHING""",
                (
                    _new_id(),
                    f"wallet-topup-approved:{order_id}",
                    order["telegram_id"],
                    f"✅ Wallet top-up approved: {int(order['amount_minor']):,} "
                    f"{order['currency']}.",
                    starts_at,
                    starts_at,
                ),
            )
            self._audit(
                connection,
                "wallet_topup_approved",
                "order",
                order_id,
                "admin",
                str(admin_id),
                {"amount_minor": int(order["amount_minor"]), "payment_id": payment_id},
            )
            return ApprovalResult(
                order_id, f"wallet:{order['telegram_id']}", "wallet_credited"
            )
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
                plan_name, quota_bytes, duration_days, status, server_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
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
                order["server_id"] if "server_id" in order.keys() else None,
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

