"""Receipt review and payment verification workflow."""

from __future__ import annotations

from datetime import datetime

from commerce_models import CommerceError, _new_id, _normalize_reference, _now_text


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
            """SELECT e.*, o.amount_minor, o.currency, o.plan_code,
                      o.status AS order_status
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
        if (
            str(evidence["plan_code"]) == "wallet_topup"
            and verified_amount_minor != int(evidence["amount_minor"])
        ):
            raise CommerceError("Wallet top-up receipt amount must match exactly")
        payment = self.payments.latest_eligible_payment(connection, str(evidence["order_id"]))
        provider = _normalize_reference(str(evidence["provider"] or "manual"))[:64]
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

