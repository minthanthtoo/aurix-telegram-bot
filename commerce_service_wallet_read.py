"""Wallet reads and consistency reporting."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from commerce_models import CommerceError, UTC, _now_text


def wallet_balance(self, telegram_id: int, currency: str = "MMK") -> int:
    now_text = _now_text()
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        if not self.wallet_reads.user_exists(connection, telegram_id):
            self._ensure_user(connection, telegram_id, "")
        existing_wallet = self.wallet_reads.wallet_currency(connection, telegram_id)
        if (
            existing_wallet is not None
            and str(existing_wallet["currency"]).upper() != currency.upper()
        ):
            raise CommerceError("A user wallet has one supported currency")
        self.wallet_reads.ensure_wallet(
            connection, telegram_id=telegram_id, currency=currency, now_text=now_text
        )
        row = self.wallet_reads.balance(connection, telegram_id)
    return int(row["balance_minor"] if row else 0)


def wallet_history(
    self, telegram_id: int, limit: int = 20, currency: str = "MMK"
) -> list[dict[str, Any]]:
    """Return immutable wallet events for the owner, newest first."""
    with self.database.connect() as connection:
        rows = self.wallet_reads.history(
            connection, telegram_id, currency.upper(), max(1, min(limit, 100))
        )
    return [dict(row) for row in rows]


def consistency_report(
    self,
    now: datetime | None = None,
    review_sla: timedelta = timedelta(hours=24),
) -> dict[str, int]:
    """Read-only invariant scan for admin operations and deployment checks."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    review_cutoff = _now_text(current - review_sla)
    with self.database.connect() as connection:
        return self.wallet_reads.consistency_report(connection, review_cutoff)


def list_pending_orders(self, limit: int = 20) -> list[dict[str, Any]]:
    with self.database.connect() as connection:
        rows = self.wallet_reads.pending_orders(
            connection, max(1, min(limit, 100))
        )
    result = []
    for row in rows:
        item = dict(row)
        item["stage"] = self._order_stage(item)
        result.append(item)
    return result
