#!/usr/bin/env python3
"""Read-only, resumable FazerCards catalog inventory.

Only documented GET catalog/capability/quote/payment-method endpoints are
called. The script never reads account profile/balance, creates invoices,
validates a recipient, submits orders, or changes reseller settings. Set the
API key in FAZERCARDS_API_KEY or pass it once on stdin with --key-stdin; the
key is never written to the snapshot or included in error output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from aurix_games.providers.fazercards import (
    MAX_PAGE_SIZE,
    STEAM_GIFT_CATALOG_MIN_INTERVAL_SECONDS,
    CatalogReadError,
    FazerCardsReadOnlyClient,
    _optional_provider_image,
)

MAX_CURSOR_PAGES = 500
GENERAL_CATALOG_PAGE_SIZE = MAX_PAGE_SIZE
MAX_CURSOR_ITEMS = MAX_CURSOR_PAGES * GENERAL_CATALOG_PAGE_SIZE
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


def _list_cursor_items(
    client: FazerCardsReadOnlyClient,
    path: str,
    *,
    limit: int = 50,
    max_pages: int = MAX_CURSOR_PAGES,
    include_ui: bool = False,
    item_id_key: str | None = None,
    initial: dict[str, Any] | None = None,
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= MAX_CURSOR_PAGES:
        raise ValueError(f"max_pages must be between 1 and {MAX_CURSOR_PAGES}")
    resume = initial or {}
    items = resume.get("items", [])
    seen_raw = resume.get("seen_cursors", [])
    first_meta = resume.get("catalog_meta", {})
    cursor = resume.get("next_cursor")
    page_count = resume.get("page_count", 0)
    complete = resume.get("complete", False)
    if not isinstance(items, list) or not all(isinstance(row, dict) for row in items):
        raise CatalogReadError(f"GET {path} resume checkpoint has malformed items")
    if not isinstance(seen_raw, list) or not all(
        isinstance(value, str) and 0 < len(value) <= 2048 for value in seen_raw
    ) or len(set(seen_raw)) != len(seen_raw) or len(seen_raw) > max_pages:
        raise CatalogReadError(f"GET {path} resume checkpoint has invalid cursors")
    if not isinstance(first_meta, dict):
        raise CatalogReadError(f"GET {path} resume checkpoint has invalid metadata")
    if cursor is not None and (
        not isinstance(cursor, str) or not cursor or len(cursor) > 2048
    ):
        raise CatalogReadError(f"GET {path} resume checkpoint has an invalid cursor")
    if isinstance(page_count, bool) or not isinstance(page_count, int) or not 0 <= page_count <= max_pages:
        raise CatalogReadError(f"GET {path} resume checkpoint has invalid page count")
    if not isinstance(complete, bool) or (complete and cursor is not None) or (not complete and page_count > 0 and cursor is None):
        raise CatalogReadError(f"GET {path} resume checkpoint has inconsistent completion state")
    seen_cursors = set(seen_raw)
    if len(items) > MAX_CURSOR_ITEMS:
        raise CatalogReadError(f"GET {path} resume checkpoint exceeds the item safety bound")
    if item_id_key:
        existing_ids: set[str] = set()
        for row in items:
            raw_id = row.get(item_id_key)
            if (
                isinstance(raw_id, bool)
                or not isinstance(raw_id, (str, int))
                or not str(raw_id).strip()
            ):
                raise CatalogReadError(f"GET {path} resume checkpoint has a row without {item_id_key}")
            normalized_id = str(raw_id).strip()
            if normalized_id in existing_ids:
                raise CatalogReadError(f"GET {path} resume checkpoint has duplicate {item_id_key}")
            existing_ids.add(normalized_id)
    if complete:
        return list(items), dict(first_meta)
    if page_count == max_pages:
        raise CatalogReadError(f"GET {path} exceeded the {max_pages}-page safety bound")

    for _ in range(page_count, max_pages):
        query: dict[str, Any] = {"limit": limit, "cursor": cursor}
        if include_ui:
            query["include_ui"] = True
        payload = client.get(path, **query)
        if include_ui:
            _validate_provider_image_fields(payload)
        page_items = payload.get("items", [])
        if not isinstance(page_items, list) or not all(isinstance(row, dict) for row in page_items):
            raise CatalogReadError(f"GET {path} returned a malformed items list")
        if len(page_items) > limit:
            raise CatalogReadError(f"GET {path} exceeded the requested page size")
        if item_id_key:
            page_ids: set[str] = set()
            for row in page_items:
                raw_id = row.get(item_id_key)
                if (
                    isinstance(raw_id, bool)
                    or not isinstance(raw_id, (str, int))
                    or not str(raw_id).strip()
                ):
                    raise CatalogReadError(f"GET {path} returned a row without {item_id_key}")
                normalized_id = str(raw_id).strip()
                if normalized_id in existing_ids or normalized_id in page_ids:
                    raise CatalogReadError(f"GET {path} returned a duplicate {item_id_key}")
                page_ids.add(normalized_id)
            existing_ids.update(page_ids)
        items.extend(page_items)
        if len(items) > MAX_CURSOR_ITEMS:
            raise CatalogReadError(f"GET {path} exceeded the {MAX_CURSOR_ITEMS}-item safety bound")
        meta = payload.get("meta")
        if not isinstance(meta, dict):
            raise CatalogReadError(f"GET {path} omitted cursor metadata")
        if not first_meta:
            first_meta = dict(meta)
        next_cursor = meta.get("next_cursor")
        if next_cursor == "":
            next_cursor = None
        if next_cursor is not None and (
            not isinstance(next_cursor, str) or len(next_cursor) > 2048
        ):
            raise CatalogReadError(f"GET {path} returned an invalid cursor")
        total = meta.get("total")
        if total is not None and (
            isinstance(total, bool) or not isinstance(total, int) or total < 0
        ):
            raise CatalogReadError(f"GET {path} returned an invalid total")
        has_more = meta.get("has_more")
        if not isinstance(has_more, bool):
            raise CatalogReadError(f"GET {path} returned invalid pagination state")
        if has_more and not next_cursor:
            raise CatalogReadError(f"GET {path} omitted its next cursor")
        if not has_more and next_cursor:
            raise CatalogReadError(f"GET {path} returned a cursor without another page")
        if not has_more:
            if checkpoint:
                checkpoint({
                    "items": list(items),
                    "catalog_meta": dict(first_meta),
                    "next_cursor": None,
                    "seen_cursors": sorted(seen_cursors),
                    "page_count": page_count + 1,
                    "complete": True,
                })
            return items, first_meta
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
            raise CatalogReadError(f"GET {path} returned an invalid/repeated cursor")
        seen_cursors.add(next_cursor)
        page_count += 1
        cursor = next_cursor
        if checkpoint:
            checkpoint({
                "items": list(items),
                "catalog_meta": dict(first_meta),
                "next_cursor": cursor,
                "seen_cursors": sorted(seen_cursors),
                "page_count": page_count,
                "complete": False,
            })
    raise CatalogReadError(f"GET {path} exceeded the {max_pages}-page safety bound")


def _capture_steam_gifts(
    client: FazerCardsReadOnlyClient,
    families: dict[str, Any],
    coverage: dict[str, Any],
    *,
    save: Callable[[], None],
    now: float | None = None,
) -> dict[str, Any]:
    """Read the full Steam title list once, respecting its three-minute quota."""
    steam = families.get("steam_gifts")
    had_checkpoint = isinstance(steam, dict) and bool(steam)
    if not isinstance(steam, dict):
        steam = {}
        families["steam_gifts"] = steam
    if steam.get("terminal_error") is True:
        return steam

    games = steam.get("games")
    metadata = steam.get("catalog_meta", {})
    full_list_present = (
        isinstance(games, list)
        and isinstance(metadata, dict)
        and metadata.get("truncated") is False
        and isinstance(metadata.get("total"), int)
        and len(games) == metadata["total"]
    )
    if full_list_present:
        return steam

    current_time = time.time() if now is None else now
    last_request = steam.get("full_list_request_started_at")
    cooldown_note = "Steam full-title catalog read is waiting for its three-minute rate-limit window."
    if last_request is None and had_checkpoint:
        # Older snapshots do not record when their last capped request ran.
        # Start a conservative cooldown before upgrading them to the full list.
        steam["full_list_request_started_at"] = current_time
        steam["read_error"] = "Steam catalog cooldown initialized; resume after 180 seconds"
        coverage["complete"] = False
        if cooldown_note not in coverage.setdefault("notes", []):
            coverage["notes"].append(cooldown_note)
        save()
        return steam
    if last_request is not None:
        if (
            isinstance(last_request, bool)
            or not isinstance(last_request, (int, float))
            or current_time - float(last_request) < STEAM_GIFT_CATALOG_MIN_INTERVAL_SECONDS
        ):
            remaining = STEAM_GIFT_CATALOG_MIN_INTERVAL_SECONDS
            if isinstance(last_request, (int, float)) and not isinstance(last_request, bool):
                remaining = max(
                    0.0,
                    STEAM_GIFT_CATALOG_MIN_INTERVAL_SECONDS
                    - (current_time - float(last_request)),
                )
            steam["read_error"] = (
                "Steam catalog cooldown active; resume after "
                f"{int(remaining + 0.999)} seconds"
            )
            coverage["complete"] = False
            if cooldown_note not in coverage.setdefault("notes", []):
                coverage["notes"].append(cooldown_note)
            save()
            return steam

    steam["full_list_request_started_at"] = current_time
    steam.pop("read_error", None)
    families["steam_gifts"] = steam
    save()
    try:
        # OpenAPI explicitly says omitting limit returns the full title catalog;
        # this avoids two calls against the endpoint's 1 request / 3 min quota.
        payload = client.get("/steam-gifts/games")
        raw_games = payload.get("games")
        meta = payload.get("meta")
        if not isinstance(raw_games, list) or not all(
            isinstance(game, dict) for game in raw_games
        ) or not isinstance(meta, dict):
            raise CatalogReadError("GET /steam-gifts/games returned malformed data")
        total = meta.get("total")
        returned = meta.get("returned")
        truncated = meta.get("truncated")
        if (
            isinstance(total, bool)
            or not isinstance(total, int)
            or total < 0
            or isinstance(returned, bool)
            or not isinstance(returned, int)
            or returned != len(raw_games)
            or returned > total
            or not isinstance(truncated, bool)
            or (returned < total and not truncated)
            or (returned == total and truncated)
        ):
            raise CatalogReadError("GET /steam-gifts/games returned inconsistent full-list metadata")
        steam.update(
            games=raw_games,
            catalog_meta=meta,
            offers_expanded=False,
            full_list_complete=(not truncated and returned == total),
        )
        if truncated:
            steam["read_error"] = "provider still truncated the Steam list without a limit"
            steam["terminal_error"] = True
            coverage["complete"] = False
        else:
            steam.pop("read_error", None)
            steam.pop("terminal_error", None)
            if cooldown_note in coverage.setdefault("notes", []):
                coverage["notes"].remove(cooldown_note)
    except CatalogReadError as exc:
        steam["read_error"] = str(exc)
        coverage["complete"] = False
        if cooldown_note not in coverage.setdefault("notes", []):
            coverage["notes"].append(cooldown_note)
    save()
    return steam


def _category_detail(
    client: FazerCardsReadOnlyClient,
    endpoint: str,
    id_key: str,
    category: dict[str, Any],
    *,
    include_ui: bool = False,
) -> dict[str, Any]:
    identifier = category.get(id_key)
    if not isinstance(identifier, (str, int)) or not str(identifier):
        raise CatalogReadError(f"catalog category omitted {id_key}")
    query: dict[str, Any] = {"category_id": str(identifier)}
    if include_ui:
        query["include_ui"] = True
    payload = client.get(endpoint, **query)
    if include_ui:
        _validate_provider_image_fields(payload)
    return payload


def _validate_provider_image_fields(value: Any) -> None:
    """Validate every optional image path before it enters a private checkpoint."""

    if isinstance(value, dict):
        for key, item in value.items():
            if key == "imageurl":
                _optional_provider_image(item)
            else:
                _validate_provider_image_fields(item)
    elif isinstance(value, list):
        for item in value:
            _validate_provider_image_fields(item)


def collect_inventory(
    client: FazerCardsReadOnlyClient,
    *,
    initial: dict[str, Any] | None = None,
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
    include_ui: bool = True,
    selection: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Fetch account catalogs and checkpoint successful reads for safe resume."""
    if selection is not None:
        if (not isinstance(selection, dict) or len(selection) != 1
                or not set(selection) <= {'topups', 'giftcards', 'gamekeys'}):
            raise ValueError('select one supported catalog family')
        identifiers = next(iter(selection.values()))
        if (not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 30
                or any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', v) for v in identifiers)
                or len(set(identifiers)) != len(identifiers)):
            raise ValueError('select 1–30 unique category identifiers')
        selection = {next(iter(selection)): sorted(identifiers)}
    if initial is not None and initial.get('selection') != selection:
        raise ValueError('resume selection must match the original capture')
    catalog = initial or {
        "snapshot_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": "FazerCards authenticated API v2; read-only catalog requests",
        "status": "running",
        "families": {},
        "coverage": {"complete": True, "notes": []},
    }
    catalog.setdefault("families", {})
    catalog.setdefault("coverage", {"complete": True, "notes": []})
    if selection is not None:
        catalog['selection'] = selection
        catalog['coverage'] = {'complete': True, 'notes': ['Coverage is limited to the explicitly selected categories.']}
    catalog["artwork_requested_on_last_run"] = include_ui
    catalog["status"] = "running"
    catalog.pop("completed_at", None)

    def save() -> None:
        if checkpoint:
            checkpoint(catalog)

    # The broad feed appears in cookbook material but is absent from the
    # public OpenAPI. Preserve legacy private rows only as unverified audit
    # material and never issue a request to that undocumented route.
    if selection is None:
        full_catalog = catalog["families"].get("full_catalog")
        if not isinstance(full_catalog, dict):
            full_catalog = {"items": []}
            catalog["families"]["full_catalog"] = full_catalog
        full_catalog["supported"] = False
        full_catalog["verification_status"] = "undocumented"
        full_catalog["complete"] = False
        full_catalog["read_error"] = "GET /catalog is absent from public OpenAPI; no request made"
        catalog["coverage"]["complete"] = False
        full_catalog_note = "The cookbook-only GET /catalog feed is absent from public OpenAPI and is not requested."
        notes = catalog["coverage"].setdefault("notes", [])
        if isinstance(notes, list) and full_catalog_note not in notes:
            notes.append(full_catalog_note)
    save()
    for family, category_path, detail_path, id_key in (
        ("topups", "/topups", "/topups/offers", "category_id"),
        ("giftcards", "/giftcards", "/giftcards/cards", "category_id"),
        ("gamekeys", "/gamekeys", "/gamekeys/keys", "game_id"),
    ):
        if selection is not None and family not in selection:
            continue
        family_data = catalog["families"].get(family)
        if (
            isinstance(family_data, dict)
            and family_data.get("details_complete") is True
            and family_data.get("categories_complete", True) is True
        ):
            continue

        if not isinstance(family_data, dict):
            family_data = {
                "categories": [],
                "details_by_id": {},
                "details_complete": False,
                "categories_complete": False,
                "category_scan": {},
                "read_errors": [],
            }
            catalog["families"][family] = family_data
        elif "categories_complete" not in family_data:
            # Earlier collector versions only checkpointed the category list
            # after exhausting all pages, so legacy lists are complete.
            family_data["categories_complete"] = isinstance(family_data.get("categories"), list)
        if not isinstance(family_data.get("categories"), list):
            family_data["read_error"] = "resume snapshot category list is malformed"
            catalog["coverage"]["complete"] = False
            save()
            continue
        if not isinstance(family_data.get("category_scan"), dict):
            family_data["read_error"] = "resume snapshot cursor state is malformed"
            catalog["coverage"]["complete"] = False
            save()
            continue
        family_data.setdefault("categories", [])
        family_data.setdefault("category_scan", {})
        family_data.setdefault("details_by_id", {})
        family_data.setdefault("read_errors", [])

        if family_data.get("categories_complete") is not True:
            def save_category_page(progress: dict[str, Any]) -> None:
                family_data["categories"] = progress["items"]
                family_data["catalog_meta"] = progress["catalog_meta"]
                family_data["category_scan"] = {
                    "next_cursor": progress["next_cursor"],
                    "seen_cursors": progress["seen_cursors"],
                    "page_count": progress["page_count"],
                    "complete": progress["complete"],
                }
                family_data["categories_complete"] = progress["complete"]
                save()

            try:
                categories, meta = _list_cursor_items(
                    client,
                    category_path,
                    include_ui=include_ui,
                    item_id_key=id_key,
                    initial={
                        **family_data["category_scan"],
                        "items": family_data["categories"],
                        "catalog_meta": family_data.get("catalog_meta", {}),
                    },
                    checkpoint=save_category_page,
                )
            except CatalogReadError as exc:
                family_data["read_error"] = str(exc)
                catalog["coverage"]["complete"] = False
                save()
                continue
            family_data["categories"] = categories
            family_data["catalog_meta"] = meta
            family_data["categories_complete"] = True
            family_data["categories_observed_at"] = datetime.now(timezone.utc).isoformat()
            family_data.pop("read_error", None)
            save()

        if family_data.get("categories_complete") is not True:
            catalog["coverage"]["complete"] = False
            continue

        if selection is not None:
            known = {str(row.get(id_key)) for row in family_data['categories']}
            if not set(selection[family]) <= known:
                raise CatalogReadError('selected category is absent from the complete supplier category list')

        family_data.setdefault("details_by_id", {})
        family_data.setdefault("details_observed_at_by_id", {})
        family_data.setdefault("read_errors", [])
        for category in family_data["categories"]:
            identifier = category.get(id_key) if isinstance(category, dict) else None
            identifier = str(identifier) if identifier is not None else ""
            if selection is not None and identifier not in selection[family]:
                continue
            if not identifier:
                family_data["read_errors"].append({"id": None, "error": f"catalog row omitted {id_key}"})
                catalog["coverage"]["complete"] = False
                save()
                continue
            if identifier in family_data["details_by_id"]:
                continue
            try:
                if family == "gamekeys":
                    detail = client.get(
                        detail_path, game_id=identifier, include_ui=include_ui
                    )
                    if include_ui:
                        _validate_provider_image_fields(detail)
                    if detail.get("region_restriction"):
                        detail["region_restrictions"] = client.get(
                            "/gamekeys/region-restriction", game_id=identifier
                        )
                else:
                    detail = _category_detail(
                        client,
                        detail_path,
                        id_key,
                        category,
                        include_ui=include_ui,
                    )
                if selection is not None and (
                    str(detail.get(id_key)) != identifier
                    or not isinstance(detail.get('keys' if family == 'gamekeys' else 'offers'), list)
                ):
                    raise CatalogReadError('selected category detail does not match the requested category')
                family_data["details_by_id"][identifier] = detail
                family_data["details_observed_at_by_id"][identifier] = datetime.now(timezone.utc).isoformat()
                family_data["read_errors"] = [
                    error for error in family_data["read_errors"]
                    if not isinstance(error, dict) or error.get("id") != identifier
                ]
            except CatalogReadError as exc:
                family_data["read_errors"] = [error for error in family_data["read_errors"]
                    if not isinstance(error, dict) or error.get("id") != identifier]
                family_data["read_errors"].append({"id": identifier, "error": str(exc)})
                catalog["coverage"]["complete"] = False
            save()
        family_data["details_complete"] = (
            len(family_data["details_by_id"]) == len(family_data["categories"])
            and not family_data["read_errors"]
        )
        if selection is not None:
            family_data['selected_details_complete'] = (
                all(identifier in family_data['details_by_id'] for identifier in selection[family])
                and not family_data['read_errors']
            )
        complete = family_data.get('selected_details_complete') if selection is not None else family_data['details_complete']
        if not complete:
            catalog["coverage"]["complete"] = False
        save()

    if selection is not None:
        catalog['status'] = 'complete' if catalog['coverage']['complete'] else 'partial'
        catalog['completed_at'] = datetime.now(timezone.utc).isoformat()
        save()
        return catalog

    validation_data = catalog["families"].get("topup_validation")
    validation_items = (
        validation_data.get("items") if isinstance(validation_data, dict) else None
    )
    if (
        not isinstance(validation_data, dict)
        or "read_error" in validation_data
        or not isinstance(validation_items, list)
        or not all(isinstance(row, dict) for row in validation_items)
    ):
        try:
            validation_data = client.get("/topups/validate-id")
            items = validation_data.get("items")
            if not isinstance(items, list) or not all(
                isinstance(row, dict) for row in items
            ):
                raise CatalogReadError(
                    "GET /topups/validate-id returned a malformed items list"
                )
            catalog["families"]["topup_validation"] = validation_data
            validation_data["observed_at"] = datetime.now(timezone.utc).isoformat()
            save()
        except CatalogReadError as exc:
            catalog["families"]["topup_validation"] = {"read_error": str(exc)}
            catalog["coverage"]["complete"] = False
            save()

    steam_data = _capture_steam_gifts(
        client, catalog["families"], catalog["coverage"], save=save
    )
    if isinstance(steam_data.get("games"), list):
        note = "Steam gift per-title regional offers are not expanded; the documented detail route requires one GET per appid."
        if note not in catalog["coverage"]["notes"]:
            catalog["coverage"]["notes"].append(note)
        catalog["coverage"]["complete"] = False
        save()

    for family, path in (
        ("steam_topup", "/steam-topup/rates"),
        ("telegram_stars", "/telegram/stars"),
        ("telegram_premium", "/telegram/premium"),
        ("manual_services", "/manual-services"),
        ("payment_methods", "/payments/methods"),
    ):
        current = catalog["families"].get(family)
        if isinstance(current, dict) and "read_error" not in current:
            payload = current
        else:
            try:
                if family == "manual_services":
                    payload = client.get(path, include_ui=include_ui)
                    if include_ui:
                        _validate_provider_image_fields(payload)
                else:
                    payload = client.get(path)
                catalog["families"][family] = payload
                if family == "manual_services":
                    payload["items_observed_at"] = datetime.now(timezone.utc).isoformat()
                save()
            except CatalogReadError as exc:
                catalog["families"][family] = {"read_error": str(exc)}
                catalog["coverage"]["complete"] = False
                save()
                continue
        if family == "manual_services":
            services = payload.get("items", [])
            if isinstance(services, list):
                offers_by_id = payload.setdefault("offers_by_id", {})
                for service in services:
                    if not isinstance(service, dict) or service.get("id") is None:
                        continue
                    service_id = str(service["id"])
                    if service_id in offers_by_id and "read_error" not in offers_by_id[service_id]:
                        continue
                    try:
                        offers_by_id[service_id] = client.get(
                            f"/manual-services/{urllib.parse.quote(service_id, safe='')}/offers",
                            include_ui=include_ui,
                        )
                        if include_ui:
                            _validate_provider_image_fields(offers_by_id[service_id])
                        payload.setdefault("offers_observed_at_by_id", {})[
                            service_id
                        ] = datetime.now(timezone.utc).isoformat()
                    except CatalogReadError as exc:
                        offers_by_id[service_id] = {"read_error": str(exc)}
                        catalog["coverage"]["complete"] = False
                    save()

    catalog["status"] = "complete" if catalog["coverage"]["complete"] else "partial"
    catalog["completed_at"] = datetime.now(timezone.utc).isoformat()
    save()
    return catalog


