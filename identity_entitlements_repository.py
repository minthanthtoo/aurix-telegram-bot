"""Persistence boundary for additive entitlement binding and backfill reads."""

from __future__ import annotations

from typing import Any


class IdentityEntitlementsRepository:
    """SQL operations for entitlement rows and legacy backfill inputs."""

    @staticmethod
    def subscription(connection: Any, subscription_id: str) -> Any:
        return connection.execute(
            "SELECT entitlement_id, account_id FROM entitlements WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchone()

    @staticmethod
    def upsert_subscription(connection: Any, **values: Any) -> None:
        connection.execute(
            """INSERT INTO entitlements
               (entitlement_id, account_id, subscription_id, source_ref, kind, quota_bytes,
                expires_at, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(entitlement_id) DO UPDATE SET
                 account_id = excluded.account_id, kind = excluded.kind,
                 quota_bytes = excluded.quota_bytes, expires_at = excluded.expires_at,
                 status = excluded.status, updated_at = excluded.updated_at""",
            (
                values["entitlement_id"], values["account_id"], values["subscription_id"],
                values["source_ref"], values["kind"], values["quota_bytes"],
                values["expires_at"], values["status"], values["timestamp"], values["timestamp"],
            ),
        )

    @staticmethod
    def source(connection: Any, source_ref: str) -> Any:
        return connection.execute(
            "SELECT entitlement_id, account_id FROM entitlements WHERE source_ref = ?",
            (source_ref,),
        ).fetchone()

    @staticmethod
    def upsert_key(connection: Any, **values: Any) -> None:
        connection.execute(
            """INSERT INTO entitlements
               (entitlement_id, account_id, source_ref, kind, quota_bytes,
                expires_at, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(entitlement_id) DO UPDATE SET
                 quota_bytes = excluded.quota_bytes, expires_at = excluded.expires_at,
                 status = excluded.status, updated_at = excluded.updated_at""",
            (
                values["entitlement_id"], values["account_id"], values["source_ref"],
                values["kind"], values["quota_bytes"], values["expires_at"],
                values["status"], values["timestamp"], values["timestamp"],
            ),
        )

    @staticmethod
    def backfill_inputs(connection: Any) -> dict[str, list[Any]]:
        return {
            "subscriptions": connection.execute(
                """SELECT telegram_id, id, plan_code, quota_bytes, expires_at, status
                     FROM subscriptions WHERE quota_bytes IS NOT NULL"""
            ).fetchall(),
            "paid_keys": connection.execute(
                """SELECT subscription_id, telegram_id, server_id, outline_key_id,
                          quota_bytes, status, created_at
                     FROM paid_vpn_keys WHERE quota_bytes IS NOT NULL"""
            ).fetchall(),
            "keys": connection.execute(
                """SELECT k.id, k.telegram_id, k.server_id, k.outline_key_id, k.key_type,
                          k.data_limit_bytes, k.expires_at, k.status, g.campaign_code
                     FROM keys k LEFT JOIN giveaway_claims g ON g.key_id = k.id"""
            ).fetchall(),
            "credentials": connection.execute(
                """SELECT credential_id, endpoint_id, external_id, status
                     FROM connectivity_credentials WHERE status = 'active'"""
            ).fetchall(),
            "endpoints": connection.execute(
                "SELECT outline_server_id, endpoint_id FROM connectivity_endpoints"
            ).fetchall(),
        }
