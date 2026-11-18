"""Private, durable review cases; customer claims never credit the ledger."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

from persistence import open_sqlite_connection
from .customer_ledger import LedgerConflictError
from .order_state import OrderState, transition_order
from .payment_service import CustomerPaymentService

MAX_RECEIPT_BYTES = 5 * 1024 * 1024
# Base64 expansion plus bounded transaction/revision JSON metadata.
MAX_RECEIPT_BODY_BYTES = 4 * ((MAX_RECEIPT_BYTES + 2) // 3) + 16 * 1024


class PaymentReviewError(ValueError):
    pass


class PaymentReviewerAuthError(PaymentReviewError):
    pass


@dataclass(frozen=True)
class StaffPaymentReviewer:
    """In-process credential issued only after a store session and role check."""

    actor: str


def _text(value: str, name: str, maximum: int = 128) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > maximum or any(ord(c) < 32 for c in value)):
        raise PaymentReviewError(f"invalid {name}")
    return value


def _time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise PaymentReviewError("timezone-aware time required")
    return value.astimezone(timezone.utc).isoformat()


class PaymentReviewService:
    """Record intent before settlement, then safely resume the frozen decision."""

    def __init__(self, payments: CustomerPaymentService, reviewers: dict[str, str], *, evidence_key: str | bytes | None = None):
        self.payments = payments
        self.repository = payments.repository
        if not isinstance(reviewers, dict):
            raise PaymentReviewError("invalid reviewer configuration")
        for actor, digest in reviewers.items():
            if (not isinstance(actor, str) or not re.fullmatch(r"staff:[A-Za-z0-9._-]{1,64}", actor)
                    or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
                raise PaymentReviewError("invalid reviewer configuration")
        if len(set(reviewers.values())) != len(reviewers):
            raise PaymentReviewError("reviewers must use distinct credentials")
        self.reviewers = dict(reviewers)
        self.evidence_cipher = None if evidence_key is None else Fernet(evidence_key)

    def authenticate(self, token: str | StaffPaymentReviewer) -> str:
        if isinstance(token, StaffPaymentReviewer):
            return token.actor
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise PaymentReviewerAuthError("reviewer authentication failed")
        digest = hashlib.sha256(token.encode()).hexdigest()
        for actor, expected in self.reviewers.items():
            if hmac.compare_digest(digest, expected):
                return actor
        raise PaymentReviewerAuthError("reviewer authentication failed")

    def _initialize(self):
        self.repository.initialize()
        with open_sqlite_connection(self.repository.path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS game_payment_reviews (
                    order_id TEXT PRIMARY KEY REFERENCES game_orders(order_id),
                    claim_json TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    decision_json TEXT,
                    decided_at TEXT,
                    applied_at TEXT
                );
                CREATE TABLE IF NOT EXISTS game_payment_receipts (
                    order_id TEXT PRIMARY KEY REFERENCES game_payment_reviews(order_id),
                    encrypted_content BLOB NOT NULL,
                    sha256 TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS game_payment_receipt_immutable
                BEFORE UPDATE ON game_payment_receipts
                BEGIN SELECT RAISE(ABORT, 'payment receipts are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS game_payment_receipt_no_delete
                BEFORE DELETE ON game_payment_receipts
                BEGIN SELECT RAISE(ABORT, 'payment receipts are retained'); END;
                CREATE TRIGGER IF NOT EXISTS game_payment_review_claim_immutable
                BEFORE UPDATE OF order_id, claim_json, submitted_at ON game_payment_reviews
                BEGIN SELECT RAISE(ABORT, 'payment claims are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS game_payment_review_decision_immutable
                BEFORE UPDATE OF decision_json, decided_at ON game_payment_reviews
                WHEN OLD.decision_json IS NOT NULL
                BEGIN SELECT RAISE(ABORT, 'payment decisions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS game_payment_review_no_delete
                BEFORE DELETE ON game_payment_reviews
                BEGIN SELECT RAISE(ABORT, 'payment reviews are retained'); END;
            """)

        with open_sqlite_connection(self.repository.path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS game_payment_review_cases (
                    order_id TEXT NOT NULL REFERENCES game_orders(order_id),
                    revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 10),
                    claim_json TEXT NOT NULL, encrypted_content BLOB,
                    submitted_at TEXT NOT NULL, decision_json TEXT, decided_at TEXT, applied_at TEXT,
                    PRIMARY KEY(order_id, revision)
                );
                CREATE INDEX IF NOT EXISTS game_review_cases_pending
                    ON game_payment_review_cases(submitted_at, order_id, revision) WHERE applied_at IS NULL;
                CREATE TRIGGER IF NOT EXISTS game_review_case_evidence_immutable
                BEFORE UPDATE OF order_id, revision, claim_json, encrypted_content, submitted_at ON game_payment_review_cases
                BEGIN SELECT RAISE(ABORT, 'review evidence is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS game_review_case_decision_immutable
                BEFORE UPDATE OF decision_json, decided_at ON game_payment_review_cases
                WHEN OLD.decision_json IS NOT NULL
                BEGIN SELECT RAISE(ABORT, 'review decision is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS game_review_case_applied_immutable
                BEFORE UPDATE OF applied_at ON game_payment_review_cases WHEN OLD.applied_at IS NOT NULL
                BEGIN SELECT RAISE(ABORT, 'review completion is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS game_review_case_no_delete
                BEFORE DELETE ON game_payment_review_cases
                BEGIN SELECT RAISE(ABORT, 'review history is retained'); END;
                CREATE TABLE IF NOT EXISTS game_payment_external_refund_authorizations (
                    order_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    incoming_transfer_key TEXT NOT NULL UNIQUE,
                    incoming_rail TEXT NOT NULL,
                    incoming_receiver_id TEXT NOT NULL,
                    incoming_transaction_id TEXT NOT NULL,
                    incoming_amount_minor INTEGER NOT NULL CHECK(incoming_amount_minor > 0),
                    currency TEXT NOT NULL,
                    incoming_occurred_at TEXT NOT NULL,
                    refund_rail TEXT NOT NULL,
                    original_sender_confirmed INTEGER NOT NULL CHECK(original_sender_confirmed = 1),
                    authorized_by TEXT NOT NULL,
                    authorized_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    PRIMARY KEY(order_id, revision),
                    FOREIGN KEY(order_id, revision) REFERENCES game_payment_review_cases(order_id, revision)
                );
                CREATE INDEX IF NOT EXISTS game_payment_refund_queue
                    ON game_payment_external_refund_authorizations(authorized_at, order_id, revision);
                CREATE TRIGGER IF NOT EXISTS game_external_refund_authorization_immutable_update
                BEFORE UPDATE ON game_payment_external_refund_authorizations
                BEGIN SELECT RAISE(ABORT, 'refund authorizations are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS game_external_refund_authorization_immutable_delete
                BEFORE DELETE ON game_payment_external_refund_authorizations
                BEGIN SELECT RAISE(ABORT, 'refund authorizations are retained'); END;
                CREATE TABLE IF NOT EXISTS game_payment_external_refund_completions (
                    order_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    refund_transfer_key TEXT NOT NULL UNIQUE,
                    refund_transaction_id TEXT NOT NULL,
                    refund_amount_minor INTEGER NOT NULL CHECK(refund_amount_minor > 0),
                    currency TEXT NOT NULL,
                    refunded_at TEXT NOT NULL,
                    completed_by TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY(order_id, revision),
                    FOREIGN KEY(order_id, revision) REFERENCES game_payment_external_refund_authorizations(order_id, revision)
                );
                CREATE TRIGGER IF NOT EXISTS game_external_refund_completion_immutable_update
                BEFORE UPDATE ON game_payment_external_refund_completions
                BEGIN SELECT RAISE(ABORT, 'refund completions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS game_external_refund_completion_immutable_delete
                BEFORE DELETE ON game_payment_external_refund_completions
                BEGIN SELECT RAISE(ABORT, 'refund completions are retained'); END;
                CREATE TRIGGER IF NOT EXISTS game_external_refund_completion_matches_authorization
                BEFORE INSERT ON game_payment_external_refund_completions
                WHEN NOT EXISTS (
                    SELECT 1 FROM game_payment_external_refund_authorizations a
                    WHERE a.order_id=NEW.order_id AND a.revision=NEW.revision
                      AND a.incoming_amount_minor=NEW.refund_amount_minor
                      AND a.currency=NEW.currency
                      AND a.incoming_transaction_id<>NEW.refund_transaction_id
                      AND julianday(NEW.refunded_at)>=julianday(a.authorized_at)
                )
                BEGIN SELECT RAISE(ABORT, 'refund completion does not match its authorization'); END;
            """)
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT OR IGNORE INTO game_payment_review_cases
                SELECT c.order_id, 1, c.claim_json, r.encrypted_content, c.submitted_at,
                    c.decision_json, c.decided_at, c.applied_at FROM game_payment_reviews c
                LEFT JOIN game_payment_receipts r USING(order_id)""")

    @staticmethod
    def _case(db, order_id, revision=None):
        if revision is not None and (type(revision) is not int or revision < 1):
            raise PaymentReviewError("invalid review revision")
        if revision is None:
            return db.execute("SELECT * FROM game_payment_review_cases WHERE order_id=? ORDER BY revision DESC LIMIT 1", (order_id,)).fetchone()
        return db.execute("SELECT * FROM game_payment_review_cases WHERE order_id=? AND revision=?", (order_id, revision)).fetchone()

    def submit(self, order_id: str, *, customer_id: str, transaction_reference: str,
               evidence_sha256: str, now: datetime, receipt: bytes | None = None,
               expected_revision: int | None = None) -> dict:
        customer_id = _text(customer_id, "customer identity")
        reference = _text(transaction_reference, "transaction reference")
        if not isinstance(evidence_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", evidence_sha256):
            raise PaymentReviewError("invalid evidence SHA-256")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise PaymentReviewError("invalid expected revision")
        encrypted = None
        if receipt is not None:
            if self.evidence_cipher is None:
                raise PaymentReviewError("receipt storage is not configured")
            if not isinstance(receipt, bytes) or not 1 <= len(receipt) <= MAX_RECEIPT_BYTES:
                raise PaymentReviewError("receipt must be between 1 byte and 5 MiB")
            if not receipt.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"%PDF-")):
                raise PaymentReviewError("receipt must have a PNG, JPEG or PDF signature")
            if hashlib.sha256(receipt).hexdigest() != evidence_sha256:
                raise PaymentReviewError("receipt digest mismatch")
            encrypted = self.evidence_cipher.encrypt(receipt)
        submitted = _time(now)
        self.repository.get(order_id, customer_id=customer_id)
        claim = json.dumps({"transaction_reference": reference, "evidence_sha256": evidence_sha256}, sort_keys=True)
        self._initialize()
        with open_sqlite_connection(self.repository.path) as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._case(db, order_id)
            if current and current["claim_json"] == claim and (receipt is None or current["encrypted_content"] is not None):
                return {"order_id": order_id, "claim_recorded": True, "revision": current["revision"]}
            order = db.execute("SELECT state, customer_id FROM game_orders WHERE order_id=?", (order_id,)).fetchone()
            if order["customer_id"] != customer_id or order["state"] != OrderState.AWAITING_PAYMENT.value:
                raise PaymentReviewError("order is not awaiting payment")
            if db.execute("SELECT 1 FROM game_order_payment_intents WHERE order_id=?", (order_id,)).fetchone() is None:
                raise PaymentReviewError("order has no payment intent")
            revision = 1
            if current:
                if expected_revision != current["revision"]:
                    raise PaymentReviewError("review changed; reload before correcting evidence")
                if current["decision_json"]:
                    decision = json.loads(current["decision_json"])
                    if (decision["approve"] or not current["applied_at"]
                            or decision.get("resolution") == "external_refund_authorized"):
                        raise PaymentReviewError("a committed decision must finish before correction")
                revision = current["revision"] + 1
                if revision > 10:
                    raise PaymentReviewError("receipt correction limit reached; contact support")
            elif expected_revision is not None:
                raise PaymentReviewError("no previous submission exists")
            db.execute("INSERT INTO game_payment_review_cases(order_id, revision, claim_json, encrypted_content, submitted_at) VALUES(?,?,?,?,?)",
                       (order_id, revision, claim, encrypted, submitted))
        return {"order_id": order_id, "claim_recorded": True, "revision": revision}

    def submit_receipt(self, order_id: str, *, customer_id: str, transaction_reference: str,
                       receipt: bytes, now: datetime, expected_revision: int | None = None) -> dict:
        if not isinstance(receipt, bytes):
            raise PaymentReviewError("receipt bytes required")
        return self.submit(order_id, customer_id=customer_id, transaction_reference=transaction_reference,
                           evidence_sha256=hashlib.sha256(receipt).hexdigest(), now=now, receipt=receipt,
                           expected_revision=expected_revision)

    def receipt(self, order_id: str, *, token: str, revision: int | None = None) -> bytes:
        self.authenticate(token)
        if self.evidence_cipher is None:
            raise PaymentReviewError("receipt storage is not configured")
        self._initialize()
        with open_sqlite_connection(self.repository.path) as db:
            row = self._case(db, order_id, revision)
        if row is None or row["encrypted_content"] is None:
            raise PaymentReviewError("receipt not found")
        try:
            content = self.evidence_cipher.decrypt(row["encrypted_content"])
        except InvalidToken:
            raise PaymentReviewError("receipt cannot be authenticated") from None
        if hashlib.sha256(content).hexdigest() != json.loads(row["claim_json"])["evidence_sha256"]:
            raise PaymentReviewError("receipt integrity check failed")
        return content

    def customer_status(self, order_id: str, *, customer_id: str,
                        connection: sqlite3.Connection | None = None) -> dict:
        if connection is None:
            self._initialize()
        with (nullcontext(connection) if connection is not None else open_sqlite_connection(self.repository.path)) as db:
            if connection is None:
                db.execute("BEGIN")
            order = self.repository.get(order_id, customer_id=_text(customer_id, "customer identity"), connection=db)
            row = self._case(db, order_id)
            refund = db.execute("""SELECT a.refund_rail, a.incoming_amount_minor, a.currency,
                    c.refunded_at, c.order_id AS completed
                FROM game_payment_external_refund_authorizations a
                LEFT JOIN game_payment_external_refund_completions c USING(order_id, revision)
                WHERE a.order_id=? ORDER BY a.revision DESC LIMIT 1""", (order_id,)).fetchone()
        status, can_correct = "not_submitted", False
        if row is not None:
            status = "in_review"
            decision = json.loads(row["decision_json"]) if row["decision_json"] else None
            if refund is not None:
                status = "refunded" if refund["completed"] else "refund_pending"
            elif row["applied_at"]:
                status = "verified" if decision["approve"] else "not_verified"
            can_correct = (refund is None and row["revision"] < 10
                and (decision is None or (not decision["approve"]
                    and decision.get("resolution") != "external_refund_authorized"
                    and bool(row["applied_at"]))))
        result = {"order_id": order_id, "receipt_upload_enabled": self.evidence_cipher is not None,
                "receipt_max_bytes": MAX_RECEIPT_BYTES,
                "review_status": status, "revision": row["revision"] if row else None,
                "can_correct": can_correct and order.state == OrderState.AWAITING_PAYMENT}
        if refund is not None:
            result["refund"] = {"route": "bank", "status": "completed" if refund["completed"] else "pending",
                "amount_minor": refund["incoming_amount_minor"], "currency": refund["currency"],
                "completed_at": refund["refunded_at"]}
        return result

    def pending(self, token: str) -> list[dict]:
        self.authenticate(token)
        self._initialize()
        with open_sqlite_connection(self.repository.path) as db:
            return [dict(row) for row in db.execute("""SELECT order_id, revision, claim_json, submitted_at,
                decision_json, decided_at, applied_at FROM game_payment_review_cases c
                WHERE applied_at IS NULL AND revision=(SELECT MAX(revision) FROM game_payment_review_cases WHERE order_id=c.order_id)
                ORDER BY submitted_at, order_id LIMIT 200""")]

    def queue(self, token: str) -> dict:
        actor = self.authenticate(token)
        rows = self.pending(token)
        for row in rows:
            order = self.repository.get(row["order_id"])
            intent = self.repository.get_payment_intent(row["order_id"])
            row["claim"] = json.loads(row.pop("claim_json"))
            saved_decision = row.pop("decision_json")
            row["decision"] = json.loads(saved_decision) if saved_decision else None
            if intent is not None:
                instructions = json.loads(intent.pop("instructions_json"))
                intent.update(account=instructions.get("account"), account_name=instructions.get("account_name"))
            row.update(amount_minor=order.snapshot.amount_minor, currency=order.snapshot.currency,
                       state=order.state.value, payment=intent,
                       product_slug=order.snapshot.product_slug, offer_id=order.snapshot.offer_id,
                       fulfillment_kind=order.snapshot.fulfillment_kind)
        return {"actor": actor, "cases": rows, "refunds": self.pending_refunds(token), "limit": 200}

    def recent_orders(self, *, token: str | StaffPaymentReviewer) -> list[dict]:
        self.authenticate(token)
        self._initialize()
        with open_sqlite_connection(self.repository.path) as db:
            rows = db.execute("""SELECT o.order_id, o.state, p.rail, p.opened_at
                FROM game_order_payment_intents p JOIN game_orders o USING(order_id)
                ORDER BY p.opened_at DESC, o.order_id DESC LIMIT 25""")
            return [dict(row) for row in rows]

    def order_history(self, order_id: str, *, token: str | StaffPaymentReviewer) -> dict:
        self.authenticate(token)
        self._initialize()
        order = self.repository.get(order_id)
        intent = self.repository.get_payment_intent(order_id)
        with open_sqlite_connection(self.repository.path) as db:
            reviews = []
            for row in db.execute("""SELECT revision, claim_json, submitted_at, decision_json, decided_at,
                applied_at, encrypted_content IS NOT NULL AS has_receipt
                FROM game_payment_review_cases WHERE order_id=? ORDER BY revision""", (order_id,)):
                case = dict(row)
                case["claim"] = json.loads(case.pop("claim_json"))
                decision = case.pop("decision_json")
                case["decision"] = json.loads(decision) if decision else None
                reviews.append(case)
            events = [dict(row) for row in db.execute(
                "SELECT occurred_at, from_state, to_state, reason FROM game_order_events WHERE order_id=? ORDER BY event_id DESC LIMIT 100",
                (order_id,),
            )]
            refunds = [dict(row) for row in db.execute("""SELECT a.revision, a.authorized_at, a.authorized_by,
                a.incoming_amount_minor, a.currency, a.refund_rail, c.refund_transaction_id, c.refunded_at, c.completed_by
                FROM game_payment_external_refund_authorizations a LEFT JOIN game_payment_external_refund_completions c
                USING(order_id, revision) WHERE a.order_id=? ORDER BY a.revision""", (order_id,))]
        bank = self.payments.ledger.bank_refund(order_id)
        settlement = self.payments.transfer_identity_claim_for_order(order_id) if (
            intent and not bank and order.state in {OrderState.FAILED, OrderState.REVIEW_REFUND}
        ) else None
        return {
            "order_id": order_id, "state": order.state.value,
            "product_slug": order.snapshot.product_slug, "offer_id": order.snapshot.offer_id,
            "fulfillment_kind": order.snapshot.fulfillment_kind,
            "amount_minor": order.snapshot.amount_minor, "currency": order.snapshot.currency,
            "rail": intent["rail"] if intent else None, "reviews": reviews, "events": events, "refunds": refunds,
            "can_request_bank_refund": bool(settlement and settlement["resolution"] == "settlement"),
            "bank_refund": ({
                "authorized_at": bank["authorized_at"], "completed_at": bank["completed_at"],
                "sender_fingerprint": json.loads(bank["authorization_json"])["sender_fingerprint"],
            } if bank else None),
        }

    def pending_refunds(self, token: str) -> list[dict]:
        """Return authorized external refunds that still lack verified completion."""
        self.authenticate(token)
        self._initialize()
        with open_sqlite_connection(self.repository.path) as db:
            rows = db.execute("""SELECT a.* FROM game_payment_external_refund_authorizations a
                LEFT JOIN game_payment_external_refund_completions c USING(order_id, revision)
                WHERE c.order_id IS NULL ORDER BY a.authorized_at, a.order_id LIMIT 200""").fetchall()
        return [dict(row) for row in rows]

    def resume(self, order_id: str, *, token: str, now: datetime, revision: int | None = None) -> dict:
        actor = self.authenticate(token)
        self._initialize()
        with open_sqlite_connection(self.repository.path) as db:
            row = self._case(db, order_id, revision)
        if row is None or row["decision_json"] is None:
            raise PaymentReviewError("no committed decision to resume")
        decision = json.loads(row["decision_json"])
        if decision["actor"] != actor:
            raise PaymentReviewerAuthError("only the original reviewer can resume this decision")
        transfer = decision["transfer"]
        if transfer is not None:
            transfer["occurred_at"] = datetime.fromisoformat(transfer["occurred_at"])
        external_refund = decision.get("external_refund")
        if external_refund is not None:
            external_refund["occurred_at"] = datetime.fromisoformat(external_refund["occurred_at"])
        return self.decide(order_id, token=token, approve=decision["approve"], reason=decision["reason"],
                           verified_transfer=transfer, external_refund=external_refund,
                           now=now, revision=row["revision"])

    def decide(self, order_id: str, *, token: str, approve: bool, reason: str,
               verified_transfer: dict | None = None, external_refund: dict | None = None,
               now: datetime, revision: int | None = None) -> dict:
        actor = self.authenticate(token)
        decided_at = _time(now)
        reason = _text(reason, "review reason", 500)
        if type(approve) is not bool:
            raise PaymentReviewError("approval must be boolean")
        if verified_transfer is not None and external_refund is not None:
            raise PaymentReviewError("settlement and refund cannot be committed together")
        transfer = None
        refund = None
        incoming_key = None
        if approve:
            if external_refund is not None:
                raise PaymentReviewError("an external refund cannot approve order settlement")
            if not isinstance(verified_transfer, dict) or set(verified_transfer) != {
                "rail", "receiver_id", "transaction_id", "amount_minor", "currency", "occurred_at"
            }:
                raise PaymentReviewError("verified merchant transfer required")
            transfer = dict(verified_transfer)
            for key in ("rail", "receiver_id", "transaction_id", "currency"):
                _text(transfer[key], key)
            if type(transfer["amount_minor"]) is not int or transfer["amount_minor"] <= 0:
                raise PaymentReviewError("invalid transfer amount")
            self.payments.validate_verified_transfer(order_id, **transfer)
            transfer["occurred_at"] = _time(transfer["occurred_at"])
        elif verified_transfer is not None:
            raise PaymentReviewError("rejected claims cannot contain settlement instructions")
        if external_refund is not None:
            if approve or not isinstance(external_refund, dict) or set(external_refund) != {
                "rail", "receiver_id", "transaction_id", "amount_minor", "currency",
                "occurred_at", "refund_destination_confirmed"
            }:
                raise PaymentReviewError("verified incoming transfer and refund attestation required")
            refund = dict(external_refund)
            if refund["refund_destination_confirmed"] is not True:
                raise PaymentReviewError("refund destination must match the verified sender")
            for key in ("rail", "receiver_id", "transaction_id", "currency"):
                _text(refund[key], key)
            if type(refund["amount_minor"]) is not int or not 0 < refund["amount_minor"] <= 9_007_199_254_740_991:
                raise PaymentReviewError("invalid received amount")
            incoming_key = self.payments.validate_refundable_transfer(order_id,
                rail=refund["rail"], receiver_id=refund["receiver_id"],
                transaction_id=refund["transaction_id"], amount_minor=refund["amount_minor"],
                currency=refund["currency"], occurred_at=refund["occurred_at"], now=now)
            refund["occurred_at"] = _time(refund["occurred_at"])
        if external_refund is None:
            decision_data = {"actor": actor, "approve": approve, "reason": reason, "transfer": transfer}
        else:
            decision_data = {"actor": actor, "approve": False, "reason": reason, "transfer": None,
                "resolution": "external_refund_authorized", "external_refund": refund}
        decision = json.dumps(decision_data, sort_keys=True)
        self.payments.initialize_transfer_identity_claims()
        self._initialize()
        with open_sqlite_connection(self.repository.path) as db:
            db.execute("BEGIN IMMEDIATE")
            latest = self._case(db, order_id)
            if latest is None:
                raise PaymentReviewError("payment claim not found")
            row = latest if revision is None else self._case(db, order_id, revision)
            if row is None:
                raise PaymentReviewError("review revision not found")
            if row["decision_json"] is not None and row["decision_json"] != decision:
                raise PaymentReviewError("a different decision is already committed")
            if row["applied_at"] is not None:
                result = {"order_id": order_id, "revision": row["revision"],
                    "approved": approve, "applied": True, "replayed": True}
                if refund is not None:
                    result["resolution"] = "external_refund_authorized"
                return result
            if row["revision"] != latest["revision"]:
                raise PaymentReviewError("submission superseded; reload the queue")
            revision = row["revision"]
            if row["decision_json"] is None:
                if refund is not None:
                    try:
                        self.payments.claim_transfer_identity(db, order_id=order_id,
                            transfer_key=incoming_key, resolution="refund", revision=revision,
                            actor=actor, at=now)
                    except LedgerConflictError as exc:
                        raise PaymentReviewError("merchant transfer already has a different payment resolution") from exc
                    current = self.repository._load(db, order_id)
                    if current.state != OrderState.AWAITING_PAYMENT:
                        raise PaymentReviewError("order no longer accepts a payment exception refund")
                    try:
                        cancelled = transition_order(current, OrderState.CANCELLED, at=now,
                            reason="payment exception accepted for an original-sender refund")
                    except ValueError as exc:
                        raise PaymentReviewError("order cannot be cancelled for payment refund") from exc
                    event = cancelled.events[-1]
                    db.execute("UPDATE game_orders SET state=?, updated_at=? WHERE order_id=? AND state=?",
                        (cancelled.state.value, decided_at, order_id, current.state.value))
                    db.execute("INSERT INTO game_order_events(order_id, occurred_at, from_state, to_state, reason) VALUES(?,?,?,?,?)",
                        (order_id, event.at.isoformat(), event.from_state.value, event.to_state.value, event.reason))
                    db.execute("""INSERT INTO game_payment_external_refund_authorizations
                        (order_id, revision, incoming_transfer_key, incoming_rail, incoming_receiver_id,
                         incoming_transaction_id, incoming_amount_minor, currency, incoming_occurred_at,
                         refund_rail, original_sender_confirmed, authorized_by, authorized_at, reason)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (order_id, revision, incoming_key, refund["rail"].casefold(), refund["receiver_id"],
                         refund["transaction_id"], refund["amount_minor"], refund["currency"],
                         refund["occurred_at"], refund["rail"].casefold(), 1, actor, decided_at, reason))
                db.execute("UPDATE game_payment_review_cases SET decision_json=?, decided_at=? WHERE order_id=? AND revision=?",
                           (decision, decided_at, order_id, revision))
        if approve:
            args = dict(transfer)
            args["occurred_at"] = datetime.fromisoformat(args["occurred_at"])
            self.payments.settle_verified_transfer(order_id, actor=actor, **args)
        with open_sqlite_connection(self.repository.path) as db:
            db.execute("UPDATE game_payment_review_cases SET applied_at=? WHERE order_id=? AND revision=? AND applied_at IS NULL",
                       (decided_at, order_id, revision))
        result = {"order_id": order_id, "revision": revision, "approved": approve,
            "applied": True, "replayed": False}
        if refund is not None:
            result["resolution"] = "external_refund_authorized"
        return result

    def complete_external_refund(self, order_id: str, *, token: str, revision: int,
                                 refund_transaction_id: str, refunded_at: datetime,
                                 reason: str, now: datetime) -> dict:
        """Record an already-sent, independently verified full external refund.

        This operation does not initiate a payment. The outbound amount is
        fixed to the verified inbound amount saved by the authorization.
        """
        actor = self.authenticate(token)
        refund_transaction_id = _text(refund_transaction_id, "refund transaction ID")
        reason = _text(reason, "refund completion reason", 500)
        refunded = _time(refunded_at)
        recorded = _time(now)
        if type(revision) is not int or revision < 1:
            raise PaymentReviewError("invalid review revision")
        if refunded_at > now:
            raise PaymentReviewError("refund time cannot be in the future")
        self._initialize()
        with open_sqlite_connection(self.repository.path) as db:
            db.execute("BEGIN IMMEDIATE")
            authorization = db.execute("SELECT * FROM game_payment_external_refund_authorizations WHERE order_id=? AND revision=?",
                (order_id, revision)).fetchone()
            case = self._case(db, order_id, revision)
            if authorization is None or case is None or case["decision_json"] is None:
                raise PaymentReviewError("external refund authorization not found")
            decision = json.loads(case["decision_json"])
            if decision.get("resolution") != "external_refund_authorized":
                raise PaymentReviewError("review does not authorize an external refund")
            if refund_transaction_id == authorization["incoming_transaction_id"]:
                raise PaymentReviewError("outbound refund must have its own transaction ID")
            if datetime.fromisoformat(refunded) < datetime.fromisoformat(authorization["authorized_at"]):
                raise PaymentReviewError("refund must occur after its authorization")
            refund_key = "refund:" + hashlib.sha256(json.dumps(
                [authorization["refund_rail"], authorization["currency"], refund_transaction_id],
                separators=(",", ":")).encode()).hexdigest()
            existing = db.execute("SELECT * FROM game_payment_external_refund_completions WHERE order_id=? AND revision=?",
                (order_id, revision)).fetchone()
            facts = (refund_key, refund_transaction_id, authorization["incoming_amount_minor"],
                authorization["currency"], refunded)
            if existing is not None:
                saved = (existing["refund_transfer_key"], existing["refund_transaction_id"],
                    existing["refund_amount_minor"], existing["currency"], existing["refunded_at"])
                if saved != facts:
                    raise PaymentReviewError("a different external refund completion is already recorded")
                return {"order_id": order_id, "revision": revision, "refunded": True, "replayed": True}
            # Claim in the shared ledger before the cross-store completion; uncertain handoffs keep the claim.
            self.payments.ledger.claim_outbound_refund(refund_key, f'exception:{order_id}:{revision}')
            try:
                db.execute("""INSERT INTO game_payment_external_refund_completions
                    (order_id, revision, refund_transfer_key, refund_transaction_id, refund_amount_minor,
                     currency, refunded_at, completed_by, reason, recorded_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""", (order_id, revision, refund_key, refund_transaction_id,
                    authorization["incoming_amount_minor"], authorization["currency"], refunded,
                    actor, reason, recorded))
            except sqlite3.IntegrityError:
                raise PaymentReviewError("outbound refund transaction is already recorded") from None
        return {"order_id": order_id, "revision": revision, "refunded": True, "replayed": False}


def build_env_payment_review(customer_api):
    """Keep receipt intake off unless private reviewer configuration is supplied."""
    import os
    raw = os.environ.get("AURIX_GAMES_PAYMENT_REVIEWERS", "{}").strip()
    reviewers = json.loads(raw)
    if not reviewers and not os.environ.get("AURIX_GAMES_SUPER_ADMIN_CUSTOMER_ID", "").strip():
        return None
    if customer_api is None:
        raise PaymentReviewError("customer API required for payment review")
    return PaymentReviewService(CustomerPaymentService(customer_api.service.repository,
        customer_api.wallet_service.ledger), reviewers,
        evidence_key=os.environ.get("AURIX_GAMES_PAYMENT_EVIDENCE_KEY") or None)
