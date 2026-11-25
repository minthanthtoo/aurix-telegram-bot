"""Durable Games-owned order records for the side-effect-free state machine.

This repository stores only AuriX order snapshots and opaque recipient
fingerprints. It does not charge a customer, call a supplier, or approve a
payment. External effects belong in a future worker after explicit gates.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any
from persistence import open_sqlite_connection

from .order_state import (
    GameOrder,
    OrderEvent,
    OrderSnapshot,
    OrderState,
    OrderTransitionError,
    transition_order,
)


UTC = timezone.utc


class OrderRepositoryError(RuntimeError):
    """A durable order operation failed safely."""


class IdempotencyConflictError(OrderRepositoryError):
    """A client reused an idempotency key for a different immutable snapshot."""


class OrderNotFoundError(OrderRepositoryError):
    """An order is missing or not owned by the requested customer."""


class ConcurrentOrderUpdateError(OrderRepositoryError):
    """The order changed between read and transactional transition."""


class RecipientLookupThrottledError(OrderRepositoryError):
    """A shared cooldown or recipient-lookup capacity limit was reached."""


@dataclass(frozen=True)
class ProviderEventResult:
    """Atomic webhook outcome; unmatched events remain retryable."""

    order: GameOrder | None
    duplicate: bool
    applied: bool


class GameOrderRepository:
    """SQLite repository with private-file and append-only event invariants."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        if self.path.is_symlink():
            raise OrderRepositoryError("order database must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077:
            raise OrderRepositoryError("order database directory must be private (mode 0700)")
        try:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = DELETE")
                connection.execute("PRAGMA synchronous = FULL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS game_orders (
                        order_id TEXT PRIMARY KEY,
                        customer_id TEXT NOT NULL,
                        customer_idempotency_key TEXT NOT NULL,
                        provider_idempotency_key TEXT NOT NULL UNIQUE,
                        snapshot_json TEXT NOT NULL,
                        state TEXT NOT NULL,
                        provider_order_id TEXT,
                        provider_status TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(customer_id, customer_idempotency_key)
                    );
                    CREATE TABLE IF NOT EXISTS game_order_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_id TEXT NOT NULL REFERENCES game_orders(order_id),
                        occurred_at TEXT NOT NULL,
                        from_state TEXT NOT NULL,
                        to_state TEXT NOT NULL,
                        reason TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS game_orders_customer
                        ON game_orders(customer_id, created_at);
                    CREATE INDEX IF NOT EXISTS game_orders_state_created
                        ON game_orders(state, created_at, order_id);
                    CREATE UNIQUE INDEX IF NOT EXISTS game_orders_provider_identity
                        ON game_orders(provider_order_id) WHERE provider_order_id IS NOT NULL;
                    CREATE INDEX IF NOT EXISTS game_order_events_order
                        ON game_order_events(order_id, event_id);
                    CREATE TABLE IF NOT EXISTS game_order_webhook_events (
                        event_id TEXT PRIMARY KEY,
                        provider_order_id TEXT NOT NULL,
                        provider_status TEXT,
                        order_id TEXT NOT NULL REFERENCES game_orders(order_id),
                        applied_state TEXT NOT NULL,
                        received_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS game_order_webhook_events_order
                        ON game_order_webhook_events(order_id, received_at);
                    CREATE TABLE IF NOT EXISTS game_order_webhook_inbox (
                        event_id TEXT PRIMARY KEY,
                        provider_order_id TEXT NOT NULL,
                        event_json TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        received_at TEXT NOT NULL,
                        processed_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS game_order_webhook_inbox_pending
                        ON game_order_webhook_inbox(provider_order_id, occurred_at) WHERE processed_at IS NULL;
                    CREATE TABLE IF NOT EXISTS game_order_poll_checks (
                        order_id TEXT PRIMARY KEY REFERENCES game_orders(order_id),
                        checked_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS game_order_payment_intents (
                        order_id TEXT PRIMARY KEY REFERENCES game_orders(order_id),
                        rail TEXT NOT NULL,
                        receiver_id TEXT NOT NULL,
                        receiver_revision TEXT NOT NULL,
                        instructions_json TEXT NOT NULL,
                        opened_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS game_order_wallet_payment_claims (
                        order_id TEXT PRIMARY KEY REFERENCES game_orders(order_id),
                        customer_id TEXT NOT NULL,
                        event_key TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL CHECK(status IN ('active','released','committed')),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS game_recipient_lookup_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        customer_fingerprint TEXT NOT NULL,
                        source_fingerprint TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS game_recipient_lookup_customer_time
                        ON game_recipient_lookup_events(customer_fingerprint, created_at);
                    CREATE INDEX IF NOT EXISTS game_recipient_lookup_time
                        ON game_recipient_lookup_events(created_at);
                    CREATE TRIGGER IF NOT EXISTS game_order_payment_intents_immutable
                        BEFORE UPDATE ON game_order_payment_intents
                    BEGIN
                        SELECT RAISE(ABORT, 'payment intents are immutable');
                    END;
                    CREATE TRIGGER IF NOT EXISTS game_order_payment_intents_no_delete
                        BEFORE DELETE ON game_order_payment_intents
                    BEGIN
                        SELECT RAISE(ABORT, 'payment intents are retained for reconciliation');
                    END;
                    CREATE TRIGGER IF NOT EXISTS game_orders_snapshot_immutable
                        BEFORE UPDATE OF order_id, customer_id,
                            customer_idempotency_key, provider_idempotency_key,
                            snapshot_json, created_at ON game_orders
                    BEGIN
                        SELECT RAISE(ABORT, 'order snapshot is immutable');
                    END;
                    CREATE TRIGGER IF NOT EXISTS game_order_events_append_only_update
                        BEFORE UPDATE ON game_order_events
                    BEGIN
                        SELECT RAISE(ABORT, 'order events are append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS game_order_events_append_only_delete
                        BEFORE DELETE ON game_order_events
                    BEGIN
                        SELECT RAISE(ABORT, 'order events are append-only');
                    END;
                    """
                )
                connection.execute("BEGIN IMMEDIATE")
                lookup_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(game_recipient_lookup_events)")
                }
                if "source_fingerprint" not in lookup_columns:
                    connection.execute(
                        "ALTER TABLE game_recipient_lookup_events ADD COLUMN source_fingerprint TEXT"
                    )
                connection.execute(
                    """CREATE INDEX IF NOT EXISTS game_recipient_lookup_source_time
                       ON game_recipient_lookup_events(source_fingerprint, created_at)"""
                )
                connection.commit()
        except sqlite3.Error:
            raise OrderRepositoryError("order database initialization failed") from None
        os.chmod(self.path, 0o600)

    def create(self, order: GameOrder, *, created_at: datetime) -> GameOrder:
        self._require_time(created_at)
        self.initialize()
        timestamp = created_at.astimezone(UTC).isoformat()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """SELECT order_id FROM game_orders
                       WHERE customer_id = ? AND customer_idempotency_key = ?""",
                    (order.customer_id, order.customer_idempotency_key),
                ).fetchone()
                if existing is not None:
                    saved = self._load(connection, str(existing["order_id"]))
                    if saved.snapshot.canonical_json() != order.snapshot.canonical_json():
                        raise IdempotencyConflictError(
                            "customer idempotency key was reused for a different snapshot"
                        )
                    return saved
                connection.execute(
                    """INSERT INTO game_orders
                       (order_id, customer_id, customer_idempotency_key,
                        provider_idempotency_key, snapshot_json, state,
                        provider_order_id, provider_status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)""",
                    (
                        order.order_id,
                        order.customer_id,
                        order.customer_idempotency_key,
                        order.provider_idempotency_key,
                        order.snapshot.canonical_json(),
                        order.state.value,
                        timestamp,
                        timestamp,
                    ),
                )
                return order
        except IdempotencyConflictError:
            raise
        except sqlite3.IntegrityError as exc:
            raise OrderRepositoryError("order could not be created") from exc
        except sqlite3.Error:
            raise OrderRepositoryError("order database write failed") from None

    def get(self, order_id: str, *, customer_id: str | None = None,
            connection: sqlite3.Connection | None = None) -> GameOrder:
        if connection is not None:
            order = self._load(connection, order_id)
            if customer_id is not None and order.customer_id != customer_id:
                raise OrderNotFoundError("order was not found")
            return order
        self.initialize()
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT order_id, customer_id FROM game_orders WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                if row is None or (customer_id is not None and row["customer_id"] != customer_id):
                    raise OrderNotFoundError("order was not found")
                return self._load(connection, order_id)
        except OrderNotFoundError:
            raise
        except sqlite3.Error:
            raise OrderRepositoryError("order database read failed") from None

    def get_by_customer_key(self, customer_id: str, key: str) -> GameOrder | None:
        """Recover a confirmed purchase without replaying expired eligibility."""
        self.initialize()
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT order_id FROM game_orders WHERE customer_id = ? AND customer_idempotency_key = ?",
                    (customer_id, key),
                ).fetchone()
                return None if row is None else self._load(connection, row["order_id"])
        except sqlite3.Error:
            raise OrderRepositoryError("order database read failed") from None

    def list_for_customer(self, customer_id: str) -> tuple[GameOrder, ...]:
        self.initialize()
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT order_id FROM game_orders WHERE customer_id = ? ORDER BY created_at, order_id",
                    (customer_id,),
                ).fetchall()
                return tuple(self._load(connection, str(row["order_id"])) for row in rows)
        except sqlite3.Error:
            raise OrderRepositoryError("order database read failed") from None

    def list_by_state(self, state: OrderState, *, limit: int = 100) -> tuple[GameOrder, ...]:
        """Return a bounded, stable batch for private workflow recovery."""

        if not isinstance(state, OrderState):
            raise OrderRepositoryError("order state is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise OrderRepositoryError("order batch limit is invalid")
        self.initialize()
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """SELECT order_id FROM game_orders WHERE state = ?
                       ORDER BY created_at, order_id LIMIT ?""",
                    (state.value, limit),
                ).fetchall()
                return tuple(self._load(connection, str(row["order_id"])) for row in rows)
        except sqlite3.Error:
            raise OrderRepositoryError("order database read failed") from None

    def transition(
        self,
        order_id: str,
        target: OrderState,
        *,
        at: datetime,
        reason: str,
        provider_order_id: str | None = None,
        provider_status: str | None = None,
    ) -> GameOrder:
        self._require_time(at)
        self.initialize()
        timestamp = at.astimezone(UTC).isoformat()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = self._load(connection, order_id)
                try:
                    updated = transition_order(
                        current,
                        target,
                        at=at,
                        reason=reason,
                        provider_order_id=provider_order_id,
                        provider_status=provider_status,
                    )
                except OrderTransitionError:
                    raise
                result = connection.execute(
                    """UPDATE game_orders
                       SET state = ?, provider_order_id = ?, provider_status = ?, updated_at = ?
                       WHERE order_id = ? AND state = ?""",
                    (
                        updated.state.value,
                        updated.provider_order_id,
                        updated.provider_status,
                        timestamp,
                        order_id,
                        current.state.value,
                    ),
                )
                if result.rowcount != 1:
                    raise ConcurrentOrderUpdateError("order changed during transition")
                event = updated.events[-1]
                connection.execute(
                    """INSERT INTO game_order_events
                       (order_id, occurred_at, from_state, to_state, reason)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        order_id,
                        event.at.isoformat(),
                        event.from_state.value,
                        event.to_state.value,
                        event.reason,
                    ),
                )
                return updated
        except (OrderNotFoundError, OrderTransitionError, ConcurrentOrderUpdateError):
            raise
        except sqlite3.Error:
            raise OrderRepositoryError("order transition failed") from None

    def reconciliation_batch(
        self, *, limit: int, now: datetime | None = None, minimum_interval_seconds: int = 0,
    ) -> tuple[GameOrder, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise OrderRepositoryError("order batch limit is invalid")
        if (type(minimum_interval_seconds) is not int
                or not 0 <= minimum_interval_seconds <= 3600):
            raise OrderRepositoryError("order poll interval is invalid")
        if now is not None:
            self._require_time(now)
        if minimum_interval_seconds and now is None:
            raise OrderRepositoryError("order poll interval requires a current time")
        self.initialize()
        with self._connect() as connection:
            query = """SELECT orders.order_id FROM game_orders orders
                LEFT JOIN game_order_poll_checks checks USING(order_id)
                WHERE orders.provider_order_id IS NOT NULL
                  AND orders.state IN ('submitted','processing','unknown','reconciliation','failed','delivered')
            """
            params: list[Any] = []
            if minimum_interval_seconds:
                cutoff = (now.astimezone(UTC) - timedelta(seconds=minimum_interval_seconds)).isoformat()
                query += " AND (checks.checked_at IS NULL OR checks.checked_at <= ?)"
                params.append(cutoff)
            query += " ORDER BY COALESCE(checks.checked_at, ''), orders.created_at, orders.order_id LIMIT ?"
            params.append(limit)
            rows = connection.execute(query, tuple(params)).fetchall()
            return tuple(self._load(connection, row["order_id"]) for row in rows)

    def claim_poll_check(
        self, order_id: str, *, now: datetime, minimum_interval_seconds: int = 20,
    ) -> bool:
        """Atomically reserve a provider status read across concurrent workers."""
        self._require_time(now)
        if (not isinstance(order_id, str) or not order_id or len(order_id) > 128
                or type(minimum_interval_seconds) is not int
                or not 1 <= minimum_interval_seconds <= 3600):
            raise OrderRepositoryError("order poll claim is invalid")
        current = now.astimezone(UTC)
        timestamp = current.isoformat(timespec="microseconds")
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT checked_at FROM game_order_poll_checks WHERE order_id = ?", (order_id,),
                ).fetchone()
                if row is not None:
                    try:
                        previous = datetime.fromisoformat(row["checked_at"])
                    except (TypeError, ValueError):
                        previous = None
                    if (previous is not None and previous.tzinfo is not None
                            and current - previous.astimezone(UTC) < timedelta(seconds=minimum_interval_seconds)):
                        return False
                connection.execute("""INSERT INTO game_order_poll_checks (order_id, checked_at)
                    VALUES (?, ?) ON CONFLICT(order_id) DO UPDATE SET checked_at=excluded.checked_at""",
                    (order_id, timestamp))
                return True
        except sqlite3.Error:
            raise OrderRepositoryError("supplier poll claim failed") from None

    def record_poll_check(self, order_id: str, *, now: datetime) -> None:
        self._require_time(now)
        with self._connect() as connection:
            connection.execute("""INSERT INTO game_order_poll_checks (order_id, checked_at) VALUES (?, ?)
                ON CONFLICT(order_id) DO UPDATE SET checked_at = excluded.checked_at""",
                (order_id, now.astimezone(UTC).isoformat()))

    def claim_recipient_lookup(
        self,
        customer_id: str,
        *,
        now: datetime,
        fingerprint_secret: bytes | str,
        source_identity: str | None = None,
        cooldown_seconds: int = 10,
        window_seconds: int = 10,
        global_limit: int = 100,
    ) -> None:
        """Atomically reserve a recipient lookup across workers sharing this DB.

        Only keyed fingerprints are stored. Claims are written before calling
        the provider, so failed attempts also consume the cooldown and cannot
        fan out requests across workers. Source addresses share 60 lookups per
        minute; stale fingerprints are pruned on a subsequent lookup.
        """
        self._require_time(now)
        if (not isinstance(customer_id, str) or not customer_id or customer_id != customer_id.strip()
                or len(customer_id) > 128 or any(ord(char) < 32 for char in customer_id)):
            raise OrderRepositoryError("recipient lookup customer identity is invalid")
        try:
            secret = fingerprint_secret.encode("utf-8") if isinstance(fingerprint_secret, str) else fingerprint_secret
        except UnicodeError as exc:
            raise OrderRepositoryError("recipient lookup fingerprint secret is invalid") from exc
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise OrderRepositoryError("recipient lookup fingerprint secret is invalid")
        if (type(cooldown_seconds) is not int or not 1 <= cooldown_seconds <= 3600
                or type(window_seconds) is not int or not cooldown_seconds <= window_seconds <= 3600
                or type(global_limit) is not int or not 1 <= global_limit <= 10000):
            raise OrderRepositoryError("recipient lookup limit settings are invalid")
        current = now.astimezone(UTC)
        timestamp = current.isoformat(timespec="microseconds")
        cooldown_cutoff = (current - timedelta(seconds=cooldown_seconds)).isoformat(timespec="microseconds")
        window_cutoff = (current - timedelta(seconds=window_seconds)).isoformat(timespec="microseconds")
        fingerprint = hmac.new(
            secret,
            b"aurix-games-recipient-lookup-v1\x00" + customer_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        source_fingerprint = None
        if source_identity is not None:
            if (not isinstance(source_identity, str) or not source_identity
                    or source_identity != source_identity.strip() or len(source_identity) > 128
                    or any(ord(char) < 32 for char in source_identity)):
                raise OrderRepositoryError("recipient lookup source identity is invalid")
            source_fingerprint = hmac.new(
                secret,
                b"aurix-games-recipient-lookup-source-v1\x00" + source_identity.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                retention_seconds = max(window_seconds, 60)
                retention_cutoff = (current - timedelta(seconds=retention_seconds)).isoformat(timespec="microseconds")
                connection.execute(
                    "DELETE FROM game_recipient_lookup_events WHERE created_at <= ?",
                    (retention_cutoff,),
                )
                recent_customer = connection.execute(
                    """SELECT 1 FROM game_recipient_lookup_events
                       WHERE customer_fingerprint = ? AND created_at > ? LIMIT 1""",
                    (fingerprint, cooldown_cutoff),
                ).fetchone()
                if recent_customer is not None:
                    connection.commit()
                    raise RecipientLookupThrottledError("recipient lookup cooldown is active")
                count = connection.execute(
                    "SELECT COUNT(*) AS total FROM game_recipient_lookup_events WHERE created_at > ?",
                    (window_cutoff,),
                ).fetchone()["total"]
                if count >= global_limit:
                    connection.commit()
                    raise RecipientLookupThrottledError("recipient lookup capacity is full")
                if source_fingerprint is not None:
                    # shortcut: visitors sharing an IP share 60 checks per minute; tune this if pilot traffic shows false throttles.
                    source_cutoff = (current - timedelta(seconds=60)).isoformat(timespec="microseconds")
                    source_count = connection.execute(
                        """SELECT COUNT(*) AS total FROM game_recipient_lookup_events
                           WHERE source_fingerprint = ? AND created_at > ?""",
                        (source_fingerprint, source_cutoff),
                    ).fetchone()["total"]
                    if source_count >= 60:
                        connection.commit()
                        raise RecipientLookupThrottledError("recipient lookup source capacity is full")
                connection.execute(
                    """INSERT INTO game_recipient_lookup_events
                       (customer_fingerprint, source_fingerprint, created_at) VALUES (?, ?, ?)""",
                    (fingerprint, source_fingerprint, timestamp),
                )
                connection.commit()
        except RecipientLookupThrottledError:
            raise
        except sqlite3.Error:
            raise OrderRepositoryError("recipient lookup limit could not be reserved") from None

    def freeze_payment_intent(self, *, order_id: str, customer_id: str, rail: str,
                              receiver_id: str, receiver_revision: str, opened_at: datetime,
                              expires_at: datetime, instructions: dict[str, str] | None = None) -> dict[str, str]:
        self._require_time(opened_at)
        self._require_time(expires_at)
        if expires_at <= opened_at:
            raise OrderRepositoryError("payment intent expiry is invalid")
        if any(not isinstance(value, str) or not value or value != value.strip() or len(value) > 128
               or any(ord(char) < 32 for char in value) for value in (order_id, customer_id, rail, receiver_id, receiver_revision)):
            raise OrderRepositoryError("payment intent identity is invalid")
        if instructions is not None:
            if not isinstance(instructions, dict):
                raise OrderRepositoryError("payment receiver instructions are invalid")
            fields = set(instructions)
            if fields not in ({"account", "account_name"}, {"account", "account_name", "qr_asset", "qr_sha256"}):
                raise OrderRepositoryError("payment receiver instructions are invalid")
            if any(not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 128
                   or any(ord(char) < 32 for char in value) for value in instructions.values()):
                raise OrderRepositoryError("payment receiver instructions are invalid")
            if "qr_asset" in instructions and (
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(?:png|jpe?g|webp)", instructions["qr_asset"], re.IGNORECASE)
                or not re.fullmatch(r"[a-f0-9]{64}", instructions["qr_sha256"])
            ):
                raise OrderRepositoryError("payment receiver instructions are invalid")
        instructions_json = json.dumps(instructions or {}, sort_keys=True)
        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            order = self._load(connection, order_id)
            if order.customer_id != customer_id or order.state != OrderState.AWAITING_PAYMENT:
                raise OrderRepositoryError("order cannot open payment")
            wallet_claim = connection.execute(
                "SELECT status FROM game_order_wallet_payment_claims WHERE order_id = ?", (order_id,)
            ).fetchone()
            if wallet_claim is not None and wallet_claim["status"] != "released":
                raise OrderRepositoryError("order already has a wallet payment attempt")
            connection.execute("""INSERT OR IGNORE INTO game_order_payment_intents
                (order_id, rail, receiver_id, receiver_revision, instructions_json, opened_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (order_id, rail, receiver_id, receiver_revision, instructions_json, opened_at.astimezone(UTC).isoformat(),
                 expires_at.astimezone(UTC).isoformat()))
            row = connection.execute("SELECT * FROM game_order_payment_intents WHERE order_id = ?", (order_id,)).fetchone()
            if (row["rail"], row["receiver_id"], row["receiver_revision"], row["instructions_json"]) != (rail, receiver_id, receiver_revision, instructions_json):
                raise OrderRepositoryError("order already has a different payment receiver")
            return dict(row)

    def claim_wallet_payment(self, *, order_id: str, customer_id: str, event_key: str,
                             occurred_at: datetime, existing_reservation: bool = False) -> str:
        """Serialize wallet payment against external instructions for this order."""
        self._require_time(occurred_at)
        if any(not isinstance(value, str) or not value or value != value.strip() or len(value) > 128
               or any(ord(char) < 32 for char in value) for value in (order_id, customer_id, event_key)):
            raise OrderRepositoryError("wallet payment identity is invalid")
        self.initialize()
        timestamp = occurred_at.astimezone(UTC).isoformat()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                order = self._load(connection, order_id)
                if order.customer_id != customer_id:
                    raise OrderNotFoundError("order was not found")
                if connection.execute(
                    "SELECT 1 FROM game_order_payment_intents WHERE order_id = ?", (order_id,)
                ).fetchone() is not None:
                    raise OrderRepositoryError("order already has external payment instructions")
                valid_states = {OrderState.AWAITING_PAYMENT, OrderState.PAID_RESERVED}
                if order.state not in valid_states and not existing_reservation:
                    raise OrderRepositoryError("order no longer accepts wallet payment")
                row = connection.execute(
                    "SELECT * FROM game_order_wallet_payment_claims WHERE order_id = ?", (order_id,)
                ).fetchone()
                if row is not None:
                    if row["customer_id"] != customer_id or row["event_key"] != event_key:
                        raise OrderRepositoryError("order already has a different wallet payment")
                    if row["status"] == "released":
                        connection.execute("""UPDATE game_order_wallet_payment_claims
                            SET status='active', updated_at=? WHERE order_id=?""", (timestamp, order_id))
                        return "active"
                    return str(row["status"])
                status = "committed" if existing_reservation and order.state != OrderState.AWAITING_PAYMENT else "active"
                connection.execute("""INSERT INTO game_order_wallet_payment_claims
                    (order_id,customer_id,event_key,status,created_at,updated_at) VALUES (?,?,?,?,?,?)""",
                    (order_id, customer_id, event_key, status, timestamp, timestamp))
                return status
        except (OrderRepositoryError, OrderNotFoundError):
            raise
        except sqlite3.Error:
            raise OrderRepositoryError("wallet payment claim could not be saved") from None

    def release_wallet_payment_claim(self, *, order_id: str, event_key: str, occurred_at: datetime) -> None:
        self._require_time(occurred_at)
        timestamp = occurred_at.astimezone(UTC).isoformat()
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute("""UPDATE game_order_wallet_payment_claims
                    SET status='released', updated_at=? WHERE order_id=? AND event_key=? AND status='active'""",
                    (timestamp, order_id, event_key))
        except sqlite3.Error as exc:
            raise OrderRepositoryError("wallet payment claim could not be released") from exc

    def complete_wallet_payment_claim(self, *, order_id: str, event_key: str, occurred_at: datetime) -> None:
        self._require_time(occurred_at)
        timestamp = occurred_at.astimezone(UTC).isoformat()
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                order = self._load(connection, order_id)
                row = connection.execute("""SELECT status FROM game_order_wallet_payment_claims
                    WHERE order_id=? AND event_key=?""", (order_id, event_key)).fetchone()
                if row is None or order.state == OrderState.AWAITING_PAYMENT:
                    raise OrderRepositoryError("wallet payment reservation is not complete")
                connection.execute("""UPDATE game_order_wallet_payment_claims
                    SET status='committed', updated_at=? WHERE order_id=? AND event_key=?""",
                    (timestamp, order_id, event_key))
        except OrderRepositoryError:
            raise
        except sqlite3.Error:
            raise OrderRepositoryError("wallet payment claim could not be completed") from None

    def get_payment_intent(self, order_id: str, *, connection: sqlite3.Connection | None = None) -> dict[str, str] | None:
        if connection is not None:
            row = connection.execute("SELECT * FROM game_order_payment_intents WHERE order_id = ?", (order_id,)).fetchone()
            return None if row is None else dict(row)
        self.initialize()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM game_order_payment_intents WHERE order_id = ?", (order_id,)).fetchone()
        return None if row is None else dict(row)

    def store_provider_event(self, *, event_id: str, provider_order_id: str, event_json: str,
                             occurred_at: datetime, received_at: datetime) -> None:
        self._require_time(occurred_at)
        self._require_time(received_at)
        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""INSERT OR IGNORE INTO game_order_webhook_inbox
                (event_id, provider_order_id, event_json, occurred_at, received_at)
                VALUES (?, ?, ?, ?, ?)""", (event_id, provider_order_id, event_json,
                occurred_at.astimezone(UTC).isoformat(), received_at.astimezone(UTC).isoformat()))
            stored = connection.execute("SELECT event_json FROM game_order_webhook_inbox WHERE event_id = ?",
                (event_id,)).fetchone()
            if stored["event_json"] != event_json:
                raise OrderRepositoryError("supplier event identity was reused with different data")

    def pending_provider_events(self, *, limit: int) -> tuple[str, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("supplier inbox limit must be between 1 and 100")
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute("""SELECT inbox.event_json FROM game_order_webhook_inbox inbox
                JOIN game_orders orders ON orders.provider_order_id = inbox.provider_order_id
                WHERE inbox.processed_at IS NULL ORDER BY inbox.occurred_at, inbox.event_id LIMIT ?""",
                (limit,)).fetchall()
        return tuple(row["event_json"] for row in rows)

    def apply_provider_event(
        self,
        *,
        event_id: str,
        provider_order_id: str,
        provider_status: str | None,
        target: OrderState | None,
        at: datetime,
        reason: str,
        from_inbox: bool = False,
    ) -> ProviderEventResult:
        """Atomically deduplicate and apply one matched supplier event.

        Events for an unknown provider order are deliberately not recorded so
        a retry can succeed after a delayed local submission commit. Duplicate
        event IDs return the original order without adding a second transition.
        """

        self._require_time(at)
        for value, label in ((event_id, "event_id"), (provider_order_id, "provider_order_id")):
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise OrderRepositoryError(f"{label} is invalid")
        if provider_status is not None and (
            not isinstance(provider_status, str) or not provider_status.strip() or len(provider_status) > 128
        ):
            raise OrderRepositoryError("provider_status is invalid")
        if target is not None and not isinstance(target, OrderState):
            raise OrderRepositoryError("provider target state is invalid")
        self.initialize()
        timestamp = at.astimezone(UTC).isoformat()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                seen = connection.execute(
                    "SELECT order_id FROM game_order_webhook_events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if seen is not None:
                    if from_inbox:
                        connection.execute("UPDATE game_order_webhook_inbox SET processed_at = ? WHERE event_id = ?",
                            (timestamp, event_id))
                    return ProviderEventResult(self._load(connection, str(seen["order_id"])), True, False)
                row = connection.execute(
                    "SELECT order_id FROM game_orders WHERE provider_order_id = ?",
                    (provider_order_id,),
                ).fetchone()
                if row is None:
                    return ProviderEventResult(None, False, False)
                order_id = str(row["order_id"])
                current = self._load(connection, order_id)
                if target == OrderState.DELIVERED and current.snapshot.fulfillment_kind not in {"direct_topup", "steam_topup"}:
                    target = OrderState.RECONCILIATION
                if current.state in {OrderState.UNKNOWN, OrderState.RECONCILIATION} and target is not None:
                    target = OrderState.RECONCILIATION
                if target == OrderState.FAILED and current.state == OrderState.DELIVERED:
                    target = OrderState.RECONCILIATION
                updated = current
                applied = False
                if from_inbox:
                    stale = connection.execute("""SELECT 1 FROM game_order_webhook_inbox newer
                        JOIN game_order_webhook_inbox incoming ON incoming.event_id = ?
                        JOIN game_order_webhook_events recorded ON recorded.event_id = newer.event_id
                        WHERE newer.provider_order_id = incoming.provider_order_id
                          AND newer.occurred_at > incoming.occurred_at
                          AND recorded.provider_status IN ('processing','completed','failed','refund','refunded') LIMIT 1""",
                        (event_id,)).fetchone()
                    connection.execute("UPDATE game_order_webhook_inbox SET processed_at = ? WHERE event_id = ?",
                        (timestamp, event_id))
                    if stale is not None:
                        target = None
                        provider_status = None
                if target is not None and target != current.state:
                    try:
                        updated = transition_order(
                            current,
                            target,
                            at=at,
                            reason=reason,
                            provider_order_id=provider_order_id,
                            provider_status=provider_status,
                        )
                    except OrderTransitionError:
                        # Late/out-of-order supplier events must be retained
                        # for audit without rewinding a terminal local state.
                        updated = current
                    else:
                        applied = True
                        event = updated.events[-1]
                        connection.execute(
                            """INSERT INTO game_order_events
                               (order_id, occurred_at, from_state, to_state, reason)
                               VALUES (?, ?, ?, ?, ?)""",
                            (order_id, event.at.isoformat(), event.from_state.value, event.to_state.value, event.reason),
                        )
                if applied or (current.provider_status is None and target is None and provider_status is not None):
                    connection.execute(
                        """UPDATE game_orders
                           SET state = ?, provider_order_id = ?, provider_status = ?, updated_at = ?
                           WHERE order_id = ?""",
                        (updated.state.value, provider_order_id, provider_status, timestamp, order_id),
                    )
                connection.execute(
                    """INSERT INTO game_order_webhook_events
                       (event_id, provider_order_id, provider_status, order_id, applied_state, received_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (event_id, provider_order_id, provider_status, order_id, updated.state.value, timestamp),
                )
                return ProviderEventResult(updated, False, applied)
        except (OrderNotFoundError, OrderTransitionError, OrderRepositoryError):
            raise
        except sqlite3.IntegrityError as exc:
            raise OrderRepositoryError("provider event could not be recorded") from exc
        except sqlite3.Error:
            raise OrderRepositoryError("provider event reconciliation failed") from None

    def _connect(self) -> sqlite3.Connection:
        return open_sqlite_connection(self.path, timeout_seconds=5)

    def _load(self, connection: sqlite3.Connection, order_id: str) -> GameOrder:
        row = connection.execute(
            """SELECT order_id, customer_id, customer_idempotency_key,
                      provider_idempotency_key, snapshot_json, state,
                      provider_order_id, provider_status
                 FROM game_orders WHERE order_id = ?""",
            (order_id,),
        ).fetchone()
        if row is None:
            raise OrderNotFoundError("order was not found")
        try:
            snapshot = _snapshot_from_json(row["snapshot_json"])
            state = OrderState(row["state"])
            events = tuple(
                OrderEvent(
                    at=datetime.fromisoformat(event["occurred_at"]),
                    from_state=OrderState(event["from_state"]),
                    to_state=OrderState(event["to_state"]),
                    reason=event["reason"],
                )
                for event in connection.execute(
                    """SELECT occurred_at, from_state, to_state, reason
                         FROM game_order_events WHERE order_id = ? ORDER BY event_id""",
                    (order_id,),
                ).fetchall()
            )
            return GameOrder(
                order_id=row["order_id"],
                customer_id=row["customer_id"],
                customer_idempotency_key=row["customer_idempotency_key"],
                provider_idempotency_key=row["provider_idempotency_key"],
                snapshot=snapshot,
                state=state,
                provider_order_id=row["provider_order_id"],
                provider_status=row["provider_status"],
                events=events,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OrderRepositoryError("stored order record was malformed") from exc

    @staticmethod
    def _require_time(value: datetime) -> None:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise OrderRepositoryError("order time must be timezone-aware")


def _snapshot_from_json(value: str) -> OrderSnapshot:
    raw = json.loads(value)
    if not isinstance(raw, dict):
        raise ValueError("snapshot was not an object")
    return OrderSnapshot(
        offer_id=raw["offer_id"],
        product_slug=raw["product_slug"],
        fulfillment_kind=raw["fulfillment_kind"],
        amount_minor=raw["amount_minor"],
        currency=raw["currency"],
        recipient_fingerprint=raw["recipient_fingerprint"],
    )


__all__ = [
    "ConcurrentOrderUpdateError",
    "GameOrderRepository",
    "IdempotencyConflictError",
    "OrderNotFoundError",
    "OrderRepositoryError",
    "ProviderEventResult",
]
