"""Shared low-level persistence operations for commerce use cases."""

from __future__ import annotations

import json
from typing import Any

from commerce_models import _new_id, _now_text
from commerce_repositories import _PostgresConnection


class SharedCommerceRepository:
    """Small, transaction-neutral primitives shared by application services."""

    @staticmethod
    def lock_order(connection: Any, order_id: str) -> None:
        if isinstance(connection, _PostgresConnection):
            connection.execute(
                "SELECT id FROM orders WHERE id = ? FOR UPDATE", (order_id,)
            ).fetchone()

    @staticmethod
    def active_promo_exists(connection: Any, telegram_id: int, now_text: str) -> bool:
        if not isinstance(connection, _PostgresConnection):
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'giveaway_claims'"
            ).fetchone()
            if table is None:
                return False
        winner = connection.execute(
            """SELECT 1
               FROM giveaway_claims g
               JOIN giveaway_campaigns c ON c.code = g.campaign_code
               JOIN keys k ON k.id = g.key_id
               WHERE g.telegram_id = ?
                 AND c.active = 1
                 AND (c.starts_at IS NULL OR c.starts_at <= ?)
                 AND (c.ends_at IS NULL OR c.ends_at > ?)
                 AND k.status IN ('active', 'revoke_failed')
                 AND k.expires_at > ?
                 AND k.quota_reason IS NULL
               LIMIT 1""",
            (telegram_id, now_text, now_text, now_text),
        ).fetchone()
        return winner is not None

    @staticmethod
    def table_exists(connection: Any, name: str) -> bool:
        if connection.__class__.__name__ == "_PostgresConnection":
            row = connection.execute(
                "SELECT to_regclass(?) AS table_name", (f"public.{name}",)
            ).fetchone()
            return bool(row and row["table_name"])
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone() is not None

    @staticmethod
    def ensure_user(
        connection: Any,
        telegram_id: int,
        first_name: str,
        username: str | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO users (telegram_id, first_name, username, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                   first_name = excluded.first_name,
                   username = COALESCE(excluded.username, users.username)""",
            (
                telegram_id,
                first_name[:128],
                (username or "").lstrip("@")[:64] or None,
                _now_text(),
            ),
        )

    @staticmethod
    def audit(
        connection: Any,
        action: str,
        target_type: str,
        target_id: str,
        actor_type: str,
        actor_id: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO audit_events
               (actor_type, actor_id, action, target_type, target_id, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                actor_type,
                actor_id,
                action,
                target_type,
                target_id,
                json.dumps(metadata or {}, sort_keys=True),
                _now_text(),
            ),
        )

    @staticmethod
    def queue_staff_notification(
        connection: Any,
        event_type: str,
        entity_id: str,
        text: str,
        created_at: str,
    ) -> None:
        try:
            rows = connection.execute(
                """SELECT s.telegram_id
                   FROM staff_accounts s
                   LEFT JOIN staff_notification_preferences p
                     ON p.telegram_id = s.telegram_id AND p.event_type = ?
                   WHERE s.status = 'active' AND COALESCE(p.enabled, 1) = 1""",
                (event_type,),
            ).fetchall()
        except Exception:
            return
        for row in rows:
            staff_id = int(row["telegram_id"])
            connection.execute(
                """INSERT INTO notifications
                   (id, dedupe_key, telegram_id, kind, text, status, next_attempt_at, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                   ON CONFLICT(dedupe_key) DO NOTHING""",
                (
                    _new_id(),
                    f"staff:{event_type}:{entity_id}:{staff_id}",
                    staff_id,
                    f"staff_{event_type}",
                    text,
                    created_at,
                    created_at,
                ),
            )

    @staticmethod
    def queue_customer_repair_notification(
        connection: Any,
        repair_id: str,
        telegram_id: int,
        status: str,
        endpoint: str,
        created_at: str,
    ) -> None:
        normalized = str(status or "").strip().lower()
        if normalized in {"pending", "running"}:
            message = (
                "🛠 AuriX noticed that your VPN key is temporarily unavailable.\n\n"
                "Secure recovery is in progress. Your remaining quota is protected; "
                "please refresh My VPN shortly."
            )
        elif normalized == "manual":
            message = (
                "⚠️ Your AuriX VPN key needs owner review because trusted usage data "
                "was unavailable.\n\n"
                "The old credential is withheld and no quota reset or replacement has "
                "been issued. We will notify you after a safe decision."
            )
        else:
            message = (
                "🟡 AuriX is retrying recovery of your VPN key.\n\n"
                "Your entitlement remains recorded and no extra quota has been granted. "
                "Please refresh My VPN later."
            )
        connection.execute(
            """INSERT INTO notifications
               (id, dedupe_key, telegram_id, kind, text, status,
                next_attempt_at, created_at)
               VALUES (?, ?, ?, 'vpn_key_repair', ?, 'pending', ?, ?)
               ON CONFLICT(dedupe_key) DO NOTHING""",
            (
                _new_id(),
                f"customer:key_repairs:{repair_id}:{normalized}",
                int(telegram_id),
                message + f"\n\nEndpoint: {str(endpoint)[:64]}",
                created_at,
                created_at,
            ),
        )

    @staticmethod
    def queue_receipt_extraction(connection: Any, evidence_id: str, created_at: str) -> None:
        connection.execute(
            """INSERT INTO receipt_extraction_jobs
               (id, evidence_id, status, next_attempt_at, created_at)
               VALUES (?, ?, 'pending', ?, ?)
               ON CONFLICT(evidence_id) DO NOTHING""",
            (_new_id(), evidence_id, created_at, created_at),
        )
