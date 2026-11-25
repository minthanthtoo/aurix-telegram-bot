"""Durable Games fulfillment queue and explicit supplier-order adapter.

The queue stores an encrypted private fulfillment payload because a worker
must be able to resume after a restart. Payloads never cross the public
catalog/API and are kept in a mode-0600 database; the Fernet key must come
from a runtime secret store. Supplier writes are still controlled by
``FazerCardsCommandClient(allow_writes=True)``; a disabled client leaves the
task retryable and cannot spend money.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any, Mapping, Protocol
import uuid

from cryptography.fernet import Fernet, InvalidToken
from persistence import open_sqlite_connection

from .order_repository import GameOrderRepository
from .order_state import GameOrder, OrderState
from .payment_service import CustomerPaymentService, PaymentBoundaryError
from .spend_guard import SpendPreflightError, SpendClaimLost, SpendOutcomeUnknown, schema_fingerprint
from .providers.fazercards_commands import (
    FazerCardsCommandClient,
    SupplierCommandError,
    SupplierWriteDisabledError,
)
from .providers.shop2topup_commands import Shop2TopupCommandClient, Shop2TopupCommandError
from .supplier_routes import PrivateSupplierRoute


UTC = timezone.utc
_PROVIDER_ORDER_ID = re.compile(r"^(?:ord-[0-9]+|s2t-[0-9a-f]{32})$")


class FulfillmentInputError(ValueError):
    """A fulfillment task is malformed or cannot be safely queued."""


class FulfillmentTaskError(RuntimeError):
    """A durable task operation failed without exposing provider details."""


class FulfillmentClaimLost(FulfillmentTaskError):
    """Another attempt owns this task."""


@dataclass(frozen=True)
class FulfillmentTask:
    order_id: str
    fulfillment_kind: str
    payload: dict[str, Any]
    status: str
    attempts: int
    available_at: datetime
    provider_order_id: str | None
    last_error: str | None
    updated_at: datetime
    dispatch_started_at: str | None = None


@dataclass(frozen=True)
class FulfillmentRun:
    order_id: str
    status: str
    provider_order_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class PaidOrderDispatchRun:
    order_id: str
    status: str
    error: str | None = None


class FulfillmentTaskStore:
    """Private SQLite queue with stale-running reclamation."""

    def __init__(self, path: str | Path, *, encryption_key: bytes | str) -> None:
        self.path = Path(path)
        try:
            raw_key = encryption_key.encode("ascii") if isinstance(encryption_key, str) else encryption_key
            if not isinstance(raw_key, bytes):
                raise TypeError
            self._fernet = Fernet(raw_key)
        except (TypeError, ValueError) as exc:
            raise FulfillmentInputError("fulfillment encryption_key must be a Fernet key") from exc

    def initialize(self) -> None:
        if self.path.is_symlink():
            raise FulfillmentInputError("fulfillment database must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077:
            raise FulfillmentInputError("fulfillment database directory must be private")
        try:
            with open_sqlite_connection(self.path, timeout_seconds=5) as connection:
                connection.execute("PRAGMA journal_mode = DELETE")
                connection.execute("PRAGMA synchronous = FULL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS game_fulfillment_tasks (
                        order_id TEXT PRIMARY KEY,
                        fulfillment_kind TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('queued','running','submitted','retry','failed')),
                        attempts INTEGER NOT NULL CHECK(attempts >= 0),
                        available_at TEXT NOT NULL,
                        provider_order_id TEXT,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        claimed_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS game_fulfillment_due
                        ON game_fulfillment_tasks(status, available_at);
                    CREATE UNIQUE INDEX IF NOT EXISTS game_fulfillment_provider_identity
                        ON game_fulfillment_tasks(provider_order_id) WHERE provider_order_id IS NOT NULL;
                    CREATE TABLE IF NOT EXISTS game_supplier_recovery_links (
                        order_id TEXT PRIMARY KEY REFERENCES game_fulfillment_tasks(order_id),
                        provider_order_id TEXT NOT NULL UNIQUE,
                        request_sha256 TEXT NOT NULL,
                        reviewer TEXT NOT NULL,
                        encrypted_evidence BLOB NOT NULL,
                        recorded_at TEXT NOT NULL
                    );
                    CREATE TRIGGER IF NOT EXISTS game_supplier_recovery_link_no_update
                    BEFORE UPDATE ON game_supplier_recovery_links
                    BEGIN SELECT RAISE(ABORT, 'supplier recovery links are immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS game_supplier_recovery_link_no_delete
                    BEFORE DELETE ON game_supplier_recovery_links
                    BEGIN SELECT RAISE(ABORT, 'supplier recovery links are retained'); END;
                    """
                )
                connection.execute("BEGIN IMMEDIATE")
                columns = {row[1] for row in connection.execute("PRAGMA table_info(game_fulfillment_tasks)")}
                if "dispatch_started_at" not in columns:
                    connection.execute("ALTER TABLE game_fulfillment_tasks ADD COLUMN dispatch_started_at TEXT")
                    connection.execute("""UPDATE game_fulfillment_tasks SET dispatch_started_at = COALESCE(claimed_at, updated_at)
                        WHERE status = 'running' AND provider_order_id IS NULL""")
        except sqlite3.Error as exc:
            raise FulfillmentTaskError("fulfillment database initialization failed") from exc
        self.path.chmod(0o600)

    def enqueue(
        self,
        *,
        order_id: str,
        fulfillment_kind: str,
        payload: Mapping[str, Any],
        available_at: datetime,
    ) -> FulfillmentTask:
        order = _id(order_id, "order_id")
        kind = _id(fulfillment_kind, "fulfillment_kind")
        current = _time(available_at)
        encoded = _payload(payload)
        encrypted = self._fernet.encrypt(encoded.encode("utf-8")).decode("ascii")
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM game_fulfillment_tasks WHERE order_id = ?", (order,)
                ).fetchone()
                if existing is not None:
                    task = _task_from_row(existing, self._decrypt_payload)
                    if task.fulfillment_kind != kind or _payload(task.payload) != encoded:
                        raise FulfillmentInputError("order already has a different fulfillment payload")
                    return task
                connection.execute(
                    """INSERT INTO game_fulfillment_tasks
                       (order_id, fulfillment_kind, payload_json, status, attempts,
                        available_at, provider_order_id, last_error, created_at, updated_at, claimed_at)
                       VALUES (?, ?, ?, 'queued', 0, ?, NULL, NULL, ?, ?, NULL)""",
                    (order, kind, encrypted, current.isoformat(), current.isoformat(), current.isoformat()),
                )
                return FulfillmentTask(order, kind, dict(payload), "queued", 0, current, None, None, current)
        except FulfillmentInputError:
            raise
        except sqlite3.Error as exc:
            raise FulfillmentTaskError("fulfillment task could not be enqueued") from exc

    def remove_queued(self, order_id: str) -> None:
        order = _id(order_id, "order_id")
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM game_fulfillment_tasks WHERE order_id = ? AND status = 'queued'",
                    (order,),
                )
        except sqlite3.Error as exc:
            raise FulfillmentTaskError("fulfillment task rollback failed") from exc

    def claim_due(
        self,
        *,
        now: datetime,
        stale_after: timedelta = timedelta(minutes=10),
        limit: int = 10,
    ) -> tuple[FulfillmentTask, ...]:
        current = _time(now)
        if not isinstance(stale_after, timedelta) or stale_after <= timedelta(0):
            raise FulfillmentInputError("stale_after must be positive")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise FulfillmentInputError("limit is invalid")
        stale_before = current - stale_after
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    """SELECT * FROM game_fulfillment_tasks
                       WHERE (status IN ('queued','retry') AND available_at <= ?)
                          OR (status = 'running' AND claimed_at <= ?)
                       ORDER BY available_at, order_id LIMIT ?""",
                    (current.isoformat(), stale_before.isoformat(), limit),
                ).fetchall()
                tasks = []
                for row in rows:
                    attempts = int(row["attempts"]) + 1
                    connection.execute(
                        "UPDATE game_fulfillment_tasks SET status = 'running', attempts = ?, claimed_at = ?, updated_at = ? WHERE order_id = ?",
                        (attempts, current.isoformat(), current.isoformat(), row["order_id"]),
                    )
                    tasks.append(
                        FulfillmentTask(
                            row["order_id"], row["fulfillment_kind"], self._decrypt_payload(row["payload_json"]),
                            "running", attempts, _time(datetime.fromisoformat(row["available_at"])),
                            row["provider_order_id"], row["last_error"], current, row["dispatch_started_at"],
                        )
                    )
                return tuple(tasks)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise FulfillmentTaskError("stored fulfillment task is malformed") from exc
        except sqlite3.Error as exc:
            raise FulfillmentTaskError("fulfillment task claim failed") from exc

    def record_acceptance(self, task: FulfillmentTask, provider_order_id: str) -> None:
        # Persist the receipt before touching the separate order and ledger databases.
        self._claimed_update(task, "provider_order_id = ?", (_provider_order(provider_order_id),))

    def begin_dispatch(self, task: FulfillmentTask, *, now: datetime) -> None:
        self._claimed_update(task, "dispatch_started_at = ?", (_time(now).isoformat(),))

    def defer_disabled_dispatch(self, task: FulfillmentTask, *, now: datetime) -> None:
        self._claimed_update(task, "dispatch_started_at = NULL, provider_order_id = NULL, status = 'retry', last_error = ?, "
            "available_at = ?, updated_at = ?, claimed_at = NULL", ("supplier writes are disabled",
            (now + _backoff(task.attempts)).isoformat(), now.isoformat()))

    def mark_submitted(self, task: FulfillmentTask, *, provider_order_id: str, now: datetime) -> None:
        self._update_task(task, status="submitted", provider_order_id=_provider_order(provider_order_id),
                          last_error=None, available_at=now, now=now)

    def mark_retry(self, task: FulfillmentTask, *, error: str, next_at: datetime, now: datetime) -> None:
        self._update_task(task, status="retry", provider_order_id=None,
                          last_error=_text(error, "error", 240), available_at=next_at, now=now)

    def mark_failed(self, task: FulfillmentTask, *, error: str, now: datetime) -> None:
        self._update_task(task, status="failed", provider_order_id=None,
                          last_error=_text(error, "error", 240), available_at=now, now=now)

    def get(self, order_id: str) -> FulfillmentTask | None:
        order = _id(order_id, "order_id")
        self.initialize()
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT * FROM game_fulfillment_tasks WHERE order_id = ?", (order,)).fetchone()
        except sqlite3.Error as exc:
            raise FulfillmentTaskError("fulfillment task read failed") from exc
        return None if row is None else _task_from_row(row, self._decrypt_payload)

    def link_recovered_receipt(self, task: FulfillmentTask, *, provider_order_id: str,
                               request_sha256: str, reviewer: str, evidence: dict, now: datetime) -> bool:
        """Fence a lost-response receipt before changing separate order/money stores."""
        provider_order_id = _provider_order(provider_order_id)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM game_supplier_recovery_links WHERE order_id=?', (task.order_id,)).fetchone()
            if existing:
                if (existing['provider_order_id'], existing['request_sha256'], existing['reviewer']) != (
                        provider_order_id, request_sha256, reviewer):
                    raise FulfillmentInputError('supplier recovery already has a different immutable decision')
                return False
            row = db.execute('SELECT * FROM game_fulfillment_tasks WHERE order_id=?', (task.order_id,)).fetchone()
            if (row is None or row['status'] != 'failed' or row['attempts'] != task.attempts
                    or row['dispatch_started_at'] is None or row['provider_order_id'] is not None
                    or self._decrypt_payload(row['payload_json']) != task.payload):
                raise FulfillmentClaimLost('unknown supplier task changed before recovery')
            if db.execute('SELECT 1 FROM game_fulfillment_tasks WHERE provider_order_id=?', (provider_order_id,)).fetchone():
                raise FulfillmentInputError('supplier receipt is already assigned to a task')
            db.execute('INSERT INTO game_supplier_recovery_links VALUES (?,?,?,?,?,?)',
                (task.order_id, provider_order_id, request_sha256, reviewer,
                 self._fernet.encrypt(json.dumps(evidence, sort_keys=True).encode()), _time(now).isoformat()))
            db.execute('UPDATE game_fulfillment_tasks SET provider_order_id=?,updated_at=? WHERE order_id=?',
                (provider_order_id, _time(now).isoformat(), task.order_id))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='game_supplier_spend'").fetchone():
                budget = db.execute('SELECT * FROM game_supplier_spend WHERE order_id=?', (task.order_id,)).fetchone()
                if budget and (budget['state'] == 'disabled' or budget['provider_order_id'] not in (None, provider_order_id)
                        or budget['payload_hash'] != schema_fingerprint(task.payload)):
                    raise FulfillmentInputError('supplier recovery conflicts with saved spend obligation')
                db.execute("UPDATE game_supplier_spend SET state='accepted',provider_order_id=? WHERE order_id=?",
                    (provider_order_id, task.order_id))
        return True

    def finish_recovered_receipt(self, order_id: str, provider_order_id: str, *, now: datetime) -> None:
        with self._connect() as db:
            result = db.execute("""UPDATE game_fulfillment_tasks
                SET status='submitted',last_error=NULL,claimed_at=NULL,updated_at=?
                WHERE order_id=? AND provider_order_id=? AND status IN ('failed','submitted')
                AND EXISTS (SELECT 1 FROM game_supplier_recovery_links l
                    WHERE l.order_id=game_fulfillment_tasks.order_id AND l.provider_order_id=?)""",
                (_time(now).isoformat(), order_id, _provider_order(provider_order_id), provider_order_id))
            if result.rowcount != 1:
                raise FulfillmentClaimLost('supplier recovery task changed before completion')

    def _update_task(
        self, task: FulfillmentTask, *, status: str, provider_order_id: str | None,
        last_error: str | None, available_at: datetime, now: datetime,
    ) -> None:
        self._claimed_update(
            task, "status = ?, provider_order_id = COALESCE(provider_order_id, ?), last_error = ?, "
            "available_at = ?, updated_at = ?, claimed_at = NULL",
            (status, provider_order_id, last_error, _time(available_at).isoformat(), _time(now).isoformat()),
        )

    def _claimed_update(self, task: FulfillmentTask, assignments: str, values: tuple) -> None:
        try:
            with self._connect() as connection:
                result = connection.execute(
                    f"UPDATE game_fulfillment_tasks SET {assignments} "
                    "WHERE order_id = ? AND status = 'running' AND attempts = ?",
                    (*values, task.order_id, task.attempts),
                )
                if result.rowcount != 1:
                    raise FulfillmentClaimLost("fulfillment claim is no longer current")
        except sqlite3.Error as exc:
            raise FulfillmentTaskError("fulfillment task update failed") from exc

    def _connect(self) -> sqlite3.Connection:
        return open_sqlite_connection(self.path, timeout_seconds=5)

    def _decrypt_payload(self, value: str) -> dict[str, Any]:
        try:
            decoded = self._fernet.decrypt(value.encode("ascii"), ttl=None)
            payload = json.loads(decoded)
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise FulfillmentTaskError("stored fulfillment payload could not be decrypted") from exc
        if not isinstance(payload, dict):
            raise FulfillmentTaskError("stored fulfillment payload is malformed")
        return payload


