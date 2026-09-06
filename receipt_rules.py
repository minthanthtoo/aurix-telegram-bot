"""Deterministic, provider-aware receipt triage.

Vision output is untrusted evidence.  These rules never approve a payment; they
only classify a screenshot as a plausible candidate, a conclusive mismatch, or
something that requires human review against the receiving account.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from receipt_rule_steps import (
    evaluate_amount,
    evaluate_completion,
    evaluate_confidence,
    evaluate_currency,
    evaluate_provider,
    evaluate_recipient,
    evaluate_timestamp,
    evaluate_transaction_id,
    verdict,
)

UTC = timezone.utc


PROVIDER_RULES: dict[str, dict[str, Any]] = {
    "kbzpay": {
        "aliases": ("kbzpay", "kbz pay", "kbz bank"),
        "reference_labels": ("transaction no", "transaction number", "transaction id"),
        "forbidden_reference_labels": ("account", "phone", "recipient"),
        "time_labels": ("transaction date/time", "date/time", "transaction time"),
    },
    "wavepay": {
        "aliases": ("wavepay", "wave pay", "wave money"),
        "reference_labels": ("transaction id", "transaction no", "transaction number"),
        "forbidden_reference_labels": ("phone", "mobile", "recipient"),
        "time_labels": ("transaction date/time", "date/time", "transaction time"),
    },
    "ayapay": {
        "aliases": ("ayapay", "aya pay", "aya bank"),
        "reference_labels": ("transaction id", "reference no", "reference number"),
        # Official AYA success views can use “Transaction Code” for the
        # recipient alias (for example YAMIN), not a unique payment reference.
        "forbidden_reference_labels": ("transaction code", "recipient", "to", "account"),
        "time_labels": ("transaction date/time", "date/time", "transaction time"),
    },
    "uabpay": {
        "aliases": ("uabpay", "uab pay", "uab bank"),
        "reference_labels": ("transaction id", "transaction no", "reference no"),
        "forbidden_reference_labels": ("account", "phone", "recipient"),
        "time_labels": ("transaction date/time", "date/time", "transaction time"),
    },
    "cbpay": {
        "aliases": ("cbpay", "cb pay", "cb bank"),
        "reference_labels": (
            "payment reference number",
            "transaction id",
            "transaction no",
            "reference no",
        ),
        "forbidden_reference_labels": ("e-filing reference number", "user name", "account"),
        "time_labels": ("transaction date/time", "date/time", "transaction time"),
    },
}

NEGATIVE_FLAGS = {
    "not_a_receipt",
    "not_a_completed_receipt",
    "payment_qr",
    "qr_receive",
    "pending_transaction",
    "failed_transaction",
    "cancelled_transaction",
}


def _normalized(value: Any) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^a-z0-9\u1000-\u109f]+", "", value)


def canonical_provider(value: Any) -> str | None:
    candidate = _normalized(value)
    if not candidate:
        return None
    for provider, rules in PROVIDER_RULES.items():
        if candidate == _normalized(provider) or any(
            candidate == _normalized(alias) for alias in rules["aliases"]
        ):
            return provider
    return None


def provider_prompt_context(provider: str | None) -> str:
    canonical = canonical_provider(provider)
    if canonical is None:
        return "The selected payment method is unknown; identify the provider without guessing."
    rules = PROVIDER_RULES[canonical]
    allowed = ", ".join(rules["reference_labels"])
    forbidden = ", ".join(rules["forbidden_reference_labels"])
    return (
        f"The customer selected {canonical}. Extract a transaction ID only when its visible "
        f"label means a unique payment reference, such as: {allowed}. Never treat values under "
        f"these labels as a transaction ID: {forbidden}. If the unique reference is not visible, "
        "return transaction_id null and flag missing_transaction_id."
    )


def load_recipient_profiles(raw: str | None = None) -> dict[str, dict[str, tuple[str, ...]]]:
    """Load merchant identities without logging or exposing their values."""
    value = raw if raw is not None else os.environ.get("PAYMENT_RECIPIENTS_JSON", "")
    if not str(value).strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for key, profile in parsed.items():
        provider = canonical_provider(key)
        if provider is None or not isinstance(profile, dict):
            continue
        names = profile.get("names") or []
        accounts = profile.get("accounts") or []
        if isinstance(names, str):
            names = [names]
        if isinstance(accounts, str):
            accounts = [accounts]
        result[provider] = {
            "names": tuple(str(item).strip() for item in names if str(item).strip()),
            "accounts": tuple(str(item).strip() for item in accounts if str(item).strip()),
        }
    return result


def _timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=6, minutes=30)))
    return parsed.astimezone(UTC)


def _recipient_matches(extraction: dict[str, Any], profile: dict[str, tuple[str, ...]]) -> bool:
    recipient = _normalized(extraction.get("recipient"))
    account = _normalized(extraction.get("recipient_account"))
    name_match = bool(recipient) and any(_normalized(name) in recipient for name in profile["names"])
    account_match = bool(account) and any(
        account.endswith(_normalized(expected)) or _normalized(expected).endswith(account)
        for expected in profile["accounts"]
        if _normalized(expected)
    )
    # A visible configured name or account is sufficient; disagreement is
    # represented by the model's recipient_ambiguity flag and remains manual.
    return name_match or account_match


def evaluate_receipt_candidate(
    extraction: dict[str, Any],
    *,
    selected_provider: str,
    expected_amount_minor: int,
    expected_currency: str,
    submitted_at: datetime,
    recipient_profiles: dict[str, dict[str, tuple[str, ...]]] | None = None,
) -> dict[str, Any]:
    """Return a non-authoritative triage verdict and auditable rule checks."""
    flags = {str(item).strip().lower().replace("-", "_") for item in extraction.get("flags", []) if item}
    checks: dict[str, str] = {}
    evaluate_completion(extraction, flags, checks, NEGATIVE_FLAGS)
    selected = evaluate_provider(
        extraction, selected_provider, flags, checks, canonical_provider
    )
    evaluate_amount(extraction, expected_amount_minor, flags, checks)
    evaluate_currency(extraction, expected_currency, flags, checks)
    evaluate_transaction_id(
        extraction, selected, flags, checks, PROVIDER_RULES, _normalized
    )
    evaluate_timestamp(extraction, submitted_at, flags, checks, _timestamp)
    profiles = recipient_profiles if recipient_profiles is not None else load_recipient_profiles()
    evaluate_recipient(extraction, selected, profiles, flags, checks, _recipient_matches)
    evaluate_confidence(extraction, flags, checks)
    return {
        "automation_decision": verdict(flags, checks),
        "rule_checks": checks,
        "flags": sorted(flags),
    }
