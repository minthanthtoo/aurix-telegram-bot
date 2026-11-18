from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from aurix_games.pricing import PricingPolicy
from aurix_games.pricing_store import PricingPolicyStore
from scripts import seed_fazercards_mapping_queue as queue_module
from scripts.seed_fazercards_mapping_queue import build_blocked_queue, reconcile_queue, main


class FazerCardsMappingQueueTests(unittest.TestCase):
    def _snapshot(self) -> dict:
        return {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "status": "partial",
            "families": {
                "topups": {
                    "details_by_id": {
                        "mlbb": {"category_id": "mlbb", "offers": [
                            {"offer_id": "d55", "name": "55 Diamonds", "price_usd": "1"},
                        ]},
                    },
                },
                "giftcards": {},
                "gamekeys": {},
            },
        }

    def test_queue_is_blocked_and_preserves_exact_provider_key(self):
        queue = build_blocked_queue(self._snapshot())
        self.assertEqual(queue["queue_status"], "all_blocked_template")
        self.assertEqual(queue["mapping_rows"], 1)
        row = queue["mappings"][0]
        self.assertEqual(row["provider_offer_id"], "d55")
        self.assertEqual(row["denomination_label"], "55 Diamonds")
        self.assertFalse(row["customer_visible"])
        self.assertFalse(row["supplier_enabled"])
        self.assertFalse(row["resale_authorized"])
        self.assertEqual(row["review_state"], "blocked")
        self.assertFalse(row["region_rule"]["verified"])
        self.assertNotIn("policy", queue)
        self.assertNotIn("pricing_policy_approval", queue)

    def test_manual_service_api_items_join_the_blocked_mapping_queue(self):
        snapshot = self._snapshot()
        snapshot["families"]["manual_services"] = {
            "items": [{"id": "manual-boost", "name": "Manual Boost", "kind": "game-service", "chat": False, "info": ""}],
            "offers_by_id": {"manual-boost": {
                "manual_service_id": "manual-boost",
                "items": [{"id": "boost-1", "name": "Boost service", "price_usd": "2.50"}],
            }},
        }
        queue = build_blocked_queue(snapshot)
        self.assertEqual(queue["mapping_rows"], 2)
        manual = next(row for row in queue["mappings"] if row["family"] == "manual_services")
        self.assertEqual(manual["provider_category_id"], "manual-boost")
        self.assertEqual(manual["provider_offer_id"], "boost-1")
        self.assertEqual(manual["fulfillment_kind"], "manual_service")
        self.assertEqual(manual["review_state"], "blocked")
        self.assertFalse(manual["customer_visible"])
        self.assertFalse(manual["supplier_enabled"])

    def test_cli_writes_private_recoverable_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.json"
            output = root / "nested" / "config.json"
            snapshot.write_text(json.dumps(self._snapshot()), encoding="utf-8")
            self.assertEqual(main(["--snapshot", str(snapshot), "--output", str(output)]), 0)
            self.assertTrue(output.exists())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(json.loads(output.read_text())["mapping_rows"], 1)

    def test_force_reseed_preserves_an_explicit_pricing_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.json"
            output = root / "config.json"
            snapshot.write_text(json.dumps(self._snapshot()), encoding="utf-8")
            now = datetime.now(timezone.utc)
            policy = PricingPolicy(fx_rate="4500").as_dict()
            PricingPolicyStore(output).save(
                PricingPolicy.from_complete_mapping(policy),
                approved_by="staff:owner",
                fx_source="test fixture",
                fx_observed_at=now.isoformat(),
                valid_until=(now + timedelta(days=1)).isoformat(),
                now=now,
            )
            self.assertEqual(main(["--snapshot", str(snapshot), "--output", str(output), "--force"]), 0)
            self.assertEqual(PricingPolicyStore(output).status(now=now)["policy_status"], "approved")
            queue = json.loads(output.read_text())
            self.assertEqual(queue["queue_status"], "all_blocked_template")
            self.assertFalse(queue["mappings"][0]["customer_visible"])

    def test_reconcile_preserves_unchanged_approval_policy_and_history(self):
        snapshot = self._snapshot()
        previous = build_blocked_queue(snapshot)
        previous.update(policy={'sentinel': 1}, approval_history=[{'sentinel': 2}])
        previous['mappings'][0].update(review_state='approved', customer_visible=True,
                                       supplier_enabled=True, offer_id='approved-pack')
        original = json.loads(json.dumps(previous))
        candidate = reconcile_queue(snapshot, previous)
        self.assertEqual(previous, original)
        self.assertEqual(candidate['mappings'][0]['offer_id'], 'approved-pack')
        self.assertEqual(candidate['mappings'][0]['review_state'], 'approved')
        self.assertTrue(candidate['mappings'][0]['customer_visible'])
        self.assertEqual(candidate['policy'], previous['policy'])
        self.assertEqual(candidate['approval_history'], previous['approval_history'])
        self.assertEqual(reconcile_queue(snapshot, candidate)['mappings'], candidate['mappings'])

    def test_price_and_stock_changes_do_not_revoke_contract_approval(self):
        snapshot = self._snapshot()
        previous = build_blocked_queue(snapshot)
        previous['mappings'][0]['review_state'] = 'approved'
        snapshot['families']['topups']['details_by_id']['mlbb']['offers'][0].update(price_usd='2', stock=9)
        self.assertEqual(reconcile_queue(snapshot, previous)['mappings'][0]['review_state'], 'approved')

    def test_schema_or_missing_baseline_pauses_approval_without_erasing_history(self):
        for baseline in (True, False):
            with self.subTest(baseline=baseline):
                snapshot = self._snapshot()
                previous = build_blocked_queue(snapshot)
                row = previous['mappings'][0]
                row.update(review_state='approved', customer_visible=True, supplier_enabled=True)
                if not baseline:
                    row.pop('source_contract_sha256')
                else:
                    snapshot['families']['topups']['details_by_id']['mlbb']['fields'] = [{'name': 'new_input'}]
                result = reconcile_queue(snapshot, previous)['mappings'][0]
                self.assertEqual(result['review_state'], 'paused')
                self.assertFalse(result['customer_visible'])
                self.assertFalse(result['supplier_enabled'])
                self.assertTrue(result['source_review_required'])

    def test_missing_rows_are_retained_and_new_rows_are_blocked(self):
        previous = build_blocked_queue(self._snapshot())
        previous['mappings'][0].update(review_state='approved', customer_visible=True)
        snapshot = self._snapshot()
        snapshot['families']['topups']['details_by_id']['mlbb']['offers'][0]['offer_id'] = 'new-sku'
        candidate = reconcile_queue(snapshot, previous)
        by_sku = {r['provider_offer_id']: r for r in candidate['mappings']}
        self.assertEqual(by_sku['d55']['source_presence'], 'not_observed')
        self.assertEqual(by_sku['d55']['review_state'], 'approved')
        self.assertTrue(by_sku['d55']['customer_visible'])
        self.assertEqual(by_sku['new-sku']['review_state'], 'blocked')
        self.assertFalse(by_sku['new-sku']['customer_visible'])
        self.assertEqual(candidate['reconciliation']['added_blocked'], 1)
        self.assertEqual(candidate['reconciliation']['retained_not_observed'], 1)

    def test_duplicate_source_keys_and_output_identities_are_rejected(self):
        snapshot = self._snapshot()
        previous = build_blocked_queue(snapshot)
        previous['mappings'].append(dict(previous['mappings'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate provider key'):
            reconcile_queue(snapshot, previous)
        previous['mappings'].pop()
        snapshot['families']['topups']['details_by_id']['mlbb']['offers'].append(
            {'offer_id': 'second', 'name': 'Second', 'price_usd': '2'})
        previous['mappings'][0]['offer_id'] = build_blocked_queue(snapshot)['mappings'][1]['offer_id']
        with self.assertRaisesRegex(ValueError, 'duplicate AuriX offer'):
            reconcile_queue(snapshot, previous)

    def test_merge_cli_leaves_active_config_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / 'snapshot.json'
            snapshot.write_text(json.dumps(self._snapshot()))
            active = root / 'active.json'
            active.write_text(json.dumps(build_blocked_queue(self._snapshot())))
            active.chmod(0o600)
            before = active.read_bytes()
            output = root / 'candidate.json'
            self.assertEqual(main(['--snapshot', str(snapshot), '--merge-existing', str(active), '--output', str(output)]), 0)
            self.assertEqual(active.read_bytes(), before)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            with self.assertRaises(SystemExit):
                main(['--snapshot', str(snapshot), '--merge-existing', str(active), '--output', str(active), '--force'])

    def test_candidate_install_cannot_clobber_a_concurrent_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'candidate.json'
            install = queue_module.os.link
            def competing_writer(source, destination):
                path.write_text('concurrent result')
                return install(source, destination)
            with patch.object(queue_module.os, 'link', side_effect=competing_writer):
                with self.assertRaises(FileExistsError):
                    queue_module._write_private(path, {'mappings': []})
            self.assertEqual(path.read_text(), 'concurrent result')
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_naive_source_timestamp_is_rejected(self):
        snapshot = self._snapshot()
        snapshot['captured_at'] = '2026-11-14T00:00:00'
        with self.assertRaisesRegex(ValueError, 'timezone'):
            build_blocked_queue(snapshot)


if __name__ == "__main__":
    unittest.main()
