"""Supplier-wallet monitoring and funding workflow.

This module is the operator boundary around FazerCards account APIs.  Reads
can be polled by a maintenance job; funding writes still require the command
client's explicit ``allow_writes=True`` switch.  The state objects are
serializable so a restart can resume from the last invoice/status observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
import stat
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit
from persistence import open_sqlite_connection

from .providers.fazercards_commands import FazerCardsCommandClient, SupplierWriteDisabledError


UTC = timezone.utc


class SupplierAccountInputError(ValueError):
    """An account snapshot, threshold, or funding intent is invalid."""


class SupplierAccountError(RuntimeError):
    """A supplier-account operation failed without exposing upstream data."""


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise SupplierAccountInputError(f"{label} must be a finite decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise SupplierAccountInputError(f"{label} must be a finite decimal") from None
    if not result.is_finite():
        raise SupplierAccountInputError(f"{label} must be a finite decimal")
    return result


def _aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SupplierAccountInputError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class SupplierWalletPolicy:
    """Low-balance and replenishment policy owned by the Games operator."""

    low_balance_threshold_usd: Decimal = Decimal("10")
    suggested_topup_usd: Decimal = Decimal("50")
    reminder_cooldown: timedelta = timedelta(hours=6)

    def __post_init__(self) -> None:
        threshold = _decimal(self.low_balance_threshold_usd, "low_balance_threshold_usd")
        suggested = _decimal(self.suggested_topup_usd, "suggested_topup_usd")
        if threshold < 0 or suggested <= 0:
            raise SupplierAccountInputError("wallet amounts are invalid")
        if not isinstance(self.reminder_cooldown, timedelta) or self.reminder_cooldown <= timedelta(0):
            raise SupplierAccountInputError("reminder_cooldown must be positive")


@dataclass(frozen=True)
class SupplierWalletSnapshot:
    balance_usd: Decimal
    observed_at: datetime
    currency: str = "USD"

    def __post_init__(self) -> None:
        balance = _decimal(self.balance_usd, "balance_usd")
        if balance < 0:
            raise SupplierAccountInputError("balance_usd cannot be negative")
        if self.currency != "USD":
            raise SupplierAccountInputError("supplier wallet currency must be USD")
        _aware(self.observed_at, "observed_at")

    def as_dict(self) -> dict[str, str]:
        return {
            "balance_usd": format(_decimal(self.balance_usd, "balance_usd"), "f"),
            "currency": self.currency,
            "observed_at": _aware(self.observed_at, "observed_at").isoformat(),
        }


def parse_supplier_wallet_snapshot(
    payload: Mapping[str, Any], *, observed_at: datetime
) -> SupplierWalletSnapshot:
    """Parse the documented GET /balance response without a zero fallback."""

    if not isinstance(payload, Mapping) or payload.get("ok") is not True:
        raise SupplierAccountInputError("supplier balance response is invalid")
    raw_balance = payload.get("balance")
    currency = payload.get("currency")
    # OpenAPI requires a decimal string and the literal USD currency.
    if not isinstance(raw_balance, str) or not raw_balance.strip():
        raise SupplierAccountInputError("supplier balance response omitted a string balance")
    if currency != "USD":
        raise SupplierAccountInputError("supplier balance response currency is invalid")
    return SupplierWalletSnapshot(_decimal(raw_balance, "balance"), observed_at, currency=currency)


@dataclass(frozen=True)
class LowWalletDecision:
    is_low: bool
    should_remind: bool
    reason: str
    balance_usd: Decimal
    threshold_usd: Decimal
    suggested_topup_usd: Decimal
    alert_key: str


def evaluate_low_wallet(
    snapshot: SupplierWalletSnapshot,
    policy: SupplierWalletPolicy,
    *,
    now: datetime,
    last_reminder_at: datetime | None = None,
) -> LowWalletDecision:
    """Decide whether the admin should receive one deduplicated reminder."""

    current = _aware(now, "now")
    observed = _aware(snapshot.observed_at, "observed_at")
    if observed > current:
        raise SupplierAccountInputError("wallet snapshot cannot be from the future")
    balance = _decimal(snapshot.balance_usd, "balance_usd")
    threshold = _decimal(policy.low_balance_threshold_usd, "low_balance_threshold_usd")
    suggested = _decimal(policy.suggested_topup_usd, "suggested_topup_usd")
    alert_key = "fazercards:supplier-wallet:low-balance"
    if balance >= threshold:
        return LowWalletDecision(False, False, "above_threshold", balance, threshold, suggested, alert_key)
    if last_reminder_at is None:
        return LowWalletDecision(True, True, "below_threshold", balance, threshold, suggested, alert_key)
    last = _aware(last_reminder_at, "last_reminder_at")
    if last > current:
        raise SupplierAccountInputError("last_reminder_at cannot be from the future")
    if current - last >= policy.reminder_cooldown:
        return LowWalletDecision(True, True, "cooldown_elapsed", balance, threshold, suggested, alert_key)
    return LowWalletDecision(True, False, "reminder_cooldown", balance, threshold, suggested, alert_key)


@dataclass(frozen=True)
class WalletFundingIntent:
    """Restart-safe local record for one supplier-wallet payment invoice."""

    intent_id: str
    payment_id: str
    method: str
    amount_usd: Decimal
    status: str
    network: str | None
    address: str | None
    unique_amount: str | None
    memo: str | None
    expires_at: str | None
    created_at: str | None
    payment_url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "payment_id": self.payment_id,
            "method": self.method,
            "amount_usd": format(self.amount_usd, "f"),
            "status": self.status,
            "network": self.network,
            "address": self.address,
            "unique_amount": self.unique_amount,
            "memo": self.memo,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "payment_url": self.payment_url,
        }


class WalletFundingIntentStore:
    """Private SQLite store preventing duplicate invoices across restarts."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        if self.path.is_symlink():
            raise SupplierAccountInputError("funding-intent database must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077:
            raise SupplierAccountInputError("funding-intent database directory must be private")
        try:
            with open_sqlite_connection(self.path, timeout_seconds=5) as connection:
                connection.execute("PRAGMA journal_mode = DELETE")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS supplier_wallet_funding_intents (
                           intent_id TEXT PRIMARY KEY,
                           payload_json TEXT NOT NULL,
                           created_at TEXT NOT NULL
                       )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS supplier_wallet_funding_attempts (
                        intent_id TEXT PRIMARY KEY,
                        method TEXT NOT NULL,
                        amount_usd TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS supplier_wallet_funding_resolutions (
                        intent_id TEXT PRIMARY KEY REFERENCES supplier_wallet_funding_attempts(intent_id),
                        provider_reference TEXT NOT NULL,
                        reviewer TEXT NOT NULL,
                        resolution TEXT NOT NULL CHECK(resolution = 'confirmed_absent'),
                        created_at TEXT NOT NULL
                    )"""
                )
                columns = {row[1] for row in connection.execute("PRAGMA table_info(supplier_wallet_funding_attempts)")}
                if "reviewer" not in columns:
                    connection.execute("ALTER TABLE supplier_wallet_funding_attempts ADD COLUMN reviewer TEXT")
                connection.execute("""CREATE TRIGGER IF NOT EXISTS supplier_wallet_funding_resolutions_immutable
                    BEFORE UPDATE ON supplier_wallet_funding_resolutions
                    BEGIN SELECT RAISE(ABORT, 'funding resolutions are immutable'); END""")
                connection.execute("""CREATE TRIGGER IF NOT EXISTS supplier_wallet_funding_resolutions_no_delete
                    BEFORE DELETE ON supplier_wallet_funding_resolutions
                    BEGIN SELECT RAISE(ABORT, 'funding resolutions are retained'); END""")
                connection.execute("""CREATE TABLE IF NOT EXISTS supplier_wallet_funding_links (
                    intent_id TEXT PRIMARY KEY REFERENCES supplier_wallet_funding_attempts(intent_id),
                    payment_id TEXT NOT NULL UNIQUE, provider_reference TEXT NOT NULL,
                    reviewer TEXT NOT NULL, created_at TEXT NOT NULL)""")
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(f"""CREATE TRIGGER IF NOT EXISTS funding_links_no_{operation.lower()}
                        BEFORE {operation} ON supplier_wallet_funding_links
                        BEGIN SELECT RAISE(ABORT, 'funding links are immutable'); END""")
        except sqlite3.Error as exc:
            raise SupplierAccountInputError("funding-intent database initialization failed") from exc
        self.path.chmod(0o600)

    def get(self, intent_id: str) -> WalletFundingIntent | None:
        key = _nonempty(intent_id, "intent_id", 128)
        self.initialize()
        try:
            with open_sqlite_connection(self.path, timeout_seconds=5) as connection:
                row = connection.execute(
                    "SELECT payload_json FROM supplier_wallet_funding_intents WHERE intent_id = ?",
                    (key,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise SupplierAccountInputError("funding-intent database read failed") from exc
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
            return _intent_from_record(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SupplierAccountInputError("stored funding intent is malformed") from exc

    def put(self, intent: WalletFundingIntent) -> WalletFundingIntent:
        if not isinstance(intent, WalletFundingIntent):
            raise SupplierAccountInputError("funding intent has unsupported type")
        self.initialize()
        payload = json.dumps(intent.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        try:
            with open_sqlite_connection(self.path, timeout_seconds=5) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT payload_json FROM supplier_wallet_funding_intents WHERE intent_id = ?",
                    (intent.intent_id,),
                ).fetchone()
                if existing is not None:
                    if existing[0] != payload:
                        raise SupplierAccountInputError("funding intent id was reused with different details")
                    return intent
                if connection.execute("SELECT 1 FROM supplier_wallet_funding_resolutions WHERE intent_id=?",
                        (intent.intent_id,)).fetchone():
                    raise SupplierAccountInputError("funding attempt was resolved absent; provider outcome needs review")
                if connection.execute("""SELECT 1 FROM supplier_wallet_funding_intents
                    WHERE json_extract(payload_json, '$.payment_id')=?""", (intent.payment_id,)).fetchone():
                    raise SupplierAccountInputError("provider invoice is already assigned to another funding request")
                connection.execute(
                    "INSERT INTO supplier_wallet_funding_intents(intent_id, payload_json, created_at) VALUES (?, ?, ?)",
                    (intent.intent_id, payload, datetime.now(UTC).isoformat()),
                )
                return intent
        except SupplierAccountInputError:
            raise
        except sqlite3.Error as exc:
            raise SupplierAccountInputError("funding intent could not be saved") from exc

    def claim_creation(self, intent_id: str, method: str, amount: Decimal, *, reviewer: str | None = None) -> bool:
        """Reserve an external create before I/O; an unresolved attempt cannot replay."""
        self.initialize()
        try:
            with open_sqlite_connection(self.path, timeout_seconds=5) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute("SELECT 1 FROM supplier_wallet_funding_intents WHERE intent_id=?", (intent_id,)).fetchone():
                    return False
                if connection.execute("SELECT 1 FROM supplier_wallet_funding_attempts WHERE intent_id=?", (intent_id,)).fetchone():
                    raise SupplierAccountInputError("funding creation is in progress or uncertain; reconcile before another invoice")
                unresolved = connection.execute("""SELECT attempts.intent_id FROM supplier_wallet_funding_attempts attempts
                    LEFT JOIN supplier_wallet_funding_intents intents USING(intent_id)
                    LEFT JOIN supplier_wallet_funding_resolutions resolutions USING(intent_id)
                    WHERE intents.intent_id IS NULL AND resolutions.intent_id IS NULL LIMIT 1""").fetchone()
                if unresolved is not None:
                    raise SupplierAccountInputError(
                        "a previous funding creation is uncertain; verify it with FazerCards before another invoice"
                    )
                connection.execute("""INSERT INTO supplier_wallet_funding_attempts
                    (intent_id,method,amount_usd,created_at,reviewer) VALUES (?, ?, ?, ?, ?)""",
                    (intent_id, method, format(amount, "f"), datetime.now(UTC).isoformat(), reviewer))
                return True
        except sqlite3.Error as exc:
            raise SupplierAccountInputError("funding creation could not be reserved") from exc

    def unresolved_attempts(self) -> tuple[dict[str, str], ...]:
        self.initialize()
        try:
            with open_sqlite_connection(self.path, timeout_seconds=5) as connection:
                rows = connection.execute("""SELECT attempts.intent_id, attempts.method,
                    attempts.amount_usd, attempts.created_at FROM supplier_wallet_funding_attempts attempts
                    LEFT JOIN supplier_wallet_funding_intents intents USING(intent_id)
                    LEFT JOIN supplier_wallet_funding_resolutions resolutions USING(intent_id)
                    WHERE intents.intent_id IS NULL AND resolutions.intent_id IS NULL
                    ORDER BY attempts.created_at, attempts.intent_id""").fetchall()
        except sqlite3.Error as exc:
            raise SupplierAccountInputError("funding reconciliation state could not be read") from exc
        return tuple({"intent_id": row["intent_id"], "method": row["method"],
            "amount_usd": row["amount_usd"], "created_at": row["created_at"]} for row in rows)

    def resolve_confirmed_absent(
        self,
        intent_id: str,
        *,
        provider_reference: str,
        reviewer: str,
        confirmed_absent: bool,
    ) -> dict[str, str]:
        """Clear a lost-create fence only after an operator checks provider evidence."""
        key = _nonempty(intent_id, "intent_id", 128)
        reference = _nonempty(provider_reference, "provider_reference", 128)
        actor = _nonempty(reviewer, "reviewer", 96)
        if confirmed_absent is not True:
            raise SupplierAccountInputError("explicit provider-confirmed no-invoice attestation is required")
        if any(ord(char) < 32 for char in reference + actor):
            raise SupplierAccountInputError("funding resolution details are invalid")
        self.initialize()
        created_at = datetime.now(UTC).isoformat()
        try:
            with open_sqlite_connection(self.path, timeout_seconds=5) as connection:
                connection.execute("BEGIN IMMEDIATE")
                attempt = connection.execute("""SELECT 1 FROM supplier_wallet_funding_attempts
                    WHERE intent_id=?""", (key,)).fetchone()
                saved = connection.execute("""SELECT provider_reference, reviewer, resolution
                    FROM supplier_wallet_funding_resolutions WHERE intent_id=?""", (key,)).fetchone()
                if saved is not None:
                    if (saved["provider_reference"], saved["reviewer"], saved["resolution"]) != (
                        reference, actor, "confirmed_absent"
                    ):
                        raise SupplierAccountInputError("funding reconciliation already has a different resolution")
                    return {"intent_id": key, "status": "confirmed_absent", "replayed": "true"}
                if attempt is None:
                    raise SupplierAccountInputError("unknown funding attempt was not found")
                if connection.execute("SELECT 1 FROM supplier_wallet_funding_intents WHERE intent_id=?", (key,)).fetchone():
                    raise SupplierAccountInputError("funding invoice is already saved; refresh its provider status")
                connection.execute("""INSERT INTO supplier_wallet_funding_resolutions
                    (intent_id, provider_reference, reviewer, resolution, created_at)
                    VALUES (?, ?, ?, 'confirmed_absent', ?)""", (key, reference, actor, created_at))
                return {"intent_id": key, "status": "confirmed_absent", "replayed": "false"}
        except SupplierAccountInputError:
            raise
        except sqlite3.Error as exc:
            raise SupplierAccountInputError("funding resolution could not be recorded") from exc

    def release_disabled_creation(self, intent_id: str) -> None:
        """Release only a locally disabled call, which never reached the provider."""
        with open_sqlite_connection(self.path, timeout_seconds=5) as connection:
            connection.execute("DELETE FROM supplier_wallet_funding_attempts WHERE intent_id=? AND NOT EXISTS (SELECT 1 FROM supplier_wallet_funding_intents WHERE intent_id=?)", (intent_id, intent_id))

    def link_discovered(self, intent: WalletFundingIntent, *, provider_reference: str,
                        reviewer: str, confirmed_match: bool) -> WalletFundingIntent:
        """Link an authenticated provider read plus explicit support evidence atomically."""
        reference = _nonempty(provider_reference, "provider_reference", 128)
        actor = _nonempty(reviewer, "reviewer", 96)
        if confirmed_match is not True or any(ord(c) < 32 for c in reference + actor):
            raise SupplierAccountInputError("explicit provider-confirmed invoice match is required")
        self.initialize()
        with open_sqlite_connection(self.path, timeout_seconds=5) as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute("SELECT * FROM supplier_wallet_funding_attempts WHERE intent_id=?",
                (intent.intent_id,)).fetchone()
            if attempt is None or attempt["method"] != intent.method or Decimal(attempt["amount_usd"]) != intent.amount_usd:
                raise SupplierAccountInputError("invoice does not match the original funding attempt")
            linked = connection.execute("SELECT * FROM supplier_wallet_funding_links WHERE intent_id=?",
                (intent.intent_id,)).fetchone()
            if linked is not None:
                if (linked["payment_id"], linked["provider_reference"], linked["reviewer"]) != (intent.payment_id, reference, actor):
                    raise SupplierAccountInputError("funding attempt already has a different invoice resolution")
                row = connection.execute("SELECT payload_json FROM supplier_wallet_funding_intents WHERE intent_id=?",
                    (intent.intent_id,)).fetchone()
                return _intent_from_record(json.loads(row[0]))
            if connection.execute("SELECT 1 FROM supplier_wallet_funding_resolutions WHERE intent_id=?",
                    (intent.intent_id,)).fetchone():
                raise SupplierAccountInputError("funding attempt already has an absence resolution")
            if connection.execute("""SELECT 1 FROM supplier_wallet_funding_intents
                WHERE intent_id=? OR json_extract(payload_json, '$.payment_id')=?""",
                    (intent.intent_id, intent.payment_id)).fetchone():
                raise SupplierAccountInputError("invoice is already saved; reconcile its original funding request")
            created = datetime.now(UTC).isoformat()
            connection.execute("INSERT INTO supplier_wallet_funding_links VALUES (?,?,?,?,?)",
                (intent.intent_id, intent.payment_id, reference, actor, created))
            connection.execute("INSERT INTO supplier_wallet_funding_intents VALUES (?,?,?)",
                (intent.intent_id, json.dumps(intent.as_dict(), sort_keys=True, separators=(",", ":")), created))
        return intent


class _AccountClient(Protocol):
    def create_balance_payment(self, *, method: str, amount_usd: Any) -> dict[str, Any]: ...
    def get_payment_methods(self) -> dict[str, Any]: ...
    def get_payment(self, payment_id: str) -> dict[str, Any]: ...
    def verify_binance_payment(self, payment_id: str, *, binance_order_id: str) -> dict[str, Any]: ...


class WalletFundingService:
    """Create and reconcile supplier wallet invoices; never auto-retries writes."""

    def __init__(self, client: _AccountClient, *, intent_store: WalletFundingIntentStore | None = None) -> None:
        self.client = client
        self.intent_store = intent_store

    def payment_methods(self) -> tuple[dict[str, str], ...]:
        return parse_supplier_payment_methods(self.client.get_payment_methods())

    def recover_existing(self, *, intent_id: str, payment_id: str, provider_reference: str,
                         reviewer: str, confirmed_match: bool) -> WalletFundingIntent:
        if self.intent_store is None:
            raise SupplierAccountInputError("persistent funding intent storage is required")
        key = _nonempty(intent_id, "intent_id", 128)
        saved = self.intent_store.get(key)
        attempt = next((row for row in self.intent_store.unresolved_attempts() if row["intent_id"] == key), None)
        if attempt is None and saved is None:
            raise SupplierAccountInputError("unresolved funding attempt was not found")
        method = saved.method if saved else attempt["method"]
        amount = saved.amount_usd if saved else Decimal(attempt["amount_usd"])
        payload = self.client.get_payment(payment_id)
        payment = payload.get("payment") if isinstance(payload, dict) and payload.get("ok") is True else None
        if (not isinstance(payment, dict) or payment.get("id") != payment_id
                or str(payment.get("network", "")).lower() != method
                or _decimal(payment.get("amount"), "provider invoice amount") != amount):
            raise SupplierAccountInputError("authenticated invoice details do not match the funding attempt")
        intent = _intent_from_payload(key, method, amount, payload)
        return self.intent_store.link_discovered(intent, provider_reference=provider_reference,
            reviewer=reviewer, confirmed_match=confirmed_match)

    def create_intent(
        self,
        *,
        intent_id: str,
        method: str,
        amount_usd: str | int | float | Decimal,
        reviewer: str | None = None,
    ) -> WalletFundingIntent:
        local_id = _nonempty(intent_id, "intent_id", 128)
        normalized_method = _nonempty(method, "method", 32).lower()
        if normalized_method != "usdt":
            raise SupplierAccountInputError("unsupported funding method")
        amount = _decimal(amount_usd, "amount_usd")
        if amount <= 0:
            raise SupplierAccountInputError("amount_usd must be positive")
        if self.intent_store is not None:
            saved = self.intent_store.get(local_id)
            if saved is not None:
                if saved.method != normalized_method or saved.amount_usd != amount:
                    raise SupplierAccountInputError("funding intent id was reused with different details")
                return saved
        methods = self.payment_methods()
        method_limits = next((item for item in methods if item["code"] == normalized_method), None)
        if method_limits is None:
            raise SupplierAccountInputError("funding method is not enabled for this account")
        if not Decimal(method_limits["min_amount_usd"]) <= amount <= Decimal(method_limits["max_amount_usd"]):
            raise SupplierAccountInputError("funding amount is outside the current account limits")
        if self.intent_store is not None and not self.intent_store.claim_creation(local_id, normalized_method, amount, reviewer=reviewer):
            saved = self.intent_store.get(local_id)
            if saved is None or saved.method != normalized_method or saved.amount_usd != amount:
                raise SupplierAccountInputError("funding intent id was reused with different details")
            return saved
        try:
            payload = self.client.create_balance_payment(method=normalized_method, amount_usd=amount)
        except SupplierWriteDisabledError:
            if self.intent_store is not None:
                self.intent_store.release_disabled_creation(local_id)
            raise
        intent = _intent_from_payload(local_id, normalized_method, amount, payload)
        if self.intent_store is not None:
            self.intent_store.put(intent)
        return intent

    def refresh(self, intent: WalletFundingIntent) -> WalletFundingIntent:
        payment = self.client.get_payment(intent.payment_id)
        return _intent_from_payload(
            intent.intent_id,
            intent.method,
            intent.amount_usd,
            payment,
            fallback_payment_url=intent.payment_url,
        )

    def verify_binance(
        self, intent: WalletFundingIntent, *, binance_order_id: str
    ) -> WalletFundingIntent:
        if intent.method != "binancepay":
            raise SupplierAccountInputError("Binance verification is only for binancepay intents")
        payload = self.client.verify_binance_payment(
            intent.payment_id, binance_order_id=_nonempty(binance_order_id, "binance_order_id", 255)
        )
        return _intent_from_payload(
            intent.intent_id,
            intent.method,
            intent.amount_usd,
            payload,
            fallback_payment_url=intent.payment_url,
        )


def parse_supplier_payment_methods(payload: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """Validate account-specific supplier funding methods and amount limits."""

    if not isinstance(payload, Mapping) or payload.get("ok") is not True:
        raise SupplierAccountInputError("supplier payment-method response is invalid")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise SupplierAccountInputError("supplier payment-method response omitted methods")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise SupplierAccountInputError("supplier payment-method row is malformed")
        code = _nonempty(item.get("code"), "payment method code", 32).lower()
        label = _nonempty(item.get("label"), "payment method label", 128)
        minimum = _decimal(item.get("minAmountUsd"), "minimum amount")
        maximum = _decimal(item.get("maxAmountUsd"), "maximum amount")
        if code in seen or minimum <= 0 or maximum < minimum:
            raise SupplierAccountInputError("supplier payment-method limits are malformed")
        seen.add(code)
        result.append({
            "code": code,
            "label": label,
            "min_amount_usd": format(minimum, "f"),
            "max_amount_usd": format(maximum, "f"),
        })
    return tuple(result)

def _intent_from_payload(
    intent_id: str,
    method: str,
    amount_usd: Decimal,
    payload: dict[str, Any],
    *,
    fallback_payment_url: str | None = None,
) -> WalletFundingIntent:
    if not isinstance(payload, dict):
        raise SupplierAccountError("supplier payment response was malformed")
    payment = payload.get("payment")
    if not isinstance(payment, dict):
        raise SupplierAccountError("supplier payment response omitted payment details")
    payment_id = _nonempty(payment.get("id"), "payment_id", 128)
    status = _nonempty(payment.get("status", "unknown"), "status", 64)
    for key in ("network", "address", "uniqueAmount", "memo", "expiresAt", "createdAt"):
        value = payment.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 2048):
            raise SupplierAccountError("supplier payment response contained malformed metadata")
    return WalletFundingIntent(
        intent_id=intent_id,
        payment_id=payment_id,
        method=method,
        amount_usd=amount_usd,
        status=status,
        network=payment.get("network"),
        address=payment.get("address"),
        unique_amount=payment.get("uniqueAmount"),
        memo=payment.get("memo"),
        expires_at=payment.get("expiresAt"),
        created_at=payment.get("createdAt"),
        payment_url=safe_payment_url(payment.get("paymentUrl") or fallback_payment_url),
    )


def _intent_from_record(payload: Any) -> WalletFundingIntent:
    if not isinstance(payload, dict):
        raise ValueError("funding intent record must be an object")
    return WalletFundingIntent(
        intent_id=_nonempty(payload.get("intent_id"), "intent_id", 128),
        payment_id=_nonempty(payload.get("payment_id"), "payment_id", 128),
        method=_nonempty(payload.get("method"), "method", 32).lower(),
        amount_usd=_decimal(payload.get("amount_usd"), "amount_usd"),
        status=_nonempty(payload.get("status"), "status", 64),
        network=_optional_text(payload.get("network"), "network"),
        address=_optional_text(payload.get("address"), "address"),
        unique_amount=_optional_text(payload.get("unique_amount"), "unique_amount"),
        memo=_optional_text(payload.get("memo"), "memo"),
        expires_at=_optional_text(payload.get("expires_at"), "expires_at"),
        created_at=_optional_text(payload.get("created_at"), "created_at"),
        payment_url=safe_payment_url(payload.get("payment_url")),
    )


def safe_payment_url(value: Any) -> str | None:
    """Keep only absolute HTTPS payment links without embedded credentials."""

    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    if any(character.isspace() or ord(character) < 0x20 for character in value):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and port != 443)
    ):
        return None
    return value


def _optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _nonempty(value, label, 2048)


def _nonempty(value: Any, label: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise SupplierAccountInputError(f"{label} is invalid")
    if any(char in value for char in "\r\n"):
        raise SupplierAccountInputError(f"{label} is invalid")
    return value.strip()


__all__ = [
    "LowWalletDecision",
    "SupplierAccountError",
    "SupplierAccountInputError",
    "SupplierAccountError",
    "SupplierWalletPolicy",
    "SupplierWalletSnapshot",
    "WalletFundingIntent",
    "WalletFundingIntentStore",
    "WalletFundingService",
    "evaluate_low_wallet",
    "parse_supplier_payment_methods",
    "parse_supplier_wallet_snapshot",
    "safe_payment_url",
]
