"""Durable low-FazerCards-wallet reminder outbox.

The monitor records observations and alert intent locally.  Delivering a
Telegram/email notification is deliberately a separate adapter so a failed
transport cannot mutate the supplier wallet or lose the alert across restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import stat
import uuid
from typing import Any

from .fazercards_account import (
    LowWalletDecision,
    SupplierAccountInputError,
    SupplierWalletPolicy,
    SupplierWalletSnapshot,
    evaluate_low_wallet,
)


UTC = timezone.utc
DEFAULT_CLAIM_LEASE = timedelta(minutes=5)


@dataclass(frozen=True)
class WalletMonitorResult:
    decision: LowWalletDecision
    alert_id: str | None


class SupplierWalletAlertStore:
    """SQLite observation/outbox store with private file permissions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        if self.path.is_symlink():
            raise SupplierAccountInputError("wallet alert database must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077:
            raise SupplierAccountInputError("wallet alert database directory must be private")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS supplier_wallet_state (
                    provider TEXT PRIMARY KEY,
                    balance_usd TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    last_reminder_at TEXT
                );
                CREATE TABLE IF NOT EXISTS supplier_wallet_alerts (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    alert_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','claimed','sent','failed')),
                    created_at TEXT NOT NULL,
                    claimed_at TEXT,
                    claim_token TEXT,
                    sent_at TEXT
                );
                CREATE INDEX IF NOT EXISTS supplier_wallet_alerts_due
                    ON supplier_wallet_alerts(provider, status, created_at);
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(supplier_wallet_alerts)")
            }
            if "claim_token" not in columns:
                connection.execute(
                    "ALTER TABLE supplier_wallet_alerts ADD COLUMN claim_token TEXT"
                )
        self.path.chmod(0o600)

    def observe(
        self,
        snapshot: SupplierWalletSnapshot,
        policy: SupplierWalletPolicy,
        *,
        now: datetime,
        provider: str = "fazercards",
    ) -> WalletMonitorResult:
        self.initialize()
        if not isinstance(provider, str) or not provider or len(provider) > 64:
            raise SupplierAccountInputError("provider is invalid")
        current = _aware(now, "now")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT last_reminder_at FROM supplier_wallet_state WHERE provider = ?",
                (provider,),
            ).fetchone()
            last = _parse_optional(row["last_reminder_at"] if row else None)
            decision = evaluate_low_wallet(snapshot, policy, now=current, last_reminder_at=last)
            reminder_at = current.isoformat() if decision.should_remind else (last.isoformat() if last else None)
            connection.execute(
                """INSERT INTO supplier_wallet_state(provider, balance_usd, observed_at, last_reminder_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(provider) DO UPDATE SET balance_usd=excluded.balance_usd,
                       observed_at=excluded.observed_at, last_reminder_at=excluded.last_reminder_at""",
                (
                    provider,
                    format(snapshot.balance_usd, "f"),
                    _aware(snapshot.observed_at, "observed_at").isoformat(),
                    reminder_at,
                ),
            )
            alert_id = None
            if decision.should_remind:
                alert_id = uuid.uuid4().hex
                payload = {
                    "provider": provider,
                    "balance_usd": format(decision.balance_usd, "f"),
                    "threshold_usd": format(decision.threshold_usd, "f"),
                    "suggested_topup_usd": format(decision.suggested_topup_usd, "f"),
                    "reason": decision.reason,
                }
                connection.execute(
                    """INSERT INTO supplier_wallet_alerts
                       (id, provider, alert_key, payload_json, status, created_at)
                       VALUES (?, ?, ?, ?, 'pending', ?)""",
                    (alert_id, provider, decision.alert_key, json.dumps(payload, sort_keys=True), current.isoformat()),
                )
            connection.commit()
        return WalletMonitorResult(decision, alert_id)

    def claim_pending(
        self,
        *,
        provider: str = "fazercards",
        limit: int = 20,
        now: datetime | None = None,
        claim_lease: timedelta = DEFAULT_CLAIM_LEASE,
    ) -> list[dict[str, Any]]:
        """Claim alerts atomically and recover claims abandoned past their lease.

        Each claim returns a fencing token. ``mark_sent``/``mark_failed`` must
        present that token so a worker whose claim expired cannot finalize or
        release a newer worker's claim. Delivery remains at-least-once if a
        sender crashes after sending but before ``mark_sent``; use the stable
        alert ``id`` as the downstream notification idempotency key.
        """
        self.initialize()
        if not isinstance(provider, str) or not provider or len(provider) > 64:
            raise SupplierAccountInputError("provider is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise SupplierAccountInputError("limit is invalid")
        current = _aware(now if now is not None else datetime.now(UTC), "now")
        if not isinstance(claim_lease, timedelta) or claim_lease <= timedelta(0):
            raise SupplierAccountInputError("claim_lease must be positive")
        expired_before = (current - claim_lease).isoformat()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            # A process may exit after claiming but before completing the
            # notification. Requeue stale claims in the same transaction as
            # the next claim so concurrent workers cannot double-claim a row.
            connection.execute(
                """UPDATE supplier_wallet_alerts
                      SET status = 'pending', claimed_at = NULL, claim_token = NULL
                    WHERE provider = ? AND status = 'claimed'
                      AND (claimed_at IS NULL OR julianday(claimed_at) <= julianday(?))""",
                (provider, expired_before),
            )
            rows = connection.execute(
                "SELECT * FROM supplier_wallet_alerts WHERE provider = ? AND status = 'pending' ORDER BY created_at LIMIT ?",
                (provider, limit),
            ).fetchall()
            claims: dict[str, str] = {}
            if rows:
                claims = {str(row["id"]): uuid.uuid4().hex for row in rows}
                connection.executemany(
                    """UPDATE supplier_wallet_alerts
                          SET status = 'claimed', claimed_at = ?, claim_token = ?
                        WHERE id = ? AND status = 'pending'""",
                    [(current.isoformat(), claims[str(row["id"])], str(row["id"])) for row in rows],
                )
            connection.commit()
        result = []
        for row in rows:
            item = dict(row)
            item["status"] = "claimed"
            item["claimed_at"] = current.isoformat()
            item["claim_token"] = claims[str(item["id"])]
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def list_alerts(
        self,
        *,
        provider: str = "fazercards",
        status: str = "pending",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Read sanitized reminder rows for an authenticated operator view."""

        self.initialize()
        if not isinstance(provider, str) or not provider or len(provider) > 64:
            raise SupplierAccountInputError("provider is invalid")
        if status not in {"pending", "claimed", "sent", "failed"}:
            raise SupplierAccountInputError("alert status is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise SupplierAccountInputError("limit is invalid")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT id, provider, alert_key, payload_json, status,
                          created_at, claimed_at, sent_at
                     FROM supplier_wallet_alerts
                    WHERE provider = ? AND status = ?
                    ORDER BY created_at DESC LIMIT ?""",
                (provider, status, limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                payload = json.loads(item.pop("payload_json"))
            except (TypeError, json.JSONDecodeError) as exc:
                raise SupplierAccountInputError("stored wallet alert is invalid") from exc
            if not isinstance(payload, dict):
                raise SupplierAccountInputError("stored wallet alert is invalid")
            item["payload"] = payload
            result.append(item)
        return result

    def mark_sent(self, alert_id: str, *, claim_token: str) -> None:
        self.initialize()
        _validate_claim(alert_id, claim_token)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            updated = connection.execute(
                """UPDATE supplier_wallet_alerts
                      SET status = 'sent', sent_at = ?, claim_token = NULL
                    WHERE id = ? AND status = 'claimed' AND claim_token = ?""",
                (datetime.now(UTC).isoformat(), alert_id, claim_token),
            )
            if updated.rowcount != 1:
                raise SupplierAccountInputError("wallet alert is not claimed")

    def mark_failed(self, alert_id: str, *, claim_token: str) -> None:
        """Return a claimed alert to the pending queue after transport failure."""

        self.initialize()
        _validate_claim(alert_id, claim_token)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            updated = connection.execute(
                """UPDATE supplier_wallet_alerts
                      SET status = 'pending', claimed_at = NULL, claim_token = NULL
                    WHERE id = ? AND status = 'claimed' AND claim_token = ?""",
                (alert_id, claim_token),
            )
            if updated.rowcount != 1:
                raise SupplierAccountInputError("wallet alert is not claimed")


def _aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SupplierAccountInputError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_optional(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        raise SupplierAccountInputError("stored reminder timestamp is invalid") from None
    return _aware(parsed, "stored reminder timestamp")


def _validate_claim(alert_id: str, claim_token: str) -> None:
    if not isinstance(alert_id, str) or not alert_id:
        raise SupplierAccountInputError("alert_id is invalid")
    if not isinstance(claim_token, str) or len(claim_token) != 32:
        raise SupplierAccountInputError("claim_token is invalid")
    try:
        int(claim_token, 16)
    except ValueError:
        raise SupplierAccountInputError("claim_token is invalid") from None


__all__ = ["DEFAULT_CLAIM_LEASE", "SupplierWalletAlertStore", "WalletMonitorResult"]
