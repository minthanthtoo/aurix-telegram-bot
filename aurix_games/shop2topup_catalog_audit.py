#!/usr/bin/env python3
"""Read-only SHOP2TOPUP account and catalog audit; never validates players or buys."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aurix_games.providers.shop2topup_commands import Shop2TopupCommandClient, Shop2TopupCommandError  # noqa: E402


def _rows(response, key):
    value = response.get(key)
    if not isinstance(value, list):
        raise ValueError(f"supplier catalog {key} response was malformed")
    return value


def audit(client: Shop2TopupCommandClient, *, max_products: int) -> dict:
    account_response = client.get_account()
    account = account_response.get("account")
    if not isinstance(account, dict):
        raise ValueError("supplier account response was malformed")

    big_categories = _rows(client.list_big_categories(), "big_categories")
    products = []
    requirements_by_category = {}
    category_complete_by_id = {}
    categories_seen = 0
    complete = True
    truncated = False
    for big in big_categories:
        if not isinstance(big, dict) or isinstance(big.get("id"), bool) or not isinstance(big.get("id"), int):
            complete = False
            continue
        categories = _rows(client.list_categories(big_category_id=big["id"]), "categories")
        for category in categories:
            if (not isinstance(category, dict) or isinstance(category.get("id"), bool)
                    or not isinstance(category.get("id"), int)):
                complete = False
                continue
            categories_seen += 1
            category_id = category["id"]
            category_complete = True
            try:
                fields = client.get_requirements(category_id).get("requirements")
            except Shop2TopupCommandError:
                fields = None
            if isinstance(fields, list) and all(
                isinstance(field, dict)
                and isinstance(field.get("field_name"), str)
                and isinstance(field.get("data_type"), str)
                for field in fields
            ):
                requirements_by_category[str(category_id)] = fields
            else:
                complete = False
                category_complete = False
            subcategories = _rows(client.list_subcategories(category_id=category["id"]), "subcategories")
            for item in subcategories:
                if not isinstance(item, dict):
                    complete = False
                    category_complete = False
                    continue
                item_id = item.get("item_id", item.get("id"))
                if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0:
                    complete = False
                    category_complete = False
                    continue
                if len(products) >= max_products:
                    complete = False
                    category_complete = False
                    truncated = True
                    break
                stock = item.get("in_stock")
                product_type = item.get("product_type")
                products.append({
                    "big_category": str(big.get("name", ""))[:120],
                    "big_category_id": big["id"],
                    "category_id": category["id"],
                    "category": str(category.get("name", ""))[:160],
                    "item_id": item_id,
                    "name": str(item.get("name", ""))[:200],
                    "product_type": product_type if product_type in {"topup", "voucher"} else "unknown",
                    "in_stock": stock if type(stock) is bool else None,
                    "listed_price": str(item.get("price", ""))[:64],
                })
            category_complete_by_id[str(category_id)] = category_complete
            if truncated:
                break
        if truncated:
            break

    return {
        "provider": "shop2topup",
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "writes": False,
        "complete": complete and not truncated,
        "truncated": truncated,
        "account": {
            "enabled": account.get("enabled") is True,
            "verified": account.get("verified") is True,
            "wallet_usd": str(account.get("wallet", ""))[:64],
        },
        "requirements_by_category": requirements_by_category,
        "category_complete_by_id": category_complete_by_id,
        "summary": {
            "big_categories": len(big_categories),
            "categories": categories_seen,
            "products_returned": len(products),
            "available": sum(item["in_stock"] is True for item in products),
            "out_of_stock": sum(item["in_stock"] is False for item in products),
            "stock_unreported": sum(item["in_stock"] is None for item in products),
            "topups": sum(item["product_type"] == "topup" for item in products),
            "vouchers": sum(item["product_type"] == "voucher" for item in products),
        },
        "products": products,
    }


def _write_private(path: Path, content: str) -> None:
    """Atomically write an audit report into a private directory/file."""
    path = path.expanduser()
    if path.is_symlink():
        raise ValueError("output file must not be a symlink")
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink() or stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise ValueError("output directory must be private")
    if path.exists() and not path.is_file():
        raise ValueError("output must be a regular file")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-products", type=int, default=2000)
    parser.add_argument("--output", type=Path, help="write the private audit JSON to this file instead of stdout")
    args = parser.parse_args(argv)
    if isinstance(args.max_products, bool) or not 1 <= args.max_products <= 10000:
        parser.error("--max-products must be between 1 and 10000")
    key = os.environ.get("SHOP2TOPUP_API_KEY", "").strip()
    if not key:
        parser.error("SHOP2TOPUP_API_KEY is required; no supplier writes are performed")
    try:
        result = audit(Shop2TopupCommandClient(key), max_products=args.max_products)
    except (Shop2TopupCommandError, ValueError) as exc:
        print(f"SHOP2TOPUP audit failed: {exc}", file=sys.stderr)
        return 1
    output = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        try:
            _write_private(args.output, output)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps({"status": "saved", "path": str(args.output.expanduser()), "writes": False}))
    else:
        print(output, end="")
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
