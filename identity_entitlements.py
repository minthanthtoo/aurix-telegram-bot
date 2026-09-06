"""Entitlement binding and additive-model backfill handlers."""

from __future__ import annotations

import secrets

from identity_entitlements_repository import IdentityEntitlementsRepository
from identity_entitlements_sync_steps import (
    build_credential_indexes,
    sync_active_free_key_leases,
    sync_free_keys,
    sync_paid_key_leases,
    sync_subscriptions,
)
from identity_support import IdentityError, _now_text, _parse_time


class IdentityEntitlementsMixin:
    entitlements_repository = IdentityEntitlementsRepository()
    def ensure_subscription_entitlement(
        self,
        telegram_id: int,
        subscription_id: str,
        *,
        kind: str,
        quota_bytes: int,
        expires_at: str,
        status: str = "active",
        now: str | None = None,
    ) -> str:
        if kind not in {"free", "paid", "trial", "promo"}:
            raise IdentityError("entitlement kind is invalid")
        if status not in {"pending", "active", "expired", "revoked", "cancelled"}:
            raise IdentityError("entitlement status is invalid")
        if int(quota_bytes) <= 0:
            raise IdentityError("entitlement quota must be positive")
        timestamp = str(now or _now_text())
        account_id = self.ensure_account(telegram_id, now=timestamp)
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            existing = self.entitlements_repository.subscription(connection, str(subscription_id))
            if existing is not None and str(existing["account_id"]) != account_id:
                raise IdentityError("subscription is already bound to another account")
            entitlement_id = str(existing["entitlement_id"]) if existing else f"entitlement-{secrets.token_hex(16)}"
            self.entitlements_repository.upsert_subscription(
                connection,
                entitlement_id=entitlement_id,
                account_id=account_id,
                subscription_id=str(subscription_id),
                source_ref=f"subscription:{subscription_id}",
                kind=kind,
                quota_bytes=int(quota_bytes),
                expires_at=str(expires_at),
                status=status,
                timestamp=timestamp,
            )
        return entitlement_id

    def ensure_key_entitlement(
        self,
        telegram_id: int,
        *,
        server_id: str,
        local_key_ref: str,
        kind: str,
        quota_bytes: int,
        expires_at: str,
        status: str = "active",
        now: str | None = None,
    ) -> str:
        if kind not in {"free", "trial", "promo"}:
            raise IdentityError("free entitlement kind is invalid")
        if int(quota_bytes) <= 0:
            raise IdentityError("entitlement quota must be positive")
        timestamp = str(now or _now_text())
        account_id = self.ensure_account(telegram_id, now=timestamp)
        source_ref = f"key:{server_id}:{local_key_ref}"
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            existing = self.entitlements_repository.source(connection, source_ref)
            if existing is not None and str(existing["account_id"]) != account_id:
                raise IdentityError("key entitlement is already bound to another account")
            entitlement_id = str(existing["entitlement_id"]) if existing else f"entitlement-{secrets.token_hex(16)}"
            self.entitlements_repository.upsert_key(
                connection,
                entitlement_id=entitlement_id,
                account_id=account_id,
                source_ref=source_ref,
                kind=kind,
                quota_bytes=int(quota_bytes),
                expires_at=str(expires_at),
                status=status,
                timestamp=timestamp,
            )
        return entitlement_id

    def sync_existing_entitlements(self, *, now: str | None = None) -> dict[str, int]:
        """Backfill the additive model without changing legacy commerce rows."""
        timestamp = str(now or _now_text())
        current_time = _parse_time(timestamp)
        with self.database.connect() as connection:
            inputs = self.entitlements_repository.backfill_inputs(connection)
        subscriptions = inputs["subscriptions"]
        paid_keys = inputs["paid_keys"]
        keys = inputs["keys"]
        entitlements_by_subscription, subscription_expiry, subscription_status = (
            sync_subscriptions(
                self, subscriptions, current_time=current_time, timestamp=timestamp
            )
        )
        entitlements_by_key = sync_free_keys(
            self, keys, current_time=current_time, timestamp=timestamp
        )
        server_endpoints, credentials_by_key = build_credential_indexes(inputs)
        paid_generations, paid_leases = sync_paid_key_leases(
            self,
            paid_keys,
            entitlements_by_subscription=entitlements_by_subscription,
            subscription_expiry=subscription_expiry,
            subscription_status=subscription_status,
            server_endpoints=server_endpoints,
            credentials_by_key=credentials_by_key,
            timestamp=timestamp,
        )
        free_generations, free_leases = sync_active_free_key_leases(
            self,
            keys,
            entitlements_by_key=entitlements_by_key,
            server_endpoints=server_endpoints,
            credentials_by_key=credentials_by_key,
            current_time=current_time,
            timestamp=timestamp,
        )
        return {
            "subscriptions": len(subscriptions),
            "free_keys": len(keys),
            "generations": paid_generations + free_generations,
            "leases": paid_leases + free_leases,
        }
