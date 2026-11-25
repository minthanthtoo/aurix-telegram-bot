"""Explicit SHOP2TOPUP API boundary for private Games operations.

The partner API has no sandbox. Account, catalog and price reads are safe;
player checks consume the provider's daily quota, and order creation debits
the reseller wallet. Those two operations have separate opt-in gates.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Mapping, Protocol

from .fazercards_commands import SupplierCommandError


BASE_URL = "https://shop2topup.com/api/endpoints/v1"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_KEY_RE = re.compile(r"^[!-~]{1,256}\.[!-~]{1,512}$")
_NUMERIC_ID_RE = re.compile(r"^[1-9][0-9]{0,14}$")
_FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


class Shop2TopupCommandError(SupplierCommandError):
    """Provider failure with response body and credentials deliberately hidden."""

    def __init__(self, message: str, *, http_status: int | None = None,
                 code: str | None = None, retry_after: int | None = None) -> None:
        super().__init__(message, http_status=http_status)
        self.http_status = http_status
        self.code = code
        self.retry_after = retry_after


class Shop2TopupWriteDisabledError(Shop2TopupCommandError):
    """A player-check or wallet-debit command was attempted without its gate."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the reseller Authorization header to a redirected host.
        return None


class _Opener(Protocol):
    def open(self, request: urllib.request.Request, timeout: float) -> Any: ...


