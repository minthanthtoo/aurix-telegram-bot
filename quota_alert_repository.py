"""Persistence boundary for customer quota-alert preferences."""

from __future__ import annotations

from typing import Any


class QuotaAlertRepository:
    @staticmethod
    def preferences(connection: Any, telegram_id: int) -> Any:
        return connection.execute(
            "SELECT * FROM user_quota_alert_preferences WHERE telegram_id = ?",
            (int(telegram_id),),
        ).fetchone()

    @staticmethod
    def upsert_preferences(connection: Any, **values: Any) -> None:
        connection.execute(
            """INSERT INTO user_quota_alert_preferences
               (telegram_id, enabled, mode, alert_count, step_value, version, updated_at)
               VALUES (?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                 enabled = excluded.enabled, mode = excluded.mode,
                 alert_count = excluded.alert_count, step_value = excluded.step_value,
                 version = user_quota_alert_preferences.version + 1,
                 updated_at = excluded.updated_at""",
            (
                int(values["telegram_id"]), values["enabled"], values["mode"],
                values["alert_count"], values["step_value"], values["updated_at"],
            ),
        )

    @staticmethod
    def clear_legacy_warning(connection: Any, table: str, telegram_id: int) -> None:
        connection.execute(
            f"UPDATE {table} SET quota_warning_percent = NULL WHERE telegram_id = ?",
            (int(telegram_id),),
        )
