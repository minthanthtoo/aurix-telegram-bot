"""Exact, private catalog inventory derived from an API snapshot.

The storefront must not invent SKU rows in JavaScript.  This module reads the
validated private snapshot produced by the FazerCards API collector and
returns exact supplier identifiers and source fields for the authenticated
operator boundary only.  Customer APIs continue to receive only approved,
sanitized AuriX offers.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
from typing import Any

from .catalog_repository import _extract_records, _read_private_snapshot


class CatalogInventoryError(ValueError):
    """The private catalog snapshot is unavailable or malformed."""


UTC = timezone.utc
DEFAULT_MAPPING_MAX_AGE_MINUTES = 15


def load_catalog_inventory(
    snapshot_path: Path | str,
    *,
    mapping_path: Path | str | None = None,
    max_age_minutes: int = DEFAULT_MAPPING_MAX_AGE_MINUTES,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Load exact normalized SKU rows from one private API snapshot.

    No network call or write occurs.  The returned ``items`` are the exact
    supplier rows after schema validation; the operator API paginates them.
    """

    if not isinstance(max_age_minutes, int) or isinstance(max_age_minutes, bool) or max_age_minutes < 0:
        raise CatalogInventoryError("mapping max age must be a non-negative integer")
    path = Path(snapshot_path)
    try:
        _raw, source_hash, snapshot = _read_private_snapshot(path)
        source_families = snapshot.get("families")
        steam_data = source_families.get("steam_gifts") if isinstance(source_families, dict) else None
        steam_games = steam_data.get("games") if isinstance(steam_data, dict) else None
        inventory_snapshot = snapshot
        if isinstance(steam_games, list):
            inventory_snapshot = dict(snapshot)
            inventory_families = dict(source_families)
            inventory_families["steam_gifts"] = {**steam_data, "games": []}
            inventory_snapshot["families"] = inventory_families
        records = _extract_records(inventory_snapshot)
    except Exception as exc:
        if isinstance(exc, CatalogInventoryError):
            raise
        raise CatalogInventoryError("catalog snapshot could not be loaded") from exc

    captured_at = _captured_at(snapshot)
    current = (now or datetime.now(UTC)).astimezone(UTC)
    freshness_status = _freshness_status(captured_at, current, timedelta(minutes=max_age_minutes))
    mapping_index, mapping_config_status = _load_mapping_index(mapping_path)
    categories = {
        (row["family"], row["provider_category_id"]): row
        for row in records["categories"]
        if isinstance(row, dict)
    }
    complete_categories: dict[str, set[str]] = {}
    if isinstance(source_families, dict):
        for family, id_key in (("topups", "category_id"), ("giftcards", "category_id"), ("gamekeys", "game_id")):
            family_data = source_families.get(family)
            if not isinstance(family_data, dict) or family_data.get("categories_complete") is not True:
                continue
            details = family_data.get("details_by_id")
            if not isinstance(details, dict):
                continue
            for category_row in family_data.get("categories", []):
                if not isinstance(category_row, dict):
                    continue
                identifier = category_row.get(id_key)
                if isinstance(identifier, bool) or not isinstance(identifier, (str, int)):
                    continue
                detail = details.get(str(identifier))
                if isinstance(detail, dict) and "read_error" not in detail:
                    complete_categories.setdefault(family, set()).add(str(identifier))
        manual = source_families.get("manual_services")
        if isinstance(manual, dict) and isinstance(manual.get("items"), list) and isinstance(manual.get("offers_by_id"), dict):
            for service in manual["items"]:
                if not isinstance(service, dict):
                    continue
                identifier = service.get("id")
                if isinstance(identifier, bool) or not isinstance(identifier, (str, int)):
                    continue
                detail = manual["offers_by_id"].get(str(identifier))
                if isinstance(detail, dict) and "read_error" not in detail:
                    complete_categories.setdefault("manual_services", set()).add(str(identifier))
    items: list[dict[str, Any]] = []
    for offer in records["offers"]:
        if not isinstance(offer, dict):
            continue
        category_id = str(offer.get("provider_category_id"))
        row_observed_at = _category_observed_at(
            snapshot, str(offer.get("family")), category_id, captured_at
        )
        row_freshness = _freshness_status(
            row_observed_at, current, timedelta(minutes=max_age_minutes)
        )
        category = categories.get((offer.get("family"), offer.get("provider_category_id")), {})
        metadata = _metadata(category.get("source_metadata_json"))
        item = {
                "family": offer.get("family"),
                "category_id": offer.get("provider_category_id"),
                "category_name": category.get("display_name"),
                "sku": offer.get("provider_offer_id"),
                "name": offer.get("display_name"),
                "price_usd": offer.get("supplier_cost_usd"),
                "stock": offer.get("stock"),
                "min_order_quantity": offer.get("minimum_quantity"),
                "max_order_quantity": offer.get("maximum_quantity"),
                "delivery_minutes": offer.get("delivery_minutes"),
                "input_schema": _json_value(offer.get("input_schema_json")),
                "region": metadata.get("region"),
                "platform": metadata.get("platform"),
                "region_restriction": metadata.get("region_restriction"),
                "review_state": offer.get("review_state", "blocked"),
                "source_category_complete": category_id
                in complete_categories.get(str(offer.get("family")), set()),
                "source_observed_at": row_observed_at.isoformat() if row_observed_at else None,
            }
        if offer.get("family") == "manual_services":
            item["manual_service"] = {
                "kind": metadata.get("kind"),
                "chat_enabled": metadata.get("chat"),
                "info": metadata.get("info", ""),
            }
        mapping = mapping_index.get(
            (offer.get("family"), offer.get("provider_category_id"), offer.get("provider_offer_id"))
        )
        mapping_gate, mapping_reasons, mapping_offer_id = _mapping_gate(offer, mapping, mapping_config_status)
        item["mapping_status"] = _mapping_status(mapping_gate, row_freshness)
        item["mapping_gate"] = mapping_gate
        item["freshness_status"] = row_freshness
        item["projection_status"] = (
            "projected" if mapping_gate == "ready" and row_freshness == "fresh" else "not_projected"
        )
        item["mapping_reasons"] = mapping_reasons
        if mapping_offer_id is not None:
            item["mapping_offer_id"] = mapping_offer_id
        items.append(item)

    # Steam app IDs and Telegram plan/rate metadata are exact API snapshot
    # records too, but they are not supplier offer SKUs in this snapshot. Keep
    # them explicit so callers never mistake a title or a calculated plan key
    # for an approved sellable offer.
    # Steam can list hundreds of thousands of titles; keep paged admin responses bounded.
    auxiliary = {
        "steam_titles": [
            {"app_id": game["appid"], "display_name": game["name"].strip()[:200]}
            for game in steam_games[:100]
            if isinstance(game, dict)
            and type(game.get("appid")) is int
            and game["appid"] > 0
            and isinstance(game.get("name"), str)
            and game["name"].strip()
        ] if isinstance(steam_games, list) else [],
        "steam_title_count": len(steam_games) if isinstance(steam_games, list) else 0,
        "steam_titles_truncated": isinstance(steam_games, list) and len(steam_games) > 100,
        "steam_full_list_complete": isinstance(steam_data, dict) and steam_data.get("full_list_complete") is True,
        "steam_offers_expanded": isinstance(steam_data, dict) and steam_data.get("offers_expanded") is True,
        "telegram_stars": _telegram_stars(snapshot),
        "telegram_premium": _telegram_premium(snapshot),
        "steam_topup": _steam_topup(snapshot),
        "payment_methods": _payment_methods(snapshot),
    }
    counts: dict[str, int] = {}
    for item in items:
        family = item.get("family")
        if isinstance(family, str):
            counts[family] = counts.get(family, 0) + 1
    return {
        "schema_version": 1,
        "source_sha256": source_hash,
        "captured_at": snapshot.get("captured_at"),
        "source_status": snapshot.get("status"),
        "source_selection": deepcopy(snapshot.get("selection")),
        "mapping_config_status": mapping_config_status,
        "freshness_status": freshness_status,
        "mapping_status_counts": _status_counts(items, "mapping_status"),
        "mapping_gate_counts": _status_counts(items, "mapping_gate"),
        "projection_status_counts": _status_counts(items, "projection_status"),
        "counts": dict(sorted(counts.items())),
        "category_counts": _category_counts(records.get("categories", [])),
        "items": items,
        "auxiliary": auxiliary,
    }


