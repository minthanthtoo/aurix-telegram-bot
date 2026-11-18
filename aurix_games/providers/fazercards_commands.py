"""Explicit, opt-in FazerCards command boundary for Games fulfillment.

The catalog client is intentionally GET-only. This module keeps state-changing
supplier calls separate so a future worker must opt in deliberately, carry a
stable idempotency key, and reconcile ambiguous outcomes instead of blindly
retrying. It is not wired to the public storefront and has no automatic retry
for POST requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import math
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Protocol

from .fazercards import BASE_URL, MAX_RESPONSE_BYTES
from ..commerce_settings import commerce_paused


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_ORDER_ID_RE = re.compile(r"^ord-[0-9]+$")
_PAYMENT_ID_RE = re.compile(r"^(?:tx[0-9]+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})$")
_TRANSACTION_ID_RE = re.compile(r"^tx[0-9]+$")
_WEBHOOK_SIGNATURE_RE = re.compile(r"^sha256=[0-9a-f]{64}$")
_PAYMENT_METHODS = frozenset({"usdt"})


class SupplierCommandError(RuntimeError):
    """A supplier command failed without exposing credentials or upstream body."""

    def __init__(self, message, *, http_status=None):
        super().__init__(message)
        self.http_status = http_status


class SupplierWriteDisabledError(SupplierCommandError):
    """A state-changing command was attempted without explicit enablement."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise SupplierCommandError("supplier redirected a command; refusing to forward auth")


class _Opener(Protocol):
    def open(self, request: urllib.request.Request, timeout: float) -> Any: ...


@dataclass(frozen=True)
class SupplierWebhookEvent:
    """Minimal event metadata safe for the order reconciliation boundary."""

    event_id: str
    event_type: str
    timestamp: str
    order_id: str | None
    supplier_kind: str | None
    status: str | None
    previous_status: str | None


