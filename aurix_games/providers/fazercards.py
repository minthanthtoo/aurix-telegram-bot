"""Read-only FazerCards catalog transport and top-up catalog adapter.

There are deliberately no invoice, validation-submit, order, cancellation, or
webhook mutation methods in this first provider milestone. Supplier IDs,
wholesale costs, and dynamic fields must remain server-side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json
import math
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol


BASE_URL = "https://api.fzr.cards/api/v2"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500
MAX_STEAM_GIFT_PAGE_SIZE = 25_000
STEAM_GIFT_CATALOG_MIN_INTERVAL_SECONDS = 180.0
_STEAM_GIFT_GAME_PATH = re.compile(r"^/steam-gifts/games/[0-9]{1,12}$")
_PROVIDER_MEDIA_PATH = re.compile(
    r"^/api/v2/media/[A-Za-z0-9_-]{1,80}/[A-Za-z0-9_-]{1,128}(?:\.[A-Za-z0-9]{1,10})?$"
)
_RASTER_IMAGE_SUFFIX = re.compile(r"\.(?:png|jpe?g|webp|avif|gif)$", re.IGNORECASE)
MAX_PROVIDER_IMAGE_URL_LENGTH = 2048


class CatalogReadError(RuntimeError):
    """A provider catalog read failed without leaking response data/secrets."""


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
        raise CatalogReadError(
            "provider redirected a catalog request; refusing to forward auth"
        )


class FazerCardsReadOnlyClient:
    """Strict GET-only HTTP client with bounded responses and conservative pacing."""

    _ALLOWED_PATHS = frozenset(
        {
            "/topups",
            "/topups/offers",
            "/topups/validate-id",
            "/giftcards",
            "/giftcards/cards",
            "/gamekeys",
            "/gamekeys/keys",
            "/gamekeys/region-restriction",
            "/steam-topup/rates",
            "/steam-gifts/games",
            "/telegram/stars",
            "/telegram/premium",
            "/manual-services",
            "/payments/methods",
        }
    )
    _QUERY_ALLOWLISTS = {
        "/topups": {"limit", "cursor", "include_ui"},
        "/topups/offers": {"category_id", "include_ui"},
        "/giftcards": {"limit", "cursor", "include_ui"},
        "/giftcards/cards": {"category_id", "card_id", "include_ui"},
        "/gamekeys": {"limit", "cursor", "include_ui"},
        "/gamekeys/keys": {"game_id", "include_ui"},
        "/gamekeys/region-restriction": {"game_id"},
        "/steam-gifts/games": {"limit"},
        "/manual-services": {"include_ui"},
    }

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 20.0,
        min_interval_seconds: float = 1.0,
        sleep: Any = time.sleep,
        opener: Any | None = None,
    ) -> None:
        if (
            not isinstance(api_key, str)
            or not api_key
            or "\n" in api_key
            or "\r" in api_key
        ):
            raise ValueError("a non-empty single-line API key is required")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be positive")
        if (
            not isinstance(min_interval_seconds, (int, float))
            or not math.isfinite(min_interval_seconds)
            or min_interval_seconds < 0
        ):
            raise ValueError("minimum request interval must be finite and non-negative")
        self._api_key = api_key
        self.timeout = timeout
        self.min_interval_seconds = max(0.6, min_interval_seconds)
        self._sleep = sleep
        self._opener = opener or urllib.request.build_opener(_NoRedirect())
        self._last_request_at = 0.0
        self._last_steam_gift_catalog_request_at: float | None = None

    def get(self, path: str, **query: Any) -> dict[str, Any]:
        if (
            path not in self._ALLOWED_PATHS
            and not self._manual_offers_path(path)
            and not _STEAM_GIFT_GAME_PATH.fullmatch(path)
        ):
            raise ValueError("endpoint is outside the read-only catalog allowlist")
        params = {key: value for key, value in query.items() if value is not None}
        if params.get("include_ui") is False:
            params.pop("include_ui")
        elif params.get("include_ui") is True:
            # The provider documents `1` and `true`; use the compact canonical form.
            params["include_ui"] = "1"
        if path == "/giftcards/cards" and "card_id" in params:
            # OpenAPI marks `card_id` as a deprecated alias. Normalize it to the
            # canonical category identity before constructing the provider URL.
            raw_alias = params.pop("card_id")
            alias = _require_nonempty_string(raw_alias)
            if isinstance(raw_alias, str) and raw_alias != alias:
                raise ValueError("card_id must not contain surrounding whitespace")
            category = params.get("category_id")
            if category is not None and str(category).strip() != alias:
                raise ValueError("category_id and deprecated card_id alias must match")
            params.setdefault("category_id", alias)
        allowed_query = self._QUERY_ALLOWLISTS.get(path)
        if allowed_query is None and self._manual_offers_path(path):
            allowed_query = {"include_ui"}
        if set(params) - (allowed_query or set()):
            raise ValueError("query parameters are outside the read-only catalog contract")
        self._validate_query(path, params)
        if path == "/steam-gifts/games" and self._last_steam_gift_catalog_request_at is not None:
            elapsed = time.monotonic() - self._last_steam_gift_catalog_request_at
            if elapsed < STEAM_GIFT_CATALOG_MIN_INTERVAL_SECONDS:
                raise CatalogReadError(
                    "GET /steam-gifts/games is cooling down under the provider rate limit"
                )
        url = BASE_URL + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "Accept": "application/json",
            "User-Agent": "AuriX-Game-Store/1.0",
            "X-API-Key": self._api_key,
        }

        for attempt in range(5):
            wait = self.min_interval_seconds - (time.monotonic() - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
            request = urllib.request.Request(url, headers=headers, method="GET")
            self._last_request_at = time.monotonic()
            if path == "/steam-gifts/games":
                self._last_steam_gift_catalog_request_at = self._last_request_at
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    status = int(response.status)
                    body = response.read(MAX_RESPONSE_BYTES + 1)
                    response_headers = response.headers
            except urllib.error.HTTPError as exc:
                try:
                    status = int(exc.code)
                    body = exc.read(MAX_RESPONSE_BYTES + 1)
                    response_headers = exc.headers
                finally:
                    exc.close()
            except CatalogReadError:
                raise
            except (OSError, TimeoutError, ValueError):
                raise CatalogReadError(
                    f"GET {path} failed; network details were suppressed"
                ) from None

            if status == 429 and attempt < 4:
                raw_retry = response_headers.get("Retry-After") if response_headers else None
                try:
                    retry_after = float(raw_retry) if raw_retry else 2.0**attempt
                except (TypeError, ValueError):
                    retry_after = 2.0**attempt
                if not math.isfinite(retry_after) or retry_after < 0:
                    retry_after = 2.0**attempt
                if retry_after > 300:
                    raise CatalogReadError(f"GET {path} is rate-limited beyond the inline retry budget; resume later")
                if path == "/steam-gifts/games":
                    retry_after = max(
                        retry_after, STEAM_GIFT_CATALOG_MIN_INTERVAL_SECONDS
                    )
                self._sleep(
                    max(0.5, retry_after) + random.uniform(0, 0.25)
                )
                continue
            if status < 200 or status >= 300:
                raise CatalogReadError(f"GET {path} returned HTTP {status}")
            if len(body) > MAX_RESPONSE_BYTES:
                raise CatalogReadError(
                    f"GET {path} exceeded the {MAX_RESPONSE_BYTES}-byte response limit"
                )
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise CatalogReadError(f"GET {path} returned invalid JSON") from None
            if not isinstance(payload, dict):
                raise CatalogReadError(f"GET {path} returned a non-object JSON response")
            if payload.get("ok") is False:
                raise CatalogReadError(f"GET {path} returned a provider error")
            return payload
        raise CatalogReadError(f"GET {path} remained rate-limited after bounded retries")

    @staticmethod
    def _manual_offers_path(path: str) -> bool:
        parts = path.split("/")
        return (
            len(parts) == 4
            and parts[0] == ""
            and parts[1] == "manual-services"
            and bool(parts[2])
            and parts[3] == "offers"
            and "/" not in parts[2]
            and parts[2] not in {".", ".."}
        )

    @staticmethod
    def _validate_query(path: str, params: dict[str, Any]) -> None:
        for key, value in params.items():
            if key in {"limit"}:
                if path == "/steam-gifts/games":
                    # The public OpenAPI declares this parameter as a string,
                    # not an integer. It is an optional result cap; no cursor
                    # or offset pagination is documented for this route.
                    if (
                        not isinstance(value, str)
                        or not value.isascii()
                        or not value.isdigit()
                        or len(value) > len(str(MAX_STEAM_GIFT_PAGE_SIZE))
                        or not 1 <= int(value) <= MAX_STEAM_GIFT_PAGE_SIZE
                    ):
                        raise ValueError(
                            "Steam gift limit must be a decimal string between 1 "
                            f"and the local safety bound {MAX_STEAM_GIFT_PAGE_SIZE}"
                        )
                    continue
                maximum = MAX_PAGE_SIZE
                if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                    raise ValueError(f"{key} must be an integer between 1 and {maximum}")
            elif key == "cursor":
                if not isinstance(value, str) or not value or len(value) > 2048:
                    raise ValueError("cursor must be a non-empty string of at most 2048 characters")
            elif key in {"category_id", "game_id"}:
                normalized = _require_nonempty_string(value)
                if isinstance(value, str) and value != normalized:
                    raise ValueError(f"{key} must not contain surrounding whitespace")
            elif key == "card_id":
                normalized = _require_nonempty_string(value)
                if isinstance(value, str) and value != normalized:
                    raise ValueError("card_id must not contain surrounding whitespace")
            elif key == "include_ui":
                if not isinstance(value, str) or value not in {"1", "true"}:
                    raise ValueError("include_ui must be true or 1")


@dataclass(frozen=True, repr=False)
class CatalogCategory:
    supplier_category_id: str = field(repr=False)
    name: str = field(repr=False)
    note: str = field(default="", repr=False)
    image_url: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return "CatalogCategory(<redacted>)"


@dataclass(frozen=True, repr=False)
class SupplierOption:
    label: str = field(repr=False)
    value: str = field(repr=False)

    def __repr__(self) -> str:
        return "SupplierOption(<redacted>)"


@dataclass(frozen=True, repr=False)
class SupplierInputField:
    key: str = field(repr=False)
    label: str = field(repr=False)
    input_type: str = field(repr=False)
    options: tuple[SupplierOption, ...] = field(default=(), repr=False)
    required: bool | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return "SupplierInputField(<redacted>)"


@dataclass(frozen=True, repr=False)
class TopupOffer:
    supplier_offer_id: str = field(repr=False)
    name: str = field(repr=False)
    supplier_cost_usd: Decimal = field(repr=False)

    def __repr__(self) -> str:
        return "TopupOffer(<redacted>)"


@dataclass(frozen=True, repr=False)
class TopupOfferCatalog:
    supplier_category_id: str = field(repr=False)
    category_name: str = field(repr=False)
    offers: tuple[TopupOffer, ...] = field(repr=False)
    fields: tuple[SupplierInputField, ...] = field(repr=False)
    image_url: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return (
            "TopupOfferCatalog(offers="
            f"{len(self.offers)}, input_fields={len(self.fields)}, <redacted>)"
        )


@dataclass(frozen=True, repr=False)
class CatalogPage:
    items: tuple[CatalogCategory, ...] = field(repr=False)
    next_cursor: str | None = field(repr=False)
    total: int | None
    has_more: bool

    def __repr__(self) -> str:
        return (
            f"CatalogPage(items={len(self.items)}, total={self.total}, "
            f"has_more={self.has_more}, cursor=<redacted>)"
        )


@dataclass(frozen=True, repr=False)
class ValidationTarget:
    supplier_category_id: str = field(repr=False)
    name: str = field(repr=False)
    fields: tuple[SupplierInputField, ...] = field(repr=False)

    def __repr__(self) -> str:
        return f"ValidationTarget(input_fields={len(self.fields)}, <redacted>)"


class TopupCatalogPort(Protocol):
    """Read-only supplier operations required by the first AuriX top-up slice."""

    def list_topup_categories(
        self,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        include_ui: bool = False,
    ) -> CatalogPage: ...

    def get_topup_offers(self, category_id: str, *, include_ui: bool = False) -> TopupOfferCatalog: ...

    def list_topup_validation_targets(self) -> tuple[ValidationTarget, ...]: ...


class FazerCardsCatalogAdapter:
    """Normalize top-up catalog GET responses for trusted server-side use."""

    def __init__(self, client: FazerCardsReadOnlyClient) -> None:
        self._client = client

    def list_topup_categories(
        self,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        include_ui: bool = False,
    ) -> CatalogPage:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        if cursor is not None and (
            not isinstance(cursor, str) or not cursor or len(cursor) > 2048
        ):
            raise ValueError("cursor must be a non-empty string of at most 2048 characters")
        query: dict[str, Any] = {"limit": limit, "cursor": cursor}
        query.update(_include_ui_query(include_ui))
        payload = self._client.get("/topups", **query)
        items = payload.get("items")
        meta = payload.get("meta")
        if not isinstance(items, list) or not isinstance(meta, dict):
            raise CatalogReadError("top-up catalog response was malformed")
        if len(items) > limit:
            raise CatalogReadError("top-up page exceeded the requested limit")
        categories = tuple(_parse_category(item, "category_id") for item in items)
        if len({item.supplier_category_id for item in categories}) != len(categories):
            raise CatalogReadError("top-up page contained duplicate category identifiers")
        next_cursor = meta.get("next_cursor")
        if next_cursor == "":
            next_cursor = None
        if next_cursor is not None and (
            not isinstance(next_cursor, str) or len(next_cursor) > 2048
        ):
            raise CatalogReadError("top-up pagination cursor was malformed")
        total = meta.get("total")
        if isinstance(total, bool) or (
            total is not None and (not isinstance(total, int) or total < 0)
        ):
            raise CatalogReadError("top-up catalog total was malformed")
        has_more = meta.get("has_more")
        if not isinstance(has_more, bool):
            raise CatalogReadError("top-up pagination state was malformed")
        if has_more and not next_cursor:
            raise CatalogReadError("top-up catalog omitted its next cursor")
        if not has_more and next_cursor:
            raise CatalogReadError("top-up catalog returned a cursor without more pages")
        return CatalogPage(categories, next_cursor, total, has_more)

    def get_topup_offers(
        self, category_id: str, *, include_ui: bool = False
    ) -> TopupOfferCatalog:
        normalized_id = _require_nonempty_string(category_id)
        query: dict[str, Any] = {"category_id": normalized_id}
        query.update(_include_ui_query(include_ui))
        payload = self._client.get("/topups/offers", **query)
        returned_id = payload.get("category_id")
        category_name = payload.get("name")
        offers = payload.get("offers")
        fields = payload.get("fields")
        if returned_id is None or str(returned_id) != normalized_id:
            raise CatalogReadError("top-up offer category did not match the request")
        if (
            not isinstance(category_name, str)
            or not category_name.strip()
            or not isinstance(offers, list)
        ):
            raise CatalogReadError("top-up offer response was malformed")
        if not isinstance(fields, list):
            raise CatalogReadError("top-up field schema was malformed")
        normalized_offers = tuple(_parse_offer(item) for item in offers)
        normalized_fields = tuple(_parse_field(item) for item in fields)
        if len({item.supplier_offer_id for item in normalized_offers}) != len(
            normalized_offers
        ):
            raise CatalogReadError("top-up offer response contained duplicate identifiers")
        if len({item.key for item in normalized_fields}) != len(normalized_fields):
            raise CatalogReadError("top-up field schema contained duplicate keys")
        return TopupOfferCatalog(
            normalized_id,
            category_name,
            normalized_offers,
            normalized_fields,
            _optional_provider_image(payload.get("imageurl")),
        )

    def list_topup_validation_targets(self) -> tuple[ValidationTarget, ...]:
        payload = self._client.get("/topups/validate-id")
        items = payload.get("items")
        if not isinstance(items, list):
            raise CatalogReadError("top-up validation capability list was malformed")
        targets: list[ValidationTarget] = []
        for item in items:
            if not isinstance(item, dict):
                raise CatalogReadError("top-up validation capability was malformed")
            category = _parse_category(item, "category_id")
            raw_fields = item.get("fields")
            if not isinstance(raw_fields, list):
                raise CatalogReadError("top-up validation field schema was malformed")
            fields = tuple(_parse_field(value) for value in raw_fields)
            targets.append(
                ValidationTarget(
                    category.supplier_category_id,
                    category.name,
                    fields,
                )
            )
        return tuple(targets)


def _require_nonempty_string(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("provider identifier must be a non-empty string")
    result = str(value).strip()
    if not result or len(result) > 200:
        raise ValueError("provider identifier must be a non-empty string")
    return result


def _parse_category(value: Any, id_key: str) -> CatalogCategory:
    if not isinstance(value, dict):
        raise CatalogReadError("top-up catalog category was malformed")
    try:
        category_id = _require_nonempty_string(value.get(id_key))
    except ValueError:
        raise CatalogReadError("top-up catalog category was malformed") from None
    name = value.get("name")
    note = value.get("note", "")
    if not isinstance(name, str) or not name.strip() or not isinstance(note, str):
        raise CatalogReadError("top-up catalog category was malformed")
    return CatalogCategory(
        category_id, name, note, _optional_provider_image(value.get("imageurl"))
    )


def _optional_provider_image(value: Any) -> str | None:
    """Preserve only bounded, same-provider raster-image paths from UI metadata."""

    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_PROVIDER_IMAGE_URL_LENGTH
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
        or any(character in value for character in ("\\", "<", ">", '"', "'"))
    ):
        raise CatalogReadError("provider image metadata was malformed")
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        raise CatalogReadError("provider image metadata was malformed") from None
    if parsed.query or parsed.fragment:
        raise CatalogReadError("provider image metadata was malformed")
    if parsed.scheme or parsed.netloc:
        if (
            parsed.scheme != "https"
            or parsed.netloc.lower() not in {"api.fzr.cards", "reseller.fazercards.com"}
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise CatalogReadError("provider image host was outside the media allowlist")
        path = parsed.path
        if path.startswith("/media/"):
            path = "/api/v2" + path
    else:
        path = parsed.path
        if path.startswith("/media/"):
            path = "/api/v2" + path
        elif path.startswith("media/"):
            path = "/api/v2/" + path
        elif path.startswith("api/v2/"):
            path = "/" + path
    filename = path.rsplit("/", 1)[-1]
    suffix = re.search(r"\.([A-Za-z0-9]+)$", filename)
    if (
        not _PROVIDER_MEDIA_PATH.fullmatch(path)
        or any(segment in {".", ".."} for segment in path.split("/"))
        or not filename
        or (suffix is not None and not _RASTER_IMAGE_SUFFIX.search(path))
    ):
        raise CatalogReadError("provider image path was outside the raster-media allowlist")
    return value


def _include_ui_query(include_ui: bool) -> dict[str, bool]:
    if not isinstance(include_ui, bool):
        raise ValueError("include_ui must be boolean")
    return {"include_ui": True} if include_ui else {}


def _parse_offer(value: Any) -> TopupOffer:
    if not isinstance(value, dict):
        raise CatalogReadError("top-up offer was malformed")
    try:
        offer_id = _require_nonempty_string(value.get("offer_id"))
    except ValueError:
        raise CatalogReadError("top-up offer was malformed") from None
    name = value.get("name")
    raw_cost = value.get("price_usd")
    if (
        not isinstance(name, str)
        or not name.strip()
        or isinstance(raw_cost, bool)
        or not isinstance(raw_cost, (str, int, Decimal))
    ):
        raise CatalogReadError("top-up offer was malformed")
    try:
        cost = Decimal(str(raw_cost))
    except (InvalidOperation, ValueError):
        raise CatalogReadError("top-up offer price was malformed") from None
    if not cost.is_finite() or cost < 0:
        raise CatalogReadError("top-up offer price was malformed")
    return TopupOffer(offer_id, name, cost)


def _parse_field(value: Any) -> SupplierInputField:
    if not isinstance(value, dict):
        raise CatalogReadError("top-up input field was malformed")
    try:
        key = _require_nonempty_string(value.get("key"))
    except ValueError:
        raise CatalogReadError("top-up input field was malformed") from None
    label = value.get("label")
    input_type = value.get("type")
    if (
        not isinstance(label, str)
        or not label.strip()
        or not isinstance(input_type, str)
        or not input_type.strip()
    ):
        raise CatalogReadError("top-up input field was malformed")
    required = value.get("required")
    if required is not None and not isinstance(required, bool):
        raise CatalogReadError("top-up required-field metadata was malformed")
    raw_options = value.get("options", [])
    if not isinstance(raw_options, list):
        raise CatalogReadError("top-up input options were malformed")
    options: list[SupplierOption] = []
    for option in raw_options:
        if not isinstance(option, dict):
            raise CatalogReadError("top-up input option was malformed")
        label_value = option.get("label")
        option_value = option.get("value")
        if (
            not isinstance(label_value, str)
            or not label_value.strip()
            or not isinstance(option_value, str)
            or not option_value.strip()
        ):
            raise CatalogReadError("top-up input option was malformed")
        options.append(SupplierOption(label_value, option_value))
    return SupplierInputField(key, label, input_type, tuple(options), required)