class SupplierOrderPort(Protocol):
    def submit(self, *, fulfillment_kind: str, payload: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]: ...


class CheckoutPayloadResolver(Protocol):
    def fulfillment_payload(self, *, customer_id: str, order_id: str) -> dict[str, Any]: ...


class PaidOrderDispatcher:
    """Recoverably turn reserved, verified orders into durable queue tasks.

    This local workflow does not verify payment or call a supplier. A trusted
    payment verifier must first move an order to ``PAID_RESERVED``. The
    dispatcher can then be run repeatedly: task creation is idempotent, and a
    crash between queue creation and the order-state update is repaired by a
    subsequent run.
    """

    def __init__(
        self,
        *,
        orders: GameOrderRepository,
        tasks: FulfillmentTaskStore,
        checkout: CheckoutPayloadResolver,
    ) -> None:
        self.orders = orders
        self.tasks = tasks
        self.checkout = checkout

    def run_once(self, *, now: datetime, limit: int = 10) -> tuple[PaidOrderDispatchRun, ...]:
        current = _time(now)
        reserved = self.orders.list_by_state(OrderState.PAID_RESERVED, limit=limit)
        outcomes: list[PaidOrderDispatchRun] = []
        for order in reserved:
            try:
                payload = self.checkout.fulfillment_payload(
                    customer_id=order.customer_id,
                    order_id=order.order_id,
                )
                self.tasks.enqueue(
                    order_id=order.order_id,
                    fulfillment_kind=order.snapshot.fulfillment_kind,
                    payload=payload,
                    available_at=current,
                )
            except Exception:
                # Avoid logging payloads, recipient fields, or provider details.
                outcomes.append(
                    PaidOrderDispatchRun(order.order_id, "retry", "private payload or task preparation failed")
                )
                continue

            try:
                self.orders.transition(
                    order.order_id,
                    OrderState.FULFILLMENT_QUEUED,
                    at=current,
                    reason="durable fulfillment task enqueued",
                )
            except Exception:
                # The task and order are in separate SQLite databases. Keep
                # the task so a retry can complete a handoff whose response
                # was lost or whose state transition was interrupted.
                try:
                    refreshed = self.orders.get(order.order_id)
                except Exception:
                    refreshed = None
                if refreshed is None or refreshed.state != OrderState.FULFILLMENT_QUEUED:
                    outcomes.append(
                        PaidOrderDispatchRun(order.order_id, "retry", "queue task retained; order transition pending")
                    )
                    continue
            outcomes.append(PaidOrderDispatchRun(order.order_id, "queued"))
        return tuple(outcomes)


