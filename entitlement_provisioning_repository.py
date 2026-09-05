"""Persistence boundary for durable free, trial, and promo provisioning."""

from __future__ import annotations

from typing import Any


class EntitlementProvisioningRepository:
    """SQL operations used by the entitlement provisioning workflow."""

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
    def server_tables_exist(connection: Any) -> bool:
        if connection.__class__.__name__ == "_PostgresConnection":
            row = connection.execute(
                "SELECT to_regclass('public.outline_servers') AS table_name"
            ).fetchone()
            return bool(row and row["table_name"])
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'outline_servers'"
        ).fetchone() is not None

    @staticmethod
    def adjust_remote_key_count(connection: Any, server_id: str, delta: int) -> None:
        if not EntitlementProvisioningRepository.server_tables_exist(connection):
            return
        connection.execute(
            """UPDATE outline_servers
               SET remote_key_count = CASE
                 WHEN COALESCE(remote_key_count, 0) + ? < 0 THEN 0
                 ELSE COALESCE(remote_key_count, 0) + ? END
               WHERE server_id = ?""",
            (int(delta), int(delta), server_id),
        )

    @staticmethod
    def active_credential(connection: Any, server_id: str, external_id: str) -> Any:
        return connection.execute(
            """SELECT c.credential_id, c.endpoint_id
                 FROM connectivity_credentials c
                 JOIN connectivity_endpoints e ON e.endpoint_id = c.endpoint_id
                WHERE e.outline_server_id = ? AND c.external_id = ?
                  AND c.status = 'active'""",
            (server_id, external_id),
        ).fetchone()

    @staticmethod
    def claim_intent(
        connection: Any,
        *,
        intent_id: str,
        max_attempts: int,
        now_text: str,
        stale_before: str,
    ) -> dict[str, Any] | None:
        connection.execute(
            """UPDATE free_provisioning_intents
               SET status = 'pending', locked_at = NULL
             WHERE status = 'running' AND locked_at < ?""",
            (stale_before,),
        )
        lock_clause = (
            " FOR UPDATE SKIP LOCKED"
            if connection.__class__.__name__ == "_PostgresConnection"
            else ""
        )
        if intent_id:
            row = connection.execute(
                """SELECT * FROM free_provisioning_intents
                   WHERE id = ?
                     AND (status = 'pending' OR (status = 'failed' AND attempts < ?))
                     AND next_attempt_at <= ?"""
                + lock_clause,
                (intent_id, max_attempts, now_text),
            ).fetchone()
        else:
            row = connection.execute(
                """SELECT * FROM free_provisioning_intents
                   WHERE (status = 'pending' OR (status = 'failed' AND attempts < ?))
                     AND next_attempt_at <= ?
                   ORDER BY created_at LIMIT 1"""
                + lock_clause,
                (max_attempts, now_text),
            ).fetchone()
        if row is None:
            return None
        cursor = connection.execute(
            """UPDATE free_provisioning_intents
               SET status = 'running', attempts = attempts + 1, locked_at = ?
             WHERE id = ? AND (status = 'pending' OR status = 'failed')""",
            (now_text, row["id"]),
        )
        if (
            connection.__class__.__name__ == "_PostgresConnection"
            and cursor.rowcount != 1
        ):
            return None
        result = dict(row)
        result.update(
            status="running",
            attempts=int(row["attempts"] or 0) + 1,
            locked_at=now_text,
        )
        return result

    @staticmethod
    def reset_intent(connection: Any, intent_id: str, next_attempt_at: str) -> None:
        connection.execute(
            """UPDATE free_provisioning_intents
               SET status = 'pending', attempts = 0, next_attempt_at = ?,
                   locked_at = NULL, last_error = NULL
             WHERE id = ? AND status = 'failed'""",
            (next_attempt_at, intent_id),
        )

    @staticmethod
    def intent_attempts(connection: Any, intent_id: str) -> int:
        row = connection.execute(
            "SELECT attempts FROM free_provisioning_intents WHERE id = ?", (intent_id,)
        ).fetchone()
        return int(row["attempts"] or 0) if row is not None else 0

    @staticmethod
    def mark_failed(
        connection: Any, *, intent_id: str, next_attempt_at: str, error: str
    ) -> None:
        connection.execute(
            """UPDATE free_provisioning_intents
               SET status = 'failed', next_attempt_at = ?, locked_at = NULL, last_error = ?
             WHERE id = ?""",
            (next_attempt_at, error, intent_id),
        )

    @staticmethod
    def mark_done(
        connection: Any, *, intent_id: str, key_id: int | None, completed_at: str
    ) -> None:
        connection.execute(
            """UPDATE free_provisioning_intents
               SET status = 'done', locked_at = NULL, last_error = NULL,
                   key_id = ?, completed_at = ? WHERE id = ?""",
            (key_id, completed_at, intent_id),
        )

    @staticmethod
    def existing_key(connection: Any, server_id: str, outline_key_id: str) -> Any:
        return connection.execute(
            """SELECT id, telegram_id, status FROM keys
               WHERE server_id = ? AND outline_key_id = ?""",
            (server_id, outline_key_id),
        ).fetchone()

    @staticmethod
    def insert_key(connection: Any, **values: Any) -> int:
        cursor = connection.execute(
            """INSERT INTO keys
               (telegram_id, server_id, outline_key_id, key_type, created_at, expires_at,
                data_limit_bytes, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'active')""",
            (
                values["telegram_id"],
                values["server_id"],
                values["outline_key_id"],
                values["key_type"],
                values["created_at"],
                values["expires_at"],
                values["quota_bytes"],
            ),
        )
        local_id = int(getattr(cursor, "lastrowid", 0) or 0)
        if local_id:
            return local_id
        return int(
            connection.execute(
                """SELECT id FROM keys WHERE server_id = ? AND outline_key_id = ?""",
                (values["server_id"], values["outline_key_id"]),
            ).fetchone()["id"]
        )

    @staticmethod
    def update_user_claim(
        connection: Any, *, kind: str, telegram_id: int, claim_started_at: str
    ) -> None:
        if kind == "daily":
            connection.execute(
                "UPDATE users SET last_claim_at = ? WHERE telegram_id = ?",
                (claim_started_at, telegram_id),
            )
        else:
            connection.execute(
                "UPDATE users SET trial_claimed_at = ? WHERE telegram_id = ?",
                (claim_started_at, telegram_id),
            )

    @staticmethod
    def giveaway_claim(connection: Any, campaign_code: str, telegram_id: int) -> Any:
        return connection.execute(
            "SELECT 1 FROM giveaway_claims WHERE campaign_code = ? AND telegram_id = ?",
            (campaign_code, telegram_id),
        ).fetchone()

    @staticmethod
    def insert_giveaway_claim(connection: Any, **values: Any) -> None:
        connection.execute(
            """INSERT INTO giveaway_claims
               (campaign_code, telegram_id, key_id, winner_number, claimed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                values["campaign_code"],
                values["telegram_id"],
                values["key_id"],
                values["winner_number"],
                values["claimed_at"],
            ),
        )

    @staticmethod
    def giveaway_window(connection: Any, campaign_code: str, window_start: str) -> Any:
        return connection.execute(
            """SELECT claimed_count FROM giveaway_windows
               WHERE campaign_code = ? AND window_start = ?""",
            (campaign_code, window_start),
        ).fetchone()

    @staticmethod
    def insert_giveaway_window(
        connection: Any, campaign_code: str, window_start: str
    ) -> None:
        connection.execute(
            """INSERT INTO giveaway_windows
               (campaign_code, window_start, claimed_count) VALUES (?, ?, 1)""",
            (campaign_code, window_start),
        )

    @staticmethod
    def increment_giveaway_window(
        connection: Any, campaign_code: str, window_start: str
    ) -> None:
        connection.execute(
            """UPDATE giveaway_windows SET claimed_count = claimed_count + 1
               WHERE campaign_code = ? AND window_start = ?""",
            (campaign_code, window_start),
        )

    @staticmethod
    def increment_campaign(connection: Any, campaign_code: str, now_text: str) -> None:
        connection.execute(
            """UPDATE giveaway_campaigns
               SET claimed_count = CASE WHEN claimed_count < winner_limit
                                        THEN claimed_count + 1 ELSE claimed_count END,
                   updated_at = ? WHERE code = ?""",
            (now_text, campaign_code),
        )

    @staticmethod
    def update_intent_outline_key(
        connection: Any, intent_id: str, outline_key_id: str
    ) -> None:
        connection.execute(
            """UPDATE free_provisioning_intents SET outline_key_id = ?
               WHERE id = ? AND status = 'running'""",
            (outline_key_id, intent_id),
        )

    @staticmethod
    def intent_by_id(connection: Any, intent_id: str) -> Any:
        return connection.execute(
            "SELECT * FROM free_provisioning_intents WHERE id = ?", (intent_id,)
        ).fetchone()

    @staticmethod
    def insert_intent(connection: Any, **values: Any) -> dict[str, Any]:
        connection.execute(
            """INSERT INTO free_provisioning_intents
               (id, telegram_id, kind, campaign_code, window_start, winner_number,
                server_id, outline_key_id, key_name, quota_bytes, duration_days,
                claim_started_at, status, attempts, next_attempt_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)""",
            (
                values["intent_id"], values["telegram_id"], values["kind"],
                values["campaign_code"], values["window_start"], values["winner_number"],
                values["server_id"], values["outline_key_id"], values["key_name"],
                values["quota_bytes"], values["duration_days"], values["claim_started_at"],
                values["claim_started_at"], values["claim_started_at"],
            ),
        )
        return dict(
            connection.execute(
                "SELECT * FROM free_provisioning_intents WHERE id = ?",
                (values["intent_id"],),
            ).fetchone()
        )

    @staticmethod
    def latest_intent(
        connection: Any, telegram_id: int, kind: str, campaign_code: str | None
    ) -> dict[str, Any] | None:
        if campaign_code is None:
            row = connection.execute(
                """SELECT * FROM free_provisioning_intents
                   WHERE telegram_id = ? AND kind = ? AND status != 'cancelled'
                   ORDER BY created_at DESC LIMIT 1""",
                (telegram_id, kind),
            ).fetchone()
        else:
            row = connection.execute(
                """SELECT * FROM free_provisioning_intents
                   WHERE telegram_id = ? AND kind = ? AND campaign_code = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (telegram_id, kind, campaign_code),
            ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def campaign_counts(connection: Any, campaign_code: str) -> Any:
        return connection.execute(
            "SELECT winner_limit, claimed_count FROM giveaway_campaigns WHERE code = ?",
            (campaign_code,),
        ).fetchone()
