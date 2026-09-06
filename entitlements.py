"""Compatibility facade for focused free, trial, and giveaway handlers."""

from __future__ import annotations

from typing import Any

from cryptography.fernet import Fernet

from entitlement_dispatch import (
    ENTITLEMENT_IMPLEMENTATIONS,
    STATIC_ENTITLEMENT_IMPLEMENTATIONS,
)
from entitlement_contracts import bind_claim_implementations
from entitlement_models import ClaimResult, GiveawayResult, OutlineError
from entitlement_support import (
    CLAIM_PERIOD,
    FREE_INTENT_MAX_ATTEMPTS,
    FREE_INTENT_RETRY_DELAY,
    FREE_INTENT_STALE_AFTER,
    GIVEAWAY_CODE,
    GIVEAWAY_LIMIT_BYTES,
    GIVEAWAY_PERIOD,
    GIVEAWAY_WINNER_LIMIT,
    LIMIT_BYTES,
    PUBLIC_LIMIT_BYTES,
    QUOTA_WARNING_THRESHOLDS,
    TRIAL_LIMIT_BYTES,
    TRIAL_PERIOD,
    UTC,
    human_bytes as _human_bytes,
    human_decimal_bytes as _human_decimal_bytes,
    new_id as _new_id,
    outline_key_name as _outline_key_name,
)
from identity import IdentityService
from ports import OutlineGateway
from repositories import RepositoryDatabase


def _access_url_cipher(access_url_key: bytes | str | None) -> Fernet | None:
    try:
        return Fernet(access_url_key) if access_url_key else None
    except (TypeError, ValueError) as exc:
        raise ValueError("access_url_key must be a Fernet key") from exc


class ClaimService:
    """Stable entitlement API over explicit provisioning and policy handlers."""

    def __init__(
        self,
        database: RepositoryDatabase,
        outline: OutlineGateway,
        limit_bytes: int = LIMIT_BYTES,
        trial_limit_bytes: int = TRIAL_LIMIT_BYTES,
        probe_service: Any | None = None,
        access_url_key: bytes | str | None = None,
    ):
        self.database = database
        self.outline = outline
        self.limit_bytes = int(limit_bytes)
        self.trial_limit_bytes = int(trial_limit_bytes)
        self.identity = IdentityService(database)
        self.probe_service = probe_service
        self.access_url_cipher = _access_url_cipher(access_url_key)
        bind_claim_implementations(self, ENTITLEMENT_IMPLEMENTATIONS, STATIC_ENTITLEMENT_IMPLEMENTATIONS)

    def _encrypt_access_url(self, access_url: str) -> str | None:
        """Encrypt an access URL for generic device delivery when configured."""
        if self.access_url_cipher is None:
            return None
        return self.access_url_cipher.encrypt(str(access_url).encode()).decode()
