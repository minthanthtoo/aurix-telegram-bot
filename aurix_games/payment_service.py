"""Customer payment boundary for Games orders.

This service separates payment evidence from supplier-wallet funding.  A
trusted payment verifier must call ``record_verified_credit`` explicitly;
customer requests, screenshots, and supplier balance reads cannot credit an
account.  Order reservations and ledger entries are idempotent, while the
order repository remains the source of customer-visible state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from persistence import open_sqlite_connection
from .customer_ledger import CustomerLedger, LedgerConflictError, LedgerResult
from .order_repository import GameOrderRepository
from .order_state import GameOrder, OrderState


class PaymentBoundaryError(ValueError):
    """A payment operation cannot safely advance an order."""


@dataclass(frozen=True)
class PaymentOrderResult:
    order: GameOrder
    ledger: LedgerResult


class CustomerPaymentService:
    """Compose explicit payment evidence, ledger reservations, and order state."""

    def __init__(self, repository: GameOrderRepository, ledger: CustomerLedger) -> None:
        self.repository = repository
        self.ledger = ledger

    def record_verified_credit(
        self,
        *,
        customer_id: str,
        amount_minor: int,
        currency: str,
        event_key: str,
        actor: str,
        reason: str,
        occurred_at: datetime,
        metadata: dict[str, object] | None = None,
    ) -> LedgerResult:
        """Credit only after an independent payment verifier approves evidence."""

        return self.ledger.credit(
            customer_id=customer_id,
            amount_minor=amount_minor,
            currency=currency,
            event_key=event_key,
            actor=actor,
            reason=reason,
            occurred_at=occurred_at,
            metadata=metadata,
        )

    def reserve_order(
        self,
        order_id: str,
        *,
        event_key: str,
        actor: str,
        reason: str,
        occurred_at: datetime,
        _verified_transfer_key: str | None = None,
    ) -> PaymentOrderResult:
        order = self.repository.get(order_id)
        existing_only = order.state not in {OrderState.AWAITING_PAYMENT, OrderState.PAID_RESERVED}
        if existing_only and _verified_transfer_key is None:
            raise PaymentBoundaryError(
                f"order is not awaiting payment: {order.state.value}"
            )
        existing_event = None
        if _verified_transfer_key is not None:
            existing_event = self.ledger.event_by_key(_verified_transfer_key)
            if existing_event is not None and (
                existing_event["kind"] != "verified_transfer"
                or existing_event["order_id"] != order.order_id
                or existing_event["customer_id"] != order.customer_id
                or existing_event["currency"] != order.snapshot.currency
                or existing_event["available_delta_minor"] != order.snapshot.amount_minor
            ):
                raise PaymentBoundaryError("verified transfer is already allocated")
            if existing_only and existing_event is None:
                order_claim = self.transfer_identity_claim_for_order(order.order_id)
                if order_claim is not None:
                    with open_sqlite_connection(self.repository.path) as db:
                        db.execute("BEGIN IMMEDIATE")
                        self.claim_transfer_identity(db, order_id=order.order_id,
                            transfer_key=_verified_transfer_key, resolution="settlement", revision=None,
                            actor=actor, at=datetime.now(timezone.utc))
                raise PaymentBoundaryError(f"order cannot accept settlement from {order.state.value}")
            self.initialize_transfer_identity_claims()
            with open_sqlite_connection(self.repository.path) as db:
                db.execute("BEGIN IMMEDIATE")
                self.claim_transfer_identity(db, order_id=order.order_id,
                    transfer_key=_verified_transfer_key, resolution="settlement", revision=None,
                    actor=actor, at=datetime.now(timezone.utc))
        ledger = self.ledger.reserve(
            order_id=order.order_id,
            customer_id=order.customer_id,
            amount_minor=order.snapshot.amount_minor,
            currency=order.snapshot.currency,
            event_key=event_key,
            actor=actor,
            reason=reason,
            occurred_at=occurred_at,
            verified_transfer_key=_verified_transfer_key,
            existing_only=existing_only,
        )
        if order.state == OrderState.AWAITING_PAYMENT:
            if ledger.reservation is None or ledger.reservation.status != "reserved":
                raise PaymentBoundaryError("terminal reservation requires operator review")
            try:
                order = self.repository.transition(
                    order.order_id,
                    OrderState.PAID_RESERVED,
                    at=occurred_at,
                    reason="verified payment reserved customer funds",
                )
            except Exception as exc:
                # A concurrent caller or a lost response may already have advanced the order.
                order = self.repository.get(order.order_id)
                if order.state != OrderState.PAID_RESERVED:
                    raise PaymentBoundaryError("reservation retained; retry the same payment intent") from exc
        return PaymentOrderResult(order, ledger)

    def settle_verified_transfer(
        self, order_id: str, *, rail: str, receiver_id: str, transaction_id: str,
        amount_minor: int, currency: str, actor: str, occurred_at: datetime,
    ) -> PaymentOrderResult:
        """Accept an exact transfer already matched to this order by a trusted verifier."""
        key = self.validate_verified_transfer(order_id, rail=rail, receiver_id=receiver_id,
            transaction_id=transaction_id, amount_minor=amount_minor, currency=currency,
            occurred_at=occurred_at)
        return self.reserve_order(order_id, event_key=f"settle:{order_id}", actor=actor,
            reason="verified order-bound transfer", occurred_at=occurred_at, _verified_transfer_key=key)

    def validate_verified_transfer(
        self, order_id: str, *, rail: str, receiver_id: str, transaction_id: str,
        amount_minor: int, currency: str, occurred_at: datetime,
    ) -> str:
        """Validate frozen instructions without allocating funds or changing state."""
        if any(not isinstance(value, str) or not value or value != value.strip()
               or len(value) > 128 or any(ord(c) < 32 for c in value)
               for value in (rail, receiver_id, transaction_id)):
            raise PaymentBoundaryError("verified settlement identity is invalid")
        order = self.repository.get(order_id)
        if type(amount_minor) is not int or (amount_minor, currency) != (order.snapshot.amount_minor, order.snapshot.currency):
            raise PaymentBoundaryError("verified settlement amount or currency does not match the order")
        intent = self.repository.get_payment_intent(order_id)
        if intent is None or (rail.casefold(), receiver_id) != (intent["rail"], intent["receiver_id"]):
            raise PaymentBoundaryError("verified settlement receiver does not match the order")
        if (not isinstance(occurred_at, datetime) or occurred_at.utcoffset() is None
                or not datetime.fromisoformat(intent["opened_at"]) <= occurred_at < datetime.fromisoformat(intent["expires_at"])):
            raise PaymentBoundaryError("late settlement requires operator review")
        key = self.transfer_identity_key(rail=rail, receiver_id=receiver_id,
            transaction_id=transaction_id, currency=currency)
        if self._external_refund_owner(key) is not None:
            raise PaymentBoundaryError("transfer is already assigned to a refund review")
        claim = self.transfer_identity_claim(key)
        if claim is not None and (claim["order_id"] != order_id or claim["resolution"] != "settlement"):
            raise LedgerConflictError("transfer is already assigned to another payment resolution")
        return key

    def validate_refundable_transfer(
        self, order_id: str, *, rail: str, receiver_id: str, transaction_id: str,
        amount_minor: int, currency: str, occurred_at: datetime, now: datetime,
    ) -> str:
        """Validate inbound merchant evidence for a manually executed full refund.

        Unlike settlement, this accepts any positive amount and transfers after
        intent expiry. It still requires the frozen receiver, order currency, a
        timestamp no earlier than the intent, and a transfer that is not in the
        future. It never credits or reserves customer funds.
        """
        if any(not isinstance(value, str) or not value or value != value.strip()
               or len(value) > 128 or any(ord(c) < 32 for c in value)
               for value in (rail, receiver_id, transaction_id)):
            raise PaymentBoundaryError("verified settlement identity is invalid")
        if type(amount_minor) is not int or not 0 < amount_minor <= 9_007_199_254_740_991:
            raise PaymentBoundaryError("invalid received amount")
        if not isinstance(currency, str) or not currency or currency != currency.strip():
            raise PaymentBoundaryError("invalid transfer currency")
        if (not isinstance(occurred_at, datetime) or occurred_at.utcoffset() is None
                or not isinstance(now, datetime) or now.utcoffset() is None
                or occurred_at > now):
            raise PaymentBoundaryError("verified transfer time is invalid")
        order = self.repository.get(order_id)
        if currency != order.snapshot.currency:
            raise PaymentBoundaryError("verified transfer currency does not match the order")
        intent = self.repository.get_payment_intent(order_id)
        if intent is None or (rail.casefold(), receiver_id) != (intent["rail"], intent["receiver_id"]):
            raise PaymentBoundaryError("verified settlement receiver does not match the order")
        if occurred_at < datetime.fromisoformat(intent["opened_at"]):
            raise PaymentBoundaryError("verified transfer predates the order payment instructions")
        key = self.transfer_identity_key(rail=rail, receiver_id=receiver_id,
            transaction_id=transaction_id, currency=currency)
        if self.ledger.contains_event_key(key):
            raise PaymentBoundaryError("merchant transfer was already allocated")
        refund_order = self._external_refund_owner(key)
        if refund_order is not None and refund_order != order_id:
            raise PaymentBoundaryError("merchant transfer already has a refund resolution")
        claim = self.transfer_identity_claim(key)
        if claim is not None and (claim["order_id"] != order_id or claim["resolution"] != "refund"):
            raise PaymentBoundaryError("merchant transfer already has another payment resolution")
        return key

    @staticmethod
    def transfer_identity_key(*, rail: str, receiver_id: str, transaction_id: str, currency: str) -> str:
        """Return the same global identity used by settlement deduplication."""
        identity = json.dumps([rail.casefold(), receiver_id, currency, transaction_id], separators=(",", ":"))
        return "transfer:" + hashlib.sha256(identity.encode()).hexdigest()

    def _external_refund_owner(self, transfer_key: str) -> str | None:
        """Prevent a later order settlement from consuming a refunded transfer."""
        with open_sqlite_connection(self.repository.path) as db:
            table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                ("game_payment_external_refund_authorizations",)).fetchone()
            if table is None:
                return None
            row = db.execute("SELECT order_id FROM game_payment_external_refund_authorizations WHERE incoming_transfer_key=?",
                (transfer_key,)).fetchone()
            return None if row is None else str(row["order_id"])

    def initialize_transfer_identity_claims(self) -> None:
        """Create the shared, order-database identity lock for settlement/refund races."""
        self.repository.initialize()
        with open_sqlite_connection(self.repository.path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS game_payment_transfer_identity_claims (
                    incoming_transfer_key TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL UNIQUE REFERENCES game_orders(order_id),
                    resolution TEXT NOT NULL CHECK(resolution IN ('settlement','refund')),
                    revision INTEGER,
                    claimed_by TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    CHECK((resolution='settlement' AND revision IS NULL) OR
                          (resolution='refund' AND revision BETWEEN 1 AND 10))
                );
                CREATE INDEX IF NOT EXISTS game_payment_transfer_claim_order
                    ON game_payment_transfer_identity_claims(order_id);
                CREATE TRIGGER IF NOT EXISTS game_payment_transfer_claim_immutable_update
                BEFORE UPDATE ON game_payment_transfer_identity_claims
                BEGIN SELECT RAISE(ABORT, 'payment transfer claims are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS game_payment_transfer_claim_immutable_delete
                BEFORE DELETE ON game_payment_transfer_identity_claims
                BEGIN SELECT RAISE(ABORT, 'payment transfer claims are retained'); END;
            """)

    def transfer_identity_claim(self, transfer_key: str) -> dict | None:
        with open_sqlite_connection(self.repository.path) as db:
            table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                ("game_payment_transfer_identity_claims",)).fetchone()
            if table is None:
                return None
            row = db.execute("SELECT * FROM game_payment_transfer_identity_claims WHERE incoming_transfer_key=?",
                (transfer_key,)).fetchone()
            return None if row is None else dict(row)

    def transfer_identity_claim_for_order(self, order_id: str) -> dict | None:
        with open_sqlite_connection(self.repository.path) as db:
            table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                ("game_payment_transfer_identity_claims",)).fetchone()
            if table is None:
                return None
            row = db.execute("SELECT * FROM game_payment_transfer_identity_claims WHERE order_id=?",
                (order_id,)).fetchone()
            return None if row is None else dict(row)

    @staticmethod
    def claim_transfer_identity(db, *, order_id: str, transfer_key: str, resolution: str,
                                revision: int | None, actor: str, at: datetime) -> bool:
        """Claim a merchant transfer within an existing order-database transaction."""
        if resolution not in {"settlement", "refund"}:
            raise PaymentBoundaryError("invalid transfer resolution")
        if not isinstance(at, datetime) or at.utcoffset() is None:
            raise PaymentBoundaryError("transfer claim time must be timezone-aware")
        by_key = db.execute("SELECT * FROM game_payment_transfer_identity_claims WHERE incoming_transfer_key=?",
            (transfer_key,)).fetchone()
        by_order = db.execute("SELECT * FROM game_payment_transfer_identity_claims WHERE order_id=?",
            (order_id,)).fetchone()
        existing = by_key or by_order
        if existing is not None:
            if by_key is not None and by_order is not None and by_key["incoming_transfer_key"] != by_order["incoming_transfer_key"]:
                raise LedgerConflictError("order and transfer already have different payment resolutions")
            if (existing["incoming_transfer_key"], existing["order_id"], existing["resolution"], existing["revision"]) == (
                transfer_key, order_id, resolution, revision
            ):
                return False
            raise LedgerConflictError("merchant transfer already has a different payment resolution")
        try:
            db.execute("""INSERT INTO game_payment_transfer_identity_claims
                (incoming_transfer_key, order_id, resolution, revision, claimed_by, claimed_at)
                VALUES(?,?,?,?,?,?)""", (transfer_key, order_id, resolution, revision, actor,
                at.astimezone(timezone.utc).isoformat()))
        except sqlite3.IntegrityError:
            raise LedgerConflictError("merchant transfer could not be claimed") from None
        return True

    def capture_order(
        self,
        order_id: str,
        *,
        event_key: str,
        actor: str,
        reason: str,
        occurred_at: datetime,
    ) -> LedgerResult:
        order = self.repository.get(order_id)
        if order.state not in {
            OrderState.SUBMITTED,
            OrderState.PROCESSING,
            OrderState.DELIVERED,
            OrderState.FAILED,
            OrderState.RECONCILIATION,
        } or not order.provider_order_id:
            raise PaymentBoundaryError(
                f"order cannot capture funds from {order.state.value}"
            )
        return self.ledger.capture(
            order_id=order.order_id,
            event_key=event_key,
            actor=actor,
            reason=reason,
            occurred_at=occurred_at,
        )

    def release_order(
        self,
        order_id: str,
        *,
        event_key: str,
        actor: str,
        reason: str,
        occurred_at: datetime,
    ) -> PaymentOrderResult:
        order = self.repository.get(order_id)
        if order.state not in {
            OrderState.PAID_RESERVED,
            OrderState.REVIEW_REFUND,
            OrderState.REFUNDED,
        }:
            raise PaymentBoundaryError(
                f"order cannot release reserved funds from {order.state.value}"
            )
        if order.state == OrderState.PAID_RESERVED:
            order = self.repository.transition(
                order.order_id, OrderState.REVIEW_REFUND, at=occurred_at,
                reason="reserved funds approved for release",
            )
        ledger = self.ledger.release(
            order_id=order.order_id,
            event_key=event_key,
            actor=actor,
            reason=reason,
            occurred_at=occurred_at,
        )
        refunded = order if order.state == OrderState.REFUNDED else self.repository.transition(
            order.order_id,
            OrderState.REFUNDED,
            at=occurred_at,
            reason="reserved customer funds returned",
        )
        return PaymentOrderResult(refunded, ledger)

    def refund_captured_order(
        self,
        order_id: str,
        *,
        event_key: str,
        actor: str,
        reason: str,
        occurred_at: datetime,
    ) -> PaymentOrderResult:
        order = self.repository.get(order_id)
        if order.state not in {OrderState.REVIEW_REFUND, OrderState.REFUNDED}:
            raise PaymentBoundaryError(
                f"captured refund requires review_refund state, got {order.state.value}"
            )
        ledger = self.ledger.refund(
            order_id=order.order_id,
            event_key=event_key,
            actor=actor,
            reason=reason,
            occurred_at=occurred_at,
        )
        refunded = order if order.state == OrderState.REFUNDED else self.repository.transition(
            order.order_id,
            OrderState.REFUNDED,
            at=occurred_at,
            reason="captured customer funds returned after review",
        )
        return PaymentOrderResult(refunded, ledger)


__all__ = ["CustomerPaymentService", "PaymentBoundaryError", "PaymentOrderResult"]
