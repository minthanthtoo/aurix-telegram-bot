"""Validation and persistence phases for human receipt verification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from commerce_models import CommerceError, _new_id, _normalize_reference


@dataclass(frozen=True, slots=True)
class ReceiptVerificationContext:
    evidence: Any
    order_id: str
    provider: str
    normalized_provider: str
    normalized_reference: str
    payment: Any


def prepare_verification(
    service: Any,
    connection: Any,
    *,
    evidence_id: str,
    provider_reference: str,
    verified_amount_minor: int,
    currency: str,
) -> tuple[str | None, ReceiptVerificationContext | None]:
    """Lock and validate evidence, returning a terminal idempotent result if any."""
    evidence = service.payments.verification_context(connection, evidence_id)
    if evidence is None:
        raise CommerceError("Receipt evidence not found")
    service._lock_order(connection, str(evidence["order_id"]))
    if evidence["order_status"] == "approved":
        return str(evidence["order_id"]), None
    if service.receipt_storage_required and str(evidence["storage_status"] or "") != "stored":
        raise CommerceError("Receipt image must be stored before verification")
    if evidence["review_status"] == "verified":
        if (
            str(evidence["verified_provider_reference"] or "").strip().casefold()
            == provider_reference.casefold()
            and int(evidence["verified_amount_minor"] or 0) == verified_amount_minor
            and str(evidence["verified_currency"] or "").upper() == currency
        ):
            return str(evidence["order_id"]), None
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
    payment = service.payments.latest_eligible_payment(connection, str(evidence["order_id"]))
    provider = _normalize_reference(str(evidence["provider"] or "manual"))[:64]
    normalized_provider = _normalize_reference(provider)
    normalized_reference = _normalize_reference(provider_reference)
    conflicts = service.payments.payment_reference_conflicts(
        connection, normalized_reference, str(evidence["order_id"])
    )
    if any(_normalize_reference(str(item["provider"])) == normalized_provider for item in conflicts):
        raise CommerceError("This transaction ID has already been submitted for another order")
    return None, ReceiptVerificationContext(
        evidence=evidence,
        order_id=str(evidence["order_id"]),
        provider=provider,
        normalized_provider=normalized_provider,
        normalized_reference=normalized_reference,
        payment=payment,
    )


def persist_verified_payment(
    service: Any,
    connection: Any,
    context: ReceiptVerificationContext,
    *,
    provider_reference: str,
    reviewed_at: str,
) -> None:
    """Insert or update the verified payment under the locked order."""
    try:
        if context.payment is None:
            service.payments.insert_verified_payment(
                connection,
                payment_id=_new_id(),
                order_id=context.order_id,
                provider=context.provider,
                provider_reference=provider_reference,
                normalized_reference=context.normalized_reference,
                reviewed_at=reviewed_at,
            )
        else:
            service.payments.update_verified_payment(
                connection,
                provider=context.provider,
                provider_reference=provider_reference,
                normalized_reference=context.normalized_reference,
                reviewed_at=reviewed_at,
                payment_id=str(context.payment["id"]),
            )
    except Exception as exc:
        if service.database.is_integrity_error(exc):
            raise CommerceError("This transaction ID has already been verified") from exc
        raise


def complete_verification(
    service: Any,
    connection: Any,
    *,
    context: ReceiptVerificationContext,
    evidence_id: str,
    admin_id: int,
    provider_reference: str,
    verified_amount_minor: int,
    currency: str,
    reviewed_at: str,
) -> None:
    """Commit evidence review, order state, and the audit record together."""
    service.payments.mark_evidence_verified(
        connection,
        admin_id=admin_id,
        provider_reference=provider_reference,
        amount=verified_amount_minor,
        currency=currency,
        reviewed_at=reviewed_at,
        evidence_id=evidence_id,
    )
    service.orders.mark_payment_submitted(connection, context.order_id)
    service._audit(
        connection,
        "receipt_verified",
        "payment_evidence",
        evidence_id,
        "admin",
        str(admin_id),
        {"amount_minor": verified_amount_minor, "currency": currency},
    )
