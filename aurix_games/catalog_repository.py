"""Private, append-only SQLite snapshots for supplier catalog discovery.

Imported offers are always BLOCKED. This repository is not connected to the
public catalog or checkout and contains supplier IDs and wholesale costs, so
its database must stay inside a private local directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any

from .catalog import MAX_PAGE_SIZE, list_products
from .catalog_matching import candidate_match_method
from migrations import Migration, apply_migrations
from persistence import open_sqlite_connection


UTC = timezone.utc
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
_IMPORT_MIGRATIONS = (
    Migration(
        1,
        "private_supplier_catalog_snapshots",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS game_catalog_imports (
                   import_id TEXT PRIMARY KEY,
                   source_sha256 TEXT NOT NULL UNIQUE,
                   captured_at TEXT NOT NULL,
                   imported_at TEXT NOT NULL,
                   source_status TEXT NOT NULL CHECK (source_status IN ('complete', 'partial', 'running')),
                   coverage_json TEXT NOT NULL,
                   counts_json TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS game_catalog_current (
                   singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                   import_id TEXT NOT NULL REFERENCES game_catalog_imports(import_id)
               )""",
            """CREATE TABLE IF NOT EXISTS game_catalog_categories (
                   import_id TEXT NOT NULL REFERENCES game_catalog_imports(import_id),
                   family TEXT NOT NULL,
                   provider_category_id TEXT NOT NULL,
                   display_name TEXT NOT NULL,
                   region TEXT,
                   platform TEXT,
                   region_restriction INTEGER CHECK (region_restriction IN (0, 1)),
                   source_metadata_json TEXT NOT NULL DEFAULT '{}',
                   PRIMARY KEY (import_id, family, provider_category_id),
                   UNIQUE (import_id, family, display_name, provider_category_id)
               )""",
            """CREATE TABLE IF NOT EXISTS game_catalog_offers (
                   import_id TEXT NOT NULL,
                   family TEXT NOT NULL,
                   provider_category_id TEXT NOT NULL,
                   provider_offer_id TEXT NOT NULL,
                   display_name TEXT NOT NULL,
                   supplier_cost_usd TEXT NOT NULL,
                   stock INTEGER CHECK (stock IS NULL OR stock >= 0),
                   minimum_quantity INTEGER CHECK (minimum_quantity IS NULL OR minimum_quantity > 0),
                   maximum_quantity INTEGER CHECK (maximum_quantity IS NULL OR maximum_quantity > 0),
                   delivery_minutes INTEGER CHECK (delivery_minutes IS NULL OR delivery_minutes >= 0),
                   input_schema_json TEXT,
                   review_state TEXT NOT NULL DEFAULT 'blocked'
                       CHECK (review_state IN ('blocked', 'pending_review', 'approved', 'paused')),
                   PRIMARY KEY (import_id, family, provider_category_id, provider_offer_id),
                   FOREIGN KEY (import_id, family, provider_category_id)
                       REFERENCES game_catalog_categories(import_id, family, provider_category_id),
                   CHECK (minimum_quantity IS NULL OR maximum_quantity IS NULL
                          OR minimum_quantity <= maximum_quantity)
               )""",
            """CREATE TABLE IF NOT EXISTS game_catalog_steam_titles (
                   import_id TEXT NOT NULL REFERENCES game_catalog_imports(import_id),
                   app_id INTEGER NOT NULL CHECK (app_id > 0),
                   display_name TEXT NOT NULL,
                   PRIMARY KEY (import_id, app_id)
               )""",
            """CREATE TABLE IF NOT EXISTS game_catalog_validation_targets (
                   import_id TEXT NOT NULL REFERENCES game_catalog_imports(import_id),
                   provider_category_id TEXT NOT NULL,
                   display_name TEXT NOT NULL,
                   input_schema_json TEXT NOT NULL,
                   PRIMARY KEY (import_id, provider_category_id)
               )""",
            """CREATE TABLE IF NOT EXISTS game_catalog_metadata (
                   import_id TEXT NOT NULL REFERENCES game_catalog_imports(import_id),
                   family TEXT NOT NULL,
                   metadata_key TEXT NOT NULL,
                   payload_json TEXT NOT NULL,
                   PRIMARY KEY (import_id, family, metadata_key)
               )""",
            "CREATE INDEX IF NOT EXISTS game_catalog_offers_family ON game_catalog_offers(import_id, family, review_state)",
            "CREATE INDEX IF NOT EXISTS game_catalog_categories_family ON game_catalog_categories(import_id, family)",
        ),
    ),
    Migration(
        2,
        "private_reference_supplier_candidates",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS game_catalog_reference_candidates (
                   import_id TEXT NOT NULL REFERENCES game_catalog_imports(import_id),
                   reference_product_slug TEXT NOT NULL,
                   reference_product_name TEXT NOT NULL,
                   reference_category_ids_json TEXT NOT NULL,
                   family TEXT NOT NULL,
                   source_kind TEXT NOT NULL CHECK (source_kind IN ('category', 'steam_title', 'validation_target')),
                   source_id TEXT NOT NULL,
                   source_name TEXT NOT NULL,
                   match_method TEXT NOT NULL CHECK (match_method IN ('exact_title', 'reference_title_substring')),
                   review_state TEXT NOT NULL DEFAULT 'blocked'
                       CHECK (review_state IN ('blocked', 'approved', 'rejected')),
                   PRIMARY KEY (import_id, reference_product_slug, family, source_kind, source_id)
               )""",
            "CREATE INDEX IF NOT EXISTS game_catalog_candidates_reference ON game_catalog_reference_candidates(import_id, reference_product_slug, family, review_state)",
        ),
    ),
    Migration(
        3,
        "retain_sold_out_zero_quantity_limit",
        sqlite_statements=(
            """CREATE TABLE game_catalog_offers_v3 (
                import_id TEXT NOT NULL,
                family TEXT NOT NULL,
                provider_category_id TEXT NOT NULL,
                provider_offer_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                supplier_cost_usd TEXT NOT NULL,
                stock INTEGER CHECK (stock IS NULL OR stock >= 0),
                minimum_quantity INTEGER CHECK (minimum_quantity IS NULL OR minimum_quantity > 0),
                maximum_quantity INTEGER CHECK (maximum_quantity IS NULL OR maximum_quantity > 0
                    OR (maximum_quantity = 0 AND stock IS NOT NULL AND stock = 0)),
                delivery_minutes INTEGER CHECK (delivery_minutes IS NULL OR delivery_minutes >= 0),
                input_schema_json TEXT,
                review_state TEXT NOT NULL DEFAULT 'blocked'
                    CHECK (review_state IN ('blocked', 'pending_review', 'approved', 'paused')),
                PRIMARY KEY (import_id, family, provider_category_id, provider_offer_id),
                FOREIGN KEY (import_id, family, provider_category_id)
                    REFERENCES game_catalog_categories(import_id, family, provider_category_id),
                CHECK (minimum_quantity IS NULL OR maximum_quantity IS NULL
                    OR minimum_quantity <= maximum_quantity OR (stock = 0 AND maximum_quantity = 0))
            )""",
            "INSERT INTO game_catalog_offers_v3 SELECT * FROM game_catalog_offers",
            "DROP TABLE game_catalog_offers",
            "ALTER TABLE game_catalog_offers_v3 RENAME TO game_catalog_offers",
            "CREATE INDEX game_catalog_offers_family ON game_catalog_offers(import_id, family, review_state)",
        ),
    ),
)


class CatalogImportError(RuntimeError):
    """A private catalog snapshot is invalid or unsafe to import."""


@dataclass(frozen=True)
class CatalogImportSummary:
    import_id: str
    status: str
    category_counts: dict[str, int]
    offer_counts: dict[str, int]
    steam_title_count: int
    validation_target_count: int
    full_catalog_item_count: int
    candidate_counts: dict[str, int]
    matched_reference_counts: dict[str, int]
    already_imported: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "import_id": self.import_id,
            "status": self.status,
            "category_counts": dict(sorted(self.category_counts.items())),
            "offer_counts": dict(sorted(self.offer_counts.items())),
            "steam_title_count": self.steam_title_count,
            "validation_target_count": self.validation_target_count,
            "full_catalog_item_count": self.full_catalog_item_count,
            "candidate_counts": dict(sorted(self.candidate_counts.items())),
            "matched_reference_counts": dict(sorted(self.matched_reference_counts.items())),
            "review_state": "blocked",
            "already_imported": self.already_imported,
        }


class GameCatalogSnapshotDatabase:
    """Games-owned private SQLite catalog history with blocked-by-default offers."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        if self.path.is_symlink():
            raise CatalogImportError("catalog database must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_mode = stat.S_IMODE(self.path.parent.stat().st_mode)
        if parent_mode & 0o077:
            raise CatalogImportError("catalog database directory must be private (mode 0700)")
        try:
            with open_sqlite_connection(self.path) as connection:
                connection.execute("PRAGMA journal_mode = DELETE")
                connection.execute("PRAGMA synchronous = FULL")
                apply_migrations(
                    connection,
                    component="game_catalog_snapshot",
                    dialect="sqlite",
                    migrations=_IMPORT_MIGRATIONS,
                )
        except sqlite3.Error:
            raise CatalogImportError("catalog database initialization failed") from None
        os.chmod(self.path, 0o600)

    def import_snapshot(self, snapshot_path: Path | str) -> CatalogImportSummary:
        self.initialize()
        raw, source_hash, snapshot = _read_private_snapshot(Path(snapshot_path))
        del raw
        records = _extract_records(snapshot)
        import_id = hashlib.sha256(
            (source_hash + ":game-catalog-v1").encode("ascii")
        ).hexdigest()[:32]
        captured_at = snapshot.get("captured_at")
        if not isinstance(captured_at, str) or not captured_at:
            raise CatalogImportError("catalog snapshot omitted its capture timestamp")
        source_status = snapshot.get("status")
        if source_status not in {"complete", "partial", "running"}:
            raise CatalogImportError("catalog snapshot status was invalid")
        coverage = snapshot.get("coverage", {})
        if not isinstance(coverage, dict):
            raise CatalogImportError("catalog coverage metadata was malformed")
        imported_at = datetime.now(UTC).isoformat()
        counts = records["counts"]

        try:
            with open_sqlite_connection(self.path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT import_id, source_status, counts_json FROM game_catalog_imports WHERE source_sha256 = ?",
                    (source_hash,),
                ).fetchone()
                if existing:
                    _insert_metadata(connection, existing["import_id"], records["metadata"])
                    _insert_candidates(connection, existing["import_id"], records["candidates"])
                    connection.execute(
                        "UPDATE game_catalog_imports SET counts_json = ? WHERE import_id = ?",
                        (_json(counts), existing["import_id"]),
                    )
                    connection.execute(
                        "INSERT INTO game_catalog_current(singleton, import_id) VALUES (1, ?) "
                        "ON CONFLICT(singleton) DO UPDATE SET import_id = excluded.import_id",
                        (existing["import_id"],),
                    )
                    summary = _summary_from_counts(
                        existing["import_id"],
                        existing["source_status"],
                        counts,
                        already_imported=True,
                    )
                else:
                    connection.execute(
                        """INSERT INTO game_catalog_imports
                           (import_id, source_sha256, captured_at, imported_at,
                            source_status, coverage_json, counts_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            import_id,
                            source_hash,
                            captured_at,
                            imported_at,
                            source_status,
                            _json(coverage),
                            _json(counts),
                        ),
                    )
                    _insert_records(connection, import_id, records)
                    connection.execute(
                        "INSERT INTO game_catalog_current(singleton, import_id) VALUES (1, ?) "
                        "ON CONFLICT(singleton) DO UPDATE SET import_id = excluded.import_id",
                        (import_id,),
                    )
                    summary = _summary_from_counts(
                        import_id, source_status, counts, already_imported=False
                    )
        except CatalogImportError:
            raise
        except (sqlite3.Error, ValueError, TypeError):
            raise CatalogImportError("catalog snapshot transaction failed") from None
        os.chmod(self.path, 0o600)
        journal = Path(str(self.path) + "-journal")
        if journal.exists():
            os.chmod(journal, 0o600)
        return summary

    def current_summary(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            with open_sqlite_connection(self.path) as connection:
                row = connection.execute(
                    """SELECT i.import_id, i.source_status, i.counts_json
                         FROM game_catalog_current c
                         JOIN game_catalog_imports i ON i.import_id = c.import_id
                        WHERE c.singleton = 1"""
                ).fetchone()
                if row is None:
                    return None
                result = _summary_from_counts(
                    row["import_id"],
                    row["source_status"],
                    json.loads(row["counts_json"]),
                    already_imported=True,
                ).as_dict()
                return result
        except (sqlite3.Error, ValueError, TypeError):
            raise CatalogImportError("catalog database could not be read") from None


def _read_private_snapshot(path: Path) -> tuple[bytes, str, dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise CatalogImportError("catalog snapshot must be a regular private file")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise CatalogImportError("catalog snapshot must not be group/world accessible")
    try:
        raw = path.read_bytes()
    except OSError:
        raise CatalogImportError("catalog snapshot could not be read") from None
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise CatalogImportError("catalog snapshot exceeded the size limit")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CatalogImportError("catalog snapshot was not valid JSON") from None
    if not isinstance(payload, dict) or payload.get("snapshot_version") != 1:
        raise CatalogImportError("catalog snapshot version was unsupported")
    return raw, hashlib.sha256(raw).hexdigest(), payload


def _extract_records(snapshot: dict[str, Any]) -> dict[str, Any]:
    families = snapshot.get("families")
    if not isinstance(families, dict):
        raise CatalogImportError("catalog snapshot omitted families")
    result: dict[str, Any] = {
        "categories": [],
        "offers": [],
        "steam_titles": [],
        "validation_targets": [],
        "metadata": [],
        "candidates": [],
    }
    definitions = (
        ("topups", "category_id", "offers", "offer_id"),
        ("giftcards", "category_id", "offers", "card_id"),
        ("gamekeys", "game_id", "keys", "key_id"),
    )
    for family, id_key, item_key, offer_id_key in definitions:
        family_data = families.get(family, {})
        if not isinstance(family_data, dict):
            raise CatalogImportError(f"{family} snapshot family was malformed")
        categories = family_data.get("categories", [])
        details = family_data.get("details_by_id", {})
        if not isinstance(categories, list) or not isinstance(details, dict):
            raise CatalogImportError(f"{family} snapshot records were malformed")
        seen_categories: set[str] = set()
        for category in categories:
            if not isinstance(category, dict):
                raise CatalogImportError(f"{family} category was malformed")
            category_id = _required_id(category.get(id_key), f"{family} category identifier")
            if category_id in seen_categories:
                raise CatalogImportError(f"{family} categories contained duplicate identifiers")
            seen_categories.add(category_id)
            name = _required_text(category.get("name"), f"{family} category name")
            category_row = _category_record(family, category_id, name, category)
            detail = details.get(category_id, {})
            if not isinstance(detail, dict) or "read_error" in detail:
                result["categories"].append(category_row)
                continue
            _assert_detail_identity(detail, family, id_key, category_id)
            if family == "gamekeys" and "region_restrictions" in detail:
                metadata = json.loads(category_row["source_metadata_json"])
                metadata["restriction_details"] = detail["region_restrictions"]
                category_row["source_metadata_json"] = _json(metadata)
            result["categories"].append(category_row)
            items = detail.get(item_key, [])
            if not isinstance(items, list):
                raise CatalogImportError(f"{family} offer list was malformed")
            schema = detail.get("fields") if family == "topups" else None
            if schema is not None and not isinstance(schema, (dict, list)):
                raise CatalogImportError("supplier input schema was malformed")
            schema_json = _json(schema) if schema is not None else None
            seen_offers: set[str] = set()
            for offer in items:
                row = _offer_record(offer, family, offer_id_key, schema_json)
                if row["provider_offer_id"] in seen_offers:
                    raise CatalogImportError(f"{family} offers contained duplicate identifiers")
                seen_offers.add(row["provider_offer_id"])
                row.update(
                    family=family,
                    provider_category_id=category_id,
                    review_state="blocked",
                )
                result["offers"].append(row)

    _extract_manual_services(families.get("manual_services", {}), result)
    _extract_steam_gifts(families.get("steam_gifts", {}), result)
    _extract_validation(families.get("topup_validation", {}), result)
    steam_rates = families.get("steam_topup")
    if isinstance(steam_rates, dict) and "read_error" not in steam_rates:
        result["metadata"].append(("steam_topup", "rates", steam_rates))
    full_catalog = families.get("full_catalog")
    full_catalog_item_count = 0
    if isinstance(full_catalog, dict):
        full_catalog_items = full_catalog.get("items", [])
        if not isinstance(full_catalog_items, list) or not all(
            isinstance(item, dict) for item in full_catalog_items
        ):
            raise CatalogImportError("full catalog snapshot items were malformed")
        full_catalog_item_count = len(full_catalog_items)
        result["metadata"].append(("full_catalog", "snapshot", full_catalog))

    references = _all_reference_products()
    result["candidates"] = _build_candidate_records(result, references)

    counts: dict[str, Any] = {
        "categories": {},
        "offers": {},
        "steam_titles": len(result["steam_titles"]),
        "validation_targets": len(result["validation_targets"]),
        "full_catalog_items": full_catalog_item_count,
        "candidate_counts": {},
        "matched_reference_counts": {},
    }
    for item in result["categories"]:
        counts["categories"][item["family"]] = counts["categories"].get(item["family"], 0) + 1
    for item in result["offers"]:
        counts["offers"][item["family"]] = counts["offers"].get(item["family"], 0) + 1
    matched: dict[str, set[str]] = {}
    for item in result["candidates"]:
        counts["candidate_counts"][item["family"]] = (
            counts["candidate_counts"].get(item["family"], 0) + 1
        )
        matched.setdefault(item["family"], set()).add(item["reference_product_slug"])
    counts["matched_reference_counts"] = {
        family: len(slugs) for family, slugs in matched.items()
    }
    result["counts"] = counts
    return result


def _extract_manual_services(family_data: Any, result: dict[str, Any]) -> None:
    if not isinstance(family_data, dict):
        raise CatalogImportError("manual-service snapshot family was malformed")
    categories = family_data.get("items", [])
    offers_by_id = family_data.get("offers_by_id", {})
    if not isinstance(categories, list) or not isinstance(offers_by_id, dict):
        raise CatalogImportError("manual-service snapshot records were malformed")
    seen: set[str] = set()
    for category in categories:
        if not isinstance(category, dict):
            raise CatalogImportError("manual-service category was malformed")
        category_id = _required_id(category.get("id"), "manual-service identifier")
        if category_id in seen:
            raise CatalogImportError("manual-service categories contained duplicate identifiers")
        seen.add(category_id)
        name = _required_text(category.get("name"), "manual-service category name")
        result["categories"].append(
            _category_record("manual_services", category_id, name, category)
        )
        detail = offers_by_id.get(category_id, {})
        if not isinstance(detail, dict) or "read_error" in detail:
            continue
        _assert_detail_identity(detail, "manual_services", "manual_service_id", category_id)
        items = detail.get("items", [])
        if not isinstance(items, list):
            raise CatalogImportError("manual-service offer list was malformed")
        detail_category = detail.get("category")
        if isinstance(detail_category, dict) and detail_category.get("id") is not None:
            if _required_id(detail_category["id"], "manual-service identifier") != category_id:
                raise CatalogImportError("manual-service detail category did not match")
        seen_offers: set[str] = set()
        for offer in items:
            row = _offer_record(offer, "manual_services", "id", None)
            if row["provider_offer_id"] in seen_offers:
                raise CatalogImportError("manual-service offers contained duplicate identifiers")
            seen_offers.add(row["provider_offer_id"])
            row.update(
                family="manual_services",
                provider_category_id=category_id,
                review_state="blocked",
            )
            result["offers"].append(row)


def _extract_steam_gifts(family_data: Any, result: dict[str, Any]) -> None:
    if not isinstance(family_data, dict):
        raise CatalogImportError("Steam gift snapshot family was malformed")
    games = family_data.get("games", [])
    if not isinstance(games, list):
        raise CatalogImportError("Steam gift title list was malformed")
    seen: set[int] = set()
    for game in games:
        if not isinstance(game, dict):
            raise CatalogImportError("Steam gift title was malformed")
        app_id = _required_positive_int(game.get("appid"), "Steam app id")
        if app_id in seen:
            raise CatalogImportError("Steam gift titles contained duplicate app ids")
        seen.add(app_id)
        result["steam_titles"].append(
            {"app_id": app_id, "display_name": _required_text(game.get("name"), "Steam title name")}
        )
    metadata = family_data.get("catalog_meta")
    if isinstance(metadata, dict):
        result["metadata"].append(("steam_gifts", "catalog", metadata))


def _extract_validation(family_data: Any, result: dict[str, Any]) -> None:
    if not isinstance(family_data, dict):
        raise CatalogImportError("top-up validation snapshot family was malformed")
    targets = family_data.get("items", [])
    if not isinstance(targets, list):
        raise CatalogImportError("top-up validation target list was malformed")
    seen: set[str] = set()
    for target in targets:
        if not isinstance(target, dict):
            raise CatalogImportError("top-up validation target was malformed")
        category_id = _required_id(target.get("category_id"), "validation category identifier")
        if category_id in seen:
            raise CatalogImportError("validation targets contained duplicate identifiers")
        seen.add(category_id)
        schema = target.get("fields")
        if not isinstance(schema, list):
            raise CatalogImportError("top-up validation schema was malformed")
        result["validation_targets"].append(
            {
                "provider_category_id": category_id,
                "display_name": _required_text(target.get("name"), "validation target name"),
                "input_schema_json": _json(schema),
            }
        )


def _all_reference_products() -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = list_products(limit=MAX_PAGE_SIZE, offset=offset)
        products.extend(page)
        if len(page) < MAX_PAGE_SIZE:
            return products
        offset += len(page)


def _build_candidate_records(
    records: dict[str, Any], references: list[dict[str, Any]]
) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    for category in records["categories"]:
        sources.append(
            {
                "family": category["family"],
                "source_kind": "category",
                "source_id": category["provider_category_id"],
                "source_name": category["display_name"],
            }
        )
    for title in records["steam_titles"]:
        sources.append(
            {
                "family": "steam_gifts",
                "source_kind": "steam_title",
                "source_id": str(title["app_id"]),
                "source_name": title["display_name"],
            }
        )
    for target in records["validation_targets"]:
        sources.append(
            {
                "family": "topup_validation",
                "source_kind": "validation_target",
                "source_id": target["provider_category_id"],
                "source_name": target["display_name"],
            }
        )

    candidates: list[dict[str, str]] = []
    for reference in references:
        slug = reference.get("slug")
        name = reference.get("name")
        if not isinstance(slug, str) or not slug or not isinstance(name, str) or not name:
            raise CatalogImportError("reference catalog product was malformed")
        category_ids = reference.get("category_ids", [])
        if not isinstance(category_ids, (list, tuple)) or not all(
            isinstance(value, str) for value in category_ids
        ):
            raise CatalogImportError("reference catalog category metadata was malformed")
        for source in sources:
            method = candidate_match_method(name, source["source_name"])
            if method is None:
                continue
            candidates.append(
                {
                    "reference_product_slug": slug,
                    "reference_product_name": name,
                    "reference_category_ids_json": _json(list(category_ids)),
                    **source,
                    "match_method": method,
                }
            )
    return candidates


def _category_record(family: str, category_id: str, name: str, raw: dict[str, Any]) -> dict[str, Any]:
    region = raw.get("region") if family == "gamekeys" else None
    platform = raw.get("platform") if family == "gamekeys" else None
    restriction = raw.get("region_restriction") if family == "gamekeys" else None
    if region is not None and not isinstance(region, str):
        raise CatalogImportError("game-key region was malformed")
    if platform is not None and not isinstance(platform, str):
        raise CatalogImportError("game-key platform was malformed")
    if restriction is not None and not isinstance(restriction, bool):
        raise CatalogImportError("game-key restriction state was malformed")
    if family == "manual_services":
        kind = _required_text(raw.get("kind"), "manual-service kind")
        chat = raw.get("chat")
        if not isinstance(chat, bool):
            raise CatalogImportError("manual-service chat flag was malformed")
        metadata = {"kind": kind, "chat": chat, "info": raw.get("info", "")}
        if not isinstance(metadata["info"], str):
            raise CatalogImportError("manual-service description was malformed")
    elif family == "gamekeys":
        metadata = {"region": region, "platform": platform, "region_restriction": restriction}
    else:
        note = raw.get("note", "")
        if not isinstance(note, str):
            raise CatalogImportError("catalog category note was malformed")
        metadata = {"note": note}
    return {
        "family": family,
        "provider_category_id": category_id,
        "display_name": name,
        "region": region,
        "platform": platform,
        "region_restriction": None if restriction is None else int(restriction),
        "source_metadata_json": _json(metadata),
    }


def _offer_record(
    offer: Any, family: str, id_key: str, schema_json: str | None
) -> dict[str, Any]:
    if not isinstance(offer, dict):
        raise CatalogImportError(f"{family} offer was malformed")
    offer_id = _required_id(offer.get(id_key), f"{family} offer identifier")
    name = _required_text(offer.get("name"), f"{family} offer name")
    raw_cost = offer.get("price_usd")
    if isinstance(raw_cost, bool) or not isinstance(raw_cost, (str, int, float)):
        raise CatalogImportError(f"{family} offer price was malformed")
    try:
        cost = Decimal(str(raw_cost))
    except (InvalidOperation, ValueError):
        raise CatalogImportError(f"{family} offer price was malformed") from None
    if not cost.is_finite() or cost < 0:
        raise CatalogImportError(f"{family} offer price was malformed")
    stock = _optional_nonnegative_int(offer.get("stock"), f"{family} stock")
    minimum = _optional_nonnegative_int(offer.get("min_order_quantity"), f"{family} minimum quantity")
    maximum = _optional_nonnegative_int(offer.get("max_order_quantity"), f"{family} maximum quantity")
    sold_out_zero_limit = stock == 0 and maximum == 0
    if minimum == 0 or (maximum == 0 and not sold_out_zero_limit) or (
        minimum is not None and maximum is not None and minimum > maximum and not sold_out_zero_limit
    ):
        raise CatalogImportError(f"{family} quantity limits were malformed")
    delivery = _optional_nonnegative_int(offer.get("delivery_minutes"), f"{family} delivery time")
    if family == "manual_services":
        schema = None
        for key in ("fields", "input_fields", "replenishment_fields"):
            if key in offer:
                if not isinstance(offer[key], (dict, list)):
                    raise CatalogImportError("manual-service input schema was malformed")
                schema = _json(offer[key])
                break
        schema_json = schema
    return {
        "provider_offer_id": offer_id,
        "display_name": name,
        "supplier_cost_usd": format(cost, "f"),
        "stock": stock,
        "minimum_quantity": minimum,
        "maximum_quantity": maximum,
        "delivery_minutes": delivery,
        "input_schema_json": schema_json,
    }


def _assert_detail_identity(detail: dict[str, Any], family: str, key: str, expected: str) -> None:
    actual = detail.get(key)
    if actual is None:
        # The inventory collector stored top-up/gift-card details keyed by the
        # requested category even when the response omitted the echoed ID.
        if family in {"topups", "giftcards"}:
            return
        raise CatalogImportError(f"{family} detail omitted its category identifier")
    if _required_id(actual, f"{family} detail identifier") != expected:
        raise CatalogImportError(f"{family} detail did not match its category")


def _insert_records(connection: sqlite3.Connection, import_id: str, records: dict[str, Any]) -> None:
    for row in records["categories"]:
        connection.execute(
            """INSERT INTO game_catalog_categories
               (import_id, family, provider_category_id, display_name, region,
                platform, region_restriction, source_metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                import_id,
                row["family"],
                row["provider_category_id"],
                row["display_name"],
                row["region"],
                row["platform"],
                row["region_restriction"],
                row["source_metadata_json"],
            ),
        )
    for row in records["offers"]:
        connection.execute(
            """INSERT INTO game_catalog_offers
               (import_id, family, provider_category_id, provider_offer_id,
                display_name, supplier_cost_usd, stock, minimum_quantity,
                maximum_quantity, delivery_minutes, input_schema_json, review_state)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'blocked')""",
            (
                import_id,
                row["family"],
                row["provider_category_id"],
                row["provider_offer_id"],
                row["display_name"],
                row["supplier_cost_usd"],
                row["stock"],
                row["minimum_quantity"],
                row["maximum_quantity"],
                row["delivery_minutes"],
                row["input_schema_json"],
            ),
        )
    for row in records["steam_titles"]:
        connection.execute(
            "INSERT INTO game_catalog_steam_titles(import_id, app_id, display_name) VALUES (?, ?, ?)",
            (import_id, row["app_id"], row["display_name"]),
        )
    for row in records["validation_targets"]:
        connection.execute(
            """INSERT INTO game_catalog_validation_targets
               (import_id, provider_category_id, display_name, input_schema_json)
               VALUES (?, ?, ?, ?)""",
            (
                import_id,
                row["provider_category_id"],
                row["display_name"],
                row["input_schema_json"],
            ),
        )
    _insert_metadata(connection, import_id, records["metadata"])
    _insert_candidates(connection, import_id, records["candidates"])


def _insert_metadata(
    connection: sqlite3.Connection,
    import_id: str,
    metadata: list[tuple[str, str, Any]],
) -> None:
    connection.executemany(
        """INSERT OR IGNORE INTO game_catalog_metadata
           (import_id, family, metadata_key, payload_json) VALUES (?, ?, ?, ?)""",
        [(import_id, family, key, _json(value)) for family, key, value in metadata],
    )


def _insert_candidates(
    connection: sqlite3.Connection, import_id: str, candidates: list[dict[str, str]]
) -> None:
    connection.executemany(
        """INSERT OR IGNORE INTO game_catalog_reference_candidates
           (import_id, reference_product_slug, reference_product_name,
            reference_category_ids_json, family, source_kind, source_id,
            source_name, match_method, review_state)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'blocked')""",
        [
            (
                import_id,
                row["reference_product_slug"],
                row["reference_product_name"],
                row["reference_category_ids_json"],
                row["family"],
                row["source_kind"],
                row["source_id"],
                row["source_name"],
                row["match_method"],
            )
            for row in candidates
        ],
    )


def _summary_from_counts(
    import_id: str, status: str, counts: dict[str, Any], *, already_imported: bool
) -> CatalogImportSummary:
    categories = counts.get("categories", {})
    offers = counts.get("offers", {})
    candidate_counts = counts.get("candidate_counts", {})
    matched_reference_counts = counts.get("matched_reference_counts", {})
    if (
        not isinstance(categories, dict)
        or not isinstance(offers, dict)
        or not isinstance(candidate_counts, dict)
        or not isinstance(matched_reference_counts, dict)
    ):
        raise CatalogImportError("catalog import summary was malformed")
    return CatalogImportSummary(
        import_id=import_id,
        status=status,
        category_counts={str(key): int(value) for key, value in categories.items()},
        offer_counts={str(key): int(value) for key, value in offers.items()},
        steam_title_count=int(counts.get("steam_titles", 0)),
        validation_target_count=int(counts.get("validation_targets", 0)),
        full_catalog_item_count=int(counts.get("full_catalog_items", 0)),
        candidate_counts={str(key): int(value) for key, value in candidate_counts.items()},
        matched_reference_counts={
            str(key): int(value) for key, value in matched_reference_counts.items()
        },
        already_imported=already_imported,
    )


def _required_id(value: Any, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CatalogImportError(f"{label} was malformed")
    result = str(value).strip()
    if not result or len(result) > 200:
        raise CatalogImportError(f"{label} was malformed")
    return result


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise CatalogImportError(f"{label} was malformed")
    return value


def _optional_nonnegative_int(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CatalogImportError(f"{label} was malformed")
    return value


def _required_positive_int(value: Any, label: str) -> int:
    result = _optional_nonnegative_int(value, label)
    if result is None or result == 0:
        raise CatalogImportError(f"{label} was malformed")
    return result


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise CatalogImportError("supplier metadata was not JSON-compatible") from None
