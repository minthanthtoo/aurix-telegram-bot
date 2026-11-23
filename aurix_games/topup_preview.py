"""Build a read-only storefront preview from saved supplier top-up evidence."""

from datetime import datetime
import json
from pathlib import Path
import re
import stat

from .catalog import list_products
from .catalog_matching import normalize_title


def load_topup_preview(paths, *, require_private=False):
    references = {
        normalize_title(product["name"]): product
        for product in list_products() + list_products(offset=50)
    }
    for alias, title in (
        ("Puzzles & Survival", "Puzzles and Survival"), ("Age of Empire Mobile", "Age of Empires Mobile"),
        ("Life Makeover", "Life Makeover Global"), ("Bleach: Soul Resonance", "Bleach: Soul Resonance (SEA)"),
    ):
        references[normalize_title(alias)] = references[normalize_title(title)]

    categories = {}
    for path in paths:
        source = Path(path)
        if source.is_symlink() or not source.is_file():
            raise ValueError("Top-up preview snapshot is unavailable.")
        info = source.stat()
        if info.st_size > 64_000_000 or (
            require_private and stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValueError("Top-up preview snapshot is not safely stored.")
        snapshot = json.loads(source.read_text(encoding="utf-8"))
        topups = snapshot["families"]["topups"]
        for category_id, detail in topups["details_by_id"].items():
            stamp = topups.get("details_observed_at_by_id", {}).get(category_id, snapshot["captured_at"])
            observed = datetime.fromisoformat(stamp)
            if observed.utcoffset() is None:
                raise ValueError("Top-up preview requires timezone-aware observations.")
            if category_id not in categories or observed > categories[category_id][0]:
                categories[category_id] = observed, detail

    products = {}
    for category_id, (observed, detail) in categories.items():
        title = detail["name"].strip()
        if any(field.get("key") in {"password", "email", "otp", "passcode"} for field in detail.get("fields", [])):
            continue
        # Strip a trailing category qualifier only. Distinct game names remain separate.
        base = re.sub(r" \([^()]+\)$", "", title)
        reference = references.get(normalize_title(title)) or references.get(normalize_title(base))
        slug = (reference["slug"] if reference else
                re.sub(r"[^a-z0-9]+", "-", base.casefold()).strip("-") or category_id.replace("_", "-"))
        offers = []
        seen = set()
        for offer in detail["offers"]:
            sku, name = offer.get("offer_id"), offer.get("name")
            if not isinstance(sku, str) or sku in seen or not isinstance(name, str) or not name.strip():
                continue
            seen.add(sku)
            stock = offer.get("stock")
            label = "Quantity unreported"
            if type(stock) is int and stock >= 0:
                label = "Stock recorded" if stock else "No stock recorded"
            offers.append({"id": f"preview-{len(offers) + 1}", "name": name.strip(), "stock_label": label})
        if not offers:
            continue
        product = products.setdefault(slug, {
            **(reference or {"slug": slug, "name": base, "category_ids": ["mobile-games"],
                             "category": "Mobile Games", "reference_badges": []}),
            "status": "supplier_preview", "purchase_enabled": False, "reference_detail_available": True,
            "preview_package_count": 0, "regions": [],
            "artwork_slug": category_id.replace("_", "-"),
        })
        if slug == "freefire":
            product["name"] = "Free Fire"
        product["regions"].append({
            "id": f"group-{len(product['regions']) + 1}", "name": title,
            "observed_at": observed.isoformat(), "offers": offers,
        })
        product["preview_package_count"] += len(offers)
    return products
