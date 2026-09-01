"""Customer-owned VPN quota alert preferences shared by free and paid keys."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


UTC = timezone.utc
MODES = {"percent", "mb", "gb"}
MODE_STEPS = {
    "percent": (5, 10, 25),
    "mb": (50, 100, 500),
    "gb": (1, 5, 10),
}
DEFAULTS = {
    "enabled": True,
    "mode": "percent",
    "alert_count": 3,
    "step_value": 25,
    "version": 1,
}


def get_quota_alert_preferences(database: Any, telegram_id: int) -> dict[str, Any]:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM user_quota_alert_preferences WHERE telegram_id = ?",
            (int(telegram_id),),
        ).fetchone()
    if row is None:
        return dict(DEFAULTS)
    result = dict(row)
    result["enabled"] = bool(result.get("enabled"))
    return result


def set_quota_alert_preferences(
    database: Any,
    telegram_id: int,
    *,
    enabled: bool | None = None,
    mode: str | None = None,
    alert_count: int | None = None,
    step_value: int | None = None,
) -> dict[str, Any]:
    current = get_quota_alert_preferences(database, telegram_id)
    selected_mode = str(mode or current["mode"]).lower()
    if selected_mode not in MODES:
        raise ValueError("Unsupported quota alert mode")
    selected_count = int(alert_count if alert_count is not None else current["alert_count"])
    selected_step = int(step_value if step_value is not None else current["step_value"])
    if selected_count not in (1, 2, 3):
        raise ValueError("Alert count must be 1, 2, or 3")
    if selected_step not in MODE_STEPS[selected_mode]:
        selected_step = MODE_STEPS[selected_mode][0]
    updated_at = datetime.now(UTC).isoformat()
    with database.connect() as connection:
        database.begin_write(connection)
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
                int(telegram_id),
                1 if (current["enabled"] if enabled is None else enabled) else 0,
                selected_mode,
                selected_count,
                selected_step,
                updated_at,
            ),
        )
        for table in ("keys", "paid_vpn_keys"):
            try:
                connection.execute(
                    f"UPDATE {table} SET quota_warning_percent = NULL WHERE telegram_id = ?",
                    (int(telegram_id),),
                )
            except Exception:
                pass
    return get_quota_alert_preferences(database, telegram_id)


def alert_thresholds(preferences: dict[str, Any], quota_bytes: int) -> list[tuple[int, str]]:
    """Return descending byte thresholds with friendly labels."""
    count = max(1, min(3, int(preferences.get("alert_count") or 3)))
    step = max(1, int(preferences.get("step_value") or 25))
    mode = str(preferences.get("mode") or "percent")
    result: list[tuple[int, str]] = []
    if mode == "percent":
        profiles = {25: (25, 10, 5), 10: (10, 5, 2), 5: (5, 2, 1)}
        values = profiles.get(step, (step, max(1, step // 2), max(1, step // 5)))[:count]
    else:
        values = tuple(step * multiplier for multiplier in range(count, 0, -1))
    for value in values:
        if mode == "percent":
            threshold = quota_bytes * value // 100
            label = f"{value}%"
        elif mode == "mb":
            threshold = value * 1_000_000
            label = f"{value} MB"
        else:
            threshold = value * 1_000_000_000
            label = f"{value} GB"
        if 0 < threshold < quota_bytes and threshold not in {item[0] for item in result}:
            result.append((threshold, label))
    return result


def alert_level_labels(preferences: dict[str, Any]) -> list[str]:
    """Render configured levels without requiring a particular key quota."""
    count = max(1, min(3, int(preferences.get("alert_count") or 3)))
    step = max(1, int(preferences.get("step_value") or 25))
    mode = str(preferences.get("mode") or "percent")
    if mode == "percent":
        profiles = {25: (25, 10, 5), 10: (10, 5, 2), 5: (5, 2, 1)}
        values = profiles.get(step, (step, max(1, step // 2), max(1, step // 5)))[:count]
        return [f"{value}%" for value in values]
    suffix = mode.upper()
    return [f"{step * value} {suffix}" for value in range(count, 0, -1)]


def reached_alert(
    preferences: dict[str, Any], quota_bytes: int, remaining_bytes: int
) -> tuple[int, str] | None:
    """Choose only the most urgent crossed threshold to avoid alert floods."""
    if not preferences.get("enabled", True):
        return None
    crossed = [
        item for item in alert_thresholds(preferences, quota_bytes) if remaining_bytes <= item[0]
    ]
    return min(crossed, default=None, key=lambda item: item[0])
