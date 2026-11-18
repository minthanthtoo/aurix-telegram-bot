from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from test_public_catalog import approve_catalog_fixture
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from aurix_games.catalog_policy import CatalogPolicyStore, CatalogPolicyConflict
from aurix_games.public_catalog import PublicCatalogStore
from aurix_games.customer_checkout import PublicCatalogOfferResolver
from aurix_games.live_catalog import SupplierOfferRecord, OfferMapping, build_priced_offers
from aurix_games.eligibility import RegionRule
from aurix_games.pricing import PricingPolicy, calculate_price
from aurix_games.game_admin import GameStoreAdminService
from aurix_games.web_api import create_server

POLICY = {'enabled_families': ['topups'], 'excluded_products': [], 'excluded_offers': []}


def projection(now, catalog_policy=None):
    records = [SupplierOfferRecord(family, 'private-category', sku, 'Pack', Decimal('1.23'), now, True)
               for family, sku in [('topups', 'topup'), ('gamekeys', 'key')]]
    mappings = {(r.family, r.provider_category_id, r.provider_offer_id): OfferMapping(
        offer_id=r.provider_offer_id, product_slug='mobile-legends' if r.family=='topups' else 'key-game',
        product_name='Game', fulfillment_kind='direct_topup' if r.family=='topups' else 'game_key',
        denomination_label='Pack', region_rule=RegionRule(verified=True, allow_all=True)) for r in records}
    priced = build_priced_offers(records, mappings, policy=PricingPolicy(fx_rate='5000'), now=now, max_age=timedelta(minutes=15), catalog_policy=catalog_policy)
    return {'schema_version': 1, 'status': 'approved_projection', 'generated_at': now.isoformat(),
            'source_observed_at': now.isoformat(), 'valid_until': (now + timedelta(minutes=15)).isoformat(), 'offers': [x.public_offer.as_dict() for x in priced]}


class CatalogPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = CatalogPolicyStore(self.root/'policy.sqlite3')

    def test_versions_survive_restart_and_competing_updates_cannot_lose_changes(self):
        self.assertFalse(self.store.status()['configured'])
        first = self.store.save(POLICY, expected_revision=0)
        self.assertEqual(first['revision'], 1)
        def save(_):
            try:
                return self.store.save({**POLICY, 'excluded_products': ['game']}, expected_revision=1)['revision']
            except CatalogPolicyConflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, range(2)))
        self.assertCountEqual(results, [2, 'conflict'])
        self.assertEqual(CatalogPolicyStore(self.store.path).status()['revision'], 2)
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual(db.execute("SELECT version FROM schema_migrations WHERE component='games_catalog_policy'").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('DELETE FROM game_catalog_policies')
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_invalid_policy_cannot_write_or_inject_credentials_or_prices(self):
        for policy in ({**POLICY, 'api_key': 'secret'}, {**POLICY, 'fx_rate': '5000'},
                       {**POLICY, 'enabled_families': ['unknown']}, {**POLICY, 'excluded_products': ['../bad']},
                       {**POLICY, 'excluded_offers': ['id', 'id']}):
            with self.assertRaises(ValueError):
                self.store.save(policy, expected_revision=0)
        self.assertFalse(self.store.path.exists())

    def test_current_backend_filter_blocks_cached_offers_and_new_checkout_only(self):
        now = datetime.now(timezone.utc)
        path = self.root/'public.json'; path.write_text(json.dumps(projection(now)))
        store = PublicCatalogStore(path, now=now, catalog_policy_store=self.store)
        self.store.save(POLICY, expected_revision=0)
        response = store.response()
        self.assertEqual([p['slug'] for p in response['products']], ['mobile-legends'])
        self.assertNotIn('private-category', json.dumps(response))
        self.assertEqual(response['products'][0]['offers'][0]['currency'], 'MMK')
        resolver = PublicCatalogOfferResolver(store)
        self.assertIsNone(resolver.resolve('key'))
        self.assertIsNotNone(resolver.resolve('topup'))
        self.store.save({**POLICY, 'excluded_offers': ['topup']}, expected_revision=1)
        self.assertIsNone(resolver.resolve('topup'))
        self.assertEqual(store.status()['offer_count'], 0)
        self.assertTrue(store.response()['catalog_controls_active'])
        # Previously accepted jobs retain their frozen route; merchandising is not a refund/cancel command.
        self.assertIsNotNone(PublicCatalogStore(path, now=now).offer('topup'))

    def test_publication_filters_before_wholesale_prices_reach_client(self):
        now = datetime.now(timezone.utc)
        data = projection(now, POLICY)
        self.assertEqual(len(data['offers']), 1)
        self.assertEqual(data['offers'][0]['retail_amount_minor'], calculate_price(Decimal('1.23'), PricingPolicy(fx_rate='5000')).final_price_minor)
        self.assertEqual({x['currency'] for x in data['offers']}, {'MMK'})
        self.assertNotIn('supplier_cost_usd', json.dumps(data))
        self.assertNotIn('private-category', json.dumps(data))
        path=self.root/'public.json';path.write_text(json.dumps(data))
        self.store.save({**POLICY, 'excluded_products': ['mobile-legends']}, expected_revision=0)
        self.assertEqual(PublicCatalogStore(path,catalog_policy_store=self.store).response()['products'], [])

    def test_corrupt_controls_fail_closed_instead_of_restoring_hidden_offers(self):
        now = datetime.now(timezone.utc)
        path = self.root/'public.json';path.write_text(json.dumps(projection(now)))
        self.store.path.write_bytes(b'corrupt');self.store.path.chmod(0o600)
        store=PublicCatalogStore(path,catalog_policy_store=self.store)
        self.assertIsNone(store.response())
        self.assertIsNone(store.offer('topup'))
        self.assertTrue(store.controls_active())


class CatalogPolicyHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = CatalogPolicyStore(self.root/'policy.sqlite3')
        self.path=self.root/'public.json';self.path.write_text(json.dumps(projection(datetime.now(timezone.utc))))
        approve_catalog_fixture(self.path)
        admin=GameStoreAdminService(admin_token='admin-fixture', supplier_client=None,
                                    wallet_alert_store=None, catalog_policy_store=self.store)
        self.server=create_server(public_catalog_path=self.path,admin_service=admin)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop)
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)

    def request(self,path,body=None,authorized=False):
        headers={'Content-Type':'application/json'}
        if authorized:headers['Authorization']='Bearer admin-fixture'
        req=Request(self.base+path,headers=headers,data=None if body is None else json.dumps(body).encode(),method='GET' if body is None else 'PUT')
        try:r=urlopen(req)
        except HTTPError as error:r=error
        with r:return r.status,json.load(r),r.headers

    def test_admin_authenticated_versioned_settings_and_all_customer_aliases(self):
        endpoint='/api/v1/admin/catalog-policy';payload={'policy':POLICY,'expected_revision':0}
        self.assertEqual(self.request(endpoint,payload)[0],401)
        self.assertFalse(self.store.path.exists())
        code,data,headers=self.request(endpoint,payload,True)
        self.assertEqual(code,200);self.assertEqual(data['revision'],1);self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertEqual(self.request(endpoint,payload,True)[0],409)
        for path in ('/api/v1/live-catalog','/api/v1/catalog','/api/v1/catalog/products'):
            code,data,_=self.request(path)
            self.assertEqual(code,200)
            self.assertEqual([x['slug'] for x in data['products']],['mobile-legends'])
        for path in ('/api/v1/catalog/key-game','/api/v1/catalog/products/key-game','/api/v1/catalog/key-game/offers','/api/v1/catalog/products/key-game/offers'):
            self.assertEqual(self.request(path)[0],404)
        self.assertEqual([r['id'] for r in self.request('/api/v1/catalog/categories')[1]['categories']], ['live-offers'])
        self.assertEqual(self.request('/api/v1/catalog/products?limit=51')[0], 400)
        self.assertEqual(self.request('/api/v1/admin/catalog-policy')[0], 401)

    def test_missing_or_expired_projection_never_restores_reference_products(self):
        self.store.save(POLICY,expected_revision=0)
        self.path.unlink()
        self.assertEqual(self.request('/api/v1/live-catalog')[1]['total_count'],0)
        self.assertEqual(self.request('/api/v1/catalog/products')[1]['total_count'],0)
        data=projection(datetime(2000,1,1,tzinfo=timezone.utc));self.path.write_text(json.dumps(data))
        self.assertEqual(self.request('/api/v1/catalog/products')[0],503)

    def test_pricing_change_revokes_every_live_alias_until_rebuild(self):
        self.store.save(POLICY, expected_revision=0)
        self.assertEqual(self.request('/api/v1/live-catalog')[0], 200)
        config_path = self.root / 'public-catalog-config.json'
        config = json.loads(config_path.read_text())
        config['policy']['fx_rate'] = '5000'
        config_path.write_text(json.dumps(config))
        for path in ('/api/v1/live-catalog', '/api/v1/catalog', '/api/v1/catalog/products',
                     '/api/v1/catalog/categories'):
            self.assertEqual(self.request(path)[0], 503)
        for path in ('/api/v1/catalog/mobile-legends', '/api/v1/catalog/products/mobile-legends',
                     '/api/v1/catalog/mobile-legends/offers', '/api/v1/catalog/products/mobile-legends/offers'):
            self.assertEqual(self.request(path)[0], 503)
        approve_catalog_fixture(self.path)
        self.assertEqual(self.request('/api/v1/live-catalog')[0], 200)


if __name__=='__main__':unittest.main()
