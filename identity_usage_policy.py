"""Pure validation and normalization for remote usage observations."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import NamedTuple

from identity_support import IdentityError, _now_text, _parse_time


class UsageObservation(NamedTuple):
    reported: int
    timestamp: str
    observed_text: str
    observed_time: datetime


def normalize_usage_observation(
    remote_bytes: int,
    *,
    observed_at: str | None,
    now: str | None,
) -> UsageObservation:
    try:
        reported = int(remote_bytes)
    except (TypeError, ValueError) as exc:
        raise IdentityError("remote usage is invalid") from exc
    if reported < 0 or reported > 100 * 1024 * 1024 * 1024 * 1024:
        raise IdentityError("remote usage is outside the allowed range")
    timestamp = str(now or _now_text())
    observed_text = str(observed_at or timestamp)
    observed_time = _parse_time(observed_text)
    if observed_time > _parse_time(timestamp) + timedelta(minutes=5):
        raise IdentityError("remote usage timestamp is too far in the future")
    return UsageObservation(reported, timestamp, observed_text, observed_time)