def _captured_at(snapshot: dict[str, Any]) -> datetime | None:
    value = snapshot.get("captured_at")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _category_observed_at(
    snapshot: dict[str, Any], family: str, category_id: str, fallback: datetime | None
) -> datetime | None:
    families = snapshot.get("families")
    family_data = families.get(family) if isinstance(families, dict) else None
    if not isinstance(family_data, dict):
        return fallback
    if family == "manual_services":
        details = family_data.get("offers_observed_at_by_id")
        category_at = _parse_observed_at(family_data.get("items_observed_at"))
    else:
        details = family_data.get("details_observed_at_by_id")
        category_at = _parse_observed_at(family_data.get("categories_observed_at"))
    detail_at = (
        _parse_observed_at(details.get(category_id))
        if isinstance(details, dict)
        else None
    )
    if detail_at is None or category_at is None:
        return fallback
    return min(detail_at, category_at)


def _parse_observed_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _freshness_status(
    captured_at: datetime | None, now: datetime, max_age: timedelta
) -> str:
    if captured_at is None or captured_at > now:
        return "unknown"
    return "fresh" if now - captured_at <= max_age else "stale"


def _load_mapping_index(path_value: Path | str | None) -> tuple[dict[tuple[str, str, str], dict[str, Any]], str]:
    if path_value is None:
        return {}, "unconfigured"
    path = Path(path_value)
    if path.is_symlink():
        return {}, "invalid"
    if not path.is_file():
        return {}, "missing"
    try:
        if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
            return {}, "invalid"
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            return {}, "invalid"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}, "invalid"
    if not isinstance(payload, dict) or not isinstance(payload.get("mappings"), list):
        return {}, "invalid"
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in payload["mappings"]:
        if not isinstance(row, dict):
            return {}, "invalid"
        key = tuple(row.get(field) for field in ("family", "provider_category_id", "provider_offer_id"))
        if not all(isinstance(value, str) and value.strip() for value in key):
            return {}, "invalid"
        if key in result:
            return {}, "invalid"
        result[key] = row
    return result, "loaded"