class FazerCardsCommandClient:
    """Typed command client; writes require ``allow_writes=True`` explicitly."""

    def __init__(
        self,
        api_key: str,
        *,
        allow_writes: bool = False,
        allow_recipient_validation: bool = False,
        timeout: float = 20.0,
        opener: _Opener | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        _require_secret(api_key)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a positive finite number")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        if not isinstance(base_url, str) or not base_url.startswith("https://"):
            raise ValueError("base_url must use HTTPS")
        if not isinstance(allow_writes, bool):
            raise ValueError("allow_writes must be boolean")
        if not isinstance(allow_recipient_validation, bool):
            raise ValueError("allow_recipient_validation must be boolean")
        self._api_key = api_key
        self._allow_writes = allow_writes
        self._allow_recipient_validation = allow_recipient_validation
        self.timeout = float(timeout)
        self._base_url = base_url.rstrip("/")
        self._opener = opener or urllib.request.build_opener(_NoRedirect())

    def get_order(self, order_id: str) -> dict[str, Any]:
        """Read one provider order for reconciliation; never mutates it."""

        normalized = _require_order_id(order_id)
        return self._request("GET", f"/orders/{urllib.parse.quote(normalized, safe='')}")

    def login_funpay(self, *, email: str, password: str) -> dict[str, Any]:
        """Start a FunPay session through an explicit, non-persisting command."""

        return self._write(
            "POST",
            "/funpay/login",
            {
                "email": _require_text(email, "email", max_length=320),
                "password": _require_text(password, "password", max_length=200),
            },
        )

    def get_funpay_me(self) -> dict[str, Any]:
        """Read FunPay session/account state; never expose it to customers."""

        return self._request("GET", "/funpay/me")

    def get_me(self) -> dict[str, Any]:
        """Read the reseller account profile; never exposes it to customers."""

        return self._request("GET", "/me")

    def get_balance(self) -> dict[str, Any]:
        """Read the supplier wallet balance in the account currency."""

        return self._request("GET", "/balance")

    def list_transactions(self, *, page: int = 1, limit: int = 50) -> dict[str, Any]:
        return self._request(
            "GET", "/transactions", query=_page_query(page, limit)
        )

    def get_transaction(self, transaction_id: str) -> dict[str, Any]:
        normalized = _require_transaction_id(transaction_id)
        return self._request(
            "GET", f"/transactions/{urllib.parse.quote(normalized, safe='')}"
        )

    def list_orders(self, *, page: int = 1, limit: int = 50) -> dict[str, Any]:
        return self._request("GET", "/orders", query=_page_query(page, limit))

    def get_payment_methods(self) -> dict[str, Any]:
        return self._request("GET", "/payments/methods")

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        normalized = _require_payment_id(payment_id)
        return self._request("GET", f"/payments/{urllib.parse.quote(normalized, safe='')}")

    def get_subscription(self) -> dict[str, Any]:
        """Read the reseller subscription state; never mutates the plan."""

        return self._request("GET", "/subscription")

    def list_subscription_plans(self) -> dict[str, Any]:
        """Read available reseller plans and their USD prices."""

        return self._request("GET", "/subscription/plans")

    def get_subscription_activation_quote(
        self, plan: str, *, promo_code: str | None = None
    ) -> dict[str, Any]:
        """Read an activation quote without activating or charging the account."""

        normalized_plan = _require_lower_enum(plan, {"bronze", "silver", "gold"}, "plan")
        query: dict[str, str] = {"plan": normalized_plan}
        if promo_code is not None:
            query["promo_code"] = _require_text(promo_code, "promo_code", max_length=64)
        return self._request("GET", "/subscription/activation-quote", query=query)

    def activate_subscription_from_balance(
        self, plan: str, *, promo_code: str | None = None
    ) -> dict[str, Any]:
        """Activate a paid reseller plan from the supplier balance.

        This is deliberately separate from the read-only quote.  The caller
        must pass the command client's explicit write gate and should record
        the quote/approval before invoking it.
        """

        body: dict[str, Any] = {
            "plan": _require_lower_enum(plan, {"bronze", "silver", "gold"}, "plan")
        }
        if promo_code is not None:
            body["promo_code"] = _require_text(promo_code, "promo_code", max_length=64)
        return self._write("POST", "/subscription/activate-from-balance", body)

    def set_subscription_auto_renew(self, enabled: bool) -> dict[str, Any]:
        """Enable or disable supplier-plan auto-renewal (write-gated)."""

        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        return self._write("PATCH", "/subscription/auto-renew", {"planAutoRenew": enabled})

    def change_subscription_plan(self, plan: str) -> dict[str, Any]:
        """Change the paid supplier plan (write-gated)."""

        normalized = _require_lower_enum(plan, {"bronze", "silver", "gold"}, "plan")
        return self._write("PATCH", "/subscription/plan", {"plan": normalized})

    def check_steam_login(self, steam_login: str) -> dict[str, Any]:
        """Ask FazerCards to validate a customer Steam login (write-gated POST)."""

        return self._write(
            "POST",
            "/steam-topup/check-login",
            {"steamLogin": _require_text(steam_login, "steam_login", max_length=255)},
        )

    def get_webhook(self) -> dict[str, Any]:
        return self._request("GET", "/account/webhook")

    def list_webhook_deliveries(self) -> dict[str, Any]:
        return self._request("GET", "/account/webhook/deliveries")

    def create_balance_payment(
        self,
        *,
        method: str,
        amount_usd: str | int | float | Decimal,
    ) -> dict[str, Any]:
        """Create a supplier-wallet funding invoice when writes are enabled."""

        normalized_method = _require_text(method, "method", max_length=32).lower()
        if normalized_method not in _PAYMENT_METHODS:
            raise ValueError("method is invalid")
        amount = _decimal_json_number(amount_usd, "amount")
        return self._write(
            "POST",
            "/payments/create",
            {"method": normalized_method, "amount": amount},
        )

    def verify_binance_payment(
        self, payment_id: str, *, binance_order_id: str
    ) -> dict[str, Any]:
        """Submit a Binance Pay payer reference for a created invoice."""

        normalized_payment = _require_payment_id(payment_id)
        reference = _require_text(binance_order_id, "binanceOrderId", max_length=255)
        return self._write(
            "POST",
            f"/payments/{urllib.parse.quote(normalized_payment, safe='')}/verify",
            {"binanceOrderId": reference},
        )

    def configure_webhook(self, *, url: str, enabled: bool = True) -> dict[str, Any]:
        normalized = _require_https_url(url, "url")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        return self._write("PUT", "/account/webhook", {"url": normalized, "enabled": enabled})

    def delete_webhook(self) -> dict[str, Any]:
        return self._write("DELETE", "/account/webhook", {})

    def regenerate_webhook_secret(self) -> dict[str, Any]:
        return self._write("POST", "/account/webhook/secret/regenerate", {})

    def test_webhook(self) -> dict[str, Any]:
        return self._write("POST", "/account/webhook/test", {})

    def validate_topup_recipient(
        self, category_id: str, fields: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Validate recipient fields; caller must still bind the result to inputs."""

        body = {
            "category_id": _require_id(category_id, "category_id"),
            "fields": _require_fields(fields),
        }
        if not self._allow_writes and not self._allow_recipient_validation:
            raise SupplierWriteDisabledError("supplier recipient validation is disabled")
        return self._request("POST", "/topups/validate-id", body=body)

    def order_topup(
        self,
        *,
        category_id: str,
        offer_id: str,
        fields: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        body = {
            "category_id": _require_id(category_id, "category_id"),
            "offer_id": _require_id(offer_id, "offer_id"),
            "fields": _require_fields(fields),
        }
        return self._write(
            "POST", "/topups/order", body, idempotency_key=_require_idempotency(idempotency_key)
        )

    def order_gift_card(
        self,
        *,
        category_id: str,
        card_id: str,
        quantity: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body = {
            "category_id": _require_id(category_id, "category_id"),
            "card_id": _require_id(card_id, "card_id"),
            "quantity": _require_quantity(quantity),
        }
        return self._write(
            "POST", "/giftcards/order", body, idempotency_key=_require_idempotency(idempotency_key)
        )

    def order_game_key(
        self,
        *,
        game_id: str,
        key_id: str,
        quantity: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body = {
            "game_id": _require_id(game_id, "game_id"),
            "key_id": _require_id(key_id, "key_id"),
            "quantity": _require_quantity(quantity),
        }
        return self._write(
            "POST", "/gamekeys/order", body, idempotency_key=_require_idempotency(idempotency_key)
        )

    def order_steam_topup(
        self,
        *,
        steam_login: str,
        currency: str,
        amount: str | int | float | Decimal,
        idempotency_key: str,
    ) -> dict[str, Any]:
        normalized_currency = _require_enum(currency, {"USD", "RUB", "UAH", "KZT"}, "currency")
        body = {
            "steamLogin": _require_text(steam_login, "steamLogin", max_length=255),
            "currency": normalized_currency,
            "amount": _decimal_amount(amount, "amount"),
        }
        return self._write(
            "POST", "/steam-topup/order", body, idempotency_key=_require_idempotency(idempotency_key)
        )

    def order_steam_gift(
        self,
        *,
        invite_url: str,
        sub_id: int,
        app_id: int,
        region: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body = {
            "invite_url": _require_text(invite_url, "invite_url", max_length=2048),
            "sub_id": _require_positive_int(sub_id, "sub_id"),
            "app_id": _require_positive_int(app_id, "app_id"),
            "region": _require_text(region, "region", max_length=64),
        }
        return self._write(
            "POST", "/steam-gifts/order", body, idempotency_key=_require_idempotency(idempotency_key)
        )

    def buy_telegram_stars(
        self, *, telegram_username: str, quantity: int
    ) -> dict[str, Any]:
        """Purchase Telegram Stars (write-gated; not part of customer checkout)."""

        return self._write(
            "POST",
            "/telegram/stars/buy",
            {
                "telegram_username": _require_text(
                    telegram_username, "telegram_username", max_length=255
                ),
                "quantity": _require_positive_int(quantity, "quantity", minimum=50, maximum=10_000),
            },
        )

    def buy_telegram_premium(
        self, *, telegram_username: str, months: int
    ) -> dict[str, Any]:
        """Purchase Telegram Premium (write-gated; not part of customer checkout)."""

        if isinstance(months, bool) or months not in {3, 6, 12}:
            raise ValueError("months is invalid")
        return self._write(
            "POST",
            "/telegram/premium/buy",
            {
                "telegram_username": _require_text(
                    telegram_username, "telegram_username", max_length=255
                ),
                "months": months,
            },
        )

    def order_manual_service(
        self,
        *,
        manual_service_id: str,
        product_id: str,
        fields: Mapping[str, Any] | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Submit a manual-service order only through the gated worker."""

        body: dict[str, Any] = {
            "manual_service_id": _require_id(manual_service_id, "manual_service_id"),
            "product_id": _require_id(product_id, "product_id"),
        }
        if fields is not None:
            body["fields"] = _require_fields(fields)
        return self._write(
            "POST",
            "/manual-services/order",
            body,
            idempotency_key=_require_idempotency(idempotency_key),
        )

    def get_manual_service_chat(self, order_id: str) -> dict[str, Any]:
        """Read a manual-service conversation for operator reconciliation."""

        normalized = _require_order_id(order_id)
        return self._request(
            "GET",
            f"/manual-services/orders/{urllib.parse.quote(normalized, safe='')}/chat",
        )

    def send_manual_service_chat(
        self,
        order_id: str,
        *,
        body: str | None = None,
        image: bytes | None = None,
        image_filename: str = "image.bin",
        image_content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """Send one operator manual-order chat message through multipart form data."""

        normalized = _require_order_id(order_id)
        if body is None and image is None:
            raise ValueError("body or image is required")
        normalized_body = None if body is None else _require_chat_body(body)
        if image is not None and (
            not isinstance(image, bytes) or not 0 < len(image) <= 8 * 1024 * 1024
        ):
            raise ValueError("image is invalid")
        filename = _require_text(image_filename, "image_filename", max_length=255)
        if any(character in filename for character in ('"', "\\")):
            raise ValueError("image_filename is invalid")
        content_type = _require_text(image_content_type, "image_content_type", max_length=128)
        if any(character in content_type for character in "\r\n; "):
            raise ValueError("image_content_type is invalid")
        try:
            filename.encode("ascii")
            content_type.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError("multipart header value is invalid") from None
        boundary = f"----aurix-fazercards-{secrets.token_hex(16)}"
        chunks: list[bytes] = []
        if normalized_body is not None:
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    b'Content-Disposition: form-data; name="body"\r\n\r\n',
                    normalized_body.encode("utf-8"),
                    b"\r\n",
                ]
            )
        if image is not None:
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'.encode("ascii"),
                    f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
                    image,
                    b"\r\n",
                ]
            )
        chunks.append(f"--{boundary}--\r\n".encode("ascii"))
        return self._write_multipart(
            "POST",
            f"/manual-services/orders/{urllib.parse.quote(normalized, safe='')}/chat",
            body=b"".join(chunks),
            content_type=f"multipart/form-data; boundary={boundary}",
        )

    def _write(
        self,
        method: str,
        path: str,
        body: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not self._allow_writes:
            raise SupplierWriteDisabledError(
                "supplier writes are disabled; use a separately approved fulfillment worker"
            )
        return self._request(method, path, body=body, idempotency_key=idempotency_key)

    def _write_multipart(
        self,
        method: str,
        path: str,
        *,
        body: bytes,
        content_type: str,
    ) -> dict[str, Any]:
        if not self._allow_writes:
            raise SupplierWriteDisabledError(
                "supplier writes are disabled; use a separately approved fulfillment worker"
            )
        return self._request(
            method,
            path,
            raw_body=body,
            content_type=content_type,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
        idempotency_key: str | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("supplier path is invalid")
        if method == "POST" and path.endswith("/order") and commerce_paused("supplier_orders_enabled"):
            raise SupplierWriteDisabledError("Supplier orders are paused in store settings.")
        if query is not None and not isinstance(query, Mapping):
            raise ValueError("supplier query must be a mapping")
        if body is not None and raw_body is not None:
            raise ValueError("supplier request cannot contain JSON and raw bodies")
        if raw_body is not None and not isinstance(raw_body, bytes):
            raise ValueError("supplier raw body must be bytes")
        payload = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "AuriX-Game-Store/1.0",
            "X-API-Key": self._api_key,
        }
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw_body is not None:
            payload = raw_body
            headers["Content-Type"] = content_type or "application/octet-stream"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        target = self._base_url + path
        if query:
            target += "?" + urllib.parse.urlencode(dict(query))
        request = urllib.request.Request(
            target,
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = int(response.status)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            try:
                status = int(exc.code)
                raw = b""
            finally:
                try:
                    exc.close()
                except Exception:
                    # Some test doubles and transports do not expose a
                    # closable response body; the status remains sufficient
                    # for the sanitized error below.
                    pass
            raise SupplierCommandError(f"supplier {method} {path} returned HTTP {status}", http_status=status) from None
        except (OSError, TimeoutError, ValueError):
            raise SupplierCommandError(f"supplier {method} {path} failed") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SupplierCommandError("supplier response exceeded the configured size limit")
        if status < 200 or status >= 300:
            raise SupplierCommandError(f"supplier {method} {path} returned HTTP {status}", http_status=status)
        try:
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SupplierCommandError("supplier returned invalid JSON") from None
        if not isinstance(parsed, dict) or parsed.get("ok") is False:
            raise SupplierCommandError("supplier returned an unsuccessful response")
        return parsed


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Verify the documented ``sha256=...`` signature over raw request bytes."""

    if not isinstance(raw_body, bytes) or not isinstance(secret, str) or not secret:
        return False
    if not isinstance(signature, str) or not _WEBHOOK_SIGNATURE_RE.fullmatch(signature):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def parse_webhook_event(raw_body: bytes) -> SupplierWebhookEvent:
    """Extract only reconciliation metadata from a verified webhook body."""

    if not isinstance(raw_body, bytes) or len(raw_body) > MAX_RESPONSE_BYTES:
        raise SupplierCommandError("webhook body is invalid")
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SupplierCommandError("webhook body is not valid JSON") from None
    if not isinstance(payload, dict):
        raise SupplierCommandError("webhook payload must be an object")
    event_id = _require_text(payload.get("event_id"), "event_id", max_length=128)
    event_type = _require_text(payload.get("event"), "event", max_length=128)
    timestamp = _require_text(payload.get("timestamp"), "timestamp", max_length=80)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SupplierCommandError("webhook data is invalid")
    raw_order_id = data.get("order_id")
    if raw_order_id is None and event_type not in {"webhook.test"}:
        raise ValueError("order_id is invalid")
    order_id = None if raw_order_id is None else _require_order_id(raw_order_id)
    supplier_kind = data.get("type")
    if supplier_kind is not None:
        supplier_kind = _require_text(supplier_kind, "type", max_length=80)
    status = data.get("status")
    if status is not None:
        status = _require_text(status, "status", max_length=80)
    previous_status = data.get("previous_status")
    if previous_status is not None:
        previous_status = _require_text(previous_status, "previous_status", max_length=80)
    return SupplierWebhookEvent(
        event_id=event_id,
        event_type=event_type,
        timestamp=timestamp,
        order_id=order_id,
        supplier_kind=supplier_kind,
        status=status,
        previous_status=previous_status,
    )


def _require_secret(value: object) -> str:
    return _require_text(value, "api_key", max_length=4096)


def _require_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(f"{label} is invalid")
    return value


def _require_order_id(value: object) -> str:
    if not isinstance(value, str) or not _ORDER_ID_RE.fullmatch(value):
        raise ValueError("order_id is invalid")
    return value


def _require_payment_id(value: object) -> str:
    if not isinstance(value, str) or not _PAYMENT_ID_RE.fullmatch(value):
        raise ValueError("payment_id is invalid")
    return value


def _require_transaction_id(value: object) -> str:
    if not isinstance(value, str) or not _TRANSACTION_ID_RE.fullmatch(value):
        raise ValueError("transaction_id is invalid")
    return value


def _page_query(page: object, limit: object) -> dict[str, int]:
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 2_147_483_647:
        raise ValueError("page is invalid")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit is invalid")
    return {"page": page, "limit": limit}


def _require_idempotency(value: object) -> str:
    return _require_text(value, "idempotency_key", max_length=255)


def _require_text(value: object, label: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"{label} is invalid")
    if any(character in value for character in "\r\n"):
        raise ValueError(f"{label} is invalid")
    return value.strip()


def _require_chat_body(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 10_000:
        raise ValueError("body is invalid")
    if "\x00" in value:
        raise ValueError("body is invalid")
    return value


def _require_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("fields must be a non-empty object")
    result: dict[str, Any] = {}
    for key, item in value.items():
        normalized_key = _require_text(key, "field key", max_length=128)
        if not isinstance(item, str):
            raise ValueError("fields must contain string values")
        result[normalized_key] = item
    return result


def _require_quantity(value: object) -> int:
    return _require_positive_int(value, "quantity", maximum=100)


def _require_positive_int(
    value: object,
    label: str,
    *,
    minimum: int = 1,
    maximum: int = 2_147_483_647,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _require_enum(value: object, choices: set[str], label: str) -> str:
    normalized = _require_text(value, label, max_length=32).upper()
    if normalized not in choices:
        raise ValueError(f"{label} is invalid")
    return normalized


def _require_lower_enum(value: object, choices: set[str], label: str) -> str:
    normalized = _require_text(value, label, max_length=32).casefold()
    if normalized not in choices:
        raise ValueError(f"{label} is invalid")
    return normalized


def _decimal_amount(value: object, label: str) -> str:
    if isinstance(value, bool):
        raise ValueError(f"{label} is invalid")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} is invalid") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"{label} is invalid")
    return format(amount, "f")


def _decimal_json_number(value: object, label: str) -> int | float:
    text = _decimal_amount(value, label)
    decimal = Decimal(text)
    if decimal == decimal.to_integral_value():
        return int(decimal)
    return float(decimal)


def _require_https_url(value: object, label: str) -> str:
    text = _require_text(value, label, max_length=2048)
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError(f"{label} is invalid")
    return text


__all__ = [
    "FazerCardsCommandClient",
    "SupplierCommandError",
    "SupplierWebhookEvent",
    "SupplierWriteDisabledError",
    "parse_webhook_event",
    "verify_webhook_signature",
]
