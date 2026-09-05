"""Entitlement binding and additive-model backfill handlers."""

from __future__ import annotations

import secrets

from identity_entitlements_repository import IdentityEntitlementsRepository
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
        entitlements_by_subscription: dict[str, str] = {}
        subscription_expiry: dict[str, str] = {}
        subscription_status: dict[str, str] = {}
        for row in subscriptions:
            status = str(row["status"])
            try:
                if status == "active" and _parse_time(str(row["expires_at"])) <= current_time:
                    status = "expired"
            except (TypeError, ValueError, OverflowError):
                status = "expired"
            entitlement_id = self.ensure_subscription_entitlement(
                int(row["telegram_id"]), str(row["id"]), kind="paid",
                quota_bytes=int(row["quota_bytes"]), expires_at=str(row["expires_at"]),
                status=status, now=timestamp,
            )
            entitlements_by_subscription[str(row["id"])] = entitlement_id
            subscription_expiry[str(row["id"])] = str(row["expires_at"])
            subscription_status[str(row["id"])] = status
        kind_map = {"daily_free": "free", "monthly_trial": "trial", "paid": "paid"}
        entitlements_by_key: dict[tuple[str, str], str] = {}
        for row in keys:
            kind = "promo" if row["campaign_code"] else kind_map.get(str(row["key_type"]), "free")
            if kind == "paid":
                continue
            entitlement_id = self.ensure_key_entitlement(
                int(row["telegram_id"]), server_id=str(row["server_id"]),
                local_key_ref=str(row["id"]), kind=kind,
                quota_bytes=int(row["data_limit_bytes"]), expires_at=str(row["expires_at"]),
                status=(
                    "active"
                    if str(row["status"]) == "active"
                    and _parse_time(str(row["expires_at"])) > current_time
                    else "expired"
                ),
                now=timestamp,
            )
            entitlements_by_key[(str(row["server_id"]), str(row["outline_key_id"]))] = entitlement_id
        generations = 0
        leases = 0
        # Registry rows are already rebuilt during CommerceService startup.
        # This pass only connects those durable credentials to the additive
        # identity model, so restart/backfill is safe and does not issue keys.
        credential_rows = inputs["credentials"]
        server_endpoints = {
            str(row["outline_server_id"]): str(row["endpoint_id"])
            for row in inputs["endpoints"]
        }
        credentials_by_key = {
            (str(row["endpoint_id"]), str(row["external_id"])): dict(row)
            for row in credential_rows
        }
        for row in paid_keys:
            entitlement_id = entitlements_by_subscription.get(str(row["subscription_id"]))
            endpoint_id = server_endpoints.get(str(row["server_id"]))
            credential = (
                credentials_by_key.get((endpoint_id, str(row["outline_key_id"])))
                if endpoint_id
                else None
            )
            if (
                not entitlement_id
                or credential is None
                or str(row["status"]) != "active"
                or subscription_status.get(str(row["subscription_id"])) != "active"
            ):
                continue
            generation_id = self.ensure_generation_for_credential(
                entitlement_id,
                str(credential["endpoint_id"]),
                credential_id=str(credential["credential_id"]),
                now=timestamp,
            )
            generations += 1
            if self._active_lease_for_generation(generation_id) is None:
                self.ensure_generation_lease(
                    entitlement_id,
                    generation_id,
                    str(credential["endpoint_id"]),
                    int(row["quota_bytes"]),
                    subscription_expiry[str(row["subscription_id"])],
                    now=timestamp,
                )
                leases += 1
        for row in keys:
            if str(row["status"]) != "active":
                continue
            try:
                if _parse_time(str(row["expires_at"])) <= current_time:
                    continue
            except (TypeError, ValueError, OverflowError):
                continue
            entitlement_id = entitlements_by_key.get(
                (str(row["server_id"]), str(row["outline_key_id"]))
            )
            endpoint_id = server_endpoints.get(str(row["server_id"]))
            credential = (
                credentials_by_key.get((endpoint_id, str(row["outline_key_id"])))
                if endpoint_id
                else None
            )
            if not entitlement_id or credential is None:
                continue
            generation_id = self.ensure_generation_for_credential(
                entitlement_id,
                str(credential["endpoint_id"]),
                credential_id=str(credential["credential_id"]),
                now=timestamp,
            )
            generations += 1
            if self._active_lease_for_generation(generation_id) is None:
                self.ensure_generation_lease(
                    entitlement_id,
                    generation_id,
                    str(credential["endpoint_id"]),
                    int(row["data_limit_bytes"]),
                    str(row["expires_at"]),
                    now=timestamp,
                )
                leases += 1
        return {
            "subscriptions": len(subscriptions),
            "free_keys": len(keys),
            "generations": generations,
            "leases": leases,
        }
