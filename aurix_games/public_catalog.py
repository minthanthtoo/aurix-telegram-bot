"""Read and validate the sanitized priced-catalog artifact."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import stat
from typing import Any
import unicodedata

from .commerce_settings import env_commerce_settings

from .catalog import CATEGORIES, MAX_OFFSET, MAX_PAGE_SIZE, MAX_QUERY_LENGTH
from .eligibility import SUPPORTED_FULFILLMENT_KINDS
from .catalog_policy import CatalogPolicyStore, allows_offer
from .pricing_store import PricingPolicyStore, catalog_config_revision


class PublicCatalogError(ValueError):
    """The public projection is missing, malformed, or contains private data."""


UTC = timezone.utc
DEFAULT_PUBLIC_CATALOG_MAX_AGE = timedelta(minutes=15)
_DISCOVERY_MAX_AGE = timedelta(days=30)
_DISCOVERY_MAX_PRODUCTS = 300
_DISCOVERY_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")
_PURCHASE_SWITCHES = (
    "checkout_enabled",
    "account_checks_enabled",
    "payments_enabled",
    "supplier_orders_enabled",
)


def _commerce_state() -> dict[str, Any]:
    try:
        return env_commerce_settings().status()["settings"]
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def _purchase_enabled(offers: list[dict[str, Any]], settings: dict[str, Any]) -> bool:
    return bool(offers) and all(settings.get(flag) is True for flag in _PURCHASE_SWITCHES) and all(
        offer.get("fulfillment_kind") == "direct_topup"
        and bool(offer.get("required_input_labels"))
        for offer in offers
    )


def _product_name_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(unicodedata.normalize("NFC", value).split()).casefold()
    return unicodedata.normalize("NFC", normalized)


def _supplier_products_without_duplicates(
    products: list[dict[str, Any]], *, approved_name_keys: set[str] | None = None
) -> list[dict[str, Any]]:
    """Keep one browse-only supplier title and suppress its approved live twin."""

    seen = set(approved_name_keys or ())
    result = []
    for product in products:
        key = _product_name_key(product.get("name"))
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(product)
    return result


def build_supplier_discovery(
    categories: list[dict[str, Any]], *, generated_at: datetime | None = None
) -> dict[str, Any]:
    """Build a names-only, non-purchasable index from FazerCards top-up categories."""

    if not isinstance(categories, list) or not 1 <= len(categories) <= _DISCOVERY_MAX_PRODUCTS:
        raise ValueError("supplier category list is outside the supported range")
    observed = generated_at or datetime.now(UTC)
    if not isinstance(observed, datetime) or observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("supplier category timestamp must be timezone-aware")

    names: dict[str, str] = {}
    seen_ids: set[str] = set()
    for row in categories:
        if not isinstance(row, dict):
            raise ValueError("supplier category is malformed")
        raw_id, raw_name = row.get("category_id"), row.get("name")
        if (isinstance(raw_id, bool) or not isinstance(raw_id, (str, int))
                or not str(raw_id).strip() or not isinstance(raw_name, str)):
            raise ValueError("supplier category is missing its identifier or name")
        category_id = str(raw_id).strip()
        name = raw_name.strip()
        if (category_id in seen_ids or not name or len(name) > 120
                or any(ord(char) < 32 or ord(char) == 127 for char in name)):
            raise ValueError("supplier category identifier or name is invalid")
        seen_ids.add(category_id)
        names.setdefault(_product_name_key(name), unicodedata.normalize("NFC", name))
    if not names:
        raise ValueError("supplier category list contains no displayable games")

    products: list[dict[str, Any]] = []
    slugs: set[str] = set()
    for index, name in enumerate(sorted(names.values(), key=str.casefold), start=1):
        base = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:70].strip("-")
        base = base or f"supplier-game-{index}"
        slug = base
        suffix = 2
        while slug in slugs:
            slug = f"{base[:70]}-{suffix}"
            suffix += 1
        if not _DISCOVERY_SLUG.fullmatch(slug):
            raise ValueError("supplier category name cannot be represented safely")
        slugs.add(slug)
        products.append({
            "slug": slug,
            "name": name,
            "category": "Supplier top-up",
            "category_ids": ["supplier-review"],
            "supplier_listed": True,
        })

    return {
        "schema_version": 1,
        "status": "supplier_listed",
        "generated_at": observed.astimezone(UTC).isoformat(),
        "checkout_enabled": False,
        "total_count": len(products),
        "products": products,
    }


def load_supplier_discovery(
    path: str | Path | None,
    *,
    now: datetime | None = None,
    max_age: timedelta = _DISCOVERY_MAX_AGE,
) -> dict[str, Any] | None:
    """Read a bounded private discovery artifact and return only its public allowlist."""

    if path is None or not isinstance(max_age, timedelta) or max_age < timedelta(0):
        return None
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        return None
    try:
        info = source.stat()
        if stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 1_000_000:
            return None
        payload = json.loads(source.read_text(encoding="utf-8"))
        current = now or datetime.now(UTC)
        if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
            return None
        expected_fields = {"schema_version", "status", "generated_at", "checkout_enabled", "total_count", "products"}
        if (not isinstance(payload, dict) or set(payload) != expected_fields
                or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
                or payload.get("status") != "supplier_listed" or payload.get("checkout_enabled") is not False):
            return None
        generated = _aware_timestamp(payload.get("generated_at"), "generated_at")
        current = current.astimezone(UTC)
        raw_products = payload.get("products")
        if (generated > current + timedelta(minutes=5) or current - generated > max_age
                or not isinstance(raw_products, list) or not 1 <= len(raw_products) <= _DISCOVERY_MAX_PRODUCTS
                or type(payload.get("total_count")) is not int or payload["total_count"] != len(raw_products)):
            return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, PublicCatalogError, TypeError, ValueError):
        return None

    products: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    for row in raw_products:
        if (not isinstance(row, dict)
                or set(row) != {"slug", "name", "category", "category_ids", "supplier_listed"}
                or not isinstance(row.get("slug"), str) or not _DISCOVERY_SLUG.fullmatch(row["slug"])
                or not isinstance(row.get("name"), str) or not row["name"].strip()
                or len(row["name"]) > 120
                or any(ord(char) < 32 or ord(char) == 127 for char in row["name"])
                or row.get("category") != "Supplier top-up"
                or row.get("category_ids") != ["supplier-review"]
                or row.get("supplier_listed") is not True
                or row["slug"] in seen_slugs):
            return None
        seen_slugs.add(row["slug"])
        products.append({
            "slug": row["slug"], "name": row["name"].strip(),
            "category": "Supplier top-up", "category_ids": ["supplier-review"],
            "supplier_listed": True, "status": "supplier_review", "live": False,
            "purchase_enabled": False, "offers": [], "last_updated_at": generated.isoformat(),
        })
    return {"generated_at": generated.isoformat(), "products": products}


class PublicCatalogStore:
    """Reload-on-request store for a generated public catalog artifact."""

    def __init__(
        self,
        path: str | Path | None,
        *,
        max_age: timedelta = DEFAULT_PUBLIC_CATALOG_MAX_AGE,
        now: datetime | None = None,
        catalog_policy_store: CatalogPolicyStore | None = None,
        pricing_policy_store: PricingPolicyStore | None = None,
        discovery_path: str | Path | None = None,
        topup_preview: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(max_age, timedelta) or max_age < timedelta(0):
            raise ValueError("public catalog max_age must be non-negative")
        if now is not None and (
            not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
        ):
            raise ValueError("public catalog now must be timezone-aware")
        self.path = Path(path) if path else None
        self.max_age = max_age
        self._now = now
        self.catalog_policy_store = catalog_policy_store
        self.pricing_policy_store = pricing_policy_store
        self.discovery_path = (
            Path(discovery_path) if discovery_path else
            (self.path.with_name("supplier-discovery.json") if self.path else None)
        )
        self.topup_preview = topup_preview if isinstance(topup_preview, dict) else None

    def controls_active(self) -> bool:
        if self.catalog_policy_store is None:
            return False
        try:
            return self.catalog_policy_store.status()['configured']
        except (ValueError, OSError, json.JSONDecodeError):
            return True

    def supplier_discovery_available(self, *, now: datetime | None = None) -> bool:
        return load_supplier_discovery(
            self.discovery_path, now=now or self._now or datetime.now(UTC)
        ) is not None

    def supplier_slug_exists(self, slug: str, *, now: datetime | None = None) -> bool:
        """Identify supplier slugs even when de-duplication suppresses their detail."""
        if not isinstance(slug, str) or not _DISCOVERY_SLUG.fullmatch(slug):
            return False
        discovery = load_supplier_discovery(
            self.discovery_path, now=now or self._now or datetime.now(UTC)
        )
        return discovery is not None and any(item["slug"] == slug for item in discovery["products"])

    def discovery(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        """Return supplier-listed names and previously approved names, never offers."""
        current = now or self._now or datetime.now(UTC)
        supplier_catalog = load_supplier_discovery(self.discovery_path, now=current)
        supplier_catalog = self._with_supplier_preview_metadata(supplier_catalog)
        supplier_products = list((supplier_catalog or {}).get("products", []))
        try:
            _, approved_offers = self._load_catalog(now=current)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, PublicCatalogError, TypeError, ValueError):
            approved_offers = None
        approved: dict[str, str] = {}
        for offer in approved_offers or []:
            approved[offer["product_slug"]] = offer["product_name"]
        approved_name_keys = {_product_name_key(name) for name in approved.values()}
        supplier_products = _supplier_products_without_duplicates(
            supplier_products, approved_name_keys=approved_name_keys
        )
        products: dict[str, dict[str, Any]] = {item["slug"]: item for item in supplier_products}
        for slug, name in approved.items():
            products[slug] = {
                "slug": slug, "name": name, "category": "Game top-up",
                "category_ids": ["live-offers"],
            }

        if not products:
            return None
        categories: list[dict[str, str]] = []
        if supplier_products:
            categories.append({"id": "supplier-review", "name": "In review"})
        if any("live-offers" in item.get("category_ids", []) for item in products.values()):
            categories.insert(0, {"id": "live-offers", "name": "Previously listed"})
        ordered = sorted(products.values(), key=lambda item: (item["name"].casefold(), item["slug"]))
        for product in ordered:
            preview = self._topup_preview_for_product(product)
            if preview:
                product["preview_package_count"] = sum(
                    len(group.get("offers", [])) for group in preview["regions"]
                )
                product["preview_region_count"] = len(preview["regions"])
        if len(ordered) > _DISCOVERY_MAX_PRODUCTS:
            ordered = ordered[:_DISCOVERY_MAX_PRODUCTS]
        return {
            "schema_version": 1, "status": "cached", "checkout_enabled": False,
            "categories": categories, "products": ordered, "total_count": len(ordered),
        }

    def _supplier_only_response(
        self, discovery: dict[str, Any] | None, *, query: str, category: str | None,
        limit: int, offset: int,
    ) -> dict[str, Any] | None:
        if discovery is None:
            return None
        products = _supplier_products_without_duplicates(discovery["products"])
        if category not in (None, "supplier-review"):
            products = []
        if query.strip():
            needle = query.strip().casefold()
            products = [product for product in products if needle in product["name"].casefold()]
        total = len(products)
        page = products[offset:offset + limit]
        next_offset = offset + len(page)
        categories = ([{"id": "supplier-review", "name": "In review"}]
                      if products else [])
        return {
            "schema_version": 1, "status": "supplier_listed", "checkout_enabled": False,
            "source": "supplier_category_index", "last_updated_at": discovery["generated_at"],
            "generated_at": discovery["generated_at"], "catalog_controls_active": self.controls_active(),
            "categories": categories, "products": page,
            "result_count": len(page), "total_count": total,
            "next_offset": next_offset if next_offset < total else None,
        }

    def response(self, *, query: str = "", category: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any] | None:
        if not 1 <= limit <= MAX_PAGE_SIZE or not 0 <= offset <= MAX_OFFSET or len(query) > MAX_QUERY_LENGTH:
            raise ValueError('catalog query or page is out of range')
        now = self._now or datetime.now(UTC)
        supplier_catalog = self._with_supplier_preview_metadata(
            load_supplier_discovery(self.discovery_path, now=now)
        )
        try:
            _, offers = self._load_catalog(now=now)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, PublicCatalogError):
            return self._supplier_only_response(supplier_catalog, query=query, category=category, limit=limit, offset=offset)
        if offers is None:
            return self._supplier_only_response(supplier_catalog, query=query, category=category, limit=limit, offset=offset)
        normalized_query = query.strip().casefold()
        settings = _commerce_state()
        controlled = self.controls_active()
        categories = _categories()[-1:] if controlled else _categories()
        review_products = list((supplier_catalog or {}).get("products", []))
        if category is not None and category not in {item["id"] for item in CATEGORIES} and category not in {"live-offers", "supplier-review"}:
            return {"schema_version": 1, "status": "live", "checkout_enabled": False, "catalog_controls_active": controlled, "categories": categories, "products": [], "result_count": 0, "total_count": 0, "next_offset": None}
        grouped: dict[str, dict[str, Any]] = {}
        for offer in offers:
            product_slug = offer["product_slug"]
            product = grouped.setdefault(
                product_slug,
                {
                    "slug": product_slug,
                    "name": offer["product_name"],
                    "category_ids": ["live-offers"],
                    "category": "Live Offers",
                    "reference_badges": [],
                    "status": "available",
                    "purchase_enabled": False,
                    "live": True,
                    "offers": [],
                },
            )
            product["offers"].append(
                {
                    "offer_id": offer["offer_id"],
                    "name": offer["denomination_label"],
                    "fulfillment_kind": offer["fulfillment_kind"],
                    "price_minor": offer["retail_amount_minor"],
                    "currency": offer["currency"],
                    "promo_discount_minor": offer.get("promo_discount_minor", 0),
                    "region": deepcopy(offer.get("region")),
                    "required_input_labels": list(offer.get("required_input_labels", [])),
                }
            )
        approved_products = list(grouped.values())
        for product in approved_products:
            product["purchase_enabled"] = _purchase_enabled(product["offers"], settings)
        if not approved_products:
            categories = [item for item in categories if item["id"] != "live-offers"]
        approved_slugs = set(grouped)
        approved_name_keys = {_product_name_key(product.get("name")) for product in approved_products}
        review_products = _supplier_products_without_duplicates(
            [product for product in review_products if product["slug"] not in approved_slugs],
            approved_name_keys=approved_name_keys,
        )
        if review_products:
            categories = [*categories, {"id": "supplier-review", "name": "In review"}]
        products = approved_products + review_products
        if normalized_query:
            products = [
                product
                for product in products
                if normalized_query in product["name"].casefold()
                or any(normalized_query in offer["name"].casefold() for offer in product["offers"])
            ]
        if category == "live-offers":
            products = [product for product in products if "live-offers" in product.get("category_ids", [])]
        elif category == "supplier-review":
            products = [product for product in products if product.get("supplier_listed") is True]
        elif category:
            products = []
        total = len(products)
        page = products[offset : offset + limit]
        next_offset = offset + len(page)
        return {
            "schema_version": 1,
            "status": "live",
            "checkout_enabled": settings.get("checkout_enabled") is True,
            "source": "approved_priced_projection",
            "catalog_controls_active": controlled,
            "categories": categories,
            "products": page,
            "result_count": len(page),
            "total_count": total,
            "next_offset": next_offset if next_offset < total else None,
        }

    def offer(self, offer_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        """Return one validated customer-safe offer for an internal resolver.

        This method exposes only the same sanitized fields used by the public
        catalog response. It never returns supplier IDs, costs, or raw
        provider data, and stale/malformed projections fail closed.
        """

        if not isinstance(offer_id, str) or not offer_id.strip():
            return None
        try:
            _, offers = self._load_catalog(now=now or self._now or datetime.now(UTC))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, PublicCatalogError):
            return None
        if offers is None:
            return None
        for offer in offers:
            if offer["offer_id"] == offer_id.strip():
                return deepcopy(offer)
        return None

    def product_detail(self, slug: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        """Return a live product/detail contract when its approved offers exist."""

        if not isinstance(slug, str) or not slug.strip():
            return None
        current = now or self._now or datetime.now(UTC)
        try:
            _, offers = self._load_catalog(now=current)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, PublicCatalogError):
            return self._supplier_product_detail(slug.strip(), now=current)
        if offers is None:
            return self._supplier_product_detail(slug.strip(), now=current)
        matching = [offer for offer in offers if offer["product_slug"] == slug.strip()]
        if not matching:
            approved_names = {_product_name_key(offer.get("product_name")) for offer in offers}
            return self._supplier_product_detail(
                slug.strip(), now=current, excluded_name_keys=approved_names
            )
        settings = _commerce_state()
        purchase_enabled = _purchase_enabled(matching, settings)
        product = _live_product(matching, purchase_enabled=purchase_enabled)
        return {
            "schema_version": 1,
            "status": "live",
            "checkout_enabled": settings.get("checkout_enabled") is True,
            "product": product,
            "regions": _live_regions(matching),
            "offers": deepcopy(product["offers"]),
            "purchase_enabled": purchase_enabled,
            "notice": "Check the game account and confirm the exact package before payment." if purchase_enabled
            else "Browsing only. Checkout is unavailable; do not transfer until an order shows payment instructions.",
        }

    def _supplier_product_detail(
        self, slug: str, *, now: datetime, excluded_name_keys: set[str] | None = None
    ) -> dict[str, Any] | None:
        discovery = self._with_supplier_preview_metadata(
            load_supplier_discovery(self.discovery_path, now=now)
        )
        if discovery is None:
            return None
        products = _supplier_products_without_duplicates(
            discovery["products"], approved_name_keys=excluded_name_keys
        )
        product = next((item for item in products if item["slug"] == slug), None)
        if product is None:
            return None
        if _product_name_key(product.get("name")) in (excluded_name_keys or set()):
            return None
        preview_groups = self._topup_preview_for_product(product)
        if preview_groups:
            preview_groups = preview_groups["regions"]
        return {
            "schema_version": 1, "status": "supplier_review", "checkout_enabled": False,
            "source": "supplier_category_index", "last_updated_at": discovery["generated_at"],
            "generated_at": discovery["generated_at"], "product": product, "regions": [],
            "offers": [], "purchase_enabled": False,
            **({"supplier_preview": {"regions": preview_groups}}
                if preview_groups else {}),
            "notice": (
                "This supplier-listed game is still under product, region, pricing, and account-check review. "
                "Package names are from a read-only supplier snapshot. Quantity is unreported. "
                "No order or payment has started."
            ),
        }

    def _topup_preview_for_product(self, product: dict[str, Any]) -> dict[str, Any] | None:
        if self.topup_preview is None or product.get("supplier_listed") is not True:
            return None
        target = product.get("name")
        if not isinstance(target, str):
            return None
        for candidate in self.topup_preview.values():
            if not isinstance(candidate, dict) or not isinstance(candidate.get("regions"), list):
                continue
            matching = [
                group for group in candidate["regions"]
                if isinstance(group, dict) and isinstance(group.get("name"), str)
                and _product_name_key(group["name"]) == _product_name_key(target)
            ]
            if matching:
                return {"regions": matching}
        return None

    def _with_supplier_preview_metadata(
        self, discovery: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if discovery is None:
            return None
        for product in discovery["products"]:
            preview = self._topup_preview_for_product(product)
            if preview:
                product["preview_package_count"] = sum(
                    len(group.get("offers", [])) for group in preview["regions"]
                )
                product["preview_region_count"] = len(preview["regions"])
        return discovery

    def product_offers(
        self,
        slug: str,
        *,
        region: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Return current approved offers for one product and optional region."""

        detail = self.product_detail(slug, now=now)
        if detail is None:
            return None
        if region is not None:
            normalized = region.strip().casefold() if isinstance(region, str) else ""
            if not normalized:
                return detail
            detail["offers"] = [
                offer
                for offer in detail["offers"]
                if _region_matches(offer.get("region"), normalized)
            ]
            detail["regions"] = _live_regions(detail["offers"])
            detail["purchase_enabled"] = detail["purchase_enabled"] and bool(detail["offers"])
            detail["product"]["purchase_enabled"] = detail["purchase_enabled"]
            detail["product"]["offers"] = deepcopy(detail["offers"])
            if not detail["purchase_enabled"]:
                detail["notice"] = "No purchasable packages are available for this region."
        detail["requested_region"] = region.strip() if isinstance(region, str) and region.strip() else None
        return detail

    def _load_catalog(
        self, *, now: datetime
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None]:
        controls = None
        if self.catalog_policy_store is not None:
            try:
                controls = self.catalog_policy_store.status()
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                raise PublicCatalogError('catalog controls are unavailable') from exc
        if self.path is None or not self.path.exists() or self.path.is_symlink():
            return None, [] if controls and controls['configured'] else None
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        offers = _safe_offers(payload, now=now, max_age=self.max_age)
        if self.pricing_policy_store is not None:
            try:
                config, _, _ = self.pricing_policy_store.approved_snapshot(
                    now=now,
                    allow_expired=True,
                )
            except (ValueError, OSError) as exc:
                raise PublicCatalogError('catalog approval is unavailable') from exc
            if payload.get('config_revision') != catalog_config_revision(config):
                raise PublicCatalogError('catalog approval changed')
        if controls is not None and controls['configured']:
            offers = [offer for offer in offers if allows_offer(controls['policy'], offer)]
        return payload, offers

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Return safe operator metadata without exposing catalog contents."""

        if self.path is None:
            return {"status": "unavailable", "reason": "not_configured"}
        if self.path.is_symlink():
            return {"status": "unavailable", "reason": "symlink"}
        if not self.path.exists():
            return {"status": "unavailable", "reason": "missing"}
        try:
            payload, offers = self._load_catalog(now=now or self._now or datetime.now(UTC))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"status": "unavailable", "reason": "invalid"}
        except PublicCatalogError as exc:
            reason = "stale" if str(exc) == "public catalog is stale" else "invalid"
            return {"status": "unavailable", "reason": reason}
        if payload is None or offers is None:
            return {'status': 'unavailable', 'reason': 'missing'}
        return {
            "status": "fresh",
            "generated_at": payload["generated_at"],
            "source_observed_at": payload["source_observed_at"],
            "valid_until": payload["valid_until"],
            "offer_count": len(offers),
            "catalog_controls_active": self.controls_active(),
            "max_age_seconds": int(self.max_age.total_seconds()),
        }


def _categories() -> list[dict[str, str]]:
    return [*deepcopy(list(CATEGORIES)), {"id": "live-offers", "name": "Live Offers"}]


def _live_product(offers: list[dict[str, Any]], *, purchase_enabled: bool) -> dict[str, Any]:
    first = offers[0]
    return {
        "slug": first["product_slug"],
        "name": first["product_name"],
        "category_ids": ["live-offers"],
        "category": "Live Offers",
        "reference_badges": [],
        "status": "available",
        "purchase_enabled": purchase_enabled,
        "live": True,
        "offers": [
            {
                "offer_id": offer["offer_id"],
                "name": offer["denomination_label"],
                "fulfillment_kind": offer["fulfillment_kind"],
                "price_minor": offer["retail_amount_minor"],
                "currency": offer["currency"],
                "promo_discount_minor": offer.get("promo_discount_minor", 0),
                "region": deepcopy(offer.get("region")),
                "required_input_labels": list(offer.get("required_input_labels", [])),
            }
            for offer in offers
        ],
    }


def _live_regions(offers: list[dict[str, Any]]) -> list[dict[str, str]]:
    regions: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for offer in offers:
        region = offer.get("region")
        if not isinstance(region, dict):
            continue
        key = (region["type"], region["label"])
        if key in seen:
            continue
        seen.add(key)
        regions.append({"type": key[0], "label": key[1]})
    return regions


def _region_matches(region: object, query: str) -> bool:
    if region is None:
        return query == "global"
    if not isinstance(region, dict):
        return False
    return query in {str(region.get("type", "")).casefold(), str(region.get("label", "")).casefold()}


def _safe_offers(
    payload: Any,
    *,
    now: datetime,
    max_age: timedelta,
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise PublicCatalogError("public catalog schema is invalid")
    if payload.get("status") != "approved_projection" or payload.get("checkout_enabled") is not None:
        # The generator deliberately omits checkout_enabled; reject a file
        # that attempts to override the application-side checkout boundary.
        raise PublicCatalogError("public catalog status is invalid")
    current = _aware_timestamp(payload.get("generated_at"), "generated_at")
    observed = _aware_timestamp(payload.get("source_observed_at"), "source_observed_at")
    expiry = _aware_timestamp(payload.get("valid_until"), "valid_until")
    current_now = now.astimezone(UTC)
    if current > current_now or observed > current or expiry <= current:
        raise PublicCatalogError("public catalog timestamps are invalid")
    if current_now >= expiry or current_now - current >= max_age or current_now - observed >= max_age:
        raise PublicCatalogError("public catalog is stale")
    offers = payload.get("offers")
    if not isinstance(offers, list):
        raise PublicCatalogError("public catalog offers are invalid")
    result: list[dict[str, Any]] = []
    private_markers = ("supplier", "provider", "cost", "secret", "api_key", "raw")
    for offer in offers:
        if not isinstance(offer, dict):
            raise PublicCatalogError("public catalog offer is invalid")
        if any(marker in key.casefold() for key in offer for marker in private_markers):
            raise PublicCatalogError("public catalog contains a private field")
        required = {"offer_id", "product_slug", "product_name", "fulfillment_kind", "denomination_label", "retail_amount_minor", "currency", "region", "required_input_labels"}
        allowed = required | {"promo_discount_minor"}
        if set(offer) - allowed:
            raise PublicCatalogError("public catalog offer has unsupported fields")
        if not required.issubset(offer):
            raise PublicCatalogError("public catalog offer omitted required fields")
        labels = offer.get("required_input_labels")
        if (
            not all(isinstance(offer.get(key), str) and offer[key].strip() for key in ("offer_id", "product_slug", "product_name", "fulfillment_kind", "denomination_label", "currency"))
            or offer["fulfillment_kind"] not in SUPPORTED_FULFILLMENT_KINDS
            or offer["currency"] != "MMK"
            or isinstance(offer["retail_amount_minor"], bool)
            or not isinstance(offer["retail_amount_minor"], int)
            or offer["retail_amount_minor"] < 0
            or not isinstance(labels, list)
            or len(labels) > 32
            or any(
                not isinstance(label, str)
                or not label.strip()
                or len(label) > 160
                or any(char in label for char in "\r\n")
                for label in labels
            )
            or len(set(labels)) != len(labels)
            or not _safe_region(offer["region"])
            or (
                "promo_discount_minor" in offer
                and (
                    isinstance(offer["promo_discount_minor"], bool)
                    or not isinstance(offer["promo_discount_minor"], int)
                    or offer["promo_discount_minor"] < 0
                )
            )
        ):
            raise PublicCatalogError("public catalog offer values are invalid")
        # Keep the validated source observation attached for the private
        # checkout resolver.  ``response()`` selects fields explicitly, so
        # this internal datetime can never cross the public JSON boundary.
        safe_offer = deepcopy(offer)
        safe_offer["_observed_at"] = observed
        result.append(safe_offer)
    return result


def _safe_region(value: object) -> bool:
    """Validate the small, customer-visible region descriptor."""

    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != {"type", "label"}:
        return False
    return all(
        isinstance(item, str)
        and bool(item.strip())
        and len(item) <= 160
        and not any(char in item for char in "\r\n")
        for item in (value["type"], value["label"])
    )


def _aware_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PublicCatalogError(f"public catalog omitted {label}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PublicCatalogError(f"public catalog {label} is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PublicCatalogError(f"public catalog {label} is not timezone-aware")
    return parsed.astimezone(UTC)


__all__ = [
    "DEFAULT_PUBLIC_CATALOG_MAX_AGE",
    "PublicCatalogError",
    "PublicCatalogStore",
]
