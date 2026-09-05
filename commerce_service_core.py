"""Shared commerce service helpers and compatibility-safe storage operations."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from cryptography.fernet import Fernet, InvalidToken
from commerce_models import UTC
from commerce_models import CommerceError
from commerce_models import _new_id
from commerce_models import _now_text
from commerce_shared_repository import SharedCommerceRepository
from receipt_rules import evaluate_receipt_candidate


_SHARED = SharedCommerceRepository()


def _encrypt_access_url(self, access_url: str) -> str:
    return self.access_url_cipher.encrypt(access_url.encode()).decode()

def _decrypt_access_url(self, encrypted: str | None) -> str | None:
    if not encrypted:
        return None
    try:
        return self.access_url_cipher.decrypt(encrypted.encode()).decode()
    except (InvalidToken, UnicodeDecodeError, ValueError):
        return None

def _repair_blocks_access(repair_status: Any) -> bool:
    """Keep a known-missing key out of customer-facing key views.

    The local entitlement row can remain active while the managed repair
    worker is waiting for observations, retrying, or awaiting an owner
    decision.  Its encrypted URL is then only historical state; exposing
    it would make the bot return a credential that the Outline endpoint
    has already confirmed missing.  Completed/cancelled repairs are not
    blocked because the normal provisioning path has reconciled them.
    """
    return str(repair_status or "").strip().lower() in {
        "pending",
        "running",
        "failed",
        "manual",
    }

def _receipt_storage_extension(mime_type: str) -> str:
    normalized = str(mime_type or "").lower().split(";", 1)[0].strip()
    return {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }.get(normalized, "bin")

def _receipt_storage_path(order_id: str, evidence_id: str, mime_type: str) -> str:
    # Order/evidence IDs are generated UUIDs. Keep this defensive because
    # old/imported order IDs may contain unexpected characters.
    safe_order = re.sub(r"[^A-Za-z0-9_-]+", "-", str(order_id)).strip("-_")[:96]
    safe_evidence = re.sub(r"[^A-Za-z0-9_-]+", "-", str(evidence_id)).strip("-_")[:96]
    extension = _receipt_storage_extension(mime_type)
    return f"orders/{safe_order or 'unknown'}/{safe_evidence or _new_id()}.{extension}"

def _storage_is_configured(self) -> bool:
    return bool(getattr(self.receipt_storage, "configured", False))

def _storage_bucket(self) -> str | None:
    bucket = getattr(self.receipt_storage, "bucket", None)
    return str(bucket) if bucket else None

def _lock_order(connection: Any, order_id: str) -> None:
    """Serialize aggregate mutations on PostgreSQL as well as SQLite.

    SQLite already serializes writers. PostgreSQL needs an explicit row
    lock because payment, receipt, approval and refund requests can arrive
    concurrently from Telegram retries or two administrators.
    """
    _SHARED.lock_order(connection, order_id)

def _assert_no_active_promo(connection: Any, telegram_id: int) -> None:
    now_text = _now_text()
    if _SHARED.active_promo_exists(connection, telegram_id, now_text):
        raise CommerceError(
            "Your promo VPN gift is currently active. Normal plans return automatically "
            "when the gift or promo season ends."
        )

def initialize(self) -> None:
    self.database.initialize()

def _table_exists(connection: Any, name: str) -> bool:
    return _SHARED.table_exists(connection, name)

def _metric_bytes(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("bytes", value.get("data"))
        if isinstance(value, dict):
            value = value.get("bytes")
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None

def _health_threshold(name: str, default: int) -> int:
    """Read a bounded hysteresis threshold from the deployment environment."""
    try:
        return max(1, min(10, int(os.environ.get(name, str(default)))))
    except (TypeError, ValueError):
        return default

def _receipt_timestamp(value: Any) -> datetime | None:
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

def _receipt_risk_flags(
    self, extraction: dict[str, Any], order: Any, submitted_at: datetime
) -> list[str]:
    selected_provider = str(order["payment_method"] or "")
    if not selected_provider:
        try:
            selected_provider = str(order["provider"] or "")
        except (KeyError, IndexError):
            selected_provider = ""
    evaluated = evaluate_receipt_candidate(
        extraction,
        selected_provider=selected_provider,
        expected_amount_minor=int(order["amount_minor"]),
        expected_currency=str(order["currency"]),
        submitted_at=submitted_at,
        recipient_profiles=self.receipt_recipient_profiles,
    )
    extraction["automation_decision"] = evaluated["automation_decision"]
    extraction["rule_checks"] = evaluated["rule_checks"]
    return list(evaluated["flags"])

def _ensure_user(
    connection: Any,
    telegram_id: int,
    first_name: str,
    username: str | None = None,
) -> None:
    _SHARED.ensure_user(connection, telegram_id, first_name, username)

def _audit(
    connection: Any,
    action: str,
    target_type: str,
    target_id: str,
    actor_type: str,
    actor_id: str | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    _SHARED.audit(
        connection, action, target_type, target_id, actor_type, actor_id, metadata
    )

def _queue_staff_notification(
    connection: Any,
    event_type: str,
    entity_id: str,
    text: str,
    created_at: str,
) -> None:
    """Durably fan out one deduplicated operational alert per opted-in staff member."""
    _SHARED.queue_staff_notification(
        connection, event_type, entity_id, text, created_at
    )

def _queue_customer_repair_notification(
    connection: Any,
    repair_id: str,
    telegram_id: int,
    status: str,
    endpoint: str,
    created_at: str,
) -> None:
    """Notify the affected customer about a repair state transition.

    The state is part of the dedupe key, so repeated inventory polls do
    not flood a chat while a meaningful transition still produces an
    auditable, credential-free update.
    """
    _SHARED.queue_customer_repair_notification(
        connection, repair_id, telegram_id, status, endpoint, created_at
    )

def _queue_receipt_extraction(connection: Any, evidence_id: str, created_at: str) -> None:
    _SHARED.queue_receipt_extraction(connection, evidence_id, created_at)
