"""Commerce value objects, constants, and pure naming helpers."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

UTC = timezone.utc


JOB_RETRY_DELAY = timedelta(seconds=30)


NOTIFICATION_RETRY_DELAY = timedelta(minutes=1)


QUOTA_WARNING_THRESHOLDS = ((25, 0.25), (10, 0.10), (5, 0.05))


def _human_bytes(value: int) -> str:
    amount = float(max(0, int(value)))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1024


def _paid_outline_key_name(subscription: Any) -> str:
    raw_identity = subscription["username"] or str(subscription["telegram_id"])
    identity = re.sub(r"[^A-Za-z0-9_-]+", "-", str(raw_identity).lstrip("@")).strip("-_")[
        :48
    ] or str(subscription["telegram_id"])
    quota = subscription["quota_bytes"]
    if quota is not None and int(quota) % (1024**3) == 0:
        tier = f"PAID{int(quota) // (1024**3)}GB"
    else:
        tier = str(subscription["plan_code"]).upper().replace("_", "-")
    duration = f"{int(subscription['duration_days'])}day"
    started = datetime.fromisoformat(subscription["starts_at"]).astimezone(UTC)
    # The short subscription suffix keeps simultaneous purchases for the same
    # user/plan/minute distinguishable on Outline versions without deterministic
    # caller-selected IDs.
    try:
        subscription_id = subscription["id"]
    except (KeyError, IndexError, TypeError):
        subscription_id = None
    suffix = str(subscription_id or "")[:8]
    base = f"{identity}-{tier}-{duration}-{started.strftime('%Y%m%d%H%M')}"
    return f"{base}-{suffix}"[:128] if suffix else base[:128]


class CommerceError(RuntimeError):
    """A safe, user-facing commerce validation error."""


@dataclass(frozen=True)
class Plan:
    code: str
    name: str
    price_minor: int
    currency: str
    quota_bytes: int | None
    duration_days: int


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    plan: Plan
    status: str
    created: bool = True
    plan_conflict: bool = False


@dataclass(frozen=True)
class ApprovalResult:
    order_id: str
    subscription_id: str
    status: str


def _now_text(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def _normalize_reference(value: str) -> str:
    """Normalize a payment reference for comparison without changing display data."""
    return "".join(str(value or "").split()).casefold()
