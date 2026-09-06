"""Receipt review and payment verification workflow."""

from __future__ import annotations

from datetime import datetime

from commerce_models import CommerceError, _new_id, _normalize_reference, _now_text
from commerce_service_receipt_review_steps import (
    complete_verification,
    persist_verified_payment,
    prepare_verification,
)


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
        terminal_result, context = prepare_verification(
            self,
            connection,
            evidence_id=evidence_id,
            provider_reference=provider_reference,
            verified_amount_minor=verified_amount_minor,
            currency=currency,
        )
        if terminal_result is not None:
            return terminal_result
        assert context is not None
        persist_verified_payment(
            self,
            connection,
            context,
            provider_reference=provider_reference,
            reviewed_at=reviewed_at,
        )
        complete_verification(
            self,
            connection,
            context=context,
            evidence_id=evidence_id,
            admin_id=admin_id,
            provider_reference=provider_reference,
            verified_amount_minor=verified_amount_minor,
            currency=currency,
            reviewed_at=reviewed_at,
        )
        return context.order_id
