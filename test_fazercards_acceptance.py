from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import tempfile
import unittest
from unittest.mock import Mock

from cryptography.fernet import Fernet
from scripts.accept_fazercards_topup import execute, AcceptanceError
from aurix_games.spend_guard import schema_fingerprint


class SupplierOnlyAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'acceptance.sqlite3'
        self.now = datetime.now(timezone.utc)
        self.key = Fernet.generate_key()
        self.fields = [{'key': 'player_id', 'type': 'text'}, {'key': 'server_id', 'type': 'text'}]
        self.request = {'acceptance_id': 'test-one', 'payload': {'category_id': 'mobile_legends_global',
            'offer_id': '12_diamonds', 'fields': {'player_id': '111', 'server_id': '222'}},
            'max_cost_usd': '0.30', 'total_limit_usd': '3', 'fields_sha256': schema_fingerprint(self.fields),
            'note_sha256': hashlib.sha256(b'reviewed note').hexdigest(), 'validation_category_id': 'mobile_legends',
            'validation_fields': {'player_id': '111', 'zone_id': '222'}, 'expected_region': 'United Kingdom',
            'expected_player_name': 'fixture player', 'expected_player_id': '111'}
        self.client = Mock()
        self.client.get_subscription.return_value = {'ok': True, 'subscriptionActive': True,
            'planExpiresAt': (self.now + timedelta(days=1)).isoformat()}
        self.client.get_balance.return_value = {'ok': True, 'balance': '10.00', 'currency': 'USD'}
        self.client.validate_topup_recipient.return_value = {'ok': True, 'valid': True, 'category_id': 'mobile_legends',
            'player_id': '111', 'player_name': 'fixture player', 'region': 'United Kingdom'}
        self.client.order_topup.return_value = {'ok': True, 'order': {'id': 'ord-17', 'status': 'processing'}}
        self.client.get_order.return_value = {'ok': True, 'order': {'id': 'ord-17', 'status': 'completed'}}
        self.catalog = Mock()
        self.catalog.get.return_value = {'category_id': 'mobile_legends_global', 'fields': self.fields,
            'note': 'reviewed note', 'offers': [{'offer_id': '12_diamonds', 'price_usd': '0.22'}]}

    def run_request(self, request=None):
        return execute(request or self.request, database=self.path, encryption_key=self.key, client=self.client,
            catalog=self.catalog, now=self.now)

    def test_same_request_reads_saved_order_without_another_purchase(self):
        self.assertEqual(self.run_request()['supplier_order_id'], 'ord-17')
        self.assertFalse(self.run_request()['customer_payment_created'])
        self.client.order_topup.assert_called_once()
        self.assertNotIn(b'fixture player', self.path.read_bytes())
        self.assertFalse((self.path.parent / 'customer-ledger.sqlite3').exists())

    def test_lost_response_fences_all_following_acceptance_ids(self):
        self.client.order_topup.side_effect = TimeoutError()
        with self.assertRaises(AcceptanceError): self.run_request()
        with self.assertRaises(AcceptanceError): self.run_request()
        with self.assertRaises(AcceptanceError): self.run_request({**self.request, 'acceptance_id': 'new-id'})
        self.client.order_topup.assert_called_once()

    def test_refunded_purchase_cannot_authorize_another_test_id(self):
        self.run_request()
        self.client.get_order.return_value['order']['status'] = 'refund'
        self.assertEqual(self.run_request()['status'], 'refund')
        with self.assertRaises(AcceptanceError):
            self.run_request({**self.request, 'acceptance_id': 'new-id'})
        self.client.order_topup.assert_called_once()

    def test_changed_price_terms_target_or_over_budget_never_purchases(self):
        for change in [{'max_cost_usd': '4'}, {'total_limit_usd': '4'}, {'expected_player_id': 'other'},
                {'expected_region': 'Indonesia'}, {'note_sha256': 'wrong'}]:
            with self.subTest(change=change), self.assertRaises(AcceptanceError):
                self.run_request({**self.request, **change})
        self.client.validate_topup_recipient.return_value['region'] = 'Brazil'
        with self.assertRaises(AcceptanceError): self.run_request()
        self.client.order_topup.assert_not_called()
