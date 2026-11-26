from __future__ import annotations

from pathlib import Path
import stat
import tempfile
import unittest

from aurix_games.shop2topup_catalog_audit import _write_private, audit


class _ReadOnlyCatalog:
    def __init__(self):
        self.calls = []

    def get_account(self):
        self.calls.append(("GET", "/account"))
        return {"account": {"enabled": True, "verified": True, "wallet": "12.340000"}}

    def list_big_categories(self):
        self.calls.append(("GET", "/catalog/big-categories"))
        return {"big_categories": [{"id": 1, "name": "Mobile Games"}]}

    def list_categories(self, *, big_category_id):
        self.calls.append(("GET", f"/catalog/categories?bigCategoryId={big_category_id}"))
        return {"categories": [{"id": 12, "name": "Mobile Legends"}]}

    def get_requirements(self, category_id):
        self.calls.append(("GET", f"/catalog/category/{category_id}/requirements"))
        return {"requirements": [
            {"field_name": "player_id", "data_type": "text"},
            {"field_name": "server", "data_type": "text"},
        ]}

    def list_subcategories(self, *, category_id):
        self.calls.append(("GET", f"/catalog/subcategories?categoryId={category_id}"))
        return {"subcategories": [
            {"item_id": 999, "name": "86 Diamonds", "product_type": "topup", "in_stock": True, "price": "0.95"},
            {"item_id": 1000, "name": "Gift card", "product_type": "voucher", "in_stock": False, "price": "5"},
        ]}


class Shop2TopupCatalogAuditTests(unittest.TestCase):
    def test_reports_supplier_stock_and_only_uses_catalog_reads(self):
        client = _ReadOnlyCatalog()
        result = audit(client, max_products=10)
        self.assertTrue(result["complete"])
        self.assertFalse(result["writes"])
        self.assertEqual(result["summary"], {
            "big_categories": 1, "categories": 1, "products_returned": 2,
            "available": 1, "out_of_stock": 1, "stock_unreported": 0,
            "topups": 1, "vouchers": 1,
        })
        self.assertEqual([method for method, _ in client.calls], ["GET"] * 5)
        self.assertEqual(result["requirements_by_category"]["12"][0]["field_name"], "player_id")

    def test_product_limit_marks_audit_incomplete(self):
        result = audit(_ReadOnlyCatalog(), max_products=1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["summary"]["products_returned"], 1)

    def test_private_audit_writer_uses_atomic_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            target = root / "catalog.json"
            _write_private(target, '{"writes": false}\n')
            self.assertEqual(target.read_text(encoding="utf-8"), '{"writes": false}\n')
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual([item.name for item in root.iterdir()], [target.name])

    def test_private_audit_writer_rejects_public_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "directory must be private"):
                _write_private(root / "catalog.json", "{}\n")


if __name__ == "__main__":
    unittest.main()
