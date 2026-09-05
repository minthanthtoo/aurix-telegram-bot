"""Compatibility facade for component-owned migration registries."""

from __future__ import annotations

from commerce_migrations import COMMERCE_MIGRATIONS
from commerce_models import _normalize_reference
from free_access_migrations import FREE_ACCESS_MIGRATIONS
from migration_hooks import (
    _add_normalized_payment_reference_guard,
    _canonicalize_payment_provider_identity,
    _rebuild_free_intents_for_server_identity,
    _rebuild_free_keys_for_server_identity,
    _rebuild_paid_keys_for_server_identity,
    _rebuild_staff_notification_preferences_for_key_repairs,
)
from schema_migrations import Migration, MigrationError, apply_migrations


__all__ = [
    "COMMERCE_MIGRATIONS",
    "FREE_ACCESS_MIGRATIONS",
    "Migration",
    "MigrationError",
    "apply_migrations",
]
