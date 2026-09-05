"""Entitlement binding and additive-model backfill handlers."""

from __future__ import annotations

import secrets

from identity_support import IdentityError, _now_text, _parse_time


class IdentityEntitlementsMixin:
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
            existing = connection.execute(
                "SELECT entitlement_id, account_id FROM entitlements WHERE subscription_id = ?",
                (str(subscription_id),),
            ).fetchone()
            if existing is not None and str(existing["account_id"]) != account_id:
                raise IdentityError("subscription is already bound to another account")
            entitlement_id = str(existing["entitlement_id"]) if existing else f"entitlement-{secrets.token_hex(16)}"
            connection.execute(
                """INSERT INTO entitlements
                   (entitlement_id, account_id, subscription_id, source_ref, kind, quota_bytes, expires_at, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entitlement_id) DO UPDATE SET
                     account_id = excluded.account_id, kind = excluded.kind,
                     quota_bytes = excluded.quota_bytes, expires_at = excluded.expires_at,
                     status = excluded.status, updated_at = excluded.updated_at""",
                (entitlement_id, account_id, str(subscription_id), f"subscription:{subscription_id}", kind, int(quota_bytes), str(expires_at), status, timestamp, timestamp),
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
            existing = connection.execute(
                "SELECT entitlement_id, account_id FROM entitlements WHERE source_ref = ?",
                (source_ref,),
            ).fetchone()
            if existing is not None and str(existing["account_id"]) != account_id:
                raise IdentityError("key entitlement is already bound to another account")
            entitlement_id = str(existing["entitlement_id"]) if existing else f"entitlement-{secrets.token_hex(16)}"
            connection.execute(
                """INSERT INTO entitlements
                   (entitlement_id, account_id, source_ref, kind, quota_bytes, expires_at, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entitlement_id) DO UPDATE SET
                     quota_bytes = excluded.quota_bytes, expires_at = excluded.expires_at,
                     status = excluded.status, updated_at = excluded.updated_at""",
                (entitlement_id, account_id, source_ref, kind, int(quota_bytes), str(expires_at), status, timestamp, timestamp),
            )
        return entitlement_id

    def sync_existing_entitlements(self, *, now: str | None = None) -> dict[str, int]:
        """Backfill the additive model without changing legacy commerce rows."""
        timestamp = str(now or _now_text())
        current_time = _parse_time(timestamp)
        subscriptions = []
        paid_keys = []
        keys = []
        with self.database.connect() as connection:
            subscriptions = connection.execute(
                """SELECT telegram_id, id, plan_code, quota_bytes, expires_at, status
                     FROM subscriptions WHERE quota_bytes IS NOT NULL"""
            ).fetchall()
            paid_keys = connection.execute(
                """SELECT subscription_id, telegram_id, server_id, outline_key_id,
                          quota_bytes, status, created_at
                     FROM paid_vpn_keys
                    WHERE quota_bytes IS NOT NULL"""
            ).fetchall()
            keys = connection.execute(
                """SELECT k.id, k.telegram_id, k.server_id, k.outline_key_id, k.key_type,
                          k.data_limit_bytes, k.expires_at, k.status,
                          g.campaign_code
                     FROM keys k
                     LEFT JOIN giveaway_claims g ON g.key_id = k.id"""
            ).fetchall()
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
        with self.database.connect() as connection:
            credential_rows = connection.execute(
                """SELECT credential_id, endpoint_id, external_id, status
                     FROM connectivity_credentials
                    WHERE status = 'active'"""
            ).fetchall()
            credentials_by_key = {
                (str(row["endpoint_id"]), str(row["external_id"])): dict(row)
                for row in credential_rows
            }
            server_endpoints = {
                str(row["outline_server_id"]): str(row["endpoint_id"])
                for row in connection.execute(
                    "SELECT outline_server_id, endpoint_id FROM connectivity_endpoints"
                ).fetchall()
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
