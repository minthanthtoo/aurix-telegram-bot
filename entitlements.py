"""Free and trial entitlement domain service and compatibility value types."""

from __future__ import annotations

import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ports import OutlineGateway
from quota_alerts import (
    get_quota_alert_preferences,
    reached_alert,
    set_quota_alert_preferences,
)
from repositories import RepositoryDatabase

UTC = timezone.utc

# Warn once as the observed trailing-30-day allowance crosses these remaining
# percentages. Outline itself enforces the hard limit; these messages make the
# approaching cutoff visible before the key is removed.

PUBLIC_LIMIT_BYTES = 300_000_000


LIMIT_BYTES = PUBLIC_LIMIT_BYTES


TRIAL_LIMIT_BYTES = 3_000_000_000


CLAIM_PERIOD = timedelta(hours=24)


TRIAL_PERIOD = timedelta(days=30)


GIVEAWAY_CODE = "100GBFREE"


GIVEAWAY_LIMIT_BYTES = 100_000_000_000


GIVEAWAY_PERIOD = timedelta(days=30)


GIVEAWAY_WINNER_LIMIT = 5


QUOTA_WARNING_THRESHOLDS = ((25, 0.25), (10, 0.10), (5, 0.05))


def _outline_key_name(
    telegram_id: int,
    username: str | None,
    tier: str,
    duration: str,
    started_at: datetime,
) -> str:
    """Build a human-readable, non-secret Outline key name."""
    identity = (username or "").strip().lstrip("@") or str(telegram_id)
    identity = re.sub(r"[^A-Za-z0-9_-]+", "-", identity).strip("-_")
    identity = identity[:48] or str(telegram_id)
    timestamp = started_at.astimezone(UTC).strftime("%Y%m%d%H%M")
    return f"{identity}-{tier}-{duration}-{timestamp}"[:128]


def _human_bytes(value: int) -> str:
    amount = float(max(0, int(value)))
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if amount < 1000 or unit == "TB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1000


