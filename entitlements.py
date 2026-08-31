"""Free and trial entitlement domain service and compatibility value types."""

from __future__ import annotations

import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ports import OutlineGateway
from repositories import RepositoryDatabase

UTC = timezone.utc

# Warn once as the observed trailing-30-day allowance crosses these remaining
# percentages. Outline itself enforces the hard limit; these messages make the
# approaching cutoff visible before the key is removed.

PUBLIC_LIMIT_BYTES = 300 * 1024 * 1024


LIMIT_BYTES = PUBLIC_LIMIT_BYTES


TRIAL_LIMIT_BYTES = 3 * 1024**3


CLAIM_PERIOD = timedelta(hours=24)


TRIAL_PERIOD = timedelta(days=30)


GIVEAWAY_CODE = "100GBFREE"


GIVEAWAY_LIMIT_BYTES = 100 * 1024**3


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
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1024


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
    def _is_giveaway_winner(connection: Any, telegram_id: int) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM giveaway_claims WHERE telegram_id = ? LIMIT 1",
                (telegram_id,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _commerce_tables_exist(connection: Any) -> bool:
        if connection.__class__.__name__ == "_PostgresConnection":
            row = connection.execute(
                "SELECT to_regclass('public.orders') AS table_name"
            ).fetchone()
            return bool(row and row["table_name"])
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'orders'"
        ).fetchone()
        return row is not None

    def giveaway_status(self, telegram_id: int) -> dict[str, Any]:
        """Return public campaign capacity and this user's durable winner state."""
        with self.database.connect() as connection:
            campaign = connection.execute(
                "SELECT * FROM giveaway_campaigns WHERE code = ?", (GIVEAWAY_CODE,)
            ).fetchone()
            claim = connection.execute(
                """SELECT g.winner_number, g.claimed_at, k.expires_at, k.status,
                          k.quota_reason
                   FROM giveaway_claims g JOIN keys k ON k.id = g.key_id
                   WHERE g.campaign_code = ? AND g.telegram_id = ?""",
                (GIVEAWAY_CODE, telegram_id),
            ).fetchone()
        claimed = int(campaign["claimed_count"]) if campaign else 0
        winner_limit = int(campaign["winner_limit"]) if campaign else GIVEAWAY_WINNER_LIMIT
        result: dict[str, Any] = {
            "code": GIVEAWAY_CODE,
            "claimed_count": claimed,
            "winner_limit": winner_limit,
            "remaining_slots": max(0, winner_limit - claimed),
            "active": bool(campaign["active"]) if campaign else True,
            "winner": claim is not None,
        }
        if claim is not None:
            result.update(dict(claim))
        return result

    def claim_giveaway(
        self,
        telegram_id: int,
        first_name: str,
        now: datetime | None = None,
        username: str | None = None,
    ) -> GiveawayResult:
        """Atomically issue one of five 100 GiB promotional entitlements."""
        now = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = now.isoformat()
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
            connection.execute(
                """INSERT INTO giveaway_campaigns
                   (code, quota_bytes, duration_days, winner_limit, claimed_count, active, created_at)
                   VALUES (?, ?, 30, ?, 0, 1, ?)
                   ON CONFLICT(code) DO NOTHING""",
                (GIVEAWAY_CODE, GIVEAWAY_LIMIT_BYTES, GIVEAWAY_WINNER_LIMIT, now_text),
            )
            suffix = " FOR UPDATE" if connection.__class__.__name__ == "_PostgresConnection" else ""
            campaign = connection.execute(
                "SELECT * FROM giveaway_campaigns WHERE code = ?" + suffix,
                (GIVEAWAY_CODE,),
            ).fetchone()
            existing = connection.execute(
                """SELECT g.winner_number, k.expires_at
                   FROM giveaway_claims g JOIN keys k ON k.id = g.key_id
                   WHERE g.campaign_code = ? AND g.telegram_id = ?""",
                (GIVEAWAY_CODE, telegram_id),
            ).fetchone()
            remaining = max(0, int(campaign["winner_limit"]) - int(campaign["claimed_count"]))
            if existing is not None:
                return GiveawayResult(
                    "already_won",
                    expires_at=datetime.fromisoformat(existing["expires_at"]),
                    winner_number=int(existing["winner_number"]),
                    remaining_slots=remaining,
                )
            if not bool(campaign["active"]) or remaining <= 0:
                return GiveawayResult("full", remaining_slots=0)
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
                _outline_key_name(telegram_id, username, "GIVEAWAY100GB", "30day", now),
                GIVEAWAY_LIMIT_BYTES,
            )
            expires_at = now + GIVEAWAY_PERIOD
            winner_number = int(campaign["claimed_count"]) + 1
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
                        GIVEAWAY_LIMIT_BYTES,
                    ),
                )
                key_row = connection.execute(
                    "SELECT id FROM keys WHERE outline_key_id = ?", (str(key["id"]),)
                ).fetchone()
                connection.execute(
                    """INSERT INTO giveaway_claims
                       (campaign_code, telegram_id, key_id, winner_number, claimed_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (GIVEAWAY_CODE, telegram_id, key_row["id"], winner_number, now_text),
                )
                connection.execute(
                    """UPDATE giveaway_campaigns SET claimed_count = claimed_count + 1
                       WHERE code = ? AND claimed_count < winner_limit""",
                    (GIVEAWAY_CODE,),
                )
            except Exception:
                try:
                    self.outline.delete_key(str(key["id"]))
                finally:
                    raise
        return GiveawayResult(
            "won",
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
            if self._is_giveaway_winner(connection, telegram_id):
                return ClaimResult(denied_reason="giveaway_winner")
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
        """Issue one 3 GiB entitlement per rolling 30 days."""
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
            if self._is_giveaway_winner(connection, telegram_id):
                return ClaimResult(denied_reason="giveaway_winner")
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
                """SELECT id, telegram_id, outline_key_id, data_limit_bytes,
                          expires_at, quota_warning_percent
                   FROM keys WHERE status = 'active'"""
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
                reached = next(
                    (
                        percent
                        for percent, fraction in reversed(QUOTA_WARNING_THRESHOLDS)
                        if remaining <= quota * fraction
                    ),
                    None,
                )
                if reached is None:
                    continue
                previous = row["quota_warning_percent"]
                if previous is not None and int(previous) <= reached:
                    continue
                dedupe_key = f"quota-warning:free:{row['id']}:{reached}"
                try:
                    existing = connection.execute(
                        "SELECT id FROM notifications WHERE dedupe_key = ?",
                        (dedupe_key,),
                    ).fetchone()
                    if existing is None:
                        if quota == GIVEAWAY_LIMIT_BYTES:
                            tier = "100 GiB giveaway"
                        elif quota == TRIAL_LIMIT_BYTES:
                            tier = "monthly 3 GiB"
                        elif quota == PUBLIC_LIMIT_BYTES:
                            tier = "daily 300 MiB"
                        else:
                            tier = "free"
                        remaining_percent = remaining * 100 / quota
                        text = (
                            f"Quota warning: your AuriX {tier} key has "
                            f"{_human_bytes(remaining)} remaining "
                            f"({remaining_percent:.1f}% of {_human_bytes(quota)}).\n"
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
                        (reached, row["id"]),
                    )
                except Exception as exc:
                    if self.database.is_integrity_error(exc):
                        continue
                    raise
        return queued

    def user_usage(self, telegram_id: int, usage_by_key: dict[str, Any]) -> list[dict[str, Any]]:
        """Return this user's free/trial key usage without exposing key secrets."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT outline_key_id, created_at, expires_at, data_limit_bytes,
                          status, last_usage_bytes, quota_reason,
                          (SELECT remote_state FROM key_termination_events e
                           WHERE e.key_id = keys.id ORDER BY e.detected_at DESC LIMIT 1) AS termination_state
                   FROM keys
                   WHERE telegram_id = ?
                     AND (status IN ('active', 'revoke_failed') OR quota_reason = 'quota')
                   ORDER BY created_at DESC LIMIT 10""",
                (telegram_id,),
            ).fetchall()
        tiers = {
            300 * 1024**2: "Daily Free 300 MiB",
            3 * 1024**3: "Monthly Free 3 GiB",
            GIVEAWAY_LIMIT_BYTES: "100 GiB Giveaway",
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
            result.append(
                {
                    "tier": tiers.get(quota, "Free access"),
                    "used_bytes": used,
                    "quota_bytes": quota,
                    "remaining_bytes": max(0, quota - used),
                    "usage_observed": observed,
                    "expires_at": row["expires_at"],
                    "status": "quota exhausted"
                    if row["quota_reason"] == "quota"
                    else (
                        "revocation failed"
                        if row["termination_state"] == "escalated"
                        else (
                            "revocation pending"
                            if row["termination_state"] in ("retrying", "delete_accepted")
                            or row["status"] == "revoke_failed"
                            else row["status"]
                        )
                    ),
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
