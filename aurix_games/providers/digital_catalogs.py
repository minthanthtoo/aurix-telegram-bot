"""Read-only FazerCards adapters for digital catalog families beyond top-ups.

These models remain supplier-side discovery data. They deliberately expose no
order, payment, validation-submit, or account-management operation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json
from typing import Any, Protocol
from urllib.parse import quote

from .fazercards import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    MAX_STEAM_GIFT_PAGE_SIZE,
    CatalogCategory,
    CatalogPage,
    CatalogReadError,
    FazerCardsReadOnlyClient,
    _include_ui_query,
    _optional_provider_image,
    _require_nonempty_string,
)


class _ReadClient(Protocol):
    def get(self, path: str, **query: Any) -> dict[str, Any]: ...


@dataclass(frozen=True, repr=False)
class InventoryOffer:
    supplier_item_id: str = field(repr=False)
    name: str = field(repr=False)
    supplier_cost_usd: Decimal = field(repr=False)
    stock: int | None = field(repr=False)
    minimum_quantity: int | None = field(repr=False)
    maximum_quantity: int | None = field(repr=False)

    def __repr__(self) -> str:
        return "InventoryOffer(<redacted>)"


@dataclass(frozen=True, repr=False)
class GiftCardCatalog:
    supplier_category_id: str = field(repr=False)
    category_name: str = field(repr=False)
    offers: tuple[InventoryOffer, ...] = field(repr=False)
    image_url: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return f"GiftCardCatalog(offers={len(self.offers)}, <redacted>)"


@dataclass(frozen=True, repr=False)
class GameKeyCategory:
    supplier_game_id: str = field(repr=False)
    name: str = field(repr=False)
    region: str | None = field(repr=False)
    platform: str | None = field(repr=False)
    region_restriction: bool | None = field(repr=False)
    app_id: int | None = field(default=None, repr=False)
    image_url: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return "GameKeyCategory(<redacted>)"


@dataclass(frozen=True, repr=False)
class GameKeyPage:
    items: tuple[GameKeyCategory, ...] = field(repr=False)
    next_cursor: str | None = field(repr=False)
    total: int | None
    has_more: bool

    def __repr__(self) -> str:
        return (
            f"GameKeyPage(items={len(self.items)}, total={self.total}, "
            f"has_more={self.has_more}, cursor=<redacted>)"
        )


@dataclass(frozen=True, repr=False)
class GameKeyCatalog:
    supplier_game_id: str = field(repr=False)
    name: str = field(repr=False)
    region: str | None = field(repr=False)
    platform: str | None = field(repr=False)
    region_restriction: bool | None = field(repr=False)
    offers: tuple[InventoryOffer, ...] = field(repr=False)
    app_id: int | None = field(default=None, repr=False)
    image_url: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return f"GameKeyCatalog(offers={len(self.offers)}, <redacted>)"


@dataclass(frozen=True, repr=False)
class GameKeyRestrictions:
    supplier_game_id: str = field(repr=False)
    details: dict[str, Any] = field(repr=False)

    def __repr__(self) -> str:
        return "GameKeyRestrictions(<redacted>)"


@dataclass(frozen=True, repr=False)
class SteamGiftGame:
    app_id: int = field(repr=False)
    name: str = field(repr=False)

    def __repr__(self) -> str:
        return "SteamGiftGame(<redacted>)"


@dataclass(frozen=True, repr=False)
class SteamGiftPage:
    games: tuple[SteamGiftGame, ...] = field(repr=False)
    total: int | None
    returned: int
    truncated: bool

    def __repr__(self) -> str:
        return (
            f"SteamGiftPage(returned={self.returned}, total={self.total}, "
            f"truncated={self.truncated}, games=<redacted>)"
        )


@dataclass(frozen=True, repr=False)
class SteamGiftRegionOffer:
    region: str = field(repr=False)
    supplier_cost_usd: Decimal = field(repr=False)

    def __repr__(self) -> str:
        return "SteamGiftRegionOffer(<redacted>)"


@dataclass(frozen=True, repr=False)
class SteamGiftEdition:
    supplier_sub_id: int = field(repr=False)
    name: str = field(repr=False)
    regions: tuple[SteamGiftRegionOffer, ...] = field(repr=False)

    def __repr__(self) -> str:
        return f"SteamGiftEdition(regions={len(self.regions)}, <redacted>)"


@dataclass(frozen=True, repr=False)
class SteamGiftCatalog:
    app_id: int = field(repr=False)
    offers: tuple[SteamGiftEdition, ...] = field(repr=False)

    def __repr__(self) -> str:
        return f"SteamGiftCatalog(offers={len(self.offers)}, <redacted>)"


@dataclass(frozen=True, repr=False)
class SteamTopupRates:
    base_currency: str = field(repr=False)
    rates: tuple[tuple[str, Decimal], ...] = field(repr=False)
    updated_at: str | None = field(repr=False)

    def __repr__(self) -> str:
        return f"SteamTopupRates(rate_count={len(self.rates)}, <redacted>)"


@dataclass(frozen=True, repr=False)
class ManualServiceCategory:
    supplier_service_id: str = field(repr=False)
    name: str = field(repr=False)
    kind: str = field(repr=False)
    chat_enabled: bool = field(repr=False)
    info: str = field(repr=False)
    image_url: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return "ManualServiceCategory(<redacted>)"


@dataclass(frozen=True, repr=False)
class ManualServiceOffer:
    supplier_offer_id: str = field(repr=False)
    name: str = field(repr=False)
    supplier_cost_usd: Decimal = field(repr=False)
    delivery_minutes: int | None = field(repr=False)
    input_schema_json: str | None = field(repr=False)

    def __repr__(self) -> str:
        return "ManualServiceOffer(<redacted>)"


@dataclass(frozen=True, repr=False)
class ManualServiceCatalog:
    category: ManualServiceCategory = field(repr=False)
    offers: tuple[ManualServiceOffer, ...] = field(repr=False)
    image_url: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return f"ManualServiceCatalog(offers={len(self.offers)}, <redacted>)"


class FazerCardsDigitalCatalogAdapter:
    """Normalize gift-card, game-key and Steam catalog GET responses."""

    def __init__(self, client: FazerCardsReadOnlyClient | _ReadClient) -> None:
        self._client = client

    def list_giftcard_categories(
        self,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        include_ui: bool = False,
    ) -> CatalogPage:
        payload = self._list_payload(
            "/giftcards", cursor=cursor, limit=limit, include_ui=include_ui
        )
        items = payload.get("items")
        if not isinstance(items, list) or not isinstance(payload.get("meta"), dict):
            raise CatalogReadError("gift-card catalog response was malformed")
        categories = tuple(_parse_simple_category(item, "category_id") for item in items)
        return _finish_page(payload, categories, limit, "gift-card")

    def get_giftcard_offers(
        self, category_id: str, *, include_ui: bool = False
    ) -> GiftCardCatalog:
        normalized_id = _require_nonempty_string(category_id)
        query: dict[str, Any] = {"category_id": normalized_id}
        query.update(_include_ui_query(include_ui))
        payload = self._client.get("/giftcards/cards", **query)
        return _parse_offer_catalog(
            payload,
            expected_id=normalized_id,
            id_key="category_id",
            offer_key="offers",
            item_id_key="card_id",
            family="gift-card",
            catalog_factory=lambda name, offers: GiftCardCatalog(
                normalized_id, name, offers, _optional_provider_image(payload.get("imageurl"))
            ),
        )

    def list_gamekey_categories(
        self,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        include_ui: bool = False,
    ) -> GameKeyPage:
        payload = self._list_payload(
            "/gamekeys", cursor=cursor, limit=limit, include_ui=include_ui
        )
        items = payload.get("items")
        if not isinstance(items, list) or not isinstance(payload.get("meta"), dict):
            raise CatalogReadError("game-key catalog response was malformed")
        categories = tuple(_parse_gamekey_category(item) for item in items)
        page = _finish_page(payload, categories, limit, "game-key")
        return GameKeyPage(page.items, page.next_cursor, page.total, page.has_more)

    def get_gamekey_offers(
        self, game_id: str, *, include_ui: bool = False
    ) -> GameKeyCatalog:
        normalized_id = _require_nonempty_string(game_id)
        query: dict[str, Any] = {"game_id": normalized_id}
        query.update(_include_ui_query(include_ui))
        payload = self._client.get("/gamekeys/keys", **query)
        returned_id = _matches_requested_id(payload, "game_id", normalized_id, "game-key")
        name = _nonempty_text(payload.get("GameName"), "game-key name")
        region, platform, restricted = _parse_gamekey_metadata(payload)
        raw_offers = payload.get("keys")
        if not isinstance(raw_offers, list):
            raise CatalogReadError("game-key offers were malformed")
        offers = tuple(
            _parse_inventory_offer(
                item,
                id_key="key_id",
                family="game-key",
            )
            for item in raw_offers
        )
        _unique_ids(offers, "game-key")
        raw_app_id = payload.get("appid")
        app_id = (
            None
            if raw_app_id is None
            else _positive_identifier(raw_app_id, "game-key Steam app id")
        )
        return GameKeyCatalog(
            returned_id,
            name,
            region,
            platform,
            restricted,
            offers,
            app_id,
            _optional_provider_image(payload.get("imageurl")),
        )

    def get_gamekey_region_restrictions(self, game_id: str) -> GameKeyRestrictions:
        normalized_id = _require_nonempty_string(game_id)
        payload = self._client.get("/gamekeys/region-restriction", game_id=normalized_id)
        _matches_requested_id(payload, "game_id", normalized_id, "game-key restriction")
        return GameKeyRestrictions(normalized_id, payload)

    def list_steam_gift_games(self, *, limit: int | None = 100) -> SteamGiftPage:
        if limit is not None and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_STEAM_GIFT_PAGE_SIZE
        ):
            raise ValueError(
                f"limit must be between 1 and {MAX_STEAM_GIFT_PAGE_SIZE}, or None for the full list"
            )
        payload = self._client.get("/steam-gifts/games", limit=limit)
        raw_games = payload.get("games")
        meta = payload.get("meta")
        if not isinstance(raw_games, list) or not isinstance(meta, dict):
            raise CatalogReadError("Steam gift game list was malformed")
        games = tuple(_parse_steam_game(item) for item in raw_games)
        _unique_ids(games, "Steam gift")
        total = _optional_count(meta.get("total"), "Steam gift total")
        returned = _optional_count(meta.get("returned"), "Steam gift returned count")
        if returned is None:
            returned = len(games)
        if returned != len(games) or (limit is not None and returned > limit):
            raise CatalogReadError("Steam gift returned count did not match its game list")
        truncated = meta.get("truncated")
        if truncated is None:
            truncated = total is not None and returned < total
        if not isinstance(truncated, bool):
            raise CatalogReadError("Steam gift truncation metadata was malformed")
        if total is not None and returned < total and not truncated:
            raise CatalogReadError("Steam gift list omitted its truncated state")
        return SteamGiftPage(games, total, returned, truncated)

    def get_steam_gift_editions(self, app_id: int | str) -> SteamGiftCatalog:
        normalized_id = _positive_identifier(app_id, "Steam app id")
        payload = self._client.get(f"/steam-gifts/games/{normalized_id}")
        returned_id = _positive_identifier(payload.get("appid"), "Steam app id")
        if returned_id != normalized_id:
            raise CatalogReadError("Steam gift edition response did not match the requested app")
        raw_offers = payload.get("offers")
        if not isinstance(raw_offers, list):
            raise CatalogReadError("Steam gift edition list was malformed")
        offers = tuple(_parse_steam_edition(item) for item in raw_offers)
        _unique_ids(offers, "Steam gift edition")
        return SteamGiftCatalog(normalized_id, offers)

    def get_steam_topup_rates(self) -> SteamTopupRates:
        payload = self._client.get("/steam-topup/rates")
        base = _nonempty_text(payload.get("base"), "Steam rate base currency")
        raw_rates = payload.get("rates")
        updated = payload.get("updated_at")
        if not isinstance(raw_rates, dict) or (updated is not None and not isinstance(updated, str)):
            raise CatalogReadError("Steam wallet rate response was malformed")
        rates: list[tuple[str, Decimal]] = []
        for currency, raw_rate in raw_rates.items():
            currency_name = _nonempty_text(currency, "Steam rate currency")
            rates.append((currency_name, _decimal_amount(raw_rate, "Steam wallet rate")))
        if not rates:
            raise CatalogReadError("Steam wallet rate response was empty")
        return SteamTopupRates(base, tuple(sorted(rates)), updated)

    def list_manual_service_categories(
        self, *, include_ui: bool = False
    ) -> tuple[ManualServiceCategory, ...]:
        query = _include_ui_query(include_ui)
        payload = self._client.get("/manual-services", **query)
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise CatalogReadError("manual-service category list was malformed")
        categories = tuple(_parse_manual_category(item) for item in raw_items)
        _unique_ids(categories, "manual-service")
        return categories

    def get_manual_service_offers(
        self, service_id: str, *, include_ui: bool = False
    ) -> ManualServiceCatalog:
        normalized_id = _require_nonempty_string(service_id)
        query = _include_ui_query(include_ui)
        payload = self._client.get(
            f"/manual-services/{quote(normalized_id, safe='')}/offers", **query
        )
        _matches_requested_id(
            payload, "manual_service_id", normalized_id, "manual-service"
        )
        raw_category = payload.get("category")
        category = _parse_manual_category(raw_category)
        if category.supplier_service_id != normalized_id:
            raise CatalogReadError("manual-service details did not match the requested category")
        raw_offers = payload.get("items")
        if not isinstance(raw_offers, list):
            raise CatalogReadError("manual-service offer list was malformed")
        offers = tuple(_parse_manual_offer(item) for item in raw_offers)
        _unique_ids(offers, "manual-service")
        return ManualServiceCatalog(
            category, offers, _optional_provider_image(payload.get("imageurl"))
        )

    def _list_payload(
        self,
        path: str,
        *,
        cursor: str | None,
        limit: int,
        include_ui: bool = False,
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        if cursor is not None and (
            not isinstance(cursor, str) or not cursor or len(cursor) > 2048
        ):
            raise ValueError("cursor must be a non-empty string of at most 2048 characters")
        query: dict[str, Any] = {"limit": limit, "cursor": cursor}
        query.update(_include_ui_query(include_ui))
        return self._client.get(path, **query)


def _finish_page(
    payload: dict[str, Any],
    items: tuple[Any, ...],
    limit: int,
    family: str,
) -> CatalogPage:
    if len(items) > limit:
        raise CatalogReadError(f"{family} page exceeded the requested limit")
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        raise CatalogReadError(f"{family} pagination metadata was malformed")
    next_cursor = meta.get("next_cursor")
    if next_cursor == "":
        next_cursor = None
    if next_cursor is not None and (
        not isinstance(next_cursor, str) or len(next_cursor) > 2048
    ):
        raise CatalogReadError(f"{family} pagination cursor was malformed")
    total = _optional_count(meta.get("total"), f"{family} catalog total")
    has_more = meta.get("has_more")
    if not isinstance(has_more, bool):
        raise CatalogReadError(f"{family} pagination state was malformed")
    if has_more and not next_cursor:
        raise CatalogReadError(f"{family} catalog omitted its next cursor")
    if not has_more and next_cursor:
        raise CatalogReadError(f"{family} catalog returned a cursor without more pages")
    return CatalogPage(items, next_cursor, total, has_more)


def _parse_simple_category(value: Any, id_key: str) -> CatalogCategory:
    if not isinstance(value, dict):
        raise CatalogReadError("catalog category was malformed")
    try:
        identifier = _require_nonempty_string(value.get(id_key))
    except ValueError:
        raise CatalogReadError("catalog category was malformed") from None
    name = _nonempty_text(value.get("name"), "catalog category name")
    note = value.get("note", "")
    if not isinstance(note, str):
        raise CatalogReadError("catalog category note was malformed")
    return CatalogCategory(identifier, name, note, _optional_provider_image(value.get("imageurl")))


def _parse_gamekey_category(value: Any) -> GameKeyCategory:
    if not isinstance(value, dict):
        raise CatalogReadError("game-key category was malformed")
    try:
        identifier = _require_nonempty_string(value.get("game_id"))
    except ValueError:
        raise CatalogReadError("game-key category was malformed") from None
    name = _nonempty_text(value.get("name"), "game-key name")
    region, platform, restricted = _parse_gamekey_metadata(value)
    raw_app_id = value.get("appid")
    app_id = (
        None
        if raw_app_id is None
        else _positive_identifier(raw_app_id, "game-key Steam app id")
    )
    return GameKeyCategory(
        identifier,
        name,
        region,
        platform,
        restricted,
        app_id,
        _optional_provider_image(value.get("imageurl")),
    )


def _parse_gamekey_metadata(value: dict[str, Any]) -> tuple[str | None, str | None, bool | None]:
    region = value.get("region")
    platform = value.get("platform")
    restricted = value.get("region_restriction")
    if region is not None and not isinstance(region, str):
        raise CatalogReadError("game-key region was malformed")
    if platform is not None and not isinstance(platform, str):
        raise CatalogReadError("game-key platform was malformed")
    if restricted is not None and not isinstance(restricted, bool):
        raise CatalogReadError("game-key restriction state was malformed")
    return region, platform, restricted


def _parse_offer_catalog(
    payload: dict[str, Any],
    *,
    expected_id: str,
    id_key: str,
    offer_key: str,
    item_id_key: str,
    family: str,
    catalog_factory: Any,
) -> Any:
    _matches_requested_id(payload, id_key, expected_id, family)
    name = _nonempty_text(payload.get("name"), f"{family} category name")
    raw_offers = payload.get(offer_key)
    if not isinstance(raw_offers, list):
        raise CatalogReadError(f"{family} offer list was malformed")
    offers = tuple(
        _parse_inventory_offer(item, id_key=item_id_key, family=family)
        for item in raw_offers
    )
    _unique_ids(offers, family)
    return catalog_factory(name, offers)


def _parse_inventory_offer(value: Any, *, id_key: str, family: str) -> InventoryOffer:
    if not isinstance(value, dict):
        raise CatalogReadError(f"{family} offer was malformed")
    try:
        item_id = _require_nonempty_string(value.get(id_key))
    except ValueError:
        raise CatalogReadError(f"{family} offer was malformed") from None
    name = _nonempty_text(value.get("name"), f"{family} offer name")
    cost = _decimal_amount(value.get("price_usd"), f"{family} offer price")
    stock = _optional_count(value.get("stock"), f"{family} stock")
    minimum = _optional_count(value.get("min_order_quantity"), f"{family} minimum quantity")
    maximum = _optional_count(value.get("max_order_quantity"), f"{family} maximum quantity")
    if minimum is not None and minimum < 1:
        raise CatalogReadError(f"{family} minimum quantity was malformed")
    if maximum is not None and maximum < 1:
        raise CatalogReadError(f"{family} maximum quantity was malformed")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise CatalogReadError(f"{family} quantity limits were inconsistent")
    return InventoryOffer(item_id, name, cost, stock, minimum, maximum)


def _parse_steam_game(value: Any) -> SteamGiftGame:
    if not isinstance(value, dict):
        raise CatalogReadError("Steam gift game was malformed")
    app_id = _positive_identifier(value.get("appid"), "Steam app id")
    name = _nonempty_text(value.get("name"), "Steam gift game name")
    return SteamGiftGame(app_id, name)


def _parse_steam_edition(value: Any) -> SteamGiftEdition:
    if not isinstance(value, dict):
        raise CatalogReadError("Steam gift edition was malformed")
    sub_id = _positive_identifier(value.get("sub_id"), "Steam gift sub id")
    name = _nonempty_text(value.get("name"), "Steam gift edition name")
    raw_regions = value.get("regions")
    if not isinstance(raw_regions, list):
        raise CatalogReadError("Steam gift edition regions were malformed")
    regions: list[SteamGiftRegionOffer] = []
    for row in raw_regions:
        if not isinstance(row, dict):
            raise CatalogReadError("Steam gift region offer was malformed")
        region = _nonempty_text(row.get("region"), "Steam gift region")
        cost = _decimal_amount(row.get("price"), "Steam gift region price")
        regions.append(SteamGiftRegionOffer(region, cost))
    if len({item.region for item in regions}) != len(regions):
        raise CatalogReadError("Steam gift edition contained duplicate regions")
    return SteamGiftEdition(sub_id, name, tuple(regions))


def _parse_manual_category(value: Any) -> ManualServiceCategory:
    if not isinstance(value, dict):
        raise CatalogReadError("manual-service category was malformed")
    try:
        service_id = _require_nonempty_string(value.get("id"))
    except ValueError:
        raise CatalogReadError("manual-service category was malformed") from None
    name = _nonempty_text(value.get("name"), "manual-service category name")
    kind = _nonempty_text(value.get("kind"), "manual-service kind")
    chat = value.get("chat")
    info = value.get("info", "")
    if not isinstance(chat, bool) or not isinstance(info, str):
        raise CatalogReadError("manual-service category details were malformed")
    return ManualServiceCategory(
        service_id, name, kind, chat, info, _optional_provider_image(value.get("imageurl"))
    )


def _parse_manual_offer(value: Any) -> ManualServiceOffer:
    if not isinstance(value, dict):
        raise CatalogReadError("manual-service offer was malformed")
    try:
        offer_id = _require_nonempty_string(value.get("id"))
    except ValueError:
        raise CatalogReadError("manual-service offer was malformed") from None
    name = _nonempty_text(value.get("name"), "manual-service offer name")
    cost = _decimal_amount(value.get("price_usd"), "manual-service offer price")
    delivery = _optional_count(value.get("delivery_minutes"), "manual-service delivery time")
    schema = None
    for schema_key in ("fields", "input_fields", "replenishment_fields"):
        if schema_key in value:
            raw_schema = value[schema_key]
            if not isinstance(raw_schema, (dict, list)):
                raise CatalogReadError("manual-service input schema was malformed")
            schema = json.dumps(raw_schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            break
    return ManualServiceOffer(offer_id, name, cost, delivery, schema)


def _parse_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise CatalogReadError(f"{label} was malformed")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise CatalogReadError(f"{label} was malformed") from None
    if not result.is_finite() or result < 0:
        raise CatalogReadError(f"{label} was malformed")
    return result


def _decimal_amount(value: Any, label: str) -> Decimal:
    return _parse_decimal(value, label)


def _optional_count(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CatalogReadError(f"{label} was malformed")
    return value


def _positive_identifier(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise CatalogReadError(f"{label} was malformed")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise CatalogReadError(f"{label} was malformed") from None
    if result < 1 or str(value).strip() != str(result):
        raise CatalogReadError(f"{label} was malformed")
    return result


def _matches_requested_id(
    payload: dict[str, Any], key: str, expected: str, family: str
) -> str:
    value = payload.get(key)
    try:
        actual = _require_nonempty_string(value)
    except ValueError:
        raise CatalogReadError(f"{family} response omitted its category identifier") from None
    if actual != expected:
        raise CatalogReadError(f"{family} response did not match the requested category")
    return actual


def _nonempty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogReadError(f"{label} was malformed")
    return value


def _unique_ids(items: tuple[Any, ...], family: str) -> None:
    identifiers = [
        getattr(item, "supplier_item_id", None)
        or getattr(item, "supplier_service_id", None)
        or getattr(item, "app_id", None)
        or getattr(item, "supplier_sub_id", None)
        or getattr(item, "supplier_offer_id", None)
    for item in items]
    if len(set(identifiers)) != len(identifiers):
        raise CatalogReadError(f"{family} catalog contained duplicate identifiers")


__all__ = [
    "FazerCardsDigitalCatalogAdapter",
    "GameKeyCatalog",
    "GameKeyCategory",
    "GameKeyPage",
    "GameKeyRestrictions",
    "GiftCardCatalog",
    "InventoryOffer",
    "ManualServiceCatalog",
    "ManualServiceCategory",
    "ManualServiceOffer",
    "SteamGiftCatalog",
    "SteamGiftEdition",
    "SteamGiftGame",
    "SteamGiftPage",
    "SteamGiftRegionOffer",
    "SteamTopupRates",
]
