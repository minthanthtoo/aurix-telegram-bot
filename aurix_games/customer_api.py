"""Games-owned customer sign-in and owner-scoped history boundary.

This module verifies Telegram/Google proofs, stores explicit provider links,
and issues short-lived Games sessions. It never reuses VPN/AI cookies, creates
orders, or accepts payment evidence.
"""

from __future__ import annotations

import base64
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import uuid
from typing import Any
from persistence import open_sqlite_connection

from .customer_ledger import CustomerLedger, LedgerInputError, LedgerOperationError
from .order_repository import GameOrderRepository, OrderNotFoundError
from .order_state import GameOrder, OrderState
from telegram_web_app import TelegramWebAppAuthError, verify_login_widget


UTC = timezone.utc
_CUSTOMER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SESSION_COOKIE_NAME = "aurix_games_session"
_AUTH_PROVIDERS = {"google", "telegram"}
_GOOGLE_SUBJECT = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


class CustomerAuthError(PermissionError):
    """A customer principal token is missing or invalid."""


class CustomerApiError(ValueError):
    """A customer API operation is malformed or unavailable."""


class CustomerIdentityConflict(CustomerApiError):
    """An identity is already linked to another Games customer."""


@dataclass(frozen=True)
class CustomerPrincipal:
    customer_id: str
    issuer: str
    expires_at: datetime
    session_id: str | None = None


class SignedCustomerTokenVerifier:
    """Verify compact ``v1.<payload>.<signature>`` HMAC principal tokens."""

    def __init__(self, secret: str | bytes, *, issuer: str = "aurix-identity", session_store: CustomerIdentityStore | None = None) -> None:
        try:
            raw = secret.encode("utf-8") if isinstance(secret, str) else secret
        except UnicodeError as exc:
            raise ValueError("customer auth secret is invalid") from exc
        if not isinstance(raw, bytes) or len(raw) < 32:
            raise ValueError("customer auth secret must be at least 32 bytes")
        if not isinstance(issuer, str) or not issuer.strip() or len(issuer) > 80:
            raise ValueError("customer auth issuer is invalid")
        self._secret = raw
        self._issuer = issuer.strip()
        self._session_store = session_store

    def verify(
        self,
        authorization: str | None = None,
        *,
        cookie_header: str | None = None,
        now: datetime | None = None,
    ) -> CustomerPrincipal:
        token = authorization[7:].strip() if isinstance(authorization, str) and authorization.startswith("Bearer ") else ""
        if not token and cookie_header:
            from http.cookies import SimpleCookie

            cookie = SimpleCookie()
            try:
                cookie.load(cookie_header)
                morsel = cookie.get(SESSION_COOKIE_NAME)
            except Exception:
                morsel = None
            token = morsel.value if morsel else ""
        if not token:
            raise CustomerAuthError("customer authorization required")
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != "v1":
            raise CustomerAuthError("customer authorization is invalid")
        encoded_payload, encoded_signature = parts[1], parts[2]
        try:
            expected = hmac.new(
                self._secret,
                f"v1.{encoded_payload}".encode("ascii"),
                hashlib.sha256,
            ).digest()
            supplied = _b64decode(encoded_signature)
            if not hmac.compare_digest(expected, supplied):
                raise CustomerAuthError("customer authorization is invalid")
            payload = json.loads(_b64decode(encoded_payload))
        except (UnicodeEncodeError, ValueError, TypeError, json.JSONDecodeError):
            raise CustomerAuthError("customer authorization is invalid") from None
        if not isinstance(payload, dict):
            raise CustomerAuthError("customer authorization is invalid")
        customer_id = payload.get("sub")
        issuer = payload.get("iss")
        exp = payload.get("exp")
        if (
            not isinstance(customer_id, str)
            or not _CUSTOMER_ID.fullmatch(customer_id)
            or issuer != self._issuer
            or isinstance(exp, bool)
            or not isinstance(exp, int)
        ):
            raise CustomerAuthError("customer authorization is invalid")
        try:
            expires_at = datetime.fromtimestamp(exp, UTC)
        except (OverflowError, OSError, ValueError):
            raise CustomerAuthError("customer authorization is invalid") from None
        current = _aware(now or datetime.now(UTC))
        if expires_at <= current or expires_at - current > timedelta(days=31):
            raise CustomerAuthError("customer authorization is expired or has an invalid lifetime")
        session_id = payload.get("sid")
        if self._session_store is not None:
            if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", session_id):
                raise CustomerAuthError("customer session is invalid; sign in again")
            if not self._session_store.session_active(session_id, customer_id, issuer, exp, now=current):
                raise CustomerAuthError("customer session has ended; sign in again")
        return CustomerPrincipal(customer_id=customer_id, issuer=issuer, expires_at=expires_at, session_id=session_id)


