"""Compatibility facade for account, device, entitlement, and quota services."""

from __future__ import annotations

from typing import Any

from identity_accounts import IdentityAccountsMixin
from identity_core import IdentityCoreMixin
from identity_entitlements import IdentityEntitlementsMixin
from identity_generations import IdentityGenerationsMixin
from identity_support import (
    IdentityError,
    UTC,
    _now_text,
    _parse_time,
    _token_hash,
    account_id_for_telegram,
)
from identity_usage import IdentityUsageMixin


class IdentityService(
    IdentityCoreMixin,
    IdentityAccountsMixin,
    IdentityEntitlementsMixin,
    IdentityGenerationsMixin,
    IdentityUsageMixin,
):
    """Stable identity API over responsibility-owned lifecycle handlers."""

    def __init__(self, database: Any):
        self.database = database