def _mapping_gate(
    offer: dict[str, Any], mapping: dict[str, Any] | None, config_status: str
) -> tuple[str, list[str], str | None]:
    if config_status in {"missing", "invalid"}:
        return "missing", [f"mapping_config_{config_status}"], None
    if mapping is None:
        return "missing", ["missing_mapping"], None
    reasons: list[str] = []
    review_state = mapping.get("review_state", "blocked")
    if review_state != "approved":
        reasons.append(f"review_state:{review_state}")
    if mapping.get("customer_visible") is not True:
        reasons.append("customer_visibility_disabled")
    if mapping.get("supplier_enabled") is not True:
        reasons.append("supplier_disabled")
    if mapping.get("resale_authorized") is not True:
        reasons.append("resale_unverified")
    rule = mapping.get("region_rule")
    if not isinstance(rule, dict) or rule.get("verified") is not True or (
        rule.get("allow_all") is not True
        and not (isinstance(rule.get("dimension"), str) and isinstance(rule.get("allowed_values"), list))
    ):
        reasons.append("region_rule_incomplete")
    stock = offer.get("stock")
    if isinstance(stock, int) and not isinstance(stock, bool) and stock <= 0:
        reasons.append("supplier_unavailable")
    if offer.get("family") in {"giftcards", "gamekeys"}:
        minimum, maximum = offer.get("minimum_quantity"), offer.get("maximum_quantity")
        if type(stock) is not int:
            reasons.append("supplier_stock_unknown")
        if type(minimum) is not int or minimum != 1 or type(maximum) is not int or maximum < 1:
            reasons.append("single_unit_quantity_not_supported")
    if reasons:
        return "blocked", reasons, mapping.get("offer_id") if isinstance(mapping.get("offer_id"), str) else None
    return "ready", [], mapping.get("offer_id") if isinstance(mapping.get("offer_id"), str) else None


def _mapping_status(mapping_gate: str, freshness_status: str) -> str:
    if mapping_gate == "missing":
        return "missing"
    if freshness_status != "fresh":
        return "stale"
    if mapping_gate == "ready":
        return "projected"
    return "blocked"


def _status_counts(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = item.get(key)
        if isinstance(value, str):
            counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _category_counts(rows: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    if not isinstance(rows, list):
        return result
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("family"), str):
            family = row["family"]
            result[family] = result.get(family, 0) + 1
    return dict(sorted(result.items()))


def _telegram_stars(snapshot: dict[str, Any]) -> dict[str, Any]:
    value = snapshot.get("families", {}).get("telegram_stars", {})
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in ("kind", "price_per_star", "min_amount", "max_amount", "rates_updated_at")
        if key in value and isinstance(value[key], (str, int, float, bool))
    }


def _telegram_premium(snapshot: dict[str, Any]) -> dict[str, Any]:
    value = snapshot.get("families", {}).get("telegram_premium", {})
    if not isinstance(value, dict):
        return {}
    plans = value.get("plans")
    return {
        "kind": value.get("kind"),
        "rates_updated_at": value.get("rates_updated_at"),
        "plans": deepcopy(plans) if isinstance(plans, list) else [],
    }


def _steam_topup(snapshot: dict[str, Any]) -> dict[str, Any]:
    value = snapshot.get("families", {}).get("steam_topup", {})
    if not isinstance(value, dict):
        return {}
    rates = value.get("rates")
    return {
        "base": value.get("base"),
        "updated_at": value.get("updated_at"),
        "rates": deepcopy(rates) if isinstance(rates, dict) else {},
    }


def _payment_methods(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    value = snapshot.get("families", {}).get("payment_methods", {})
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        return []
    allowed = {"code", "label", "minAmountUsd", "maxAmountUsd"}
    return [
        {key: item[key] for key in allowed if key in item}
        for item in value["items"]
        if isinstance(item, dict)
    ]


__all__ = ["CatalogInventoryError", "load_catalog_inventory"]
