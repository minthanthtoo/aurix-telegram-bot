"""Shared identity constants and opaque-token helpers."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

UTC = timezone.utc


class IdentityError(RuntimeError):
    """Raised when an account or device lifecycle invariant is violated."""


def _now_text() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def account_id_for_telegram(telegram_id: int) -> str:
    """Return the legacy deterministic fallback used by old callers.

    New persisted accounts are assigned random UUIDs by ``ensure_account``.
    The fallback remains for source compatibility only and is not used for
    new account storage or lookup.
    """
    digest = hashlib.sha256(f"aurix:telegram:{int(telegram_id)}".encode()).digest()[:16]
    return str(uuid.UUID(bytes=digest))


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()