def _human_decimal_bytes(value: int) -> str:
    amount = float(max(0, int(value)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1000 or unit == "TB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1000


def _new_id() -> str:
    return uuid.uuid4().hex


class OutlineError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ClaimResult:
    access_url: str | None = None
    expires_at: datetime | None = None
    next_claim_at: datetime | None = None
    denied_reason: str | None = None


@dataclass(frozen=True)
class GiveawayResult:
    outcome: str
    code: str | None = None
    quota_bytes: int | None = None
    duration_days: int | None = None
    access_url: str | None = None
    expires_at: datetime | None = None
    winner_number: int | None = None
    remaining_slots: int = 0
    reason: str | None = None


class ClaimService:
    def __init__(
        self,
        database: RepositoryDatabase,
        outline: OutlineGateway,
        limit_bytes: int = LIMIT_BYTES,
        trial_limit_bytes: int = TRIAL_LIMIT_BYTES,
    ):
        self.database = database
        self.outline = outline
        self.limit_bytes = int(limit_bytes)
        self.trial_limit_bytes = int(trial_limit_bytes)

    @staticmethod
    def _lock_user(connection: Any, telegram_id: int) -> None:
        if connection.__class__.__name__ == "_PostgresConnection":
            connection.execute(
                "SELECT telegram_id FROM users WHERE telegram_id = ? FOR UPDATE",
                (telegram_id,),
            ).fetchone()

    @staticmethod
    def _has_active_promo_gift(connection: Any, telegram_id: int, now: datetime) -> bool:
        """Return whether a live campaign and usable gift currently pause other plans."""
        now_text = now.astimezone(UTC).isoformat()
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
    def _campaign_window_start(campaign: Any, now: datetime) -> str:
        frequency = str(campaign["frequency"] or "campaign").lower()
        if frequency == "hourly":
            return now.replace(minute=0, second=0, microsecond=0).isoformat()
        if frequency == "daily":
            return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        return str(campaign["starts_at"] or campaign["created_at"])

    @staticmethod
    def _campaign_state(campaign: Any, now: datetime) -> str:
        if not bool(campaign["active"]):
            return "paused"
        starts_at = campaign["starts_at"]
        ends_at = campaign["ends_at"]
        if starts_at and now < datetime.fromisoformat(str(starts_at)).astimezone(UTC):
            return "scheduled"
        if ends_at and now >= datetime.fromisoformat(str(ends_at)).astimezone(UTC):
            return "ended"
        return "active"

    @staticmethod
    def _commerce_tables_exist(connection: Any) -> bool:
        if connection.__class__.__name__ == "_PostgresConnection":
            row = connection.execute("SELECT to_regclass('public.orders') AS table_name").fetchone()
            return bool(row and row["table_name"])
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'orders'"
        ).fetchone()
        return row is not None

    def giveaway_status(
        self,
        telegram_id: int,
        code: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return campaign schedule/capacity and this user's durable gift state."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        normalized = str(code or "").strip().upper()
        with self.database.connect() as connection:
            if normalized:
                campaign = connection.execute(
                    "SELECT * FROM giveaway_campaigns WHERE UPPER(code) = ?", (normalized,)
                ).fetchone()
            else:
                campaign = connection.execute(
                    """SELECT * FROM giveaway_campaigns
                       ORDER BY active DESC, COALESCE(updated_at, created_at) DESC
                       LIMIT 1"""
                ).fetchone()
            if campaign is None:
                return {
                    "exists": False,
                    "code": normalized or GIVEAWAY_CODE,
                    "active": False,
                    "campaign_state": "unavailable",
                    "winner": False,
                    "gift_active": False,
                    "access_lock_active": False,
                    "claimed_count": 0,
                    "window_claimed_count": 0,
                    "winner_limit": 0,
                    "remaining_slots": 0,
                }
            claim = connection.execute(
                """SELECT g.winner_number, g.claimed_at, k.expires_at, k.status,
                          k.quota_reason, k.data_limit_bytes
                   FROM giveaway_claims g JOIN keys k ON k.id = g.key_id
                   WHERE g.campaign_code = ? AND g.telegram_id = ?""",
                (campaign["code"], telegram_id),
            ).fetchone()
            total_claimed = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM giveaway_claims WHERE campaign_code = ?",
                    (campaign["code"],),
                ).fetchone()["n"]
            )
            window_start = self._campaign_window_start(campaign, current)
            window = connection.execute(
                """SELECT claimed_count FROM giveaway_windows
                   WHERE campaign_code = ? AND window_start = ?""",
                (campaign["code"], window_start),
            ).fetchone()
        frequency = str(campaign["frequency"] or "campaign")
        window_claimed = (
            int(window["claimed_count"])
            if window is not None
            else (total_claimed if frequency == "campaign" else 0)
        )
        winner_limit = int(campaign["winner_limit"])
        state = self._campaign_state(campaign, current)
        gift_active = bool(
            claim is not None
            and claim["status"] in ("active", "revoke_failed")
            and not claim["quota_reason"]
            and datetime.fromisoformat(str(claim["expires_at"])).astimezone(UTC) > current
        )
        result: dict[str, Any] = {
            "exists": True,
            "code": str(campaign["code"]),
            "quota_bytes": int(campaign["quota_bytes"]),
            "duration_days": int(campaign["duration_days"]),
            "frequency": frequency,
            "starts_at": campaign["starts_at"],
            "ends_at": campaign["ends_at"],
            "campaign_state": state,
            "claimed_count": total_claimed,
            "window_claimed_count": window_claimed,
            "winner_limit": winner_limit,
            "remaining_slots": max(0, winner_limit - window_claimed),
            "active": state == "active",
            "winner": claim is not None,
            "gift_active": gift_active,
            "access_lock_active": state == "active" and gift_active,
        }
        if claim is not None:
            result.update(dict(claim))
        return result

    def configure_giveaway(
        self,
        *,
        code: str,
        quota_bytes: int,
        duration_days: int,
        winner_limit: int,
        frequency: str,
        starts_at: datetime,
        ends_at: datetime,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Create or update the single owner-selected promo season."""
        normalized = str(code).strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{2,31}", normalized):
            raise ValueError("Promo code must be 3-32 letters, numbers, underscores, or hyphens")
        quota_bytes = int(quota_bytes)
        duration_days = int(duration_days)
        winner_limit = int(winner_limit)
        frequency = str(frequency).strip().lower()
        if not 1_000_000 <= quota_bytes <= 10_000_000_000_000:
            raise ValueError("Promo quota must be between 0.001 GB and 10,000 GB")
        if not 1 <= duration_days <= 365:
            raise ValueError("Promo duration must be between 1 and 365 days")
        if not 1 <= winner_limit <= 100_000:
            raise ValueError("Giveaway count must be between 1 and 100,000")
        if frequency not in {"campaign", "daily", "hourly"}:
            raise ValueError("Frequency must be campaign, daily, or hourly")
        starts_at = starts_at.astimezone(UTC)
        ends_at = ends_at.astimezone(UTC)
        if starts_at >= ends_at:
            raise ValueError("Promo end must be after its start")
        now_text = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            existing = connection.execute(
                "SELECT * FROM giveaway_campaigns WHERE code = ?", (normalized,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO giveaway_campaigns
                       (code, quota_bytes, duration_days, winner_limit, claimed_count,
                        active, created_at, starts_at, ends_at, frequency, updated_at)
                       VALUES (?, ?, ?, ?, 0, 1, ?, ?, ?, ?, ?)""",
                    (
                        normalized,
                        quota_bytes,
                        duration_days,
                        winner_limit,
                        now_text,
                        starts_at.isoformat(),
                        ends_at.isoformat(),
                        frequency,
                        now_text,
                    ),
                )
            else:
                claim_count = int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM giveaway_claims WHERE campaign_code = ?",
                        (normalized,),
                    ).fetchone()["n"]
                )
                max_window = int(
                    connection.execute(
                        """SELECT COALESCE(MAX(claimed_count), 0) AS n
                           FROM giveaway_windows WHERE campaign_code = ?""",
                        (normalized,),
                    ).fetchone()["n"]
                )
                if claim_count:
                    immutable_changed = any(
                        (
                            int(existing["quota_bytes"]) != quota_bytes,
                            int(existing["duration_days"]) != duration_days,
                            str(existing["frequency"] or "campaign") != frequency,
                            str(existing["starts_at"] or "") != starts_at.isoformat(),
                        )
                    )
                    if immutable_changed:
                        raise ValueError(
                            "A claimed promo's quota, duration, frequency, and start are immutable; "
                            "create a new promo code for a new season"
                        )
                if winner_limit < max_window:
                    raise ValueError(
                        f"Giveaway count cannot be below {max_window} claims already made in a window"
                    )
                connection.execute(
                    """UPDATE giveaway_campaigns
                       SET quota_bytes = ?, duration_days = ?, winner_limit = ?, active = 1,
                           starts_at = ?, ends_at = ?, frequency = ?, updated_at = ?
                       WHERE code = ?""",
                    (
                        quota_bytes,
                        duration_days,
                        winner_limit,
                        starts_at.isoformat(),
                        ends_at.isoformat(),
                        frequency,
                        now_text,
                        normalized,
                    ),
                )
            connection.execute(
                "UPDATE giveaway_campaigns SET active = 0, updated_at = ? WHERE code != ? AND active = 1",
                (now_text, normalized),
            )
        return self.giveaway_status(0, normalized, now=now)

    def set_giveaway_active(
        self, code: str, active: bool, now: datetime | None = None
    ) -> dict[str, Any]:
        normalized = str(code).strip().upper()
        now_text = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                "SELECT code FROM giveaway_campaigns WHERE UPPER(code) = ?", (normalized,)
            ).fetchone()
            if row is None:
                raise ValueError("Promo campaign not found")
            if active:
                connection.execute(
                    "UPDATE giveaway_campaigns SET active = 0, updated_at = ? WHERE code != ?",
                    (now_text, row["code"]),
                )
            connection.execute(
                "UPDATE giveaway_campaigns SET active = ?, updated_at = ? WHERE code = ?",
                (1 if active else 0, now_text, row["code"]),
            )
        return self.giveaway_status(0, str(row["code"]), now=now)

    def reconcile_giveaway_limits(self) -> int:
        """Converge already-issued remote promo keys to their stored exact quota."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT k.outline_key_id, c.quota_bytes
                   FROM giveaway_claims g
                   JOIN giveaway_campaigns c ON c.code = g.campaign_code
                   JOIN keys k ON k.id = g.key_id
                   WHERE k.status IN ('active', 'revoke_failed')
                     AND k.quota_reason IS NULL"""
            ).fetchall()
        if not rows:
            return 0
        remote = self.outline.list_keys()
        items = remote.get("accessKeys", []) if isinstance(remote, dict) else []
        if not isinstance(items, list):
            raise OutlineError("Outline returned invalid access key data")
        existing_ids = {
            str(item.get("id"))
            for item in items
            if isinstance(item, dict) and item.get("id") is not None
        }
        updated = 0
        for row in rows:
            key_id = str(row["outline_key_id"])
            if key_id not in existing_ids:
                continue
            self.outline.set_data_limit(key_id, int(row["quota_bytes"]))
            updated += 1
        return updated

    def claim_giveaway(
        self,
        telegram_id: int,
        first_name: str,
        now: datetime | None = None,
        username: str | None = None,
        code: str | None = None,
    ) -> GiveawayResult:
        """Atomically issue one configured promotional entitlement."""
        now = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = now.isoformat()
        normalized = str(code or GIVEAWAY_CODE).strip().upper()
        key: dict[str, Any] | None = None
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
                    (
                        GIVEAWAY_CODE,
                        GIVEAWAY_LIMIT_BYTES,
                        GIVEAWAY_WINNER_LIMIT,
                        now_text,
                        now_text,
                    ),
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
            window_start = self._campaign_window_start(campaign, now)
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
            remaining = max(0, int(campaign["winner_limit"]) - window_claimed)
            if existing is not None:
                return GiveawayResult(
                    "already_won",
                    code=str(campaign["code"]),
                    quota_bytes=int(campaign["quota_bytes"]),
                    duration_days=int(campaign["duration_days"]),
                    expires_at=datetime.fromisoformat(existing["expires_at"]),
                    winner_number=int(existing["winner_number"]),
                    remaining_slots=remaining,
                )
            state = self._campaign_state(campaign, now)
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
            key = self.outline.create_key(
                _outline_key_name(
                    telegram_id,
                    username,
                    f"PROMO-{campaign['code']}",
                    f"{int(campaign['duration_days'])}day",
                    now,
                ),
                int(campaign["quota_bytes"]),
            )
            expires_at = now + timedelta(days=int(campaign["duration_days"]))
            total_claimed = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM giveaway_claims WHERE campaign_code = ?",
                    (campaign["code"],),
                ).fetchone()["n"]
            )
            winner_number = total_claimed + 1
            try:
                connection.execute(
                    """INSERT INTO keys
                       (telegram_id, outline_key_id, key_type, created_at, expires_at,
                        data_limit_bytes, status)
                       VALUES (?, ?, 'monthly_trial', ?, ?, ?, 'active')""",
                    (
                        telegram_id,
                        str(key["id"]),
                        now_text,
                        expires_at.isoformat(),
                        int(campaign["quota_bytes"]),
                    ),
                )
                key_row = connection.execute(
                    "SELECT id FROM keys WHERE outline_key_id = ?", (str(key["id"]),)
                ).fetchone()
                connection.execute(
                    """INSERT INTO giveaway_claims
                       (campaign_code, telegram_id, key_id, winner_number, claimed_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (campaign["code"], telegram_id, key_row["id"], winner_number, now_text),
                )
                connection.execute(
                    """UPDATE giveaway_windows SET claimed_count = claimed_count + 1
                       WHERE campaign_code = ? AND window_start = ?
                         AND claimed_count < ?""",
                    (campaign["code"], window_start, int(campaign["winner_limit"])),
                )
                connection.execute(
                    """UPDATE giveaway_campaigns
                       SET claimed_count = CASE
                               WHEN claimed_count < winner_limit THEN claimed_count + 1
                               ELSE claimed_count
                           END,
                           updated_at = ?
                       WHERE code = ?""",
                    (now_text, campaign["code"]),
                )
            except Exception:
                try:
                    self.outline.delete_key(str(key["id"]))
                finally:
                    raise
        return GiveawayResult(
            "won",
            code=str(campaign["code"]),
            quota_bytes=int(campaign["quota_bytes"]),
            duration_days=int(campaign["duration_days"]),
            access_url=str(key["accessUrl"]),
            expires_at=expires_at,
            winner_number=winner_number,
            remaining_slots=max(0, remaining - 1),
        )

    def track_user(
        self,
        telegram_id: int,
        first_name: str,
        now: datetime | None = None,
        username: str | None = None,
    ) -> None:
        now_text = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO users (telegram_id, first_name, username, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                       first_name = excluded.first_name,
                       username = excluded.username""",
                (telegram_id, first_name[:128], (username or "")[:64] or None, now_text),
            )

    def claim(
        self,
        telegram_id: int,
        first_name: str,
        now: datetime | None = None,
        username: str | None = None,
    ) -> ClaimResult:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = now.isoformat()
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
            user = connection.execute(
                "SELECT last_claim_at FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            if self._has_active_promo_gift(connection, telegram_id, now):
                return ClaimResult(denied_reason="active_promo")
            if user["last_claim_at"]:
                next_claim = datetime.fromisoformat(user["last_claim_at"]) + CLAIM_PERIOD
                if now < next_claim:
                    return ClaimResult(next_claim_at=next_claim)

            key = self.outline.create_key(
                _outline_key_name(telegram_id, username, "FREE300MB", "24hr", now),
                self.limit_bytes,
            )
            expires_at = now + CLAIM_PERIOD
            try:
                connection.execute(
                    """INSERT INTO keys
                       (telegram_id, outline_key_id, key_type, created_at, expires_at, data_limit_bytes, status)
                       VALUES (?, ?, 'daily_free', ?, ?, ?, 'active')""",
                    (
                        telegram_id,
                        str(key["id"]),
                        now_text,
                        expires_at.isoformat(),
                        self.limit_bytes,
                    ),
                )
                connection.execute(
                    """UPDATE users
                       SET last_claim_at = ?
                       WHERE telegram_id = ?""",
                    (now_text, telegram_id),
                )
            except Exception:
                try:
                    self.outline.delete_key(str(key["id"]))
                finally:
                    raise
        return ClaimResult(access_url=str(key["accessUrl"]), expires_at=expires_at)

    def claim_trial(
        self,
        telegram_id: int,
        first_name: str,
        now: datetime | None = None,
        username: str | None = None,
    ) -> ClaimResult:
        """Issue one 3 GB entitlement per rolling 30 days."""
        now = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = now.isoformat()
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
            user = connection.execute(
                "SELECT trial_claimed_at FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            if self._has_active_promo_gift(connection, telegram_id, now):
                return ClaimResult(denied_reason="active_promo")
            if user["trial_claimed_at"]:
                next_claim = datetime.fromisoformat(user["trial_claimed_at"]) + TRIAL_PERIOD
                if now < next_claim:
                    return ClaimResult(next_claim_at=next_claim)
            key = self.outline.create_key(
                _outline_key_name(telegram_id, username, "FREE3GB", "30day", now),
                self.trial_limit_bytes,
            )
            expires_at = now + TRIAL_PERIOD
            try:
                connection.execute(
                    """INSERT INTO keys
                       (telegram_id, outline_key_id, key_type, created_at, expires_at, data_limit_bytes, status)
                       VALUES (?, ?, 'monthly_trial', ?, ?, ?, 'active')""",
                    (
                        telegram_id,
                        str(key["id"]),
                        now_text,
                        expires_at.isoformat(),
                        self.trial_limit_bytes,
                    ),
                )
                connection.execute(
                    "UPDATE users SET trial_claimed_at = ? WHERE telegram_id = ?",
                    (now_text, telegram_id),
                )
            except Exception:
                try:
                    self.outline.delete_key(str(key["id"]))
                finally:
                    raise
        return ClaimResult(access_url=str(key["accessUrl"]), expires_at=expires_at)

    def _terminate_key(
        self,
        row: Any,
        reason: str,
        now: datetime,
        used_bytes: int | None = None,
    ) -> bool:
        """Record, delete, and (when supported) verify one remote credential."""
        now_text = now.astimezone(UTC).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE keys SET status = 'active', last_usage_bytes = COALESCE(?, last_usage_bytes),
                          quota_reason = CASE WHEN ? = 'quota' THEN 'quota' ELSE quota_reason END
                   WHERE id = ? AND status != 'revoked'""",
                (used_bytes, reason, row["id"]),
            )
            connection.execute(
                """INSERT INTO key_termination_events
                   (key_id, telegram_id, outline_key_id, reason, used_bytes, quota_bytes,
                    expires_at, detected_at, remote_state)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'retrying')
                   ON CONFLICT(key_id, reason) DO UPDATE SET
                       used_bytes = COALESCE(excluded.used_bytes, key_termination_events.used_bytes)""",
                (
                    row["id"],
                    row["telegram_id"],
                    str(row["outline_key_id"]),
                    reason,
                    used_bytes,
                    int(row["data_limit_bytes"]),
                    row["expires_at"],
                    now_text,
                ),
            )
        try:
            self.outline.delete_key(str(row["outline_key_id"]))
            getter = getattr(self.outline, "get_key", None)
            verified = callable(getter)
            if verified and getter(str(row["outline_key_id"])) is not None:
                raise OutlineError("Outline key still exists after delete")
        except Exception as exc:
            with self.database.connect() as connection:
                connection.execute(
                    """UPDATE key_termination_events
                       SET remote_state = CASE
                               WHEN delete_attempts + 1 >= 10 THEN 'escalated'
                               ELSE 'retrying'
                           END,
                           delete_attempts = delete_attempts + 1,
                           last_error = ? WHERE key_id = ? AND reason = ?""",
                    (type(exc).__name__, row["id"], reason),
                )
            return False
        remote_state = "deleted_verified" if verified else "delete_accepted"
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute("UPDATE keys SET status = 'revoked' WHERE id = ?", (row["id"],))
            connection.execute(
                """UPDATE key_termination_events
                   SET remote_state = ?, delete_attempts = delete_attempts + 1,
                       last_error = NULL, deletion_verified_at = ?
                   WHERE key_id = ? AND reason = ?""",
                (remote_state, now_text if verified else None, row["id"], reason),
            )
        return True

    def enforce_quota(
        self,
        now: datetime | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> int:
        """Fail closed and revoke free/trial keys whose Outline metric hit its cap."""
        if metrics is None:
            try:
                metrics = self.outline.transfer_metrics()
            except Exception:
                return 0
        by_key = metrics.get("bytesTransferredByUserId", {}) if isinstance(metrics, dict) else {}
        if not isinstance(by_key, dict):
            return 0
        current = (now or datetime.now(UTC)).astimezone(UTC)
        try:
            self.queue_quota_warnings(current, metrics)
        except Exception as exc:
            # A notification outage must never delay the hard quota revoke.
            print(f"quota warning error: {type(exc).__name__}", file=sys.stderr)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT id, telegram_id, outline_key_id, data_limit_bytes, expires_at FROM keys
                   WHERE status = 'active' OR (status = 'revoke_failed' AND quota_reason = 'quota')"""
            ).fetchall()
        revoked = 0
        for row in rows:
            try:
                used = int(by_key.get(str(row["outline_key_id"]), 0) or 0)
            except (TypeError, ValueError):
                continue
            if used < int(row["data_limit_bytes"]):
                continue
            if self._terminate_key(row, "quota", current, used):
                revoked += 1
        return revoked

    def queue_quota_warnings(
        self,
        now: datetime | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> int:
        """Queue one Telegram warning as each remaining-quota threshold is crossed.

        The warning level is persisted per key, so repeated maintenance passes
        and temporary metric fluctuations cannot spam a customer. The final
        hard stop remains ``enforce_quota`` and never depends on delivery.
        """
        if metrics is None:
            try:
                metrics = self.outline.transfer_metrics()
            except Exception:
                return 0
        by_key = metrics.get("bytesTransferredByUserId", {}) if isinstance(metrics, dict) else {}
        if not isinstance(by_key, dict):
            return 0
        current = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = current.isoformat()
        queued = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            rows = connection.execute(
                """SELECT keys.id, keys.telegram_id, keys.outline_key_id,
                          keys.data_limit_bytes, keys.expires_at,
                          keys.quota_warning_percent, g.campaign_code
                   FROM keys
                   LEFT JOIN giveaway_claims g ON g.key_id = keys.id
                   WHERE keys.status = 'active'"""
            ).fetchall()
            for row in rows:
                try:
                    used = max(0, int(by_key.get(str(row["outline_key_id"]), 0) or 0))
                    quota = int(row["data_limit_bytes"])
                except (TypeError, ValueError):
                    continue
                if quota <= 0 or used >= quota:
                    continue
                remaining = quota - used
                preferences = get_quota_alert_preferences(self.database, int(row["telegram_id"]))
                reached = reached_alert(preferences, quota, remaining)
                if reached is None:
                    continue
                threshold_bytes, threshold_label = reached
                remaining_percent = remaining * 100 / quota
                dedupe_key = (
                    f"quota-warning:free:{row['id']}:v{preferences.get('version', 1)}:"
                    f"{threshold_bytes}"
                )
                try:
                    existing = connection.execute(
                        "SELECT id FROM notifications WHERE dedupe_key = ?",
                        (dedupe_key,),
                    ).fetchone()
                    if existing is None:
                        if row["campaign_code"]:
                            tier = f"promo {row['campaign_code']}"
                        elif quota == TRIAL_LIMIT_BYTES:
                            tier = "monthly 3 GB"
                        elif quota == PUBLIC_LIMIT_BYTES:
                            tier = "daily 300 MB"
                        else:
                            tier = "free"
                        formatter = _human_decimal_bytes if row["campaign_code"] else _human_bytes
                        text = (
                            f"📶 VPN usage alert: your AuriX {tier} key has "
                            f"{formatter(remaining)} remaining "
                            f"({remaining_percent:.1f}% of {formatter(quota)}).\n"
                            f"Your configured alert level: {threshold_label} remaining.\n"
                            "This is based on Outline's trailing-30-day usage. "
                            "When no quota remains, the key will be blocked and deleted. "
                            f"Expires: {row['expires_at']}"
                        )
                        connection.execute(
                            """INSERT INTO notifications
                               (id, dedupe_key, telegram_id, kind, text, status,
                                next_attempt_at, created_at)
                               VALUES (?, ?, ?, 'quota_warning', ?, 'pending', ?, ?)""",
                            (_new_id(), dedupe_key, row["telegram_id"], text, now_text, now_text),
                        )
                        queued += 1
                    connection.execute(
                        "UPDATE keys SET quota_warning_percent = ? WHERE id = ?",
                        (int(remaining_percent), row["id"]),
                    )
                except Exception as exc:
                    if self.database.is_integrity_error(exc):
                        continue
                    raise
        return queued

    def user_usage(
        self,
        telegram_id: int,
        usage_by_key: dict[str, Any],
        access_by_key: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return this user's current free/trial key state for the customer dashboard."""
        access_by_key = access_by_key or {}
        now = datetime.now(UTC)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT keys.outline_key_id, keys.key_type, keys.created_at,
                          keys.expires_at, keys.data_limit_bytes, keys.status,
                          keys.last_usage_bytes, keys.quota_reason,
                          g.campaign_code,
                          (SELECT remote_state FROM key_termination_events e
                           WHERE e.key_id = keys.id ORDER BY e.detected_at DESC LIMIT 1) AS termination_state
                   FROM keys
                   LEFT JOIN giveaway_claims g ON g.key_id = keys.id
                   WHERE keys.telegram_id = ?
                     AND (keys.status IN ('active', 'revoke_failed') OR keys.quota_reason = 'quota')
                   ORDER BY keys.created_at DESC LIMIT 10""",
                (telegram_id,),
            ).fetchall()
        tiers = {
            300_000_000: "Daily Free 300 MB",
            3_000_000_000: "Monthly Free 3 GB",
        }
        result = []
        for row in rows:
            key_id = str(row["outline_key_id"])
            observed = key_id in usage_by_key
            raw_used = usage_by_key.get(key_id, row["last_usage_bytes"] or 0)
            try:
                used = max(0, int(raw_used or 0))
            except (TypeError, ValueError):
                used = max(0, int(row["last_usage_bytes"] or 0))
                observed = False
            quota = int(row["data_limit_bytes"])
            effective_status = (
                "quota exhausted"
                if row["quota_reason"] == "quota"
                else (
                    "revocation failed"
                    if row["termination_state"] == "escalated"
                    else (
                        "revocation pending"
                        if row["termination_state"] in ("retrying", "delete_accepted")
                        or row["status"] == "revoke_failed"
                        else (
                            "expired"
                            if datetime.fromisoformat(row["expires_at"]).astimezone(UTC) <= now
                            else row["status"]
                        )
                    )
                )
            )
            result.append(
                {
                    "outline_key_id": key_id,
                    "key_type": row["key_type"],
                    "tier": (
                        f"{quota / 1_000_000_000:g} GB Promo · {row['campaign_code']}"
                        if row["campaign_code"]
                        else tiers.get(quota, "Free access")
                    ),
                    "decimal_quota": bool(row["campaign_code"]),
                    "used_bytes": used,
                    "quota_bytes": quota,
                    "remaining_bytes": max(0, quota - used),
                    "usage_observed": observed,
                    "expires_at": row["expires_at"],
                    "status": effective_status,
                    "access_url": access_by_key.get(key_id)
                    if effective_status == "active"
                    else None,
                    "created_at": row["created_at"],
                }
            )
        return result

    def revoke_expired(self, now: datetime | None = None) -> int:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = current.isoformat()
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT id, telegram_id, outline_key_id, data_limit_bytes, expires_at FROM keys
                   WHERE status IN ('active', 'revoke_failed') AND expires_at <= ?""",
                (now_text,),
            ).fetchall()
        revoked = 0
        for row in rows:
            if self._terminate_key(row, "expiry", current):
                revoked += 1
        return revoked

    def reconcile_terminations(self, now: datetime | None = None, limit: int = 20) -> int:
        """Retry recorded remote deletions, including paid-upgrade cleanup."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT k.id, k.telegram_id, k.outline_key_id, k.data_limit_bytes,
                          k.expires_at, e.reason, e.used_bytes
                   FROM keys k JOIN key_termination_events e ON e.key_id = k.id
                   WHERE e.remote_state IN ('retrying', 'escalated') AND k.status != 'revoked'
                   ORDER BY e.detected_at LIMIT ?""",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        completed = 0
        for row in rows:
            if self._terminate_key(row, str(row["reason"]), current, row["used_bytes"]):
                completed += 1
        return completed

    def pending_termination_notices(self, audience: str) -> list[dict[str, Any]]:
        column = "admin_notice_state" if audience == "admin" else "user_notice_state"
        with self.database.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    f"""SELECT * FROM key_termination_events
                        WHERE COALESCE({column}, '') != remote_state
                        ORDER BY detected_at LIMIT 50"""
                ).fetchall()
            ]

    def mark_termination_notice(self, event_id: int, audience: str, state: str) -> None:
        column = "admin_notice_state" if audience == "admin" else "user_notice_state"
        with self.database.connect() as connection:
            connection.execute(
                f"UPDATE key_termination_events SET {column} = ? WHERE id = ?",
                (state, event_id),
            )

    def termination_summary(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM key_termination_events
                   ORDER BY detected_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            ]

    def quota_alert_preferences(self, telegram_id: int) -> dict[str, Any]:
        return get_quota_alert_preferences(self.database, telegram_id)

    def set_quota_alert_preferences(self, telegram_id: int, **changes: Any) -> dict[str, Any]:
        return set_quota_alert_preferences(self.database, telegram_id, **changes)
