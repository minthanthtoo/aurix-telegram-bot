"""Dependency-injection models for the AuriX runtime composition root."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from access_control import StaffAccessControl
from commerce import CommerceDatabase, CommerceService, PostgresCommerceDatabase
from entitlements import ClaimService
from fleet_probe import FleetProbeService
from free_repository import Database
from identity import IdentityService
from outline_adapter import OutlineClient, OutlineServerPool
from supabase_storage import NullReceiptStorage, SupabaseReceiptStorage
from telegram_transport import TelegramBot


@dataclass(frozen=True)
class RuntimeFactories:
    """Factories kept injectable so composition tests need no external services."""

    database: Callable[..., Any] = Database
    commerce_database: Callable[..., Any] = CommerceDatabase
    postgres_commerce_database: Callable[..., Any] = PostgresCommerceDatabase
    receipt_storage: Callable[..., Any] = SupabaseReceiptStorage
    null_receipt_storage: Callable[..., Any] = NullReceiptStorage
    outline_client: Callable[..., Any] = OutlineClient
    outline_server_pool: Callable[..., Any] = OutlineServerPool
    commerce: Callable[..., Any] = CommerceService
    probe_service: Callable[..., Any] = FleetProbeService
    identity_service: Callable[..., Any] = IdentityService
    claim_service: Callable[..., Any] = ClaimService
    staff_access: Callable[..., Any] = StaffAccessControl
    telegram_bot: Callable[..., Any] = TelegramBot

@dataclass
class RuntimeApplication:
    """Constructed services required by the process lifecycle."""

    database: Any
    commerce_database: Any
    outline: Any
    commerce: Any
    claim_service: Any
    probe_service: Any
    staff_access: Any
    bot: Any
