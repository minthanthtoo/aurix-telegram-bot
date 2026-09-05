"""Shared constants and pure helpers for free, trial, and giveaway access."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone


UTC = timezone.utc
PUBLIC_LIMIT_BYTES = 300_000_000
LIMIT_BYTES = PUBLIC_LIMIT_BYTES
TRIAL_LIMIT_BYTES = 3_000_000_000
CLAIM_PERIOD = timedelta(hours=24)
TRIAL_PERIOD = timedelta(days=30)
GIVEAWAY_CODE = "100GBFREE"
GIVEAWAY_LIMIT_BYTES = 100_000_000_000
GIVEAWAY_PERIOD = timedelta(days=30)
GIVEAWAY_WINNER_LIMIT = 5
QUOTA_WARNING_THRESHOLDS = ((25, 0.25), (10, 0.10), (5, 0.05))
FREE_INTENT_RETRY_DELAY = timedelta(minutes=1)
FREE_INTENT_STALE_AFTER = timedelta(minutes=5)
FREE_INTENT_MAX_ATTEMPTS = 8


def outline_key_name(
    telegram_id: int,
    username: str | None,
    tier: str,
    duration: str,
    started_at: datetime,
) -> str:
    identity = (username or "").strip().lstrip("@") or str(telegram_id)
    identity = re.sub(r"[^A-Za-z0-9_-]+", "-", identity).strip("-_")
    identity = identity[:48] or str(telegram_id)
    timestamp = started_at.astimezone(UTC).strftime("%Y%m%d%H%M")
    return f"{identity}-{tier}-{duration}-{timestamp}"[:128]


def human_bytes(value: int) -> str:
    amount = float(max(0, int(value)))
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if amount < 1000 or unit == "TB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1000
    return f"{amount:.2f} TB"


def human_decimal_bytes(value: int) -> str:
    amount = float(max(0, int(value)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1000 or unit == "TB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1000
    return f"{amount:.2f} TB"


def new_id() -> str:
    return uuid.uuid4().hex
