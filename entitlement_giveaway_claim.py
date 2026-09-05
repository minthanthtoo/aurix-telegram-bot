"""Giveaway claim transaction workflow."""

from __future__ import annotations

from datetime import datetime

from entitlement_models import GiveawayResult
from entitlement_support import (
    GIVEAWAY_CODE,
    GIVEAWAY_LIMIT_BYTES,
    GIVEAWAY_WINNER_LIMIT,
    UTC,
    outline_key_name as _outline_key_name,
)


def claim_giveaway(
    self,
    telegram_id: int,
    first_name: str,
    now: datetime | None = None,
    username: str | None = None,
    code: str | None = None,
) -> GiveawayResult:
    """Reserve and issue one configured promotional entitlement.

    The reservation is committed before the Outline call.  If the process
    dies after remote creation, the deterministic intent is recovered by
    the maintenance worker instead of creating a second key.
    """
    current = (now or datetime.now(UTC)).astimezone(UTC)
    now_text = current.isoformat()
    normalized = str(code or GIVEAWAY_CODE).strip().upper()
    intent_id: str | None = None
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        connection.execute(
            """INSERT INTO users (telegram_id, first_name, username, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                   first_name = excluded.first_name,
                   username = excluded.username""",
            (telegram_id, first_name[:128], (username or "")[:64] or None, now_text),
        )
        self._lock_user(connection, telegram_id)
        if normalized == GIVEAWAY_CODE:
            connection.execute(
                """INSERT INTO giveaway_campaigns
                   (code, quota_bytes, duration_days, winner_limit, claimed_count, active,
                    created_at, frequency, updated_at)
                   VALUES (?, ?, 30, ?, 0, 1, ?, 'campaign', ?)
                   ON CONFLICT(code) DO NOTHING""",
                (GIVEAWAY_CODE, GIVEAWAY_LIMIT_BYTES, GIVEAWAY_WINNER_LIMIT, now_text, now_text),
            )
        suffix = " FOR UPDATE" if connection.__class__.__name__ == "_PostgresConnection" else ""
        campaign = connection.execute(
            "SELECT * FROM giveaway_campaigns WHERE UPPER(code) = ?" + suffix,
            (normalized,),
        ).fetchone()
        if campaign is None:
            return GiveawayResult("unavailable", reason="Promo code is invalid or unavailable.")
        existing = connection.execute(
            """SELECT g.winner_number, k.expires_at
               FROM giveaway_claims g JOIN keys k ON k.id = g.key_id
               WHERE g.campaign_code = ? AND g.telegram_id = ?""",
            (campaign["code"], telegram_id),
        ).fetchone()
        if existing is not None:
            return GiveawayResult(
                "already_won",
                code=str(campaign["code"]),
                quota_bytes=int(campaign["quota_bytes"]),
                duration_days=int(campaign["duration_days"]),
                expires_at=datetime.fromisoformat(existing["expires_at"]),
                winner_number=int(existing["winner_number"]),
            )
        existing_intent = self._latest_free_intent(
            connection, telegram_id, "promo", str(campaign["code"])
        )
        if existing_intent is not None and existing_intent["status"] == "done":
            intent_id = str(existing_intent["id"])
        elif existing_intent is not None and existing_intent["status"] in {"pending", "running"}:
            intent_id = str(existing_intent["id"])
            if existing_intent["status"] == "pending":
                connection.execute(
                    "UPDATE free_provisioning_intents SET next_attempt_at = ? WHERE id = ?",
                    (now_text, intent_id),
                )
        elif existing_intent is not None and existing_intent["status"] == "failed":
            intent_id = str(existing_intent["id"])
            connection.execute(
                "UPDATE free_provisioning_intents SET status = 'pending', attempts = 0, next_attempt_at = ?, locked_at = NULL, last_error = NULL WHERE id = ?",
                (now_text, intent_id),
            )
        else:
            window_start = self._campaign_window_start(campaign, current)
            window = connection.execute(
                """SELECT claimed_count FROM giveaway_windows
                   WHERE campaign_code = ? AND window_start = ?""",
                (campaign["code"], window_start),
            ).fetchone()
            if window is None:
                initial_count = (
                    int(campaign["claimed_count"])
                    if str(campaign["frequency"] or "campaign") == "campaign"
                    else 0
                )
                connection.execute(
                    """INSERT INTO giveaway_windows
                       (campaign_code, window_start, claimed_count) VALUES (?, ?, ?)""",
                    (campaign["code"], window_start, initial_count),
                )
                window_claimed = initial_count
            else:
                window_claimed = int(window["claimed_count"])
            pending_window = int(
                connection.execute(
                    """SELECT COUNT(*) AS n FROM free_provisioning_intents
                       WHERE campaign_code = ? AND window_start = ?
                         AND status IN ('pending', 'running')""",
                    (campaign["code"], window_start),
                ).fetchone()["n"]
            )
            window_claimed += pending_window
            remaining = max(0, int(campaign["winner_limit"]) - window_claimed)
            total_claimed = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM giveaway_claims WHERE campaign_code = ?",
                    (campaign["code"],),
                ).fetchone()["n"]
            )
            pending_campaign = int(
                connection.execute(
                    """SELECT COUNT(*) AS n FROM free_provisioning_intents
                       WHERE campaign_code = ? AND status IN ('pending', 'running')""",
                    (campaign["code"],),
                ).fetchone()["n"]
            )
            state = self._campaign_state(campaign, current)
            if state != "active":
                return GiveawayResult(
                    state,
                    code=str(campaign["code"]),
                    quota_bytes=int(campaign["quota_bytes"]),
                    duration_days=int(campaign["duration_days"]),
                    remaining_slots=remaining,
                )
            if remaining <= 0:
                return GiveawayResult(
                    "full",
                    code=str(campaign["code"]),
                    quota_bytes=int(campaign["quota_bytes"]),
                    duration_days=int(campaign["duration_days"]),
                    remaining_slots=0,
                )
            if self._commerce_tables_exist(connection):
                conflict = connection.execute(
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
                if conflict is not None:
                    return GiveawayResult(
                        "ineligible",
                        remaining_slots=remaining,
                        reason="An open or completed paid order already belongs to this account.",
                    )
            server_id = self._select_server_for_tier(
                connection, "PROMO", int(campaign["quota_bytes"]), current,
                telegram_id=telegram_id,
            )
            key_name = _outline_key_name(
                telegram_id,
                username,
                f"PROMO-{campaign['code']}",
                f"{int(campaign['duration_days'])}day",
                current,
            )
            intent = self._insert_free_intent(
                connection,
                telegram_id=telegram_id,
                kind="promo",
                campaign_code=str(campaign["code"]),
                window_start=window_start,
                winner_number=total_claimed + pending_campaign + 1,
                server_id=server_id,
                outline_key_id=self._deterministic_slot_id(
                    "promo", str(campaign["code"]), str(telegram_id)
                ),
                key_name=key_name,
                quota_bytes=int(campaign["quota_bytes"]),
                duration_days=int(campaign["duration_days"]),
                claim_started_at=now_text,
            )
            intent_id = str(intent["id"])
    if intent_id is None:
        return GiveawayResult("unavailable", reason="Promo reservation could not be created.")
    with self.database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM free_provisioning_intents WHERE id = ?", (intent_id,)
        ).fetchone()
    if row is None:
        return GiveawayResult("unavailable", reason="Promo reservation is unavailable.")
    intent = dict(row)
    if intent["status"] == "done":
        result = self._intent_result(intent, current)
        if isinstance(result, GiveawayResult):
            return result
    if intent["status"] == "running":
        return GiveawayResult(
            "pending",
            code=str(intent["campaign_code"]),
            quota_bytes=int(intent["quota_bytes"]),
            duration_days=int(intent["duration_days"]),
            pending=True,
        )
    try:
        key = self._execute_free_intent(intent_id, current)
    except Exception:
        raise
    if key is None:
        return GiveawayResult(
            "pending",
            code=str(intent["campaign_code"]),
            quota_bytes=int(intent["quota_bytes"]),
            duration_days=int(intent["duration_days"]),
            pending=True,
        )
    with self.database.connect() as connection:
        completed = dict(
            connection.execute(
                "SELECT * FROM free_provisioning_intents WHERE id = ?", (intent_id,)
            ).fetchone()
        )
    result = self._intent_result(completed, current, key)
    return result if isinstance(result, GiveawayResult) else GiveawayResult("pending", pending=True)

