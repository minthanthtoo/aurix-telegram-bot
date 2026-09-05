"""Receipt evidence, extraction and approval review use cases."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from commerce_models import UTC
from commerce_models import CommerceError
from commerce_models import _new_id
from commerce_models import _normalize_reference
from commerce_models import _now_text
from receipt_fingerprint import NEAR_DUPLICATE_DISTANCE
from receipt_fingerprint import fingerprint_distance
from receipt_fingerprint import receipt_perceptual_hash


from commerce_service_receipt_submission import submit_receipt
def claim_receipt_extraction_job(self, now: datetime | None = None) -> dict[str, Any] | None:
    """Claim one durable assisted-extraction job for the maintenance worker."""
    current = now or datetime.now(UTC)
    current_text = _now_text(current)
    stale_before = _now_text(current - timedelta(minutes=10))
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        return self.payments.claim_extraction_job(connection, current_text, stale_before)

def finish_receipt_extraction(
    self,
    job_id: str,
    evidence_id: str,
    extraction: dict[str, Any],
    diagnostics: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """Commit untrusted extraction metadata without changing payment approval state."""
    if not isinstance(extraction, dict):
        raise CommerceError("Receipt extraction result is invalid")
    completed = now or datetime.now(UTC)
    completed_at = _now_text(completed)
    result = dict(extraction)
    tx_candidate = str(result.get("transaction_id") or "").strip()[:128]
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        evidence = self.payments.extraction_context(connection, evidence_id)
        if evidence is None:
            self.payments.complete_missing_extraction(connection, job_id, completed_at)
            return
        if tx_candidate:
            prior_rows = self.payments.transaction_candidates(
                connection,
                str(evidence["order_id"]),
                exclude_evidence_id=evidence_id,
            )
            for prior in prior_rows:
                try:
                    prior_result = json.loads(prior["extraction_json"] or "{}")
                except json.JSONDecodeError:
                    prior_result = {}
                prior_tx = prior_result.get("transaction_id") if isinstance(prior_result, dict) else None
                if (
                    _normalize_reference(str(prior["provider"] or ""))
                    == _normalize_reference(str(result.get("provider") or evidence["provider"]))
                    and _normalize_reference(str(prior_tx or ""))
                    == _normalize_reference(tx_candidate)
                ):
                    result["flags"] = sorted(
                        set(result.get("flags") or []) | {"duplicate_transaction_candidate"}
                    )
                    break
        try:
            submitted = datetime.fromisoformat(str(evidence["submitted_at"]))
        except (TypeError, ValueError):
            submitted = completed
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=UTC)
        result["flags"] = self._receipt_risk_flags(result, evidence, submitted)
        # Preserve the existing storage enum: `parsed` means all triage
        # checks passed, while every mismatch/ambiguity stays reviewable.
        # The detailed three-way decision lives in extraction_json.
        status = (
            "parsed"
            if result.get("automation_decision") == "candidate_pass"
            else "needs_review"
        )
        self.payments.complete_extraction(
            connection,
            evidence_id=evidence_id,
            job_id=job_id,
            extraction_json=json.dumps(result, sort_keys=True),
            status=status,
            completed_at=completed_at,
        )
        self._audit(
            connection,
            "receipt_assisted_extraction_completed",
            "payment_evidence",
            evidence_id,
            "system",
            None,
            {
                "extraction_status": status,
                "selected_model": str((diagnostics or {}).get("selected_model") or "")[:128],
            },
        )

def fail_receipt_extraction_job(
    self, job_id: str, error: Exception, now: datetime | None = None
) -> None:
    """Retry bounded provider failures; manual review remains available throughout."""
    current = now or datetime.now(UTC)
    next_attempt = _now_text(current + timedelta(minutes=5))
    safe_error = f"{type(error).__name__}: {str(error)[:240]}"
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        self.payments.fail_extraction(connection, job_id, next_attempt, safe_error)

def list_pending_receipts(self, limit: int = 20) -> list[dict[str, Any]]:
    with self.database.connect() as connection:
        rows = self.payments.list_pending_receipts(connection, max(1, min(limit, 100)))
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
        row = self.payments.get_receipt(connection, evidence_id)
    if row is None:
        return None
    result = dict(row)
    try:
        result["extraction"] = json.loads(result.pop("extraction_json") or "{}")
    except json.JSONDecodeError:
        result["extraction"] = {}
    return result

from commerce_service_receipt_review import verify_receipt
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
        evidence = self.payments.rejection_context(connection, evidence_id)
        if evidence is None:
            raise CommerceError("Receipt evidence not found")
        self._lock_order(connection, str(evidence["order_id"]))
        if evidence["review_status"] == "verified":
            raise CommerceError("Verified receipt cannot be rejected")
        if evidence["review_status"] == "rejected":
            return str(evidence["order_id"])
        rejection_text = (
            "❌ Your receipt was not accepted.\n\n"
            f"Order: #{str(evidence['order_id'])[:8]}\n"
            f"Reason: {(notes or 'Please submit a clearer screenshot.')[:240]}\n\n"
            "Your order remains available for a replacement receipt."
        )
        self.payments.reject_evidence(
            connection,
            admin_id=admin_id,
            notes=(notes or "rejected by admin")[:500],
            reviewed_at=reviewed_at,
            evidence_id=evidence_id,
            order_id=evidence["order_id"],
        )
        self.payments.insert_notification(
            connection,
            notification_id=_new_id(),
            dedupe_key=f"receipt-rejected:{evidence_id}",
            telegram_id=evidence["telegram_id"],
            kind="receipt_rejected",
            text=rejection_text,
            now_text=reviewed_at,
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
        self._queue_staff_notification(
            connection,
            "rejected",
            evidence_id,
            "❌ RECEIPT REJECTED\n\n"
            f"Order: #{str(evidence['order_id'])[:8]}\n"
            f"Evidence: {evidence_id[:10]}\n"
            f"Customer: tg:{evidence['telegram_id']}\n"
            f"Method: {str(evidence['provider'] or 'manual').upper()}\n"
            f"Reviewed by: tg:{admin_id}\n\n"
            f"Reason: {(notes or 'rejected by admin')[:240]}",
            reviewed_at,
        )
    return str(evidence["order_id"])

def start_receipt_diagnostic(self, admin_id: int) -> str:
    run_id = _new_id()
    with self.database.connect() as connection:
        self.payments.start_diagnostic(connection, run_id, int(admin_id), _now_text())
    return run_id

def finish_receipt_diagnostic(
    self, run_id: str, admin_id: int, status: str, result: dict[str, Any]
) -> dict[str, Any]:
    normalized = "passed" if status == "passed" else "failed"
    safe_result = json.loads(json.dumps(result, default=str))
    encoded = json.dumps(safe_result, sort_keys=True)
    if len(encoded) > 20_000:
        safe_result["raw_response"] = str(safe_result.get("raw_response") or "")[:4000]
        safe_result["truncated"] = True
        encoded = json.dumps(safe_result, sort_keys=True)
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        updated = self.payments.finish_diagnostic(
            connection, str(run_id), int(admin_id), normalized, encoded, _now_text()
        )
        if int(getattr(updated, "rowcount", 0) or 0) != 1:
            raise CommerceError("Receipt diagnostic run is no longer active")
        self._audit(
            connection,
            f"receipt_diagnostic_{normalized}",
            "receipt_diagnostic",
            str(run_id),
            "admin",
            str(admin_id),
            {"summary": str(safe_result.get("summary") or "")[:300]},
        )
    return self.last_receipt_diagnostic()

def last_receipt_diagnostic(self) -> dict[str, Any] | None:
    with self.database.connect() as connection:
        row = self.payments.latest_diagnostic(connection)
    if row is None:
        return None
    result = dict(row)
    try:
        result["result"] = json.loads(result.pop("result_json") or "{}")
    except json.JSONDecodeError:
        result["result"] = {}
    return result

def receipt_system_snapshot(self) -> dict[str, Any]:
    policy = self.receipt_policy()
    with self.database.connect() as connection:
        pending, failed_uploads = self.payments.receipt_counts(connection)
    return {
        "policy": policy,
        "pending_receipts": pending,
        "failed_uploads": failed_uploads,
        "last_diagnostic": self.last_receipt_diagnostic(),
        "storage_configured": self._storage_is_configured(),
        "storage_bucket": self._storage_bucket(),
    }
