"""Individual deterministic checks used by receipt triage."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable


def evaluate_completion(
    extraction: dict[str, Any],
    flags: set[str],
    checks: dict[str, str],
    negative_flags: set[str],
) -> None:
    completed = str(extraction.get("completion_status") or "").strip().lower()
    if flags & negative_flags or completed in {"failed", "pending", "cancelled", "not_completed"}:
        flags.add("not_a_completed_receipt")
        checks["completed"] = "fail"
    elif completed == "completed":
        checks["completed"] = "pass"
    else:
        flags.add("completion_status_unconfirmed")
        checks["completed"] = "unknown"


def evaluate_provider(
    extraction: dict[str, Any],
    selected_provider: str,
    flags: set[str],
    checks: dict[str, str],
    canonical_provider: Callable[[Any], str | None],
) -> str | None:
    selected = canonical_provider(selected_provider)
    extracted = canonical_provider(extraction.get("provider"))
    if selected is None or extracted is None:
        flags.add("provider_unconfirmed")
        checks["provider"] = "unknown"
    elif selected != extracted:
        flags.add("provider_mismatch")
        checks["provider"] = "fail"
    else:
        checks["provider"] = "pass"
    return selected


def evaluate_amount(
    extraction: dict[str, Any],
    expected_amount_minor: int,
    flags: set[str],
    checks: dict[str, str],
) -> None:
    try:
        amount_matches = int(extraction.get("amount_minor")) == int(expected_amount_minor)
    except (TypeError, ValueError):
        amount_matches = False
        flags.add("missing_or_invalid_amount")
        checks["amount"] = "unknown"
    else:
        checks["amount"] = "pass" if amount_matches else "fail"
        if not amount_matches:
            flags.add("amount_mismatch")


def evaluate_currency(
    extraction: dict[str, Any],
    expected_currency: str,
    flags: set[str],
    checks: dict[str, str],
) -> None:
    currency = str(extraction.get("currency") or "").strip().upper()
    if not currency:
        flags.add("missing_currency")
        checks["currency"] = "unknown"
    elif currency != str(expected_currency).upper():
        flags.add("currency_mismatch")
        checks["currency"] = "fail"
    else:
        checks["currency"] = "pass"


def evaluate_transaction_id(
    extraction: dict[str, Any],
    selected: str | None,
    flags: set[str],
    checks: dict[str, str],
    provider_rules: dict[str, dict[str, Any]],
    normalized: Callable[[Any], str],
) -> None:
    transaction_id = str(extraction.get("transaction_id") or "").strip()
    label = normalized(extraction.get("transaction_id_label"))
    if not transaction_id:
        flags.add("missing_transaction_id")
        checks["transaction_id"] = "unknown"
        return
    if selected is None or not label:
        flags.add("transaction_id_label_unconfirmed")
        checks["transaction_id"] = "unknown"
        return
    rules = provider_rules[selected]
    forbidden = any(normalized(item) == label for item in rules["forbidden_reference_labels"])
    allowed = any(normalized(item) == label for item in rules["reference_labels"])
    if forbidden:
        flags.add("ambiguous_transaction_id")
        checks["transaction_id"] = "fail"
    elif not allowed:
        flags.add("transaction_id_label_unconfirmed")
        checks["transaction_id"] = "unknown"
    else:
        checks["transaction_id"] = "pass"


def evaluate_timestamp(
    extraction: dict[str, Any],
    submitted_at: datetime,
    flags: set[str],
    checks: dict[str, str],
    timestamp: Callable[[Any], datetime | None],
) -> None:
    receipt_time = timestamp(extraction.get("timestamp"))
    if receipt_time is None:
        flags.add("missing_or_invalid_timestamp")
        checks["timestamp"] = "unknown"
        return
    age = submitted_at.astimezone(receipt_time.tzinfo) - receipt_time
    if age > timedelta(hours=1):
        flags.add("receipt_older_than_1_hour")
        checks["timestamp"] = "fail"
    elif age < -timedelta(minutes=5):
        flags.add("receipt_timestamp_in_future")
        checks["timestamp"] = "fail"
    else:
        checks["timestamp"] = "pass"


def evaluate_recipient(
    extraction: dict[str, Any],
    selected: str | None,
    profiles: dict[str, dict[str, tuple[str, ...]]],
    flags: set[str],
    checks: dict[str, str],
    recipient_matches: Callable[[dict[str, Any], dict[str, tuple[str, ...]]], bool],
) -> None:
    profile = profiles.get(selected or "")
    if not profile or not (profile["names"] or profile["accounts"]):
        flags.add("merchant_profile_not_configured")
        checks["recipient"] = "unknown"
    elif not extraction.get("recipient") and not extraction.get("recipient_account"):
        flags.add("missing_recipient")
        checks["recipient"] = "unknown"
    elif recipient_matches(extraction, profile):
        checks["recipient"] = "pass"
    else:
        flags.add("recipient_mismatch")
        checks["recipient"] = "fail"


def evaluate_confidence(
    extraction: dict[str, Any], flags: set[str], checks: dict[str, str]
) -> None:
    try:
        confidence = float(extraction.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0
    if confidence < 0.85:
        flags.add("low_extraction_confidence")
        checks["confidence"] = "unknown"
    else:
        checks["confidence"] = "pass"


def verdict(flags: set[str], checks: dict[str, str]) -> str:
    conclusive_reject = {
        "not_a_completed_receipt",
        "provider_mismatch",
        "amount_mismatch",
        "currency_mismatch",
        "recipient_mismatch",
        "receipt_older_than_1_hour",
        "receipt_timestamp_in_future",
        "duplicate_transaction_candidate",
    }
    if flags & conclusive_reject:
        return "candidate_reject"
    if all(value == "pass" for value in checks.values()) and not flags:
        return "candidate_pass"
    return "manual_review"
