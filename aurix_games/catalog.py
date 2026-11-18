"""Safe, read-only preview catalog for the AuriX Game Store.

Only public product labels are seeded here. Supplier SKUs, wholesale prices,
recipient schemas, historical package samples, and current availability remain
private and intentionally do not flow into the storefront preview.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .commerce_settings import commerce_enabled


CATALOG_SCHEMA_VERSION = 1
NAVIGATION_SCHEMA_VERSION = 1
MAX_PAGE_SIZE = 50
MAX_QUERY_LENGTH = 80
MAX_OFFSET = 10_000

CATEGORIES: tuple[dict[str, str], ...] = (
    {"id": "mobile-games", "name": "Mobile Games"},
    {"id": "pc-games", "name": "PC Games"},
    {"id": "gift-cards", "name": "Gift Card"},
    {"id": "mobile-apps", "name": "Mobile App"},
    {"id": "redeem-codes", "name": "Redeem Code"},
)

# Safe navigation metadata reconstructed from the observed sample-app home and
# drawer. These entries describe UI surfaces only; they do not create account,
# support, payment, or order capabilities. ``empty_state`` entries are backed
# by observed empty states; ``not_captured`` entries deliberately disclose that
# the reference content was not available for copying.
_NAVIGATION_ITEMS: tuple[dict[str, Any], ...] = (
    {"id": "home", "label": "Home", "surface": "catalog", "status": "available"},
    {
        "id": "top-up",
        "label": "Top-up",
        "surface": "wallet_preview",
        "status": "reference_only",
        "notice": "The observed first wallet step is preview-only; no payment or balance change is enabled.",
    },
    {
        "id": "order-history",
        "label": "Order History",
        "surface": "empty_state",
        "status": "reference_empty",
        "tabs": ("Orders", "Pre-orders"),
        "empty_label": "No Data",
        "notice": "The reference account showed no orders in either tab.",
    },
    {
        "id": "product-order-history",
        "label": "Product Order History",
        "surface": "empty_state",
        "status": "reference_empty",
        "tabs": ("Orders", "Pre-orders"),
        "empty_label": "No Data",
        "notice": "No product-order records are available in this preview.",
    },
    {
        "id": "top-up-history",
        "label": "Top-up History",
        "surface": "empty_state",
        "status": "reference_empty",
        "empty_label": "No Data",
        "notice": "The reference account showed no top-up history.",
    },
    {
        "id": "create-gift-code",
        "label": "Create Gift Code",
        "surface": "gift_code_reference",
        "status": "unavailable",
        "notice": "Gift-code creation is disabled until the identity, product rights, and fulfillment policy are defined.",
    },
    {
        "id": "redeem-gift-code",
        "label": "Redeem Gift Code",
        "surface": "gift_code_reference",
        "status": "unavailable",
        "notice": "Gift-code redemption is disabled until an authorized code-validation contract is available.",
    },
    {
        "id": "gift-code-history",
        "label": "Gift Code History",
        "surface": "gift_code_reference",
        "status": "unavailable",
        "notice": "Gift-code history is not captured and requires an authenticated owner-scoped boundary.",
    },
    {
        "id": "events-news",
        "label": "Events/News",
        "surface": "reference_feed",
        "status": "reference_only",
        "notice": "Reference-only feed metadata captured from the sample app; images, links, and publishing are not enabled.",
        "items": (
            {
                "id": "jjk-skin-event-august-7",
                "title": "JJK Skin Event August 7",
                "date": "Jul 30, 2026",
                "summary": "Mobile Legends JJK collaboration event preview with two free-token phases.",
            },
            {
                "id": "august-collector-skin",
                "title": "August Collector Skin",
                "date": "Jul 28, 2026",
                "summary": "Reference article about August Collector Skin exchange choices.",
            },
            {
                "id": "yin-collector",
                "title": "Yin Collector",
                "date": "Jun 08, 2026",
                "summary": "Reference announcement for a future Yin Collector Skin.",
            },
        ),
    },
    {
        "id": "support-chat",
        "label": "Support Chat",
        "surface": "support_chat",
        "status": "unavailable",
        "notice": "Support messaging is disabled until ownership, retention, and response policy are defined.",
    },
    {
        "id": "mbb-hero-guide",
        "label": "MLBB Hero Guide",
        "surface": "hero_guide",
        "status": "reference_only",
        "notice": "Dated, source-attributed MLBB strategy reference. No player, match, or account data is queried.",
    },
    {
        "id": "myanmar-game-guide",
        "label": "Myanmar Game Guide",
        "surface": "strategy_guide",
        "status": "reference_only",
        "guide_slug": "pubg-mobile-myanmar",
        "notice": "Myanmar-localized strategy reference for PUBG Mobile. No player lookup, match history, or account action is enabled.",
    },
    {
        "id": "mobile-legends-region-checker",
        "label": "Mobile Legends Region Checker",
        "surface": "region_checker",
        "status": "available",
        "notice": "Find the player name and account region using your MLBB Account ID and Zone ID.",
    },
    {
        "id": "discount-events",
        "label": "Discount Events",
        "surface": "empty_state",
        "status": "reference_empty",
        "empty_label": "No Data",
        "notice": "The supplied reference capture showed an empty Discount Events surface.",
    },
    {
        "id": "leaderboard",
        "label": "Most active buyers",
        "surface": "leaderboard",
        "status": "available",
        "notice": "Opt-in · delivered top-ups · rolling 30 days. Badges mark milestones; no prize or giveaway is active.",
    },
    {
        "id": "update-profile",
        "label": "Profile",
        "surface": "customer_profile",
        "status": "account",
        "notice": "View your AuriX account ID and linked sign-in methods.",
    },
)

# Labels and category membership below are taken from the connected sample
# app's visible UI. They are not a claim that AuriX can currently sell or
# fulfill these products. Uncertain membership stays unclassified.
_MOBILE_GAMES = (
    "Mobile Legends",
    "PUBG Mobile",
    "Ragnarok Origin",
    "Honor of Kings",
    "Magic Chess: Go Go",
    "Where Winds Meet",
    "Genshin Impact",
    "Freefire",
    "Heartopia",
    "Delta Force",
    "Ragnarok Idle Adventure Plus",
    "Puzzles and Survival",
    "Solo Leveling: Arise",
    "Age of Empires Mobile",
    "Crystal of Atlan",
    "Deadly Dudes",
    "Metal Slug Awakening",
    "State of Survival",
    "Devil May Cry: Peak of Combat",
    "Bullet Echo",
    "EAFC 24",
    "Zepeto",
    "Ragnarok Crush",
    "Star Resonance",
    "Love and Deepspace",
    "Lord of the Rings: Rise to War",
    "AFK Journey",
    "Identity V",
    "Sausage Man",
    "Legends of Runeterra",
    "Blockman Go",
    "Marvel Duel",
    "Pixel Gun 3D",
    "Life Makeover Global",
    "Path to Nowhere",
    "Shining Nikki",
    "Moonlight Blade M",
    "Blood Strike",
    "Harry Potter: Magic Awakened",
    "Once Human",
    "Stumble Guys",
    "Arena Breakout",
    "Zenless Zone Zero",
    "Wuthering Waves",
    "Whiteout Survival",
    "Super Sus",
    "Sky Children of the Light",
    "Modern Strike Online",
    "Dragonheir: Silent Gods",
    "Farlight 84",
    "Snowbreak Containment Zone",
    "Marvel Mystic Mayhem",
    "Punishing Gray Raven",
    "Silver and Blood",
    "Sword of Justice",
    "Marvel Rivals",
    "Legend of the Phoenix",
    "Bleach: Soul Resonance (SEA)",
    "Undawn Global",
    "Dragon Raja",
    "Heaven Burns Red",
)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


def _build_products() -> tuple[dict[str, Any], ...]:
    products: dict[str, dict[str, Any]] = {}

    def add(name: str, category_ids: tuple[str, ...] = ()) -> None:
        slug = _slug(name)
        product = products.setdefault(
            slug,
            {"slug": slug, "name": name, "category_ids": [], "reference_badges": []},
        )
        for category_id in category_ids:
            if category_id not in product["category_ids"]:
                product["category_ids"].append(category_id)

    for name in _MOBILE_GAMES:
        add(name, ("mobile-games",))

    # Some items deliberately appear in more than one reference category.
    for name in ("Where Winds Meet", "Genshin Impact"):
        add(name, ("pc-games",))
    for name in ("Bigo Live", "Telegram Premium"):
        add(name, ("gift-cards", "mobile-apps"))
    add("Commerce", ("mobile-apps",))
    add("PUBG Mobile", ("redeem-codes",))
    # The All view showed this label, but its category membership was not
    # established in the observed session.
    add("Ecommerce")

    return tuple(
        {
            **product,
            "category_ids": tuple(product["category_ids"]),
            "reference_badges": tuple(product["reference_badges"]),
        }
        for product in products.values()
    )


_PRODUCTS = _build_products()

class CatalogInputError(ValueError):
    """A catalog request contains an unsupported filter or range."""


def list_categories() -> list[dict[str, str]]:
    """Return customer-safe category labels."""

    return deepcopy(list(CATEGORIES))


def navigation_manifest() -> dict[str, Any]:
    """Return safe, read-only navigation metadata for observed surfaces."""

    return {
        "schema_version": NAVIGATION_SCHEMA_VERSION,
        "status": "preview",
        "checkout_enabled": commerce_enabled("checkout_enabled"),
        "items": deepcopy(list(_NAVIGATION_ITEMS)),
    }


def reference_news() -> dict[str, Any]:
    """Return the captured Events/News feed as a safe read-only contract."""

    item = next(entry for entry in _NAVIGATION_ITEMS if entry["id"] == "events-news")
    entries = deepcopy(list(item.get("items", ())))
    return {
        "schema_version": NAVIGATION_SCHEMA_VERSION,
        "status": "reference_only",
        "checkout_enabled": commerce_enabled("checkout_enabled"),
        "items": entries,
        "notice": item["notice"],
    }


def reference_news_detail(news_id: str) -> dict[str, Any] | None:
    """Return one captured news card, or ``None`` for an unknown id."""

    if not isinstance(news_id, str) or not re.fullmatch(r"[a-z0-9-]{1,80}", news_id):
        raise CatalogInputError("news id is invalid")
    feed = reference_news()
    entry = next((candidate for candidate in feed["items"] if candidate.get("id") == news_id), None)
    if entry is None:
        return None
    return {
        "schema_version": NAVIGATION_SCHEMA_VERSION,
        "status": "reference_only",
        "checkout_enabled": commerce_enabled("checkout_enabled"),
        "item": entry,
        "notice": "Reference metadata only; original article images, links, and publishing are not enabled.",
    }


def reference_discount_events() -> dict[str, Any]:
    """Return the captured empty Discount Events surface read-only."""

    item = next(entry for entry in _NAVIGATION_ITEMS if entry["id"] == "discount-events")
    return {
        "schema_version": NAVIGATION_SCHEMA_VERSION,
        "status": "reference_empty",
        "checkout_enabled": commerce_enabled("checkout_enabled"),
        "items": [],
        "empty_label": item["empty_label"],
        "notice": item["notice"],
    }


def _public_product(
    source: dict[str, Any], selected_category: str | None = None
) -> dict[str, Any]:
    categories_by_id = {item["id"]: item["name"] for item in CATEGORIES}
    category_names = [categories_by_id[key] for key in source["category_ids"]]
    return {
        **source,
        "category_ids": list(source["category_ids"]),
        "reference_badges": list(source["reference_badges"]),
        "reference_detail_available": False,
        "category": (
            categories_by_id[selected_category]
            if selected_category
            else " · ".join(category_names) or "Category unconfirmed"
        ),
        "status": "supplier_review",
        "purchase_enabled": False,
    }


def list_products(
    *,
    category: str | None = None,
    query: str = "",
    limit: int = MAX_PAGE_SIZE,
    offset: int = 0,
    topup_preview: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return visible preview entries; never return supplier offers or prices."""

    normalized_category = category.strip().lower() if category else None
    known_categories = {item["id"] for item in CATEGORIES}
    if normalized_category and normalized_category not in known_categories:
        raise CatalogInputError("unknown category")
    if not isinstance(query, str) or len(query) > MAX_QUERY_LENGTH:
        raise CatalogInputError("query is too long")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise CatalogInputError("limit must be between 1 and 50")
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= MAX_OFFSET:
        raise CatalogInputError("offset must be between 0 and 10000")

    needle = query.strip().casefold()
    filtered = []
    for source in _preview_products(topup_preview):
        if normalized_category and normalized_category not in source["category_ids"]:
            continue
        if needle and needle not in source["name"].casefold():
            continue
        product = _public_product(source, normalized_category)
        if "preview_package_count" in source:
            product.update(status="supplier_preview", reference_detail_available=True)
        filtered.append(product)
    return filtered[offset : offset + limit]