class FazerCardsOrderAdapter:
    """Map trusted private task payloads to typed FazerCards order commands."""

    def __init__(self, client: FazerCardsCommandClient) -> None:
        self.client = client

    def submit(self, *, fulfillment_kind: str, payload: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        if fulfillment_kind == "direct_topup":
            return self.client.order_topup(
                category_id=_required_payload_text(payload, "category_id"),
                offer_id=_required_payload_text(payload, "offer_id"),
                fields=_required_payload_mapping(payload, "fields"),
                idempotency_key=idempotency_key,
            )
        if fulfillment_kind == "gift_card":
            return self.client.order_gift_card(
                category_id=_required_payload_text(payload, "category_id"),
                card_id=_required_payload_text(payload, "card_id"),
                quantity=_required_payload_int(payload, "quantity"),
                idempotency_key=idempotency_key,
            )
        if fulfillment_kind == "game_key":
            return self.client.order_game_key(
                game_id=_required_payload_text(payload, "game_id"),
                key_id=_required_payload_text(payload, "key_id"),
                quantity=_required_payload_int(payload, "quantity"),
                idempotency_key=idempotency_key,
            )
        if fulfillment_kind == "manual_service":
            raw_fields = payload.get("fields")
            if raw_fields is not None and not isinstance(raw_fields, Mapping):
                raise FulfillmentInputError("fulfillment payload field fields is invalid")
            fields = dict(raw_fields) if raw_fields else None
            return self.client.order_manual_service(
                manual_service_id=_required_payload_text(payload, "manual_service_id"),
                product_id=_required_payload_text(payload, "product_id"),
                fields=fields,
                idempotency_key=idempotency_key,
            )
        if fulfillment_kind == "steam_topup":
            return self.client.order_steam_topup(
                steam_login=_required_payload_text(payload, "steam_login"),
                currency=_required_payload_text(payload, "currency"),
                amount=payload.get("amount"),
                idempotency_key=idempotency_key,
            )
        if fulfillment_kind == "steam_gift":
            return self.client.order_steam_gift(
                invite_url=_required_payload_text(payload, "invite_url"),
                sub_id=_required_payload_int(payload, "sub_id"),
                app_id=_required_payload_int(payload, "app_id"),
                region=_required_payload_text(payload, "region"),
                idempotency_key=idempotency_key,
            )
        raise FulfillmentInputError("fulfillment kind requires an explicit operator workflow")


class SupplierOrderRouter:
    """Keep provider-specific IDs and API shapes behind one private boundary."""

    def __init__(self, fazercards: FazerCardsCommandClient | None,
                 shop2topup: Shop2TopupCommandClient | None = None) -> None:
        if fazercards is not None and not isinstance(fazercards, FazerCardsCommandClient):
            raise FulfillmentInputError("FazerCards command client is invalid")
        if shop2topup is not None and not isinstance(shop2topup, Shop2TopupCommandClient):
            raise FulfillmentInputError("SHOP2TOPUP command client is invalid")
        self.fazercards = fazercards
        self.shop2topup = shop2topup
        self._fazercards_adapter = None if fazercards is None else FazerCardsOrderAdapter(fazercards)

    def validate_route(self, *, route: PrivateSupplierRoute, fields: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(route, PrivateSupplierRoute) or not isinstance(fields, Mapping):
            raise FulfillmentInputError("supplier validation route is invalid")
        if route.supplier_provider == "fazercards":
            if self.fazercards is None or route.validation_category_id is None:
                raise SupplierCommandError("FazerCards account validation is unavailable")
            return self.fazercards.validate_topup_recipient(route.validation_category_id, fields)
        if route.supplier_provider != "shop2topup" or self.shop2topup is None:
            raise SupplierCommandError("approved supplier account validation is unavailable")
        player_id = fields.get("player_id")
        requirements = {key: value for key, value in fields.items() if key != "player_id"}
        if not isinstance(player_id, str) or not player_id.strip():
            raise FulfillmentInputError("approved player_id mapping is missing")
        response = self.shop2topup.validate_player(
            sub_category_id=int(route.sku), player_id=player_id, requirements=requirements,
        )
        player = response.get("player")
        if not isinstance(player, dict):
            raise Shop2TopupCommandError("SHOP2TOPUP player response was malformed")
        return {
            "ok": True,
            "valid": True,
            "category_id": route.validation_category_id,
            "player_id": player.get("player_id"),
            "player_name": player.get("player_name"),
            "region": player.get("region"),
        }

    def attempt_reference(self, *, payload: Mapping[str, Any], idempotency_key: str) -> str | None:
        if payload.get("supplier_provider", "fazercards") != "shop2topup":
            return None
        return "s2t-" + _shop2topup_uuid(idempotency_key).replace("-", "")

    def submit(self, *, fulfillment_kind: str, payload: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        provider = payload.get("supplier_provider", "fazercards")
        if provider != "shop2topup":
            if self._fazercards_adapter is None:
                raise SupplierCommandError("FazerCards order service is unavailable")
            return self._fazercards_adapter.submit(
                fulfillment_kind=fulfillment_kind, payload=payload, idempotency_key=idempotency_key,
            )
        if self.shop2topup is None or fulfillment_kind != "direct_topup":
            raise SupplierCommandError("SHOP2TOPUP order service is unavailable for this product")
        order_uuid = _shop2topup_uuid(idempotency_key)
        receipt = "s2t-" + order_uuid.replace("-", "")
        expected_price = payload.get("expected_unit_price")
        if expected_price is None:
            raise SupplierCommandError("SHOP2TOPUP order is missing its live price lock")
        response = self.shop2topup.create_order(
            order_id=order_uuid,
            sub_category_id=_required_payload_int(payload, "sub_category_id"),
            quantity=_required_payload_int(payload, "quantity"),
            requirements=_shop2topup_requirements(payload),
            expected_unit_price=expected_price,
        )
        order = response.get("order")
        if not isinstance(order, dict) or order.get("order_id") != order_uuid:
            raise Shop2TopupCommandError("SHOP2TOPUP order response did not match the submitted order")
        status = order.get("status")
        if not isinstance(status, str) or not status.strip() or len(status) > 80:
            raise Shop2TopupCommandError("SHOP2TOPUP order status was malformed")
        return {"ok": True, "order": {"id": receipt, "status": status}}

    def get_order(self, provider_order_id: str) -> dict[str, Any]:
        if provider_order_id.startswith("s2t-"):
            if self.shop2topup is None:
                raise SupplierCommandError("SHOP2TOPUP status reads are unavailable")
            order_uuid = _uuid_from_shop2topup_receipt(provider_order_id)
            response = self.shop2topup.get_order(order_uuid)
            order = response.get("order")
            if not isinstance(order, dict) or order.get("order_id") != order_uuid:
                raise Shop2TopupCommandError("SHOP2TOPUP status response did not match the accepted order")
            status = order.get("status")
            if not isinstance(status, str) or not status.strip() or len(status) > 80:
                raise Shop2TopupCommandError("SHOP2TOPUP order status was malformed")
            return {"ok": True, "order": {"id": provider_order_id, "status": status}}
        if self.fazercards is None:
            raise SupplierCommandError("FazerCards status reads are unavailable")
        return self.fazercards.get_order(provider_order_id)


def _shop2topup_uuid(idempotency_key: str) -> str:
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 255:
        raise FulfillmentInputError("supplier idempotency key is invalid")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "aurix-games/shop2topup/" + idempotency_key))


def _uuid_from_shop2topup_receipt(value: str) -> str:
    match = re.fullmatch(r"s2t-([0-9a-f]{32})", value)
    if not match:
        raise FulfillmentInputError("SHOP2TOPUP order receipt is invalid")
    return str(uuid.UUID(hex=match.group(1)))


def _shop2topup_requirements(payload: Mapping[str, Any]) -> dict[str, Any]:
    player_id = _required_payload_text(payload, "player_id")
    requirements = _required_payload_mapping(payload, "requirements") if payload.get("requirements") else {}
    return {"player_id": player_id, **requirements}


class GameFulfillmentWorker:
    """Claim durable tasks and submit with the order's stable provider key."""

    def __init__(
        self,
        *,
        orders: GameOrderRepository,
        tasks: FulfillmentTaskStore,
        supplier: SupplierOrderPort,
        payments: CustomerPaymentService | None = None,
        max_attempts: int = 5,
        spend_guard: Any = None,
    ) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 100:
            raise FulfillmentInputError("max_attempts is invalid")
        self.orders = orders
        self.tasks = tasks
        self.supplier = supplier
        self.payments = payments
        self.spend_guard = spend_guard
        # Retain the constructor argument for existing callers; uncertain writes now stop immediately.
        self.max_attempts = max_attempts

    def enqueue_paid_order(
        self, order_id: str, *, payload: Mapping[str, Any], now: datetime
    ) -> FulfillmentTask:
        order = self.orders.get(order_id)
        if order.state != OrderState.PAID_RESERVED:
            raise FulfillmentInputError("only paid_reserved orders can be queued")
        task = self.tasks.enqueue(
            order_id=order.order_id,
            fulfillment_kind=order.snapshot.fulfillment_kind,
            payload=payload,
            available_at=now,
        )
        try:
            self.orders.transition(
                order.order_id,
                OrderState.FULFILLMENT_QUEUED,
                at=now,
                reason="durable fulfillment task enqueued",
            )
        except Exception as exc:
            # Do not delete a durable task across separate SQLite databases:
            # the state update may have committed despite a lost response,
            # and a retry must be able to finish the handoff idempotently.
            try:
                refreshed = self.orders.get(order.order_id)
            except Exception:
                refreshed = None
            if refreshed is None or refreshed.state != OrderState.FULFILLMENT_QUEUED:
                raise FulfillmentTaskError("order transition pending; fulfillment task was retained") from exc
        return task

    def run_once(self, *, now: datetime, limit: int = 10) -> tuple[FulfillmentRun, ...]:
        current = _time(now)
        claimed = self.tasks.claim_due(now=current, limit=limit)
        outcomes = []
        for task in claimed:
            try:
                outcomes.append(self._run_claim(task, current))
            except FulfillmentClaimLost:
                outcomes.append(FulfillmentRun(task.order_id, "claim_lost"))
            except Exception:
                # Leave the claim reclaimable: a local commit may already have succeeded.
                outcomes.append(FulfillmentRun(task.order_id, "recovery_pending", error="local fulfillment handoff incomplete"))
        return tuple(outcomes)

    def _run_claim(self, task: FulfillmentTask, current: datetime) -> FulfillmentRun:
        order = self.orders.get(task.order_id)
        receipt = task.provider_order_id or order.provider_order_id
        status = None
        if not receipt:
            if task.dispatch_started_at is not None:
                candidate = _supplier_attempt_reference(
                    self.supplier, payload=task.payload,
                    idempotency_key=order.provider_idempotency_key,
                )
                return self._hold_uncertain(task, order, current, provider_order_id=candidate)
            if order.state == OrderState.PAID_RESERVED:
                message = "order queue transition is pending"
                self.tasks.mark_retry(task, error=message, next_at=current + _backoff(task.attempts), now=current)
                return FulfillmentRun(task.order_id, "waiting_for_order_transition", error=message)
            if order.state != OrderState.FULFILLMENT_QUEUED:
                self.tasks.mark_failed(task, error="order is not queued for fulfillment", now=current)
                return FulfillmentRun(task.order_id, "failed", error="order state is not queued")
            if self.spend_guard is not None:
                try:
                    self.spend_guard.prepare(task, order, now=current)
                except SpendClaimLost:
                    raise FulfillmentClaimLost("fulfillment claim is no longer current") from None
                except SpendOutcomeUnknown:
                    return self._hold_uncertain(task, order, current)
                except SpendPreflightError as exc:
                    self.orders.transition(order.order_id, OrderState.REVIEW_REFUND, at=current,
                        reason="supplier purchase preflight requires review")
                    self.tasks.mark_failed(task, error=str(exc), now=current)
                    return FulfillmentRun(task.order_id, "preflight_review", error=str(exc))
                except Exception:
                    self.tasks.mark_retry(task, error="supplier preflight unavailable; no purchase sent",
                        next_at=current + _backoff(task.attempts), now=current)
                    return FulfillmentRun(task.order_id, "preflight_retry", error="supplier preflight unavailable; no purchase sent")
            attempt_reference = _supplier_attempt_reference(
                self.supplier, payload=task.payload,
                idempotency_key=order.provider_idempotency_key,
            )
            self.tasks.begin_dispatch(task, now=current)
            try:
                receipt, status = _provider_response(self.supplier.submit(
                    fulfillment_kind=task.fulfillment_kind, payload=task.payload,
                    idempotency_key=order.provider_idempotency_key,
                ))
            except SupplierWriteDisabledError:
                message = "supplier writes are disabled"
                if self.spend_guard is not None:
                    self.spend_guard.disabled(task.order_id)
                self.tasks.defer_disabled_dispatch(task, now=current)
                return FulfillmentRun(task.order_id, "retry", error=message)
            except Exception:
                return self._hold_uncertain(task, order, current, provider_order_id=attempt_reference)
            self.tasks.record_acceptance(task, receipt)

        if self.spend_guard is not None:
            self.spend_guard.accepted(task.order_id, receipt)

        if order.provider_order_id and order.provider_order_id != receipt:
            message = "supplier receipt conflicts with stored order; review required"
            self.tasks.mark_failed(task, error=message, now=current)
            return FulfillmentRun(task.order_id, "receipt_review", error=message)
        if order.state == OrderState.FULFILLMENT_QUEUED:
            self.orders.transition(order.order_id, OrderState.SUBMITTED, at=current,
                                   reason="supplier accepted fulfillment request",
                                   provider_order_id=receipt, provider_status=status)
        elif order.state not in {OrderState.SUBMITTED, OrderState.PROCESSING, OrderState.DELIVERED,
                                 OrderState.FAILED, OrderState.RECONCILIATION} or not order.provider_order_id:
            message = "accepted supplier order needs payment review"
            self.tasks.mark_failed(task, error=message, now=current)
            return FulfillmentRun(task.order_id, "capture_review", receipt, message)
        if self.payments is not None:
            try:
                self.payments.capture_order(order.order_id, event_key=f"capture:{order.order_id}",
                                            actor="system:fulfillment-worker",
                                            reason="supplier accepted fulfillment request", occurred_at=current)
            except PaymentBoundaryError:
                self.tasks.mark_failed(task, error="payment capture needs review", now=current)
                return FulfillmentRun(task.order_id, "capture_review", receipt, "payment capture needs review")
        self.tasks.mark_submitted(task, provider_order_id=receipt, now=current)
        return FulfillmentRun(task.order_id, "submitted", receipt)

    def _hold_uncertain(self, task: FulfillmentTask, order: GameOrder, now: datetime,
                        *, provider_order_id: str | None = None) -> FulfillmentRun:
        message = "supplier outcome is uncertain; reconcile before retry or refund"
        attempt_reference = provider_order_id or task.provider_order_id
        if order.state == OrderState.FULFILLMENT_QUEUED:
            self.orders.transition(order.order_id, OrderState.UNKNOWN, at=now, reason=message,
                                   provider_order_id=attempt_reference)
        self.tasks.mark_failed(task, error=message, now=now)
        return FulfillmentRun(task.order_id, "failed", error=message)


def _task_from_row(row: sqlite3.Row, decrypt: Any) -> FulfillmentTask:
    try:
        payload = decrypt(row["payload_json"])
    except FulfillmentTaskError:
        raise
    return FulfillmentTask(
        order_id=str(row["order_id"]), fulfillment_kind=str(row["fulfillment_kind"]), payload=payload,
        status=str(row["status"]), attempts=int(row["attempts"]),
        available_at=_time(datetime.fromisoformat(str(row["available_at"]))),
        provider_order_id=row["provider_order_id"], last_error=row["last_error"],
        updated_at=_time(datetime.fromisoformat(str(row["updated_at"]))),
        dispatch_started_at=row["dispatch_started_at"],
    )


def _provider_response(value: object) -> tuple[str, str | None]:
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise SupplierCommandError("supplier returned an unsuccessful order response")
    order = value.get("order")
    if not isinstance(order, dict):
        raise SupplierCommandError("supplier order response was malformed")
    provider_id = order.get("id")
    if not isinstance(provider_id, str) or not _PROVIDER_ORDER_ID.fullmatch(provider_id):
        raise SupplierCommandError("supplier order response omitted a valid order id")
    status = order.get("status")
    if status is not None and (not isinstance(status, str) or len(status) > 80):
        raise SupplierCommandError("supplier order response contained invalid status")
    return provider_id, status

def _supplier_attempt_reference(
    supplier: SupplierOrderPort, *, payload: Mapping[str, Any], idempotency_key: str,
) -> str | None:
    # Inspect the class so dynamic mocks cannot invent a callable method.
    builder = getattr(type(supplier), "attempt_reference", None)
    if not callable(builder):
        return None
    value = builder(supplier, payload=payload, idempotency_key=idempotency_key)
    return value if isinstance(value, str) and _PROVIDER_ORDER_ID.fullmatch(value) else None


def _payload(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping) or not value:
        raise FulfillmentInputError("fulfillment payload must be a non-empty object")
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise FulfillmentInputError("fulfillment payload must be finite JSON") from exc
    if len(encoded) > 16 * 1024:
        raise FulfillmentInputError("fulfillment payload is too large")
    return encoded


def _required_payload_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise FulfillmentInputError(f"fulfillment payload field {key} is invalid")
    return value.strip()


def _required_payload_mapping(payload: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping) or not value:
        raise FulfillmentInputError(f"fulfillment payload field {key} is invalid")
    return dict(value)


def _required_payload_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100000:
        raise FulfillmentInputError(f"fulfillment payload field {key} is invalid")
    return value


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise FulfillmentInputError(f"{label} is invalid")
    if any(char in value for char in "\r\n"):
        raise FulfillmentInputError(f"{label} is invalid")
    return value.strip()


def _provider_order(value: object) -> str:
    if not isinstance(value, str) or not _PROVIDER_ORDER_ID.fullmatch(value):
        raise FulfillmentInputError("provider_order_id is invalid")
    return value


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise FulfillmentInputError(f"{label} is invalid")
    if any(char in value for char in "\r\n"):
        raise FulfillmentInputError(f"{label} is invalid")
    return value.strip()


def _time(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FulfillmentInputError("time must be timezone-aware")
    return value.astimezone(UTC)


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(3600, max(1, 2 ** min(attempts, 10))))


__all__ = [
    "FazerCardsOrderAdapter",
    "SupplierOrderRouter",
    "FulfillmentInputError",
    "FulfillmentRun",
    "FulfillmentTask",
    "FulfillmentTaskError",
    "FulfillmentTaskStore",
    "GameFulfillmentWorker",
    "SupplierOrderPort",
]