def _write_snapshot(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise ValueError("catalog snapshot exceeded the 64 MiB safety limit")
    try:
        with temporary.open("xb") as output:
            os.chmod(temporary, 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _summary(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"captured_at": payload["captured_at"], "coverage": payload["coverage"], "families": {}}
    if payload.get('selection') is not None:
        result['selection'] = payload['selection']
    for name, family in payload["families"].items():
        if not isinstance(family, dict):
            continue
        if name == "full_catalog":
            rows = family.get("items", [])
            meta = family.get("catalog_meta", {})
            result["families"][name] = {
                "item_count": len(rows) if isinstance(rows, list) else 0,
                "reported_total": meta.get("total") if isinstance(meta, dict) else None,
                "page_count": family.get("page_count", 0),
                "complete": family.get("complete") is True,
                "supported": family.get("supported") is True,
                "verification_status": family.get("verification_status", "unknown"),
                "truncated": family.get("complete") is not True,
            }
        elif "categories" in family:
            detail_map = family.get("details_by_id", {})
            offer_count = sum(
                len(detail.get("offers", detail.get("keys", [])))
                for detail in detail_map.values()
                if isinstance(detail, dict)
            )
            result["families"][name] = {
                "category_count": len(family["categories"]),
                "detail_count": len(detail_map),
                "offer_or_key_count": offer_count,
                "details_complete": family.get("details_complete"),
                "selected_details_complete": family.get('selected_details_complete'),
                "catalog_total": family.get("catalog_meta", {}).get("total"),
            }
            if "read_error" in family:
                result["families"][name]["error"] = family["read_error"]
            result["families"][name]["categories_complete"] = family.get("categories_complete") is True
        elif name == "steam_gifts":
            steam_summary = {
                "game_count": len(family.get("games", [])),
                "catalog_total": family.get("catalog_meta", {}).get("total"),
                "truncated": family.get("catalog_meta", {}).get("truncated"),
                "offers_expanded": family.get("offers_expanded"),
                "full_list_complete": family.get("full_list_complete") is True,
            }
            if "read_error" in family:
                steam_summary["error"] = family["read_error"]
            result["families"][name] = steam_summary
        elif "read_error" in family:
            result["families"][name] = {"error": family["read_error"]}
        elif name == "manual_services":
            result["families"][name] = {
                "category_count": len(family.get("items", [])),
                "offer_category_count": len(family.get("offers_by_id", {})),
            }
        elif name == "topup_validation":
            result["families"][name] = {
                "validation_target_count": len(family.get("items", [])),
                "read": "ok",
            }
        else:
            result["families"][name] = {"read": "ok"}
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="private snapshot JSON path (mode 0600)")
    parser.add_argument("--key-stdin", action="store_true", help="read API key from stdin instead of environment")
    parser.add_argument("--resume", action="store_true", help="resume from an existing snapshot")
    parser.add_argument("--without-ui", action="store_true", help="omit optional provider artwork metadata; keep core catalog discovery independent of media approval")
    parser.add_argument("--min-interval", type=float, default=1.0, help="minimum seconds between GET requests")
    parser.add_argument('--family', choices=('topups', 'giftcards', 'gamekeys'), help='capture only selected categories in this family')
    parser.add_argument('--category', action='append', default=[], help='exact category ID; repeat up to 30 times with --family')
    args = parser.parse_args(argv)
    if bool(args.family) != bool(args.category):
        parser.error('--family and --category must be supplied together')
    initial = None
    if args.output.exists() and not args.resume:
        parser.error("output exists; use a new path or pass --resume")
    if args.resume:
        try:
            initial = json.loads(args.output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            parser.error("resume snapshot is missing or invalid JSON")
        if not isinstance(initial, dict) or initial.get("snapshot_version") != 1:
            parser.error("resume snapshot has an unsupported format")
    api_key = sys.stdin.readline().rstrip("\r\n") if args.key_stdin else os.environ.get("FAZERCARDS_API_KEY", "")
    if not api_key:
        parser.error("provide FAZERCARDS_API_KEY or use --key-stdin")
    try:
        payload = collect_inventory(
            FazerCardsReadOnlyClient(api_key, min_interval_seconds=args.min_interval),
            initial=initial,
            checkpoint=lambda current: _write_snapshot(args.output, current),
            include_ui=not args.without_ui,
            selection={args.family: args.category} if args.family else None,
        )
        _write_snapshot(args.output, payload)
    except (CatalogReadError, OSError, ValueError) as exc:
        print(f"catalog inventory failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(_summary(payload), ensure_ascii=False, indent=2))
    print(f"Private catalog snapshot saved: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
