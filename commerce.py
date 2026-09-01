#!/usr/bin/env python3
"""Backward-compatible facade for the modular paid-commerce subsystem."""

from commerce_models import (
    JOB_RETRY_DELAY,
    NOTIFICATION_RETRY_DELAY,
    QUOTA_WARNING_THRESHOLDS,
    ApprovalResult as ApprovalResult,
    CommerceError as CommerceError,
    OrderResult as OrderResult,
    Plan as Plan,
)
from commerce_repositories import (
    CommerceDatabase as CommerceDatabase,
    PostgresCommerceDatabase as PostgresCommerceDatabase,
    _PostgresConnection as _PostgresConnection,
)
from commerce_service import CommerceService as CommerceService

__all__ = [
    "ApprovalResult",
    "CommerceDatabase",
    "CommerceError",
    "CommerceService",
    "JOB_RETRY_DELAY",
    "NOTIFICATION_RETRY_DELAY",
    "OrderResult",
    "Plan",
    "PostgresCommerceDatabase",
    "QUOTA_WARNING_THRESHOLDS",
]
