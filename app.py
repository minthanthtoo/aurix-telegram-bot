#!/usr/bin/env python3
"""Executable compatibility facade for the modular AuriX application."""

from datetime import timezone

from commerce import (
    CommerceDatabase as CommerceDatabase,
    CommerceError as CommerceError,
    CommerceService as CommerceService,
    PostgresCommerceDatabase as PostgresCommerceDatabase,
)
from entitlements import (
    CLAIM_PERIOD as CLAIM_PERIOD,
    LIMIT_BYTES as LIMIT_BYTES,
    PUBLIC_LIMIT_BYTES as PUBLIC_LIMIT_BYTES,
    QUOTA_WARNING_THRESHOLDS as QUOTA_WARNING_THRESHOLDS,
    TRIAL_LIMIT_BYTES as TRIAL_LIMIT_BYTES,
    TRIAL_PERIOD as TRIAL_PERIOD,
    ClaimResult as ClaimResult,
    ClaimService as ClaimService,
    OutlineError as OutlineError,
    _human_bytes as _human_bytes,
    _new_id as _new_id,
    _outline_key_name as _outline_key_name,
)
from free_repository import Database as Database
from outline_adapter import OutlineClient as OutlineClient
from ports import ReceiptExtractorGateway as ReceiptExtractorGateway
from receipt_llm import (
    OpenAICompatibleReceiptExtractor as OpenAICompatibleReceiptExtractor,
    ReceiptExtractionError as ReceiptExtractionError,
    ReceiptLLMUnavailable as ReceiptLLMUnavailable,
)
from runtime import main as main
from supabase_storage import (
    NullReceiptStorage as NullReceiptStorage,
    SupabaseReceiptStorage as SupabaseReceiptStorage,
)
from telegram_transport import (
    ADMIN_CONFIRMATION_TTL as ADMIN_CONFIRMATION_TTL,
    DEFAULT_MAINTENANCE_INTERVAL_SECONDS as DEFAULT_MAINTENANCE_INTERVAL_SECONDS,
    AdminOperations as AdminOperations,
    TelegramBot as TelegramBot,
)

UTC = timezone.utc
LEGACY_LIMIT_BYTES = 100 * 1024 * 1024


if __name__ == "__main__":
    main()
