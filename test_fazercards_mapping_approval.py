from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import stat
import tempfile
import unittest

from scripts.approve_fazercards_mapping import main
from aurix_games.mapping_review import mapping_history


class FazerCardsMappingApprovalTests(unittest.TestCase):
    def _config(self) -> dict:
        return {
            "schema_version": 1,
            "queue_status": "all_blocked_template",
            "mappings": [{
                "family": "topups",
                "provider_category_id": "mlbb",
                "provider_offer_id": "d55",
                "offer_id": "pending-d55",
                "product_slug": "pending-review",
                "product_name": "Pending operator review",
                "fulfillment_kind": "direct_topup",
                "denomination_label": "55 Diamonds",
                "review_state": "blocked",
                "customer_visible": False,
                "supplier_enabled": False,
                "resale_authorized": False,
                "region_rule": {"verified": False, "allow_all": False},
                "required_input_labels": [],
            }],
        }

    def _args(self, config: Path, *, confirm: bool = True) -> list[str]:
        args = [
            "--config", str(config), "--family", "topups", "--category", "mlbb", "--sku", "d55",
            "--offer-id", "aurix-mlbb-d55", "--product-slug", "mobile-legends",
            "--product-name", "Mobile Legends 55 Diamonds", "--fulfillment-kind", "direct_topup",
            "--denomination-label", "55 Diamonds", "--region-dimension", "game_server",
            "--region-value", "mm", "--required-input-label", "User ID",
            "--rights-evidence", "https://rights.example.test/mlbb-d55", "--reviewer", "owner",
            "--approved-at", "2026-09-23T00:00:00+00:00",
        ]
        if confirm:
            args.append("--confirm-approval")
        return args

    def test_approval_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps(self._config()), encoding="utf-8")
            config.chmod(0o600)
            self.assertEqual(main(self._args(config, confirm=False)), 2)
            self.assertEqual(json.loads(config.read_text())["mappings"][0]["review_state"], "blocked")

    def test_approval_updates_one_exact_row_atomically_without_supplier_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps(self._config()), encoding="utf-8")
            config.chmod(0o600)
            self.assertEqual(main(self._args(config)), 0)
            payload = json.loads(config.read_text(encoding="utf-8"))

        row = payload["mappings"][0]
        self.assertEqual(payload["queue_status"], "operator_reviewed")
        self.assertEqual(row["provider_offer_id"], "d55")
        self.assertEqual(row["offer_id"], "aurix-mlbb-d55")
        self.assertEqual(row["review_state"], "approved")
        self.assertTrue(row["customer_visible"])
        self.assertTrue(row["supplier_enabled"])
        self.assertTrue(row["resale_authorized"])
        self.assertEqual(row["region_rule"], {
            "verified": True, "allow_all": False, "dimension": "game_server", "allowed_values": ["mm"],
        })
        self.assertEqual(len(payload["approval_history"]), 1)
        self.assertEqual(payload["approval_history"][0]["provider_key"], ["topups", "mlbb", "d55"])
        self.assertEqual(len(payload["approval_history"][0]["event_id"]), 24)
        self.assertFalse(payload.get("supplier_key_used", False))

    def test_approval_rejects_unknown_provider_key(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps(self._config()), encoding="utf-8")
            config.chmod(0o600)
            args = self._args(config)
            args[args.index("--sku") + 1] = "missing"
            self.assertEqual(main(args), 2)

    def test_mapping_history_is_read_only_and_redacts_evidence_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            payload = self._config()
            payload["approval_history"] = [{
                "event_id": "event-1",
                "provider_key": ["topups", "mlbb", "d55"],
                "offer_id": "aurix-mlbb-d55",
                "product_slug": "mobile-legends",
                "fulfillment_kind": "direct_topup",
                "region_rule": {"verified": True, "allow_all": True},
                "rights_evidence": "https://private.example/evidence",
                "reviewer": "owner",
                "approved_at": "2026-09-23T00:00:00+00:00",
            }]
            config.write_text(json.dumps(payload), encoding="utf-8")
            config.chmod(0o600)
            result = mapping_history(config_path=config)
            self.assertEqual(result["status"], "available")
            self.assertEqual(result["count"], 1)
            self.assertTrue(result["events"][0]["rights_evidence_recorded"])
            self.assertNotIn('"rights_evidence":', json.dumps(result))
            self.assertNotIn("private.example", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
