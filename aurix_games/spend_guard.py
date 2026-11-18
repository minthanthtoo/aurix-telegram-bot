"""Private pilot preflight and durable USD budget for first supplier dispatch."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import time

from persistence import open_sqlite_connection
from .commerce_settings import env_commerce_settings
from .fazercards_account import _decimal, parse_supplier_wallet_snapshot


class SpendPreflightError(ValueError):
    """No purchase was sent; the paid order needs an operator decision."""


class SpendClaimLost(SpendPreflightError):
    """Another worker owns the dispatch; this worker must not change the order."""


class SpendOutcomeUnknown(SpendPreflightError):
    """Saved spending evidence requires reconciliation rather than another POST."""


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def schema_fingerprint(fields):
    return hashlib.sha256(_canonical(fields).encode()).hexdigest()


class PilotSpendGuard:
    def __init__(self, *, tasks, client, catalog_client, checkout, policy):
        required = {"daily_limit_usd", "minimum_balance_usd", "offers"}
        if not isinstance(policy, dict) or not required <= set(policy) or set(policy) - required - {"lifetime_limit_usd"}:
            raise ValueError("invalid pilot spend policy")
        self.daily_limit = None if policy["daily_limit_usd"] is None else _decimal(policy["daily_limit_usd"], "daily limit")
        self.minimum_balance = _decimal(policy["minimum_balance_usd"], "minimum balance")
        self.lifetime_limit = _decimal(policy["lifetime_limit_usd"], "total limit") if "lifetime_limit_usd" in policy else None
        if (self.daily_limit is not None and self.daily_limit <= 0) or self.minimum_balance < 0:
            raise ValueError("pilot budget must be positive and minimum balance nonnegative")
        if self.lifetime_limit is not None and self.lifetime_limit <= 0:
            raise ValueError("pilot total spend limit must be positive")
        self.offers = {}
        if not isinstance(policy["offers"], list) or not 1 <= len(policy["offers"]) <= 30:
            raise ValueError("pilot spend policy requires 1 to 30 reviewed offers")
        for row in policy["offers"]:
            if not isinstance(row, dict) or set(row) != {"category_id", "offer_id", "max_cost_usd", "allow_unknown_stock", "fields_sha256"}:
                raise ValueError("invalid pilot spend offer")
            key = (row["category_id"], row["offer_id"])
            if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value) for value in key):
                raise ValueError("invalid pilot spend offer identity")
            cap = _decimal(row["max_cost_usd"], "offer ceiling")
            if (key in self.offers or cap <= 0 or (self.daily_limit is not None and cap > self.daily_limit)
                    or type(row["allow_unknown_stock"]) is not bool
                    or not isinstance(row["fields_sha256"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", row["fields_sha256"])):
                raise ValueError("invalid pilot offer ceiling, stock policy or schema approval")
            self.offers[key] = (cap, row["allow_unknown_stock"], row["fields_sha256"])
        self.tasks, self.client, self.catalog, self.checkout = tasks, client, catalog_client, checkout
        tasks.initialize()
        with open_sqlite_connection(tasks.path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS game_supplier_spend (
                order_id TEXT PRIMARY KEY REFERENCES game_fulfillment_tasks(order_id),
                payload_hash TEXT NOT NULL, budget_day TEXT NOT NULL, ceiling_usd TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('held','accepted','disabled')),
                provider_order_id TEXT, created_at TEXT NOT NULL)""")

    @classmethod
    def from_file(cls, path, **kwargs):
        source = Path(path)
        if (source.is_symlink() or not source.is_file() or source.stat().st_mode & 0o077
                or source.parent.stat().st_mode & 0o077 or source.stat().st_size > 65536):
            raise ValueError("pilot spend policy must be a private regular file")
        return cls(policy=json.loads(source.read_text()), **kwargs)

    def check_supplier(self, payload, *, now):
        if now.utcoffset() is None:
            raise SpendPreflightError("timezone-aware preflight time required")
        approved = self.offers.get((payload.get("category_id"), payload.get("offer_id")))
        if approved is None:
            raise SpendPreflightError("offer has no approved dispatch cost ceiling")
        cap, allow_unknown, expected_schema = approved
        started = time.monotonic()
        subscription = self.client.get_subscription()
        try:
            expiry = datetime.fromisoformat(subscription["planExpiresAt"].replace("Z", "+00:00"))
            if (subscription.get("ok") is not True or subscription.get("subscriptionActive") is not True
                    or expiry.utcoffset() is None or expiry <= now):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise SpendPreflightError("supplier subscription is inactive, expired or unconfirmed") from None
        detail = self.catalog.get("/topups/offers", category_id=payload["category_id"])
        if (detail.get("category_id") != payload["category_id"]
                or not isinstance(detail.get("fields"), list)
                or schema_fingerprint(detail["fields"]) != expected_schema
                or not isinstance(detail.get("offers"), list)):
            raise SpendPreflightError("supplier schema changed or category could not be verified")
        matches = [row for row in detail["offers"] if isinstance(row, dict) and row.get("offer_id") == payload["offer_id"]]
        if len(matches) != 1:
            raise SpendPreflightError("supplier offer is missing or ambiguous")
        offer = matches[0]
        cost = _decimal(offer.get("price_usd"), "live cost")
        stock = offer.get("stock")
        if cost <= 0 or cost > cap:
            raise SpendPreflightError("supplier cost exceeds the approved dispatch ceiling")
        if (stock is None and not allow_unknown) or (stock is not None and (type(stock) is not int or stock < 1)):
            raise SpendPreflightError("supplier stock does not meet the approved policy")
        balance = parse_supplier_wallet_snapshot(self.client.get_balance(), observed_at=now)
        if time.monotonic() - started > 30:
            raise SpendPreflightError("supplier preflight exceeded its 30-second freshness window")
        if balance.balance_usd < cap + self.minimum_balance:
            raise SpendPreflightError("supplier balance is below the purchase ceiling and required reserve")
        return cap, started

    def check_payment(self, payload, *, now):
        cap, _ = self.check_supplier(payload, now=now)
        with open_sqlite_connection(self.tasks.path) as db:
            rows = db.execute("SELECT * FROM game_supplier_spend").fetchall()
        self._check_budget(rows, cap, now.astimezone(timezone.utc).date().isoformat())

    def _check_budget(self, rows, cap, day):
        if any(row["state"] == "held" for row in rows):
            raise SpendPreflightError("another supplier purchase has an unresolved budget reservation")
        used = sum((Decimal(row["ceiling_usd"]) for row in rows
            if row["budget_day"] == day and row["state"] != "disabled"), Decimal(0))
        settings = env_commerce_settings().status()
        daily = settings["settings"]["daily_limit_usd"] if settings["configured"] else self.daily_limit
        if daily is not None and used + cap > Decimal(daily):
            raise SpendPreflightError("daily supplier spend ceiling would be exceeded")
        total = sum((Decimal(row["ceiling_usd"]) for row in rows if row["state"] != "disabled"), Decimal(0))
        if self.lifetime_limit is not None and total + cap > self.lifetime_limit:
            raise SpendPreflightError("total supplier acceptance spend ceiling would be exceeded")

    def prepare(self, task, order, *, now):
        if task.fulfillment_kind != "direct_topup":
            raise SpendPreflightError("pilot spend policy permits reviewed direct top-ups only")
        if self.checkout.fulfillment_payload(customer_id=order.customer_id, order_id=order.order_id) != task.payload:
            raise SpendPreflightError("approved recipient or supplier route changed before dispatch")
        cap, started = self.check_supplier(task.payload, now=now)
        payload_hash = schema_fingerprint(task.payload)
        day = now.astimezone(timezone.utc).date().isoformat()
        with open_sqlite_connection(self.tasks.path) as db:
            db.execute("BEGIN IMMEDIATE")
            # A stale worker must not reserve a budget after another worker reclaims its task.
            claim = db.execute("SELECT status,attempts FROM game_fulfillment_tasks WHERE order_id=?", (order.order_id,)).fetchone()
            if claim is None or claim["status"] != "running" or claim["attempts"] != task.attempts:
                raise SpendClaimLost("dispatch task claim is no longer current")
            rows = db.execute("SELECT * FROM game_supplier_spend").fetchall()
            existing = next((row for row in rows if row["order_id"] == order.order_id), None)
            if existing and existing["state"] != "disabled":
                if existing["state"] == "accepted":
                    raise SpendOutcomeUnknown("accepted supplier budget has no recoverable order receipt")
                if existing["payload_hash"] != payload_hash or Decimal(existing["ceiling_usd"]) != cap:
                    raise SpendPreflightError("saved supplier budget belongs to different purchase details")
                return
            self._check_budget(rows, cap, day)
            db.execute("""INSERT INTO game_supplier_spend VALUES (?,?,?,?, 'held',NULL,?)
                ON CONFLICT(order_id) DO UPDATE SET budget_day=excluded.budget_day,
                ceiling_usd=excluded.ceiling_usd,state='held',created_at=excluded.created_at""",
                (order.order_id, payload_hash, day, str(cap), now.isoformat()))
        # Hold the budget before the final balance read so another local purchase cannot pass on the same stale float.
        try:
            fresh_balance = parse_supplier_wallet_snapshot(self.client.get_balance(), observed_at=now)
            if fresh_balance.balance_usd < cap + self.minimum_balance:
                raise SpendPreflightError("supplier balance changed before dispatch")
            if time.monotonic() - started > 30:
                raise SpendPreflightError("supplier preflight exceeded its 30-second freshness window")
        except Exception:
            self.disabled(order.order_id)
            raise

    def accepted(self, order_id, provider_order_id):
        with open_sqlite_connection(self.tasks.path) as db:
            row = db.execute("SELECT * FROM game_supplier_spend WHERE order_id=?", (order_id,)).fetchone()
            # Receipts saved before this guard was deployed remain recoverable without a new POST.
            if row is None:
                return
            if row["provider_order_id"] not in (None, provider_order_id) or row["state"] == "disabled":
                raise SpendPreflightError("supplier receipt conflicts with the saved budget")
            db.execute("UPDATE game_supplier_spend SET state='accepted',provider_order_id=? WHERE order_id=?",
                (provider_order_id, order_id))

    def disabled(self, order_id):
        with open_sqlite_connection(self.tasks.path) as db:
            db.execute("UPDATE game_supplier_spend SET state='disabled' WHERE order_id=? AND state='held'", (order_id,))
