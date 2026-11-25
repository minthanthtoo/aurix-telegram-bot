from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
import unittest
import uuid

from cryptography.fernet import Fernet

import test_game_fulfillment as fixtures
from aurix_games.fulfillment import FulfillmentTaskStore, GameFulfillmentWorker, SupplierOrderRouter
from aurix_games.order_reconciliation import reconcile_order
from aurix_games.order_state import OrderState
from aurix_games.providers.shop2topup_commands import Shop2TopupCommandClient, Shop2TopupCommandError
from aurix_games.spend_guard import Shop2TopupPilotSpendGuard, schema_fingerprint
from aurix_games.supplier_routes import PrivateSupplierRouteStore, supplier_order_payload


UTC = timezone.utc
NOW = datetime(2026, 9, 22, 17, 0, tzinfo=UTC)
PLAYER_FIELDS = [
    {"field_name": "player_id", "data_type": "text"},
    {"field_name": "server", "data_type": "text"},
]


class _Shop2Topup(Shop2TopupCommandClient):
    def __init__(self, *, fail_create=False):
        super().__init__("key-id.secret", allow_orders=True, allow_player_validation=True)
        self.fail_create = fail_create
        self.created = []
        self.read_ids = []
        self.order_status = "pending"

    def get_account(self):
        return {"success": True, "account": {"enabled": True, "verified": True, "wallet": "10.000000"}}

    def get_requirements(self, category_id):
        return {"success": True, "requirements": PLAYER_FIELDS}

    def get_price(self, item_id):
        return {"success": True, "price": {
            "item_id": item_id, "unit_price": "0.950000", "currency": "USD",
            "in_stock": True, "timestamp": NOW.isoformat(),
        }}

    def validate_player(self, *, sub_category_id, player_id, requirements=None):
        self.read_ids.append((sub_category_id, player_id, requirements))
        return {"success": True, "player": {"player_id": player_id, "player_name": "Player One", "region": "Asia"}}

    def create_order(self, *, order_id, sub_category_id, quantity, requirements, expected_unit_price=None):
        self.created.append({
            "order_id": order_id, "sub_category_id": sub_category_id, "quantity": quantity,
            "requirements": dict(requirements), "expected_unit_price": expected_unit_price,
        })
        if self.fail_create:
            raise Shop2TopupCommandError("SHOP2TOPUP request failed or timed out")
        return {"success": True, "order": {"order_id": order_id, "status": "pending"}}

    def get_order(self, order_id):
        self.read_ids.append(order_id)
        return {"success": True, "order": {"order_id": order_id, "status": self.order_status}}


def _mapping(path: Path):
    path.write_text(json.dumps({"schema_version": 1, "mappings": [{
        "supplier_provider": "shop2topup",
        "family": "topups",
        "provider_category_id": "12",
        "provider_offer_id": "999",
        "validation_category_id": "12",
        "validation_field_map": {"player_id": "player_id", "server": "server_id"},
        "offer_id": "offer-1",
        "product_slug": "mobile-legends",
        "fulfillment_kind": "direct_topup",
        "denomination_label": "86 Diamonds",
        "required_input_labels": ["player_id", "server_id"],
        "review_state": "approved",
        "customer_visible": True,
        "supplier_enabled": True,
        "resale_authorized": True,
        "region_rule": {"verified": True, "allow_all": True},
    }]}), encoding="utf-8")
    path.chmod(0o600)
    path.parent.chmod(0o700)


