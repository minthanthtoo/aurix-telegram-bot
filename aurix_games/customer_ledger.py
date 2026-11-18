"""Games-owned customer money ledger and reservation boundary.

The ledger is deliberately independent from supplier wallet funds.  Customer
credits are recorded only by an explicit, attributed payment verifier; this
module never treats screenshots, supplier balance, or an order request as
payment evidence.  Every balance mutation is append-only, idempotent, and
durable in a private SQLite database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import stat
from typing import Any, Mapping

from persistence import open_sqlite_connection


UTC = timezone.utc


class LedgerInputError(ValueError):
    """A ledger input or immutable operation key is invalid."""


class LedgerConflictError(RuntimeError):
    """An idempotency key or reservation was reused with different facts."""


class InsufficientFundsError(RuntimeError):
    """The customer has insufficient available balance for a reservation."""


class LedgerOperationError(RuntimeError):
    """A ledger operation could not be committed safely."""


@dataclass(frozen=True)
class AccountBalance:
    customer_id: str
    currency: str
    available_minor: int
    reserved_minor: int
    observed_at: datetime

    @property
    def total_minor(self) -> int:
        return self.available_minor + self.reserved_minor

    def as_dict(self) -> dict[str, Any]:
        return {
            "customer_id": self.customer_id,
            "currency": self.currency,
            "available_minor": self.available_minor,
            "reserved_minor": self.reserved_minor,
            "total_minor": self.total_minor,
            "observed_at": self.observed_at.astimezone(UTC).isoformat(),
        }


@dataclass(frozen=True)
class LedgerCreditView:
    """Sanitized customer-visible view of one posted top-up credit."""

    entry_id: int
    currency: str
    amount_minor: int
    available_after_minor: int
    occurred_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "currency": self.currency,
            "amount_minor": self.amount_minor,
            "available_after_minor": self.available_after_minor,
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat(),
            "status": "posted",
        }


@dataclass(frozen=True)
class Reservation:
    order_id: str
    customer_id: str
    currency: str
    amount_minor: int
    status: str
    updated_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "customer_id": self.customer_id,
            "currency": self.currency,
            "amount_minor": self.amount_minor,
            "status": self.status,
            "updated_at": self.updated_at.astimezone(UTC).isoformat(),
        }


@dataclass(frozen=True)
class LedgerResult:
    balance: AccountBalance
    reservation: Reservation | None = None
    replayed: bool = False


class CustomerLedger:
    """Private SQLite append-only ledger with reservation invariants."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        if self.path.is_symlink():
            raise LedgerInputError("ledger database must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077:
            raise LedgerInputError("ledger database directory must be private")
        try:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = DELETE")
                connection.execute("PRAGMA synchronous = FULL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS game_customer_accounts (
                        customer_id TEXT PRIMARY KEY,
                        currency TEXT NOT NULL,
                        available_minor INTEGER NOT NULL CHECK(available_minor >= 0),
                        reserved_minor INTEGER NOT NULL CHECK(reserved_minor >= 0),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS game_ledger_entries (
                        entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_key TEXT NOT NULL UNIQUE,
                        customer_id TEXT NOT NULL,
                        order_id TEXT,
                        currency TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        available_delta_minor INTEGER NOT NULL,
                        reserved_delta_minor INTEGER NOT NULL,
                        available_after_minor INTEGER NOT NULL,
                        reserved_after_minor INTEGER NOT NULL,
                        actor TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        occurred_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS game_ledger_customer_time
                        ON game_ledger_entries(customer_id, entry_id);
                    CREATE TABLE IF NOT EXISTS game_bank_refund_intents (
                        order_id TEXT PRIMARY KEY,
                        authorization_json TEXT NOT NULL,
                        authorized_by TEXT NOT NULL,
                        authorized_at TEXT NOT NULL,
                        completion_json TEXT,
                        completed_at TEXT,
                        outbound_key TEXT UNIQUE
                    );
                    CREATE TABLE IF NOT EXISTS game_outbound_refund_claims (
                        outbound_key TEXT PRIMARY KEY,
                        owner TEXT NOT NULL
                    );
                    CREATE TRIGGER IF NOT EXISTS game_outbound_claim_no_update
                    BEFORE UPDATE ON game_outbound_refund_claims
                    BEGIN SELECT RAISE(ABORT, 'outbound claims are immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS game_outbound_claim_no_delete
                    BEFORE DELETE ON game_outbound_refund_claims
                    BEGIN SELECT RAISE(ABORT, 'outbound claims are retained'); END;
                    CREATE TRIGGER IF NOT EXISTS game_bank_refund_authorization_immutable
                    BEFORE UPDATE OF order_id,authorization_json,authorized_by,authorized_at ON game_bank_refund_intents
                    BEGIN SELECT RAISE(ABORT, 'bank refund authorization is immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS game_bank_refund_completion_immutable
                    BEFORE UPDATE ON game_bank_refund_intents WHEN OLD.completion_json IS NOT NULL
                    BEGIN SELECT RAISE(ABORT, 'bank refund completion is immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS game_bank_refund_no_delete
                    BEFORE DELETE ON game_bank_refund_intents
                    BEGIN SELECT RAISE(ABORT, 'bank refund evidence is retained'); END;
                    CREATE TABLE IF NOT EXISTS game_order_reservations (
                        order_id TEXT PRIMARY KEY,
                        customer_id TEXT NOT NULL,
                        currency TEXT NOT NULL,
                        amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
                        status TEXT NOT NULL CHECK(status IN ('reserved','captured','released','refunded')),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TRIGGER IF NOT EXISTS game_ledger_entries_append_only_update
                        BEFORE UPDATE ON game_ledger_entries
                    BEGIN
                        SELECT RAISE(ABORT, 'ledger entries are append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS game_ledger_entries_append_only_delete
                        BEFORE DELETE ON game_ledger_entries
                    BEGIN
                        SELECT RAISE(ABORT, 'ledger entries are append-only');
                    END;
                    """
                )
        except sqlite3.Error as exc:
            raise LedgerOperationError("ledger database initialization failed") from exc
        self.path.chmod(0o600)

    def balance(self, customer_id: str) -> AccountBalance | None:
        customer = _id(customer_id, "customer_id")
        self.initialize()
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM game_customer_accounts WHERE customer_id = ?",
                    (customer,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise LedgerOperationError("ledger balance read failed") from exc
        return None if row is None else _balance_from_row(row)

    def contains_event_key(self, event_key: str) -> bool:
        """Check whether an immutable ledger key has already been consumed."""

        key = _key(event_key, "event_key")
        self.initialize()
        try:
            with self._connect() as connection:
                return connection.execute(
                    "SELECT 1 FROM game_ledger_entries WHERE event_key = ?", (key,)
                ).fetchone() is not None
        except sqlite3.Error as exc:
            raise LedgerOperationError("ledger event lookup failed") from exc

    def event_by_key(self, event_key: str) -> dict[str, Any] | None:
        """Read only the private facts needed to reconcile an immutable event key."""

        key = _key(event_key, "event_key")
        self.initialize()
        try:
            with self._connect() as connection:
                row = connection.execute("""SELECT event_key, customer_id, order_id, currency, kind,
                    available_delta_minor, occurred_at FROM game_ledger_entries WHERE event_key=?""",
                    (key,)).fetchone()
        except sqlite3.Error as exc:
            raise LedgerOperationError("ledger event lookup failed") from exc
        return None if row is None else dict(row)

    def list_credits(
        self,
        customer_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[LedgerCreditView, ...]:
        """Return only posted customer credits, newest first.

        Internal event keys, actors, reasons, metadata, and reservation
        movements are intentionally excluded from this customer-facing view.
        """

        customer = _id(customer_id, "customer_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise LedgerInputError("limit must be between 1 and 100")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10_000:
            raise LedgerInputError("offset must be between 0 and 10000")
        self.initialize()
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """SELECT entry_id, currency, available_delta_minor,
                              available_after_minor, occurred_at
                         FROM game_ledger_entries
                        WHERE customer_id = ? AND kind = 'credit'
                        ORDER BY entry_id DESC
                        LIMIT ? OFFSET ?""",
                    (customer, limit, offset),
                ).fetchall()
        except sqlite3.Error as exc:
            raise LedgerOperationError("ledger history read failed") from exc
        return tuple(
            LedgerCreditView(
                entry_id=int(row["entry_id"]),
                currency=str(row["currency"]),
                amount_minor=int(row["available_delta_minor"]),
                available_after_minor=int(row["available_after_minor"]),
                occurred_at=_time(datetime.fromisoformat(str(row["occurred_at"]))),
            )
            for row in rows
        )

    def credit(
        self,
        *,
        customer_id: str,
        amount_minor: int,
        currency: str,
        event_key: str,
        actor: str,
        reason: str,
        occurred_at: datetime,
        metadata: Mapping[str, Any] | None = None,
    ) -> LedgerResult:
        """Record an explicitly verified customer payment or adjustment."""

        self._validate_amount(amount_minor)
        return self._mutate(
            customer_id=_id(customer_id, "customer_id"),
            currency=_currency(currency),
            event_key=_key(event_key, "event_key"),
            actor=_text(actor, "actor", 128),
            reason=_text(reason, "reason", 240),
            occurred_at=_time(occurred_at),
            kind="credit",
            order_id=None,
            available_delta=amount_minor,
            reserved_delta=0,
            metadata=metadata,
        )

    def reserve(
        self,
        *,
        order_id: str,
        customer_id: str,
        amount_minor: int,
        currency: str,
        event_key: str,
        actor: str,
        reason: str,
        occurred_at: datetime,
        metadata: Mapping[str, Any] | None = None,
        verified_transfer_key: str | None = None,
        existing_only: bool = False,
    ) -> LedgerResult:
        self._validate_amount(amount_minor)
        order = _id(order_id, "order_id")
        customer = _id(customer_id, "customer_id")
        cur = _currency(currency)
        event = _key(event_key, "event_key")
        transfer_key = None if verified_transfer_key is None else _key(verified_transfer_key, "verified_transfer_key")
        if transfer_key is not None and transfer_key == event:
            raise LedgerInputError("transfer and reservation keys must be distinct")
        current = _time(occurred_at)
        actor_text = _text(actor, "actor", 128)
        reason_text = _text(reason, "reason", 240)
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                replay = _existing_event(connection, event)
                transfer = _existing_event(connection, transfer_key) if transfer_key is not None else None
                if transfer is not None and (
                    transfer["order_id"] != order or transfer["kind"] != "verified_transfer"
                    or transfer["currency"] != cur or transfer["customer_id"] != customer
                    or transfer["available_delta_minor"] != amount_minor or replay is None
                ):
                    raise LedgerConflictError("verified transfer is already allocated")
                if replay is not None:
                    if transfer_key is not None and transfer is None:
                        raise LedgerConflictError("order already has a different settlement")
                    return self._replay_reservation(connection, replay, order, customer, amount_minor, cur)
                if existing_only:
                    raise LedgerConflictError("order is no longer accepting a new settlement")
                existing = connection.execute(
                    "SELECT * FROM game_order_reservations WHERE order_id = ?", (order,)
                ).fetchone()
                if existing is not None:
                    saved = _reservation_from_row(existing)
                    if (saved.customer_id, saved.amount_minor, saved.currency) != (customer, amount_minor, cur):
                        raise LedgerConflictError("order reservation details were reused with different facts")
                    if saved.status == "reserved":
                        raise LedgerConflictError("order already has a reservation with a different event key")
                    raise LedgerConflictError("order reservation is already terminal")
                account = _ensure_account(connection, customer, cur, current)
                if account["currency"] != cur:
                    raise LedgerConflictError("customer account currency does not match reservation")
                available = int(account["available_minor"])
                if transfer_key is not None:
                    available += amount_minor
                    self._insert_entry(connection, transfer_key, customer, order, cur, "verified_transfer",
                        amount_minor, 0, available, int(account["reserved_minor"]), actor_text, reason_text,
                        metadata, current)
                if available < amount_minor:
                    raise InsufficientFundsError("customer has insufficient available funds")
                available -= amount_minor
                reserved = int(account["reserved_minor"]) + amount_minor
                connection.execute(
                    "UPDATE game_customer_accounts SET available_minor = ?, reserved_minor = ?, updated_at = ? WHERE customer_id = ?",
                    (available, reserved, current.isoformat(), customer),
                )
                connection.execute(
                    "INSERT INTO game_order_reservations(order_id, customer_id, currency, amount_minor, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'reserved', ?, ?)",
                    (order, customer, cur, amount_minor, current.isoformat(), current.isoformat()),
                )
                self._insert_entry(
                    connection, event, customer, order, cur, "reserve", -amount_minor, amount_minor,
                    available, reserved, actor_text, reason_text, metadata, current,
                )
                return LedgerResult(
                    _balance_from_values(customer, cur, available, reserved, current),
                    Reservation(order, customer, cur, amount_minor, "reserved", current),
                )
        except (LedgerConflictError, InsufficientFundsError):
            raise
        except sqlite3.Error as exc:
            raise LedgerOperationError("funds could not be reserved") from exc

    def capture(
        self,
        *,
        order_id: str,
        event_key: str,
        actor: str,
        reason: str,
        occurred_at: datetime,
        metadata: Mapping[str, Any] | None = None,
    ) -> LedgerResult:
        return self._settle_reservation(
            order_id=order_id, event_key=event_key, actor=actor, reason=reason,
            occurred_at=occurred_at, target_status="captured", available_delta=0,
            reserved_delta=None, kind="capture", metadata=metadata,
        )

    def release(
        self,
        *,
        order_id: str,
        event_key: str,
        actor: str,
        reason: str,
        occurred_at: datetime,
        metadata: Mapping[str, Any] | None = None,
    ) -> LedgerResult:
        return self._settle_reservation(
            order_id=order_id, event_key=event_key, actor=actor, reason=reason,
            occurred_at=occurred_at, target_status="released", available_delta=None,
            reserved_delta=None, kind="release", metadata=metadata,
        )

    def refund(
        self,
        *,
        order_id: str,
        event_key: str,
        actor: str,
        reason: str,
        occurred_at: datetime,
        metadata: Mapping[str, Any] | None = None,
    ) -> LedgerResult:
        return self._settle_reservation(
            order_id=order_id, event_key=event_key, actor=actor, reason=reason,
            occurred_at=occurred_at, target_status="refunded", available_delta=None,
            reserved_delta=None, kind="refund", metadata=metadata,
        )

    def _settle_reservation(
        self, *, order_id: str, event_key: str, actor: str, reason: str,
        occurred_at: datetime, target_status: str, available_delta: int | None,
        reserved_delta: int | None, kind: str, metadata: Mapping[str, Any] | None,
    ) -> LedgerResult:
        order = _id(order_id, "order_id")
        event = _key(event_key, "event_key")
        current = _time(occurred_at)
        actor_text = _text(actor, "actor", 128)
        reason_text = _text(reason, "reason", 240)
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                replay = _existing_event(connection, event)
                if replay is not None:
                    return self._replay_reservation_by_event(connection, replay, order, kind)
                row = connection.execute(
                    "SELECT * FROM game_order_reservations WHERE order_id = ?", (order,)
                ).fetchone()
                if row is None:
                    raise LedgerConflictError("order has no customer reservation")
                reservation = _reservation_from_row(row)
                if reservation.status == target_status:
                    raise LedgerConflictError("reservation already has this terminal status")
                allowed = {
                    "captured": {"reserved"},
                    "released": {"reserved"},
                    "refunded": {"captured"},
                }[target_status]
                if reservation.status not in allowed:
                    raise LedgerConflictError(
                        f"cannot {kind} reservation from {reservation.status}"
                    )
                if kind in {'refund','release'} and connection.execute(
                        'SELECT 1 FROM game_bank_refund_intents WHERE order_id=?', (order,)).fetchone():
                    raise LedgerConflictError('order is committed to bank reimbursement; wallet credit is blocked')
                account = connection.execute(
                    "SELECT * FROM game_customer_accounts WHERE customer_id = ?",
                    (reservation.customer_id,),
                ).fetchone()
                if account is None:
                    raise LedgerOperationError("customer account is missing for reservation")
                available = int(account["available_minor"])
                reserved = int(account["reserved_minor"])
                if target_status == "released":
                    available += reservation.amount_minor
                    reserved -= reservation.amount_minor
                    delta_available, delta_reserved = reservation.amount_minor, -reservation.amount_minor
                elif target_status == "captured":
                    reserved -= reservation.amount_minor
                    delta_available, delta_reserved = 0, -reservation.amount_minor
                else:  # refund
                    available += reservation.amount_minor
                    delta_available, delta_reserved = reservation.amount_minor, 0
                if reserved < 0:
                    raise LedgerOperationError("reservation balance invariant failed")
                connection.execute(
                    "UPDATE game_customer_accounts SET available_minor = ?, reserved_minor = ?, updated_at = ? WHERE customer_id = ?",
                    (available, reserved, current.isoformat(), reservation.customer_id),
                )
                connection.execute(
                    "UPDATE game_order_reservations SET status = ?, updated_at = ? WHERE order_id = ? AND status = ?",
                    (target_status, current.isoformat(), order, reservation.status),
                )
                self._insert_entry(
                    connection, event, reservation.customer_id, order, reservation.currency, kind,
                    delta_available, delta_reserved, available, reserved, actor_text, reason_text,
                    metadata, current,
                )
                return LedgerResult(
                    _balance_from_values(reservation.customer_id, reservation.currency, available, reserved, current),
                    Reservation(order, reservation.customer_id, reservation.currency, reservation.amount_minor, target_status, current),
                )
        except (LedgerConflictError, LedgerOperationError):
            raise
        except sqlite3.Error as exc:
            raise LedgerOperationError("reservation settlement failed") from exc

    def authorize_bank_refund(self, order_id, *, authorization, actor, now):
        """Claim the reimbursement route in the same transaction domain as wallet refunds."""
        order_id = _id(order_id, 'order_id')
        actor = _text(actor, 'actor', 128)
        at = _time(now).isoformat()
        encoded = json.dumps(authorization, sort_keys=True, separators=(',', ':'))
        if len(encoded) > 8192:
            raise LedgerInputError('bank refund authorization too large')
        self.initialize()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM game_bank_refund_intents WHERE order_id=?', (order_id,)).fetchone()
            if existing:
                if existing['authorization_json'] != encoded or existing['authorized_by'] != actor:
                    raise LedgerConflictError('a different bank reimbursement is already authorized')
                return False
            reservation = db.execute('SELECT * FROM game_order_reservations WHERE order_id=?', (order_id,)).fetchone()
            if reservation is None or reservation['status'] != 'captured':
                raise LedgerConflictError('bank reimbursement requires captured funds without wallet reimbursement')
            if (reservation['amount_minor'], reservation['currency'], reservation['customer_id']) != (
                    authorization['amount_minor'], authorization['currency'], authorization['customer_id']):
                raise LedgerConflictError('bank authorization differs from the captured obligation')
            db.execute('INSERT INTO game_bank_refund_intents VALUES (?,?,?,?,NULL,NULL,NULL)',
                (order_id, encoded, actor, at))
        return True

    def complete_bank_refund(self, order_id, *, completion, outbound_key, now):
        """Record verified manual payout without creating customer wallet credit."""
        order_id = _id(order_id, 'order_id')
        outbound_key = _key(outbound_key, 'outbound_key')
        encoded = json.dumps(completion, sort_keys=True, separators=(',', ':'))
        self.initialize()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM game_bank_refund_intents WHERE order_id=?', (order_id,)).fetchone()
            if row is None:
                raise LedgerConflictError('bank payout was not authorized')
            if row['completion_json'] is not None:
                if row['completion_json'] != encoded or row['outbound_key'] != outbound_key:
                    raise LedgerConflictError('bank payout completion differs from recorded evidence')
                return False
            self._claim_outbound(db, outbound_key, 'settled:'+order_id)
            try:
                db.execute('UPDATE game_bank_refund_intents SET completion_json=?,completed_at=?,outbound_key=? WHERE order_id=?',
                    (encoded, _time(now).isoformat(), outbound_key, order_id))
            except sqlite3.IntegrityError:
                raise LedgerConflictError('outbound transaction already reimbursed another order') from None
        return True

    @staticmethod
    def _claim_outbound(db, key, owner):
        row=db.execute('SELECT owner FROM game_outbound_refund_claims WHERE outbound_key=?',(key,)).fetchone()
        if row and row['owner']!=owner:
            raise LedgerConflictError('outbound transfer already assigned to another reimbursement')
        db.execute('INSERT OR IGNORE INTO game_outbound_refund_claims VALUES (?,?)',(key,owner))

    def claim_outbound_refund(self, key, owner):
        self.initialize()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._claim_outbound(db,_key(key,'outbound_key'),_key(owner,'owner'))

    def bank_refund(self, order_id):
        self.initialize()
        with self._connect() as db:
            row = db.execute('SELECT * FROM game_bank_refund_intents WHERE order_id=?', (_id(order_id,'order_id'),)).fetchone()
        return None if row is None else dict(row)

    def customer_refund(self, order_id: str, *, customer_id: str) -> dict[str, Any] | None:
        """Project committed reimbursement facts, including cross-store handoffs."""
        self.initialize()
        with self._connect() as db:
            db.execute("BEGIN")
            reservation = db.execute(
                "SELECT * FROM game_order_reservations WHERE order_id=? AND customer_id=?",
                (_id(order_id, "order_id"), _id(customer_id, "customer_id")),
            ).fetchone()
            if reservation is None:
                return None
            bank = db.execute("SELECT * FROM game_bank_refund_intents WHERE order_id=?", (order_id,)).fetchone()
            if bank is not None:
                return {"route": "bank", "status": "completed" if bank["completed_at"] else "pending",
                    "amount_minor": reservation["amount_minor"], "currency": reservation["currency"],
                    "completed_at": bank["completed_at"]}
            if reservation["status"] in {"released", "refunded"}:
                return {"route": "wallet", "status": "completed",
                    "amount_minor": reservation["amount_minor"], "currency": reservation["currency"],
                    "completed_at": reservation["updated_at"]}
        return None

    def _mutate(
        self, *, customer_id: str, currency: str, event_key: str, actor: str, reason: str,
        occurred_at: datetime, kind: str, order_id: str | None, available_delta: int,
        reserved_delta: int, metadata: Mapping[str, Any] | None,
    ) -> LedgerResult:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                replay = _existing_event(connection, event_key)
                if replay is not None:
                    if (
                        replay["customer_id"] != customer_id
                        or replay["kind"] != kind
                        or replay["currency"] != currency
                        or int(replay["available_delta_minor"]) != available_delta
                        or int(replay["reserved_delta_minor"]) != reserved_delta
                    ):
                        raise LedgerConflictError("ledger event key was reused with different facts")
                    account = connection.execute(
                        "SELECT * FROM game_customer_accounts WHERE customer_id = ?", (customer_id,)
                    ).fetchone()
                    if account is None:
                        raise LedgerOperationError("ledger replay account is missing")
                    return LedgerResult(_balance_from_row(account), replayed=True)
                account = _ensure_account(connection, customer_id, currency, occurred_at)
                if account["currency"] != currency:
                    raise LedgerConflictError("customer account currency does not match credit")
                available = int(account["available_minor"]) + available_delta
                reserved = int(account["reserved_minor"]) + reserved_delta
                if available < 0 or reserved < 0:
                    raise LedgerOperationError("ledger balance invariant failed")
                connection.execute(
                    "UPDATE game_customer_accounts SET available_minor = ?, reserved_minor = ?, updated_at = ? WHERE customer_id = ?",
                    (available, reserved, occurred_at.isoformat(), customer_id),
                )
                self._insert_entry(
                    connection, event_key, customer_id, order_id, currency, kind,
                    available_delta, reserved_delta, available, reserved, actor, reason,
                    metadata, occurred_at,
                )
                return LedgerResult(_balance_from_values(customer_id, currency, available, reserved, occurred_at))
        except LedgerConflictError:
            raise
        except sqlite3.Error as exc:
            raise LedgerOperationError("ledger mutation failed") from exc

    def _insert_entry(
        self, connection: sqlite3.Connection, event_key: str, customer_id: str,
        order_id: str | None, currency: str, kind: str, available_delta: int,
        reserved_delta: int, available_after: int, reserved_after: int, actor: str,
        reason: str, metadata: Mapping[str, Any] | None, occurred_at: datetime,
    ) -> None:
        raw_metadata = {} if metadata is None else dict(metadata)
        try:
            encoded = json.dumps(raw_metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise LedgerInputError("ledger metadata must be JSON serializable") from exc
        if len(encoded) > 8192:
            raise LedgerInputError("ledger metadata is too large")
        connection.execute(
            """INSERT INTO game_ledger_entries
               (event_key, customer_id, order_id, currency, kind,
                available_delta_minor, reserved_delta_minor,
                available_after_minor, reserved_after_minor, actor, reason,
                metadata_json, occurred_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_key, customer_id, order_id, currency, kind,
                available_delta, reserved_delta, available_after, reserved_after,
                actor, reason, encoded, occurred_at.isoformat(),
            ),
        )

    def _replay_reservation(
        self, connection: sqlite3.Connection, event: sqlite3.Row, order: str,
        customer: str, amount: int, currency: str,
    ) -> LedgerResult:
        if event["customer_id"] != customer or event["order_id"] != order or event["kind"] != "reserve":
            raise LedgerConflictError("reservation event key was reused with different facts")
        row = connection.execute("SELECT * FROM game_order_reservations WHERE order_id = ?", (order,)).fetchone()
        if row is None:
            raise LedgerOperationError("reservation replay record is missing")
        saved = _reservation_from_row(row)
        if saved.amount_minor != amount or saved.currency != currency:
            raise LedgerConflictError("reservation event key amount or currency changed")
        account = connection.execute("SELECT * FROM game_customer_accounts WHERE customer_id = ?", (customer,)).fetchone()
        if account is None:
            raise LedgerOperationError("reservation replay account is missing")
        return LedgerResult(_balance_from_row(account), saved, True)

    def _replay_reservation_by_event(
        self, connection: sqlite3.Connection, event: sqlite3.Row, order: str, kind: str
    ) -> LedgerResult:
        if event["order_id"] != order or event["kind"] != kind:
            raise LedgerConflictError("settlement event key was reused with different facts")
        row = connection.execute("SELECT * FROM game_order_reservations WHERE order_id = ?", (order,)).fetchone()
        if row is None:
            raise LedgerOperationError("settlement replay reservation is missing")
        reservation = _reservation_from_row(row)
        account = connection.execute("SELECT * FROM game_customer_accounts WHERE customer_id = ?", (reservation.customer_id,)).fetchone()
        if account is None:
            raise LedgerOperationError("settlement replay account is missing")
        return LedgerResult(_balance_from_row(account), reservation, True)

    def _connect(self) -> sqlite3.Connection:
        return open_sqlite_connection(self.path, timeout_seconds=5)

    @staticmethod
    def _validate_amount(value: object) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 2_147_483_647:
            raise LedgerInputError("amount_minor must be a positive integer")


def _ensure_account(connection: sqlite3.Connection, customer: str, currency: str, now: datetime) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM game_customer_accounts WHERE customer_id = ?", (customer,)).fetchone()
    if row is not None:
        return row
    connection.execute(
        "INSERT INTO game_customer_accounts(customer_id, currency, available_minor, reserved_minor, created_at, updated_at) VALUES (?, ?, 0, 0, ?, ?)",
        (customer, currency, now.isoformat(), now.isoformat()),
    )
    return connection.execute("SELECT * FROM game_customer_accounts WHERE customer_id = ?", (customer,)).fetchone()


def _existing_event(connection: sqlite3.Connection, event_key: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM game_ledger_entries WHERE event_key = ?", (event_key,)).fetchone()


def _balance_from_row(row: sqlite3.Row) -> AccountBalance:
    return _balance_from_values(
        str(row["customer_id"]), str(row["currency"]), int(row["available_minor"]),
        int(row["reserved_minor"]), _time(datetime.fromisoformat(str(row["updated_at"]))),
    )


def _balance_from_values(customer: str, currency: str, available: int, reserved: int, at: datetime) -> AccountBalance:
    return AccountBalance(customer, currency, available, reserved, at.astimezone(UTC))


def _reservation_from_row(row: sqlite3.Row) -> Reservation:
    return Reservation(
        order_id=str(row["order_id"]), customer_id=str(row["customer_id"]),
        currency=str(row["currency"]), amount_minor=int(row["amount_minor"]),
        status=str(row["status"]), updated_at=_time(datetime.fromisoformat(str(row["updated_at"]))),
    )


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise LedgerInputError(f"{label} is invalid")
    if any(char in value for char in "\r\n"):
        raise LedgerInputError(f"{label} is invalid")
    return value.strip()


def _key(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise LedgerInputError(f"{label} is invalid")
    if any(char in value for char in "\r\n"):
        raise LedgerInputError(f"{label} is invalid")
    return value.strip()


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise LedgerInputError(f"{label} is invalid")
    if any(char in value for char in "\r\n"):
        raise LedgerInputError(f"{label} is invalid")
    return value.strip()


def _currency(value: object) -> str:
    if not isinstance(value, str) or len(value) != 3 or not value.isalpha() or value.upper() != value:
        raise LedgerInputError("currency is invalid")
    return value


def _time(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise LedgerInputError("occurred_at must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "AccountBalance",
    "CustomerLedger",
    "InsufficientFundsError",
    "LedgerConflictError",
    "LedgerInputError",
    "LedgerOperationError",
    "LedgerResult",
    "Reservation",
]
