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
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if amount < 1000 or unit == "TB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1000


def _paid_outline_key_name(subscription: Any) -> str:
    raw_identity = subscription["username"] or str(subscription["telegram_id"])
    identity = re.sub(r"[^A-Za-z0-9_-]+", "-", str(raw_identity).lstrip("@")).strip("-_")[
        :48
    ] or str(subscription["telegram_id"])
    plan_name = str(subscription["plan_name"] or "")
    named_gb = re.search(r"\b(\d+)\s*GB\b", plan_name, re.IGNORECASE)
    quota = subscription["quota_bytes"]
    if named_gb:
        tier = f"PAID{named_gb.group(1)}GB"
    elif quota is not None and int(quota) % 1_000_000_000 == 0:
        tier = f"PAID{int(quota) // 1_000_000_000}GB"
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


def summarize_entitlement_usage(
    entries: list[dict[str, Any]],
    *,
    historical_used_bytes: int = 0,
) -> dict[str, Any]:
    """Build a conservative account-wide usage roll-up.

    Outline transfer counters are scoped to one endpoint/key.  Customers may
    therefore have several simultaneous keys, or a replacement key after an
    endpoint migration.  This helper deliberately reports a *known* total
    when any endpoint has not supplied a fresh counter and never invents a
    remaining balance from an unavailable metric.  Per-key hard limits remain
    authoritative; the roll-up is a transparent cross-server view.
    """

    # Only credentials which can still carry traffic are part of the current
    # allowance. Expired, revoked, exhausted, and pending revocations are
    # historical records and must not make the available balance look larger.
    active_states = {"active", "revoke_failed"}
    active: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("status") or "").lower() not in active_states:
            continue
        try:
            quota = int(entry.get("quota_bytes") or 0)
            used = max(0, int(entry.get("used_bytes") or 0))
        except (TypeError, ValueError):
            continue
        if quota <= 0:
            # A fair-use entitlement has no numeric quota to add. Keep it in
            # the count so the UI can say that the roll-up is mixed.
            active.append({**entry, "quota_bytes": 0, "used_bytes": used})
            continue
        active.append({**entry, "quota_bytes": quota, "used_bytes": used})

    total_quota = sum(int(item["quota_bytes"]) for item in active)
    observed = [item for item in active if bool(item.get("usage_observed"))]
    unknown = len(active) - len(observed)
    known_used = sum(int(item["used_bytes"]) for item in observed)
    try:
        historical = max(0, int(historical_used_bytes or 0))
    except (TypeError, ValueError):
        historical = 0
    known_used += historical
    # A remaining value is exact only if every bounded active entitlement has
    # been observed. For fair-use-only users, leave it unset rather than
    # pretending that an unbounded product has a numeric balance.
    bounded_count = sum(1 for item in active if int(item["quota_bytes"]) > 0)
    complete = unknown == 0 and (bounded_count > 0 or not active)
    remaining = max(0, total_quota - known_used) if complete and bounded_count else None
    servers = sorted(
        {
            str(item.get("server_id") or item.get("server_label") or "unknown")
            for item in active
        }
    )
    return {
        "active_key_count": len(active),
        "observed_key_count": len(observed),
        "unknown_key_count": unknown,
        "server_count": len(servers),
        "servers": servers,
        "total_quota_bytes": total_quota,
        "known_used_bytes": known_used,
        "historical_used_bytes": historical,
        "remaining_bytes": remaining,
        "complete": complete,
        "has_fair_use": any(int(item["quota_bytes"]) <= 0 for item in active),
    }