def catalog_response(
    *,
    category: str | None = None,
    query: str = "",
    limit: int = MAX_PAGE_SIZE,
    offset: int = 0,
    topup_preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a response contract that is safe to expose without checkout."""

    products = list_products(category=category, query=query, limit=limit, offset=offset, topup_preview=topup_preview)
    normalized_category = category.strip().lower() if category else None
    matches = [
        item
        for item in _preview_products(topup_preview)
        if (not normalized_category or normalized_category in item["category_ids"])
        and (not query.strip() or query.strip().casefold() in item["name"].casefold())
    ]
    next_offset = offset + len(products)
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "status": "preview",
        "checkout_enabled": commerce_enabled("checkout_enabled"),
        "categories": list_categories(),
        "products": products,
        "result_count": len(products),
        "total_count": len(matches),
        "next_offset": next_offset if next_offset < len(matches) else None,
    }


def _preview_products(topup_preview):
    if not topup_preview:
        return _PRODUCTS
    products = {item["slug"]: item for item in _PRODUCTS}
    for slug, product in topup_preview.items():
        products[slug] = {key: value for key, value in product.items() if key != "regions"}
    return products.values()


def product_detail(slug: str, *, topup_preview: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Return empty live offer data plus a separately labeled UI reference, if observed."""

    source = next((item for item in _preview_products(topup_preview) if item["slug"] == slug), None)
    if source is None:
        return None
    product = _public_product(source)
    response = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "checkout_enabled": commerce_enabled("checkout_enabled"),
        "product": product,
        "regions": [],
        "offers": [],
        "notice": "Supplier authorization and regional availability are under review.",
    }
    if topup_preview and slug in topup_preview:
        response["supplier_preview"] = {"regions": topup_preview[slug]["regions"]}
        product.update(status="supplier_preview", reference_detail_available=True)
    return response


def product_offers(
    slug: str, *, region: str | None = None, topup_preview: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Return the live-offer contract for one product.

    The current release has no approved projection, so this deliberately
    returns an empty, checkout-disabled response. Reference-only package
    observations never appear on this endpoint.
    """

    source = next((item for item in _preview_products(topup_preview) if item["slug"] == slug), None)
    if source is None:
        return None
    if region is not None:
        if not isinstance(region, str) or len(region) > MAX_QUERY_LENGTH:
            raise CatalogInputError("region is too long")
        region = region.strip()
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "checkout_enabled": commerce_enabled("checkout_enabled"),
        "product": _public_product(source),
        "requested_region": region or None,
        "regions": [],
        "offers": [],
        "notice": "No approved AuriX offers are available for this product.",
    }