class Shop2TopupIntegrationTests(unittest.TestCase):
    def test_route_maps_exact_inputs_and_provider_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "shop2topup-routes.json"
            _mapping(config)
            fazer_config = root / "public-catalog-config.json"
            fazer_config.write_text(json.dumps({"schema_version": 1, "mappings": [{
                "family": "topups", "provider_category_id": "12", "provider_offer_id": "999",
                "offer_id": "fazer-shadow",
            }]}), encoding="utf-8")
            fazer_config.chmod(0o600)
            routes = PrivateSupplierRouteStore(fazer_config, additional_paths=(config,))
            route = routes.resolve(
                offer_id="offer-1", product_slug="mobile-legends", fulfillment_kind="direct_topup",
                denomination_label="86 Diamonds", required_input_labels=("player_id", "server_id"),
            )
            self.assertEqual(route.supplier_provider, "shop2topup")
            payload = supplier_order_payload(route=route, fulfillment_kind="direct_topup", fields={
                "player_id": "12345678", "server_id": "Asia",
            })
            self.assertEqual(payload, {
                "supplier_provider": "shop2topup", "category_id": "12", "offer_id": "999",
                "sub_category_id": 999, "quantity": 1, "player_id": "12345678",
                "requirements": {"server": "Asia"},
            })
            shop = _Shop2Topup()
            router = SupplierOrderRouter(None, shop)
            result = router.validate_route(route=route, fields={"player_id": "12345678", "server": "Asia"})
            self.assertEqual(result["player_name"], "Player One")
            self.assertEqual(result["category_id"], "12")
            self.assertEqual(shop.read_ids[0], (999, "12345678", {"server": "Asia"}))

    def test_submit_and_status_use_stable_uuid_and_provider_namespace(self):
        shop = _Shop2Topup()
        router = SupplierOrderRouter(None, shop)
        payload = {
            "supplier_provider": "shop2topup", "category_id": "12", "offer_id": "999",
            "sub_category_id": 999, "quantity": 1, "player_id": "12345678",
            "requirements": {"server": "Asia"}, "expected_unit_price": "0.950000",
        }
        key = "aurix-game:stable-key"
        expected = router.attempt_reference(payload=payload, idempotency_key=key)
        result = router.submit(fulfillment_kind="direct_topup", payload=payload, idempotency_key=key)
        self.assertEqual(result["order"]["id"], expected)
        self.assertEqual(shop.created[0]["order_id"].replace("-", ""), expected[4:])
        self.assertEqual(shop.created[0]["expected_unit_price"], "0.950000")
        self.assertEqual(router.get_order(expected)["order"]["id"], expected)

    def test_spend_guard_locks_fresh_price_and_worker_never_resends_ambiguous_order(self):
        fixtures.GameFulfillmentTests.setUp(self)
        self.addCleanup(fixtures.GameFulfillmentTests.tearDown, self)
        root = Path(self.tempdir.name)
        tasks = FulfillmentTaskStore(root / "fulfillment.sqlite3", encryption_key=self.fulfillment_key)
        shop = _Shop2Topup(fail_create=True)
        payload = {
            "supplier_provider": "shop2topup", "category_id": "12", "offer_id": "999",
            "sub_category_id": 999, "quantity": 1, "player_id": "12345678",
            "requirements": {"server": "Asia"},
        }
        checkout = type("Checkout", (), {"fulfillment_payload": lambda _self, **_kwargs: payload})()
        policy = {
            "daily_limit_usd": "5", "minimum_balance_usd": "1", "offers": [{
                "category_id": "12", "offer_id": "999", "max_cost_usd": "1.25",
                "allow_unknown_stock": False, "fields_sha256": schema_fingerprint(PLAYER_FIELDS),
            }],
        }
        guard = Shop2TopupPilotSpendGuard(tasks=tasks, client=shop, checkout=checkout, policy=policy)
        router = SupplierOrderRouter(None, shop)
        worker = GameFulfillmentWorker(orders=self.orders, tasks=tasks, supplier=router,
            payments=self.payments, spend_guard=guard)
        worker.enqueue_paid_order("order-1", payload=payload, now=NOW)

        outcome = worker.run_once(now=NOW)[0]
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(len(shop.created), 1)
        receipt = router.attempt_reference(payload=payload,
            idempotency_key=self.orders.get("order-1").provider_idempotency_key)
        self.assertEqual(self.orders.get("order-1").state, OrderState.UNKNOWN)
        self.assertEqual(self.orders.get("order-1").provider_order_id, receipt)
        self.assertEqual(self.ledger.balance("customer-1").reserved_minor, 4500)

        shop.order_status = "completed"
        reconciled = reconcile_order(self.orders, router, "order-1", now=NOW + timedelta(seconds=1))
        self.assertEqual(reconciled.state, OrderState.RECONCILIATION.value)
        self.assertTrue(reconciled.applied)
        self.assertEqual(shop.read_ids[-1], str(uuid.UUID(hex=receipt[4:])))
        self.assertEqual(worker.run_once(now=NOW + timedelta(days=1)), ())
        self.assertEqual(len(shop.created), 1)


if __name__ == "__main__":
    unittest.main()
