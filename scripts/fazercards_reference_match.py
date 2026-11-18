#!/usr/bin/env python3
"""Summarize private supplier-to-reference title matches without exposing SKUs.

This is an offline-only report over the ignored FazerCards snapshot. It makes
no HTTP requests and emits aggregate metadata/counts only: never supplier
names, IDs, prices, stock, or recipient-field values.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from aurix_games.catalog import MAX_PAGE_SIZE, list_products
from aurix_games.catalog_matching import candidate_match_method


def _title_match(reference_name: str, supplier_name: Any) -> bool:
    """Conservative name candidate only; not a canonical product mapping."""
    return candidate_match_method(reference_name, supplier_name) is not None


def _all_reference_products() -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = list_products(limit=MAX_PAGE_SIZE, offset=offset)
        products.extend(page)
        if len(page) < MAX_PAGE_SIZE:
            return products
        offset += len(page)


def _family(snapshot: dict[str, Any], family_name: str) -> dict[str, Any]:
    families = snapshot.get("families", {})
    if not isinstance(families, dict):
        return {}
    result = families.get(family_name, {})
    return result if isinstance(result, dict) else {}


def _family_summary(
    snapshot: dict[str, Any],
    family_name: str,
    id_key: str,
    item_key: str,
    products: list[dict[str, Any]],
) -> dict[str, int]:
    family = _family(snapshot, family_name)
    categories = family.get("categories", [])
    details_by_id = family.get("details_by_id", {})
    if not isinstance(categories, list):
        categories = []
    if not isinstance(details_by_id, dict):
        details_by_id = {}

    matched_labels: set[str] = set()
    clean_labels: set[str] = set()
    password_labels: set[str] = set()
    matched_categories: dict[str, dict[str, Any]] = {}

    for product in products:
        name = str(product.get("name", ""))
        for category in categories:
            if not isinstance(category, dict) or not _title_match(
                name, category.get("name", "")
            ):
                continue
            matched_labels.add(str(product.get("slug", name)))
            identifier = category.get(id_key)
            if identifier is None:
                continue
            key = str(identifier)
            detail = details_by_id.get(key, {})
            if not isinstance(detail, dict):
                detail = {}
            items = detail.get(item_key, [])
            if not isinstance(items, list):
                items = []
            fields = detail.get("fields")
            field_list = fields if isinstance(fields, list) else []
            field_dicts = [field for field in field_list if isinstance(field, dict)]
            has_password = "password" in json.dumps(
                field_list, ensure_ascii=False
            ).casefold()
            matched_categories[key] = {
                "item_count": len(items),
                "password_marker": has_password,
                "field_array_present": isinstance(fields, list),
                "field_descriptor_count": len(field_dicts),
                "required_property_count": sum("required" in field for field in field_dicts),
                "required_true_count": sum(field.get("required") is True for field in field_dicts),
            }
            if items and isinstance(fields, list):
                if has_password:
                    password_labels.add(str(product.get("slug", name)))
                else:
                    clean_labels.add(str(product.get("slug", name)))

    categories_with_items = sum(
        item["item_count"] > 0 for item in matched_categories.values()
    )
    item_count = sum(
        item["item_count"] for item in matched_categories.values()
    )
    password_categories = sum(
        item["item_count"] > 0 and item["password_marker"]
        for item in matched_categories.values()
    )
    password_items = sum(
        item["item_count"]
        for item in matched_categories.values()
        if item["password_marker"]
    )
    categories_with_field_arrays = sum(
        item["field_array_present"] for item in matched_categories.values()
    )
    field_descriptors = sum(
        item["field_descriptor_count"] for item in matched_categories.values()
    )
    field_required_properties = sum(
        item["required_property_count"] for item in matched_categories.values()
    )
    field_required_true = sum(
        item["required_true_count"] for item in matched_categories.values()
    )
    return {
        "name_matched_product_labels": len(matched_labels),
        "labels_with_nonpassword_marker_schema_items": len(clean_labels),
        "labels_with_only_password_marked_items": len(password_labels - clean_labels),
        "matched_categories_with_items": categories_with_items,
        "items_in_matched_categories": item_count,
        "matched_categories_with_field_array": categories_with_field_arrays,
        "field_descriptors_in_matched_categories": field_descriptors,
        "field_descriptors_with_required_property": field_required_properties,
        "field_descriptors_explicitly_required": field_required_true,
        "password_schema_categories_with_items": password_categories,
        "items_in_password_schema_categories": password_items,
    }


def summarize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return sanitized aggregate coverage for the 65 public reference labels."""

    products = _all_reference_products()
    families = {
        "topups": _family_summary(
            snapshot, "topups", "category_id", "offers", products
        ),
        "giftcards": _family_summary(
            snapshot, "giftcards", "category_id", "offers", products
        ),
        "gamekeys": _family_summary(
            snapshot, "gamekeys", "game_id", "keys", products
        ),
    }
    category_coverage: dict[str, dict[str, int]] = {}
    for category_id in ("mobile-games", "pc-games", "gift-cards", "mobile-apps", "redeem-codes"):
        subset = [
            product
            for product in products
            if category_id in product.get("category_ids", [])
        ]
        topup_coverage = _family_summary(
            snapshot, "topups", "category_id", "offers", subset
        )
        giftcard_coverage = _family_summary(
            snapshot, "giftcards", "category_id", "offers", subset
        )
        gamekey_coverage = _family_summary(
            snapshot, "gamekeys", "game_id", "keys", subset
        )
        category_coverage[category_id] = {
            "topups_name_matched": topup_coverage["name_matched_product_labels"],
            "topups_with_nonpassword_marker_items": topup_coverage[
                "labels_with_nonpassword_marker_schema_items"
            ],
            "topups_with_only_password_marked_items": topup_coverage[
                "labels_with_only_password_marked_items"
            ],
            "giftcards_name_matched": giftcard_coverage[
                "name_matched_product_labels"
            ],
            "gamekeys_name_matched": gamekey_coverage[
                "name_matched_product_labels"
            ],
        }

    steam = _family(snapshot, "steam_gifts")
    games = steam.get("games", [])
    if not isinstance(games, list):
        games = []
    metadata = steam.get("catalog_meta", {})
    if not isinstance(metadata, dict):
        metadata = {}
    total = metadata.get("total")
    steam_match_count = sum(
        any(
            isinstance(game, dict)
            and _title_match(str(product.get("name", "")), game.get("name", ""))
            for game in games
        )
        for product in products
    )
    valid_total = isinstance(total, int) and not isinstance(total, bool)
    truncated = bool(metadata.get("truncated")) or (
        valid_total and len(games) < total
    )

    validation = _family(snapshot, "topup_validation")
    validation_items = validation.get("items")
    validation_captured = isinstance(validation_items, list) and all(
        isinstance(item, dict) for item in validation_items
    )
    validation_targets = validation_items if validation_captured else []
    validation_matches = {
        str(product.get("slug", product.get("name", "")))
        for product in products
        if any(
            _title_match(str(product.get("name", "")), item.get("name", ""))
            for item in validation_targets
        )
    }
    validation_match_schema_count = sum(
        _title_match(str(product.get("name", "")), item.get("name", ""))
        and isinstance(item.get("fields"), list)
        for product in products
        for item in validation_targets
    )

    return {
        "snapshot_captured_at": str(snapshot.get("captured_at", "unknown")),
        "snapshot_status": str(snapshot.get("status", "unknown")),
        "reference_product_labels": len(products),
        "matching_method": (
            "case-folded alphanumeric exact match, or reference title as a "
            "substring of a supplier title when at least 10 characters long"
        ),
        "candidate_families": families,
        "storefront_category_name_match_counts": category_coverage,
        "steam_gifts": {
            "titles_examined": len(games),
            "reported_total": total if valid_total else None,
            "truncated": truncated,
            "name_matched_reference_labels_in_examined_titles": steam_match_count,
            "per_title_offers_expanded": bool(steam.get("offers_expanded")),
        },
        "topup_validation_capabilities": {
            "captured": validation_captured,
            "target_count": len(validation_targets) if validation_captured else None,
            "name_matched_reference_labels": (
                len(validation_matches) if validation_captured else None
            ),
            "matched_target_rows_with_field_schema": (
                validation_match_schema_count if validation_captured else None
            ),
        },
        "limitations": [
            "Name matches are research candidates, not verified SKU mappings.",
            "Counts do not establish regional eligibility, valid recipient fields, resale rights, or current availability.",
            "Absence of a literal password marker does not certify a recipient schema as safe.",
            "Do not infer optionality when a provider field descriptor omits a required flag.",
            "No supplier names, identifiers, wholesale prices, stock, or recipient schemas are included.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        required=True,
        type=Path,
        help="path to the ignored, private FazerCards snapshot",
    )
    args = parser.parse_args(argv)
    try:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        parser.error("snapshot is missing or invalid JSON")
    if not isinstance(snapshot, dict):
        parser.error("snapshot must be a JSON object")
    print(json.dumps(summarize_snapshot(snapshot), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
