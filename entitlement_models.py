"""Compatibility-safe value types for entitlement operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class OutlineError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ClaimResult:
    access_url: str | None = None
    expires_at: datetime | None = None
    next_claim_at: datetime | None = None
    denied_reason: str | None = None
    pending: bool = False


@dataclass(frozen=True)
class GiveawayResult:
    outcome: str
    code: str | None = None
    quota_bytes: int | None = None
    duration_days: int | None = None
    access_url: str | None = None
    expires_at: datetime | None = None
    winner_number: int | None = None
    remaining_slots: int = 0
    reason: str | None = None
    pending: bool = False