class Shop2TopupCommandClient:
    """Typed H2H client; order writes require ``allow_orders=True`` explicitly."""

    def __init__(self, api_key: str, *, allow_orders: bool = False,
                 allow_player_validation: bool = False, timeout: float = 10,
                 opener: _Opener | None = None) -> None:
        if not isinstance(api_key, str) or not _KEY_RE.fullmatch(api_key):
            raise ValueError("SHOP2TOPUP API key must have the key-id.secret format")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a positive finite number")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        if not isinstance(allow_orders, bool) or not isinstance(allow_player_validation, bool):
            raise ValueError("command gates must be boolean")
        self._api_key = api_key
        self._allow_orders = allow_orders
        self._allow_player_validation = allow_player_validation
        self.timeout = float(timeout)
        self._opener = opener or urllib.request.build_opener(_NoRedirect())

    @property
    def orders_enabled(self) -> bool:
        return self._allow_orders

    @property
    def player_validation_enabled(self) -> bool:
        return self._allow_player_validation

    def get_account(self) -> dict[str, Any]:
        return self._request("GET", "/account")

    def list_big_categories(self, *, for_ui: bool = False) -> dict[str, Any]:
        return self._request("GET", "/catalog/big-categories", query={"for_ui": str(for_ui).lower()} if for_ui else None)

    def list_categories(self, *, big_category_id: int | None = None,
                        for_ui: bool = False) -> dict[str, Any]:
        query = {}
        if big_category_id is not None:
            query["bigCategoryId"] = _numeric_id(big_category_id, "big_category_id")
        if for_ui:
            query["for_ui"] = "true"
        return self._request("GET", "/catalog/categories", query=query or None)

    def list_subcategories(self, *, category_id: int | None = None) -> dict[str, Any]:
        query = None if category_id is None else {"categoryId": _numeric_id(category_id, "category_id")}
        return self._request("GET", "/catalog/subcategories", query=query)

    def get_price(self, item_id: int) -> dict[str, Any]:
        return self._request("GET", f"/catalog/subcategory/{_numeric_id(item_id, 'item_id')}/price")

    def get_requirements(self, category_id: int) -> dict[str, Any]:
        return self._request("GET", f"/catalog/category/{_numeric_id(category_id, 'category_id')}/requirements")

    def validate_player(self, *, sub_category_id: int, player_id: str,
                        requirements: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not self._allow_player_validation:
            raise Shop2TopupWriteDisabledError("player validation is disabled")
        body = {"sub_category_id": _numeric_id(sub_category_id, "sub_category_id"),
                "player_id": _text(player_id, "player_id", 255)}
        body.update(_requirements(requirements or {}))
        return self._request("POST", "/player/validate", body=body)

    def create_order(self, *, order_id: str, sub_category_id: int, quantity: int,
                     requirements: Mapping[str, Any], expected_unit_price: str | Decimal | None = None) -> dict[str, Any]:
        if not self._allow_orders:
            raise Shop2TopupWriteDisabledError("SHOP2TOPUP order writes are disabled")
        stable_id = _uuid(order_id)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 1000:
            raise ValueError("quantity must be an integer from 1 to 1000")
        body: dict[str, Any] = {
            "order_id": stable_id,
            "sub_category_id": _numeric_id(sub_category_id, "sub_category_id"),
            "quantity": quantity,
            "requirements": _requirements(requirements),
        }
        if expected_unit_price is not None:
            body["expected_unit_price"] = _decimal_amount(expected_unit_price)
        # No retry here: this POST immediately debits the supplier wallet.
        return self._request("POST", "/orders/create", body=body)

    def get_order(self, order_id: str) -> dict[str, Any]:
        stable_id = _uuid(order_id)
        return self._request("GET", f"/orders/{urllib.parse.quote(stable_id, safe='')}")

    def _request(self, method: str, path: str, *, query: Mapping[str, str] | None = None,
                 body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        url = BASE_URL + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        payload = None if body is None else json.dumps(
            dict(body), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if payload is not None and len(payload) > MAX_REQUEST_BYTES:
            raise ValueError("SHOP2TOPUP request exceeded the size limit")
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._api_key}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            code = None
            if len(raw) <= MAX_RESPONSE_BYTES:
                try:
                    error = json.loads(raw).get("error", {})
                    candidate = error.get("code") if isinstance(error, dict) else None
                    if isinstance(candidate, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", candidate):
                        code = candidate
                except (ValueError, UnicodeError, AttributeError):
                    pass
            retry = exc.headers.get("Retry-After", "")
            retry_after = int(retry) if retry.isascii() and retry.isdigit() and len(retry) <= 7 else None
            raise Shop2TopupCommandError(
                "SHOP2TOPUP request was rejected", http_status=exc.code,
                code=code, retry_after=retry_after,
            ) from None
        except (OSError, urllib.error.URLError):
            # A transport failure on create_order is ambiguous; callers must reconcile
            # this exact UUID with get_order before deciding whether any retry is safe.
            raise Shop2TopupCommandError("SHOP2TOPUP request failed or timed out") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise Shop2TopupCommandError("SHOP2TOPUP response exceeded the size limit")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise Shop2TopupCommandError("SHOP2TOPUP returned invalid JSON") from None
        if not isinstance(result, dict) or type(result.get("success")) is not bool:
            raise Shop2TopupCommandError("SHOP2TOPUP returned an invalid response envelope")
        if result["success"] is not True:
            error = result.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]{1,80}", code):
                code = None
            raise Shop2TopupCommandError("SHOP2TOPUP request was rejected", code=code)
        return result


def _numeric_id(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not _NUMERIC_ID_RE.fullmatch(str(value)):
        raise ValueError(f"{name} must be a positive numeric supplier ID")
    return int(value)


def _text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{name} is invalid")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{name} is invalid")
    return value


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("order_id must be a UUID")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise ValueError("order_id must be a UUID") from None


def _decimal_amount(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("expected_unit_price must be a positive decimal string")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("expected_unit_price must be a positive decimal string") from None
    if not amount.is_finite() or amount <= 0 or amount.adjusted() > 12:
        raise ValueError("expected_unit_price must be a positive decimal string")
    return format(amount, "f")


def _requirements(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or len(value) > 32:
        raise ValueError("requirements must be a mapping of supplier-defined fields")
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not _FIELD_RE.fullmatch(key):
            raise ValueError("requirements contain an invalid field name")
        if isinstance(item, str):
            normalized[key] = _text(item, key, 512)
        elif type(item) is bool:
            normalized[key] = item
        elif type(item) is int and abs(item) <= 10**18:
            normalized[key] = item
        elif type(item) is float and math.isfinite(item) and abs(item) <= 10**18:
            normalized[key] = item
        elif isinstance(item, list) and len(item) <= 32 and all(
            isinstance(option, str) and option == option.strip() and 0 < len(option) <= 128
            and not any(ord(char) < 32 for char in option) for option in item
        ):
            normalized[key] = item
        else:
            raise ValueError("requirements contain an unsupported value")
    return normalized


__all__ = [
    "BASE_URL", "MAX_REQUEST_BYTES", "MAX_RESPONSE_BYTES", "Shop2TopupCommandClient",
    "Shop2TopupCommandError", "Shop2TopupWriteDisabledError",
]
