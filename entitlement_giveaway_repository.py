"""Persistence boundary for giveaway campaigns and free-claim state."""

from __future__ import annotations

from typing import Any


class GiveawayRepository:
    """Queries and writes shared by campaign, claim, and status workflows."""

    @staticmethod
    def table_exists(connection: Any, name: str) -> bool:
        if connection.__class__.__name__ == "_PostgresConnection":
            row = connection.execute(
                "SELECT to_regclass(?) AS table_name", (f"public.{name}",)
            ).fetchone()
            return bool(row and row["table_name"])
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
            ).fetchone()
            is not None
        )

    @classmethod
    def commerce_tables_exist(cls, connection: Any) -> bool:
        return cls.table_exists(connection, "orders")

    @classmethod
    def intent_tables_exist(cls, connection: Any) -> bool:
        return cls.table_exists(connection, "free_provisioning_intents")

    @staticmethod
    def lock_user(connection: Any, telegram_id: int) -> None:
        if connection.__class__.__name__ == "_PostgresConnection":
            connection.execute(
                "SELECT telegram_id FROM users WHERE telegram_id = ? FOR UPDATE",
                (telegram_id,),
            ).fetchone()

    @staticmethod
    def has_active_promo_gift(connection: Any, telegram_id: int, now_text: str) -> bool:
        return (
            connection.execute(
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
            is not None
        )

    @staticmethod
    def upsert_user(
        connection: Any,
        telegram_id: int,
        first_name: str,
        username: str | None,
        now_text: str,
    ) -> None:
        connection.execute(
            """INSERT INTO users (telegram_id, first_name, username, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                   first_name = excluded.first_name, username = excluded.username""",
            (telegram_id, first_name[:128], (username or "")[:64] or None, now_text),
        )

    @staticmethod
    def user_claim_marker(connection: Any, telegram_id: int, marker: str) -> Any:
        if marker not in {"last_claim_at", "trial_claimed_at"}:
            raise ValueError("unsupported claim marker")
        return connection.execute(
            f"SELECT {marker} FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()

    @staticmethod
    def campaign_by_code(
        connection: Any, code: str, *, for_update: bool = False
    ) -> Any:
        suffix = " FOR UPDATE" if for_update and connection.__class__.__name__ == "_PostgresConnection" else ""
        return connection.execute(
            "SELECT * FROM giveaway_campaigns WHERE UPPER(code) = ?" + suffix,
            (code,),
        ).fetchone()

    @staticmethod
    def active_campaign(connection: Any) -> Any:
        return connection.execute(
            """SELECT * FROM giveaway_campaigns
               ORDER BY active DESC, COALESCE(updated_at, created_at) DESC
               LIMIT 1"""
        ).fetchone()

    @staticmethod
    def claim_for_user(connection: Any, campaign_code: str, telegram_id: int) -> Any:
        return connection.execute(
            """SELECT g.winner_number, g.claimed_at, k.expires_at, k.status,
                      k.quota_reason, k.data_limit_bytes
               FROM giveaway_claims g JOIN keys k ON k.id = g.key_id
               WHERE g.campaign_code = ? AND g.telegram_id = ?""",
            (campaign_code, telegram_id),
        ).fetchone()

    @staticmethod
    def claim_count(connection: Any, campaign_code: str) -> int:
        return int(
            connection.execute(
                "SELECT COUNT(*) AS n FROM giveaway_claims WHERE campaign_code = ?",
                (campaign_code,),
            ).fetchone()["n"]
        )

    @staticmethod
    def giveaway_window(connection: Any, campaign_code: str, window_start: str) -> Any:
        return connection.execute(
            """SELECT claimed_count FROM giveaway_windows
               WHERE campaign_code = ? AND window_start = ?""",
            (campaign_code, window_start),
        ).fetchone()

    @staticmethod
    def pending_window_count(connection: Any, campaign_code: str, window_start: str) -> int:
        return int(
            connection.execute(
                """SELECT COUNT(*) AS n FROM free_provisioning_intents
                   WHERE campaign_code = ? AND window_start = ?
                     AND status IN ('pending', 'running')""",
                (campaign_code, window_start),
            ).fetchone()["n"]
        )

    @staticmethod
    def pending_campaign_count(connection: Any, campaign_code: str) -> int:
        return int(
            connection.execute(
                """SELECT COUNT(*) AS n FROM free_provisioning_intents
                   WHERE campaign_code = ? AND status IN ('pending', 'running')""",
                (campaign_code,),
            ).fetchone()["n"]
        )

    @staticmethod
    def insert_window(
        connection: Any, campaign_code: str, window_start: str, claimed_count: int
    ) -> None:
        connection.execute(
            """INSERT INTO giveaway_windows
               (campaign_code, window_start, claimed_count) VALUES (?, ?, ?)""",
            (campaign_code, window_start, claimed_count),
        )

    @staticmethod
    def paid_order_conflict(connection: Any, telegram_id: int) -> Any:
        return connection.execute(
            """SELECT 1 FROM orders
               WHERE telegram_id = ?
                 AND status IN ('awaiting_payment', 'payment_submitted')
                 AND COALESCE(refund_status, 'none') != 'refunded'
               UNION ALL
               SELECT 1 FROM subscriptions
               WHERE telegram_id = ? AND status IN ('pending', 'active')
               LIMIT 1""",
            (telegram_id, telegram_id),
        ).fetchone()

    @staticmethod
    def reconciliation_rows(connection: Any) -> list[Any]:
        return connection.execute(
            """SELECT k.server_id, k.outline_key_id, c.quota_bytes
               FROM giveaway_claims g
               JOIN giveaway_campaigns c ON c.code = g.campaign_code
               JOIN keys k ON k.id = g.key_id
               WHERE k.status IN ('active', 'revoke_failed')
                 AND k.quota_reason IS NULL"""
        ).fetchall()

    @staticmethod
    def insert_default_campaign(
        connection: Any, code: str, quota_bytes: int, winner_limit: int, now_text: str
    ) -> None:
        connection.execute(
            """INSERT INTO giveaway_campaigns
               (code, quota_bytes, duration_days, winner_limit, claimed_count, active,
                created_at, frequency, updated_at)
               VALUES (?, ?, 30, ?, 0, 1, ?, 'campaign', ?)
               ON CONFLICT(code) DO NOTHING""",
            (code, quota_bytes, winner_limit, now_text, now_text),
        )

    @staticmethod
    def update_intent_next(connection: Any, intent_id: str, now_text: str) -> None:
        connection.execute(
            "UPDATE free_provisioning_intents SET next_attempt_at = ? WHERE id = ?",
            (now_text, intent_id),
        )

    @staticmethod
    def reset_intent(connection: Any, intent_id: str, now_text: str) -> None:
        connection.execute(
            """UPDATE free_provisioning_intents
               SET status = 'pending', attempts = 0, next_attempt_at = ?,
                   locked_at = NULL, last_error = NULL WHERE id = ?""",
            (now_text, intent_id),
        )

    @staticmethod
    def intent_by_id(connection: Any, intent_id: str) -> Any:
        return connection.execute(
            "SELECT * FROM free_provisioning_intents WHERE id = ?", (intent_id,)
        ).fetchone()

    @staticmethod
    def max_window_claims(connection: Any, campaign_code: str) -> int:
        return int(
            connection.execute(
                """SELECT COALESCE(MAX(claimed_count), 0) AS n
                   FROM giveaway_windows WHERE campaign_code = ?""",
                (campaign_code,),
            ).fetchone()["n"]
        )

    @staticmethod
    def insert_campaign(
        connection: Any,
        *,
        code: str,
        quota_bytes: int,
        duration_days: int,
        winner_limit: int,
        created_at: str,
        starts_at: str,
        ends_at: str,
        frequency: str,
    ) -> None:
        connection.execute(
            """INSERT INTO giveaway_campaigns
               (code, quota_bytes, duration_days, winner_limit, claimed_count,
                active, created_at, starts_at, ends_at, frequency, updated_at)
               VALUES (?, ?, ?, ?, 0, 1, ?, ?, ?, ?, ?)""",
            (code, quota_bytes, duration_days, winner_limit, created_at, starts_at, ends_at, frequency, created_at),
        )

    @staticmethod
    def update_campaign(
        connection: Any,
        *,
        code: str,
        quota_bytes: int,
        duration_days: int,
        winner_limit: int,
        starts_at: str,
        ends_at: str,
        frequency: str,
        now_text: str,
    ) -> None:
        connection.execute(
            """UPDATE giveaway_campaigns
               SET quota_bytes = ?, duration_days = ?, winner_limit = ?, active = 1,
                   starts_at = ?, ends_at = ?, frequency = ?, updated_at = ?
               WHERE code = ?""",
            (quota_bytes, duration_days, winner_limit, starts_at, ends_at, frequency, now_text, code),
        )

    @staticmethod
    def deactivate_other_campaigns(connection: Any, code: str, now_text: str) -> None:
        connection.execute(
            "UPDATE giveaway_campaigns SET active = 0, updated_at = ? WHERE code != ? AND active = 1",
            (now_text, code),
        )

    @staticmethod
    def set_active(connection: Any, code: str, active: bool, now_text: str) -> Any:
        row = GiveawayRepository.campaign_by_code(connection, code)
        if row is None:
            return None
        if active:
            connection.execute(
                "UPDATE giveaway_campaigns SET active = 0, updated_at = ? WHERE code != ?",
                (now_text, row["code"]),
            )
        connection.execute(
            "UPDATE giveaway_campaigns SET active = ?, updated_at = ? WHERE code = ?",
            (1 if active else 0, now_text, row["code"]),
        )
        return row
