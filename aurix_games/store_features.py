"""Customer-safe leaderboard and operator-published store notices."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import re
import secrets
import sqlite3
from pathlib import Path
import stat
from typing import Any
import unicodedata


UTC = timezone.utc
_NOTICE_ID = re.compile(r"^[A-Za-z0-9_-]{12,40}$")
_SEVERITIES = {"info", "success", "warning"}
_ACHIEVEMENT_TIERS = (("Starter", 1), ("Rising", 5), ("Elite", 10), ("Legend", 25))


class StoreFeatureError(ValueError):
    """Invalid public profile or notice data."""


class StoreFeatureStore:
    """Small durable feature store sharing the private Games order database."""

    def __init__(self, database: Path | str) -> None:
        self.database = Path(database)
        if self.database.is_symlink():
            raise ValueError("store feature database must not be a symlink")
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(self.database.parent.stat().st_mode) & 0o077:
            raise ValueError("store feature database directory must be private (mode 0700)")
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS game_store_notices (
                    notice_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    severity TEXT NOT NULL CHECK(severity IN ('info','success','warning')),
                    status TEXT NOT NULL CHECK(status IN ('active','archived')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS game_store_notices_public
                    ON game_store_notices(status, created_at DESC);
                CREATE TABLE IF NOT EXISTS game_store_leaderboard_profiles (
                    customer_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    opted_in INTEGER NOT NULL CHECK(opted_in IN (0,1)),
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS game_store_leaderboard_opted_in
                    ON game_store_leaderboard_profiles(opted_in, display_name);
            """)
        self.database.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @staticmethod
    def _now(now: datetime | None = None) -> datetime:
        value = now or datetime.now(UTC)
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("store feature timestamps must include a timezone")
        return value.astimezone(UTC)

    def public_notices(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        self._now(now)
        with closing(self._connect()) as db:
            rows = db.execute("""SELECT notice_id, title, body, severity, created_at, updated_at
                FROM game_store_notices WHERE status='active'
                ORDER BY created_at DESC, notice_id DESC LIMIT 50""").fetchall()
        return [dict(row) for row in rows]

    def admin_notices(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute("""SELECT notice_id, title, body, severity, status, created_at, updated_at
                FROM game_store_notices ORDER BY updated_at DESC, notice_id DESC LIMIT 100""").fetchall()
        return [dict(row) for row in rows]

    def save_notice(self, payload: Any, *, notice_id: str | None = None,
                    now: datetime | None = None) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {"title", "body", "severity", "status"}:
            raise StoreFeatureError("notice fields are invalid")
        title, body, severity, status = (payload.get(key) for key in ("title", "body", "severity", "status"))
        if not isinstance(title, str) or not 3 <= len(title.strip()) <= 100:
            raise StoreFeatureError("title must be 3 to 100 characters")
        if not isinstance(body, str) or not 3 <= len(body.strip()) <= 1200:
            raise StoreFeatureError("message must be 3 to 1,200 characters")
        if (not isinstance(severity, str) or severity not in _SEVERITIES
                or not isinstance(status, str) or status not in {"active", "archived"}):
            raise StoreFeatureError("notice type or status is invalid")
        if notice_id is not None and not _NOTICE_ID.fullmatch(notice_id):
            raise StoreFeatureError("notice ID is invalid")
        is_new = notice_id is None
        stamp = self._now(now).isoformat()
        notice_id = notice_id or secrets.token_urlsafe(12)
        with closing(self._connect()) as db, db:
            previous = db.execute("SELECT created_at FROM game_store_notices WHERE notice_id=?", (notice_id,)).fetchone()
            if previous is None and not is_new:
                raise StoreFeatureError("notice was not found")
            created_at = previous["created_at"] if previous else stamp
            db.execute("""INSERT INTO game_store_notices
                (notice_id,title,body,severity,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(notice_id) DO UPDATE SET title=excluded.title, body=excluded.body,
                severity=excluded.severity, status=excluded.status, updated_at=excluded.updated_at""",
                       (notice_id, title.strip(), body.strip(), severity, status, created_at, stamp))
            row = db.execute("""SELECT notice_id,title,body,severity,status,created_at,updated_at
                FROM game_store_notices WHERE notice_id=?""", (notice_id,)).fetchone()
        return dict(row)

    def leaderboard_profile(self, customer_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute("SELECT display_name,opted_in FROM game_store_leaderboard_profiles WHERE customer_id=?",
                             (customer_id,)).fetchone()
        return {"display_name": row["display_name"] if row else "", "opted_in": bool(row["opted_in"]) if row else False}

    def save_leaderboard_profile(self, customer_id: str, payload: Any, *, now: datetime | None = None) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {"display_name", "opted_in"}:
            raise StoreFeatureError("leaderboard settings are invalid")
        name, opted_in = payload["display_name"], payload["opted_in"]
        if type(opted_in) is not bool or not isinstance(name, str):
            raise StoreFeatureError("leaderboard settings are invalid")
        name = unicodedata.normalize("NFC", name.strip())
        if len(name) > 24 or any(unicodedata.category(char).startswith("C") for char in name):
            raise StoreFeatureError("public name must be 24 characters or fewer and contain no control characters")
        if opted_in and len(name) < 2:
            raise StoreFeatureError("choose a public name from 2 to 24 characters")
        stamp = self._now(now).isoformat()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing_names = db.execute("""SELECT display_name FROM game_store_leaderboard_profiles
                WHERE opted_in=1 AND customer_id<>?""", (customer_id,)).fetchall() if opted_in else ()
            if any(unicodedata.normalize("NFC", row["display_name"]).casefold() == name.casefold()
                   for row in existing_names):
                raise StoreFeatureError("that public name is already in use")
            db.execute("""INSERT INTO game_store_leaderboard_profiles(customer_id,display_name,opted_in,updated_at)
                VALUES(?,?,?,?) ON CONFLICT(customer_id) DO UPDATE SET display_name=excluded.display_name,
                opted_in=excluded.opted_in,updated_at=excluded.updated_at""",
                       (customer_id, name, int(opted_in), stamp))
        return {"display_name": name, "opted_in": opted_in}

    def leaderboard(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = self._now(now)
        since = (current - timedelta(days=30)).isoformat()
        with closing(self._connect()) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('game_orders','game_order_events')"
            )}
            if tables != {"game_orders", "game_order_events"}:
                return {"period": "rolling_30_days", "as_of": current.isoformat(), "entries": []}
            rows = db.execute("""SELECT p.display_name, COUNT(*) AS completed_orders
                FROM game_store_leaderboard_profiles p
                JOIN game_orders o ON o.customer_id=p.customer_id
                WHERE p.opted_in=1 AND o.state='delivered' AND EXISTS (
                    SELECT 1 FROM game_order_events e
                    WHERE e.order_id=o.order_id AND e.to_state='delivered'
                      AND e.occurred_at>=? AND e.occurred_at<=?
                )
                GROUP BY p.customer_id,p.display_name
                ORDER BY completed_orders DESC,p.display_name COLLATE NOCASE,p.customer_id LIMIT 20""",
                              (since, current.isoformat())).fetchall()
        entries = []
        previous_count: int | None = None
        rank = 0
        for position, row in enumerate(rows, 1):
            count = int(row["completed_orders"])
            if count != previous_count:
                rank = position
            previous_count = count
            reached = [(label, threshold) for label, threshold in _ACHIEVEMENT_TIERS if count >= threshold]
            achievement = reached[-1][0]
            next_tier = next(((label, threshold) for label, threshold in _ACHIEVEMENT_TIERS if threshold > count), None)
            entries.append({
                "rank": rank,
                "display_name": row["display_name"],
                "completed_orders": count,
                "achievement": achievement,
                "next_achievement": next_tier[0] if next_tier else None,
                "orders_to_next": next_tier[1] - count if next_tier else 0,
            })
        return {"period": "rolling_30_days", "as_of": current.isoformat(), "entries": entries}