def issue_test_token(
    customer_id: str,
    secret: str | bytes,
    *,
    expires_at: datetime,
    issuer: str = "aurix-identity",
) -> str:
    """Create a token for contract tests; the HTTP server never exposes this helper."""

    if not isinstance(customer_id, str) or not _CUSTOMER_ID.fullmatch(customer_id):
        raise ValueError("customer_id is invalid")
    if not isinstance(issuer, str) or not issuer.strip() or len(issuer) > 80:
        raise ValueError("issuer is invalid")
    return issue_customer_token(customer_id, secret, expires_at=expires_at, issuer=issuer)


def issue_customer_token(
    customer_id: str,
    secret: str | bytes,
    *,
    expires_at: datetime,
    issuer: str = "aurix-identity",
    session_id: str | None = None,
) -> str:
    """Issue a short-lived Games principal token for verified identities."""
    if not isinstance(customer_id, str) or not _CUSTOMER_ID.fullmatch(customer_id):
        raise ValueError("customer_id is invalid")
    if not isinstance(issuer, str) or not issuer.strip() or len(issuer) > 80:
        raise ValueError("issuer is invalid")
    current = _aware(expires_at)
    raw = secret.encode("utf-8") if isinstance(secret, str) else secret
    if not isinstance(raw, bytes) or len(raw) < 32:
        raise ValueError("secret must be at least 32 bytes")
    claims = {"exp": int(current.timestamp()), "iss": issuer.strip(), "sub": customer_id}
    if session_id is not None:
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", session_id):
            raise ValueError("session_id is invalid")
        claims["sid"] = session_id
    payload = _b64encode(
        json.dumps(
            claims,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    signature = _b64encode(hmac.new(raw, f"v1.{payload}".encode("ascii"), hashlib.sha256).digest())
    return f"v1.{payload}.{signature}"


class CustomerIdentityStore:
    """Games-owned identity links and short-lived, single-use auth proofs."""

    def __init__(self, database: str | Path) -> None:
        self.database = Path(database)
        if self.database.is_symlink():
            raise ValueError("customer identity database must not be a symlink")
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(self.database.parent.stat().st_mode) & 0o077:
            raise ValueError("customer identity database directory must be private (mode 0700)")
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS customer_identities (
                    provider TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (provider, subject),
                    UNIQUE (customer_id, provider)
                );
                CREATE TABLE IF NOT EXISTS customer_sessions (
                    digest TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    issuer TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS customer_sessions_expiry ON customer_sessions(expires_at);
                CREATE TABLE IF NOT EXISTS customer_auth_proofs (
                    purpose TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY (purpose, digest)
                );
                CREATE TABLE IF NOT EXISTS game_staff_admins (
                    customer_id TEXT PRIMARY KEY,
                    granted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS game_staff_access_events (
                    id INTEGER PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('grant', 'revoke')),
                    occurred_at TEXT NOT NULL
                );
                """
            )
        os.chmod(self.database, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def create_session(self, session_id: str, customer_id: str, issuer: str, expires_at: datetime, *, now: datetime) -> None:
        digest = hashlib.sha256(session_id.encode("ascii")).hexdigest()
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM customer_sessions WHERE expires_at <= ?", (int(_aware(now).timestamp()),))
            connection.execute(
                "INSERT INTO customer_sessions (digest, customer_id, issuer, expires_at) VALUES (?, ?, ?, ?)",
                (digest, customer_id, issuer, int(_aware(expires_at).timestamp())),
            )

    def session_active(self, session_id: str, customer_id: str, issuer: str, expires_at: int, *, now: datetime) -> bool:
        digest = hashlib.sha256(session_id.encode("ascii")).hexdigest()
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT 1 FROM customer_sessions WHERE digest = ? AND customer_id = ? AND issuer = ? AND expires_at = ? AND expires_at > ?",
                (digest, customer_id, issuer, expires_at, int(_aware(now).timestamp())),
            ).fetchone() is not None

    def revoke_session(self, session_id: str) -> None:
        digest = hashlib.sha256(session_id.encode("ascii")).hexdigest()
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM customer_sessions WHERE digest = ?", (digest,))

    def customer_for(self, provider: str, subject: str, *, now: datetime) -> str:
        _validate_identity(provider, subject)
        stamp = _aware(now).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT customer_id FROM customer_identities WHERE provider=? AND subject=?",
                (provider, subject),
            ).fetchone()
            if row is None:
                candidate = f"customer_{uuid.uuid4().hex}"
                connection.execute(
                    "INSERT INTO customer_identities(provider, subject, customer_id, created_at) VALUES (?, ?, ?, ?)",
                    (provider, subject, candidate, stamp),
                )
                row = connection.execute(
                    "SELECT customer_id FROM customer_identities WHERE provider=? AND subject=?",
                    (provider, subject),
                ).fetchone()
            connection.commit()
        return str(row["customer_id"])

    def link(self, customer_id: str, provider: str, subject: str, *, now: datetime) -> None:
        _validate_identity(provider, subject)
        if not _CUSTOMER_ID.fullmatch(customer_id):
            raise CustomerApiError("customer identity is invalid")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            linked = connection.execute(
                "SELECT customer_id FROM customer_identities WHERE provider=? AND subject=?",
                (provider, subject),
            ).fetchone()
            if linked and linked["customer_id"] != customer_id:
                raise CustomerIdentityConflict("This sign-in method is linked to another account")
            existing = connection.execute(
                "SELECT subject FROM customer_identities WHERE customer_id=? AND provider=?",
                (customer_id, provider),
            ).fetchone()
            if existing and existing["subject"] != subject:
                raise CustomerIdentityConflict("This account already has a linked sign-in method of this type")
            if not linked:
                connection.execute(
                    "INSERT INTO customer_identities(provider, subject, customer_id, created_at) VALUES (?, ?, ?, ?)",
                    (provider, subject, customer_id, _aware(now).isoformat()),
                )
            connection.commit()

    def providers_for(self, customer_id: str) -> tuple[str, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT provider FROM customer_identities WHERE customer_id=? ORDER BY provider",
                (customer_id,),
            ).fetchall()
        return tuple(str(row["provider"]) for row in rows)

    def staff_role(self, customer_id: str, owner_id: str) -> str | None:
        if not owner_id or not _CUSTOMER_ID.fullmatch(owner_id):
            return None
        with closing(self._connect()) as db:
            if db.execute(
                "SELECT COUNT(*) FROM customer_identities WHERE customer_id=? AND provider IN ('google', 'telegram')",
                (owner_id,),
            ).fetchone()[0] != 2:
                return None
            if customer_id == owner_id:
                return "super_admin"
            if db.execute("SELECT 1 FROM game_staff_admins WHERE customer_id=?", (customer_id,)).fetchone():
                return "admin"
        return None

    def staff_admins(self) -> list[dict]:
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute(
                "SELECT customer_id, granted_at FROM game_staff_admins ORDER BY granted_at, customer_id"
            )]

    def set_staff_admin(self, owner_id: str, customer_id: str, *, grant: bool, now: datetime) -> bool:
        if not isinstance(customer_id, str) or not _CUSTOMER_ID.fullmatch(customer_id) or customer_id == owner_id:
            raise CustomerApiError("Enter a registered store account ID other than the super admin.")
        when = _aware(now).isoformat()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if grant:
                if not db.execute("SELECT 1 FROM customer_identities WHERE customer_id=?", (customer_id,)).fetchone():
                    raise CustomerApiError("No registered store account has that ID.")
                result = db.execute("INSERT OR IGNORE INTO game_staff_admins VALUES (?, ?)", (customer_id, when))
            else:
                result = db.execute("DELETE FROM game_staff_admins WHERE customer_id=?", (customer_id,))
            if result.rowcount:
                db.execute(
                    "INSERT INTO game_staff_access_events (actor_id, customer_id, action, occurred_at) VALUES (?, ?, ?, ?)",
                    (owner_id, customer_id, "grant" if grant else "revoke", when),
                )
            return bool(result.rowcount)

    def issue_proof(self, purpose: str, proof: str, *, expires_at: datetime, now: datetime) -> None:
        digest = hashlib.sha256(proof.encode("utf-8")).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM customer_auth_proofs WHERE expires_at<=?", (_aware(now).isoformat(),)
            )
            connection.execute(
                "INSERT INTO customer_auth_proofs(purpose, digest, expires_at) VALUES (?, ?, ?)",
                (purpose, digest, _aware(expires_at).isoformat()),
            )
            connection.commit()

    def consume_proof(self, purpose: str, proof: str, *, now: datetime) -> bool:
        digest = hashlib.sha256(proof.encode("utf-8")).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM customer_auth_proofs WHERE expires_at<=?", (_aware(now).isoformat(),)
            )
            cursor = connection.execute(
                "DELETE FROM customer_auth_proofs WHERE purpose=? AND digest=? AND expires_at>?",
                (purpose, digest, _aware(now).isoformat()),
            )
            connection.commit()
        return cursor.rowcount == 1

    def remember_once(self, purpose: str, proof: str, *, expires_at: datetime, now: datetime) -> bool:
        digest = hashlib.sha256(proof.encode("utf-8")).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            stamp = _aware(now).isoformat()
            connection.execute("DELETE FROM customer_auth_proofs WHERE expires_at<=?", (stamp,))
            try:
                connection.execute(
                    "INSERT INTO customer_auth_proofs(purpose, digest, expires_at) VALUES (?, ?, ?)",
                    (purpose, digest, _aware(expires_at).isoformat()),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return False
            connection.commit()
        return True


class CustomerIdentityService:
    """Verify Telegram/Google sign-ins and issue Games-owned customer tokens."""

    def __init__(
        self,
        secret: str | bytes,
        identity_store: CustomerIdentityStore,
        *,
        issuer: str = "aurix-identity",
        telegram_bot_token: str = "",
        telegram_bot_username: str = "",
        google_client_id: str = "",
        google_backup_client_id: str = "",
        google_token_verifier: Any = None,
        cookie_secure: bool = True,
        session_seconds: int = 43_200,
    ) -> None:
        self._secret = secret
        # Reuse the verifier's startup validation for signing configuration.
        self.verifier = SignedCustomerTokenVerifier(secret, issuer=issuer, session_store=identity_store)
        self.identity_store = identity_store
        self.issuer = issuer.strip()
        self.telegram_bot_token = telegram_bot_token.strip()
        self.telegram_bot_username = telegram_bot_username.strip().lstrip("@")
        self.google_client_id = google_client_id.strip() or google_backup_client_id.strip()
        self.google_client_ids = tuple(
            dict.fromkeys(
                client_id.strip()
                for client_id in (self.google_client_id, google_backup_client_id)
                if client_id.strip()
            )
        )
        self._google_token_verifier = google_token_verifier or _verify_google_id_token
        self.cookie_secure = bool(cookie_secure)
        if isinstance(session_seconds, bool) or not isinstance(session_seconds, int) or not 300 <= session_seconds <= 86_400:
            raise ValueError("customer auth session lifetime is invalid")
        self.session_seconds = session_seconds

    def config_payload(self) -> dict[str, Any]:
        telegram_enabled = bool(self.telegram_bot_username and re.fullmatch(r"[0-9]{1,20}:[A-Za-z0-9_-]+", self.telegram_bot_token))
        google_enabled = bool(self.google_client_id)
        return {
            "providers": {
                "telegram": {"enabled": telegram_enabled, "bot_username": self.telegram_bot_username if telegram_enabled else None,
                             "bot_id": self.telegram_bot_token.split(":", 1)[0] if telegram_enabled else None},
                "google": {"enabled": google_enabled, "client_id": self.google_client_id if google_enabled else None},
            }
        }

    def issue_google_challenge(self, *, now: datetime | None = None) -> str:
        if not self.google_client_id:
            raise CustomerAuthError("Google sign-in is unavailable")
        current = _aware(now or datetime.now(UTC))
        nonce = secrets.token_urlsafe(32)
        self.identity_store.issue_proof(
            "google-nonce", nonce, expires_at=current + timedelta(minutes=10), now=current
        )
        return nonce

    def login_telegram(
        self, payload: dict[str, Any], *, link_customer_id: str | None = None, now: datetime | None = None
    ) -> tuple[str | None, CustomerPrincipal]:
        if not self.telegram_bot_token or not self.telegram_bot_username:
            raise CustomerAuthError("Telegram sign-in is unavailable")
        current = _aware(now or datetime.now(UTC))
        try:
            user = verify_login_widget(payload, self.telegram_bot_token, now=current.timestamp(), max_age_seconds=300)
        except TelegramWebAppAuthError as exc:
            raise CustomerAuthError(str(exc)) from None
        signature = payload.get("hash") if isinstance(payload, dict) else None
        if not isinstance(signature, str) or not self.identity_store.remember_once(
            "telegram-login", signature, expires_at=current + timedelta(minutes=5), now=current
        ):
            raise CustomerAuthError("Telegram sign-in has expired or was already used")
        subject = str(user.telegram_id)
        if link_customer_id:
            self.identity_store.link(link_customer_id, "telegram", subject, now=current)
            principal = CustomerPrincipal(link_customer_id, self.issuer, current + timedelta(seconds=self.session_seconds))
            return None, principal
        customer_id = self.identity_store.customer_for("telegram", subject, now=current)
        return self._issue(customer_id, current)

    def login_google(
        self, credential: str, nonce: str, *, link_customer_id: str | None = None, now: datetime | None = None
    ) -> tuple[str | None, CustomerPrincipal]:
        if not self.google_client_id:
            raise CustomerAuthError("Google sign-in is unavailable")
        if not isinstance(credential, str) or not credential or len(credential) > 16_384:
            raise CustomerAuthError("Google sign-in credential is invalid")
        if not isinstance(nonce, str) or not nonce or len(nonce) > 256:
            raise CustomerAuthError("Google sign-in challenge is invalid")
        try:
            claims = None
            for client_id in self.google_client_ids:
                try:
                    claims = self._google_token_verifier(credential, client_id)
                    break
                except Exception:
                    continue
            if claims is None:
                raise ValueError("Google ID token audience is not allowed")
        except Exception:
            raise CustomerAuthError("Google sign-in could not be verified") from None
        current = _aware(now or datetime.now(UTC))
        if (
            not isinstance(claims, dict)
            or claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}
            or claims.get("nonce") != nonce
            or not isinstance(claims.get("sub"), str)
            or not _GOOGLE_SUBJECT.fullmatch(claims["sub"])
            or not self.identity_store.consume_proof("google-nonce", nonce, now=current)
        ):
            raise CustomerAuthError("Google sign-in challenge is invalid or expired")
        if link_customer_id:
            self.identity_store.link(link_customer_id, "google", claims["sub"], now=current)
            principal = CustomerPrincipal(link_customer_id, self.issuer, current + timedelta(seconds=self.session_seconds))
            return None, principal
        customer_id = self.identity_store.customer_for("google", claims["sub"], now=current)
        return self._issue(customer_id, current)

    def _issue(self, customer_id: str, now: datetime) -> tuple[str, CustomerPrincipal]:
        expires_at = now + timedelta(seconds=self.session_seconds)
        session_id = secrets.token_urlsafe(32)
        token = issue_customer_token(customer_id, self._secret, expires_at=expires_at, issuer=self.issuer, session_id=session_id)
        self.identity_store.create_session(session_id, customer_id, self.issuer, expires_at, now=now)
        return token, self.verifier.verify(f"Bearer {token}", now=now)

    def logout(self, authorization: str | None = None, *, cookie_header: str | None = None, now: datetime | None = None) -> None:
        try:
            principal = self.verifier.verify(authorization, cookie_header=cookie_header, now=now)
        except CustomerAuthError:
            return  # Invalid, expired and already-revoked sessions are already signed out.
        self.identity_store.revoke_session(principal.session_id)


def _validate_identity(provider: str, subject: str) -> None:
    if provider not in _AUTH_PROVIDERS or not isinstance(subject, str) or not _GOOGLE_SUBJECT.fullmatch(subject):
        raise CustomerApiError("customer identity is invalid")


def _verify_google_id_token(credential: str, client_id: str) -> dict[str, Any]:
    from google.auth.transport.requests import Request
    from google.oauth2.id_token import verify_oauth2_token

    return verify_oauth2_token(credential, Request(), client_id)


@dataclass(frozen=True)
class CustomerOrderService:
    repository: GameOrderRepository
    ledger: CustomerLedger | None = None

    def list_orders(self, principal: CustomerPrincipal) -> list[dict[str, Any]]:
        results = []
        for order in self.repository.list_for_customer(principal.customer_id):
            result = _order_view(order)
            self._add_refund(result, order)
            results.append(result)
        return results

    def get_order(self, principal: CustomerPrincipal, order_id: str, *, payment_review=None,
                  show_transfer_instructions: bool = True) -> dict[str, Any]:
        try:
            order = self.repository.get(order_id, customer_id=principal.customer_id)
        except OrderNotFoundError:
            raise CustomerApiError("order was not found") from None
        review = None
        if payment_review is not None:
            if Path(payment_review.repository.path).resolve() != Path(self.repository.path).resolve():
                raise CustomerApiError("payment review store does not match order store")
            payment_review._initialize()
            # Order, intent and evidence must describe the same SQLite snapshot.
            with open_sqlite_connection(self.repository.path) as db:
                db.execute("BEGIN")
                order = self.repository.get(order_id, customer_id=principal.customer_id, connection=db)
                intent = self.repository.get_payment_intent(order_id, connection=db)
                review = payment_review.customer_status(order_id, customer_id=principal.customer_id, connection=db)
        else:
            intent = self.repository.get_payment_intent(order_id)
        result = _order_view(order)
        if review is not None:
            result["payment_review"] = review
            if "refund" in review:
                result["refund"] = review["refund"]
        self._add_refund(result, order)
        if intent is not None:
            payment = {
                "rail": intent["rail"], "amount_minor": order.snapshot.amount_minor,
                "currency": order.snapshot.currency, "expires_at": intent["expires_at"],
                "expired": datetime.now(UTC) >= datetime.fromisoformat(intent["expires_at"]),
            }
            if order.state == OrderState.AWAITING_PAYMENT:
                if show_transfer_instructions:
                    payment.update(json.loads(intent["instructions_json"]))
                else:
                    payment["instructions_unavailable"] = True
            result["payment"] = payment
        elif self.ledger is not None:
            wallet_event = self.ledger.event_by_key(f"wallet:{order_id}")
            if (wallet_event is not None and wallet_event["kind"] == "reserve"
                    and wallet_event["order_id"] == order_id
                    and wallet_event["customer_id"] == principal.customer_id):
                result["payment"] = {"rail": "wallet", "amount_minor": order.snapshot.amount_minor,
                    "currency": order.snapshot.currency, "expired": False}
        return result

    def _add_refund(self, result: dict[str, Any], order: GameOrder) -> None:
        if self.ledger is not None:
            refund = self.ledger.customer_refund(order.order_id, customer_id=order.customer_id)
            if refund is not None:
                result["refund"] = refund


@dataclass(frozen=True)
class CustomerWalletService:
    """Owner-scoped, read-only wallet and top-up history projection."""

    ledger: CustomerLedger
    default_currency: str = "MMK"

    def __post_init__(self) -> None:
        if not isinstance(self.default_currency, str) or not re.fullmatch(r"[A-Z]{3}", self.default_currency):
            raise ValueError("default wallet currency is invalid")

    def balance(self, principal: CustomerPrincipal) -> dict[str, Any]:
        balance = self.ledger.balance(principal.customer_id)
        if balance is None:
            return {
                "currency": self.default_currency,
                "available_minor": 0,
                "reserved_minor": 0,
                "total_minor": 0,
                "observed_at": None,
                "status": "empty",
            }
        return {
            "currency": balance.currency,
            "available_minor": balance.available_minor,
            "reserved_minor": balance.reserved_minor,
            "total_minor": balance.total_minor,
            "observed_at": balance.observed_at.astimezone(UTC).isoformat(),
            "status": "active",
        }

    def top_up_history(
        self,
        principal: CustomerPrincipal,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        entries = [entry.as_dict() for entry in self.ledger.list_credits(principal.customer_id, limit=limit, offset=offset)]
        return {"entries": entries, "limit": limit, "offset": offset}


@dataclass(frozen=True)
class CustomerApiConfig:
    service: CustomerOrderService
    verifier: SignedCustomerTokenVerifier
    wallet_service: CustomerWalletService
    identity_service: CustomerIdentityService | None = None


def build_env_customer_api(*, root: str | Path | None = None) -> CustomerApiConfig | None:
    """Enable Games-owned sign-in and customer reads when its session secret is set."""

    secret = os.environ.get("AURIX_GAMES_CUSTOMER_AUTH_SECRET", "").strip()
    if not secret:
        return None
    base = Path(root or os.environ.get("AURIX_GAMES_BASE_DIR", ".local/fazercards"))
    database = os.environ.get("AURIX_GAMES_ORDER_DATABASE", str(base / "game-orders.sqlite3"))
    ledger_database = os.environ.get("AURIX_GAMES_LEDGER_DATABASE", str(base / "game-ledger.sqlite3"))
    identities = CustomerIdentityService(
        secret,
        CustomerIdentityStore(
            os.environ.get("AURIX_GAMES_CUSTOMER_IDENTITIES_DATABASE", str(base / "customer-identities.sqlite3"))
        ),
        issuer=os.environ.get("AURIX_GAMES_CUSTOMER_AUTH_ISSUER", "aurix-identity"),
        telegram_bot_token=os.environ.get("AURIX_GAMES_TELEGRAM_BOT_TOKEN", ""),
        telegram_bot_username=os.environ.get("AURIX_GAMES_TELEGRAM_BOT_USERNAME", ""),
        google_client_id=os.environ.get("AURIX_GAMES_GOOGLE_CLIENT_ID", ""),
        google_backup_client_id=os.environ.get("AURIX_GAMES_GOOGLE_BACKUP_CLIENT_ID", ""),
        cookie_secure=os.environ.get("AURIX_GAMES_AUTH_COOKIE_SECURE", "0").strip() == "1",
    )
    return CustomerApiConfig(
        CustomerOrderService(GameOrderRepository(database),CustomerLedger(ledger_database)),
        identities.verifier,
        CustomerWalletService(CustomerLedger(ledger_database)),
        identities,
    )


def _order_view(order: GameOrder) -> dict[str, Any]:
    # Do not expose recipient fingerprints, supplier IDs, provider keys, or
    # internal transition reasons to browser clients.
    return {
        "order_id": order.order_id,
        "state": order.state.value,
        "product_slug": order.snapshot.product_slug,
        "offer_id": order.snapshot.offer_id,
        "fulfillment_kind": order.snapshot.fulfillment_kind,
        "amount_minor": order.snapshot.amount_minor,
        "currency": order.snapshot.currency,
        "provider_status": order.provider_status,
    }


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > 8192 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("base64 value is invalid")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "CustomerApiError",
    "CustomerAuthError",
    "CustomerApiConfig",
    "CustomerIdentityConflict",
    "CustomerIdentityService",
    "CustomerIdentityStore",
    "CustomerOrderService",
    "CustomerWalletService",
    "build_env_customer_api",
    "CustomerPrincipal",
    "SignedCustomerTokenVerifier",
    "issue_test_token",
    "issue_customer_token",
    "SESSION_COOKIE_NAME",
]
