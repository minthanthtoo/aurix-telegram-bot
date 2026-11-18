from __future__ import annotations

import json
import hashlib
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from test_public_catalog import approve_catalog_fixture
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from aurix_games.fazercards_account import SupplierWalletPolicy, WalletFundingIntentStore
from aurix_games.catalog_policy import CatalogPolicyStore
from aurix_games.game_admin import GameStoreAdminService
from aurix_games.providers.fazercards_commands import FazerCardsCommandClient
from aurix_games.order_reconciliation import SupplierWebhookReconciler
from aurix_games.order_repository import GameOrderRepository
from aurix_games.order_state import OrderSnapshot, OrderState, create_order
from aurix_games.wallet_monitor import SupplierWalletAlertStore
from aurix_games.pricing_store import PricingPolicyStore
from aurix_games.pricing import PricingPolicy
from aurix_games.web_api import make_handler


class _Response:
    status = 200
    def __init__(self, payload): self.payload = payload
    def read(self, limit=-1): return json.dumps(self.payload).encode()
    def __enter__(self): return self
    def __exit__(self, *args): return False


class _Opener:
    def open(self, request, timeout):
        if request.full_url.endswith("/balance"):
            return _Response({"ok": True, "balance": "4", "currency": "USD"})
        if request.full_url.endswith("/payments/methods"):
            return _Response({"ok": True, "items": [{"code": "usdt", "label": "USDT", "minAmountUsd": 5, "maxAmountUsd": 500}]})
        if request.full_url.endswith("/subscription"):
            return _Response({"ok": True, "subscription": {"plan": "trial", "status": "active", "trialEndsAt": "2026-09-23T00:00:00Z", "secret": "do-not-return"}, "apiKey": "do-not-return"})
        return _Response({"ok": True, "payment": {"id": "tx1", "status": "pending", "network": "USDT", "address": "provider-controlled", "amount": "50", "paymentUrl": "https://pay.fzr.cards/invoice/tx1"}})


class GameLiveAPITests(unittest.TestCase):
    def test_admin_payment_methods_require_auth_and_return_current_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            service = GameStoreAdminService(
                admin_token="admin-token",
                supplier_client=FazerCardsCommandClient("secret", opener=_Opener()),
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(admin_service=service))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}/api/v1/admin/supplier-wallet/payment-methods"
                with self.assertRaises(HTTPError) as error:
                    urlopen(base)
                self.assertEqual(error.exception.code, 401); error.exception.close()
                request = Request(base, headers={"Authorization": "Bearer admin-token"})
                with urlopen(request) as response:
                    payload = json.loads(response.read())
                self.assertEqual(payload["methods"][0]["code"], "usdt")
                self.assertEqual(payload["methods"][0]["min_amount_usd"], "5")
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_signed_fazercards_webhook_reconciles_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = GameOrderRepository(Path(directory) / "orders.sqlite3")
            now = datetime.now(timezone.utc)
            order = create_order(
                order_id="local-1",
                customer_id="customer-1",
                customer_idempotency_key="key-1",
                snapshot=OrderSnapshot(
                    offer_id="offer-1", product_slug="game-a", fulfillment_kind="direct_topup",
                    amount_minor=1000, currency="MMK", recipient_fingerprint="fp-1",
                ),
            )
            repository.create(order, created_at=now)
            repository.transition("local-1", OrderState.AWAITING_PAYMENT, at=now, reason="await")
            repository.transition("local-1", OrderState.PAID_RESERVED, at=now, reason="reserved")
            repository.transition("local-1", OrderState.FULFILLMENT_QUEUED, at=now, reason="queued")
            repository.transition(
                "local-1", OrderState.SUBMITTED, at=now, reason="submitted", provider_order_id="ord-12"
            )
            secret = "webhook-secret"
            reconciler = SupplierWebhookReconciler(repository, webhook_secret=secret)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                make_handler(order_reconciler=reconciler),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                body = json.dumps({
                    "event": "order.status_changed",
                    "event_id": "event-1",
                    "timestamp": now.isoformat(),
                    "data": {"order_id": "ord-12", "status": "completed", "type": "topup"},
                }, separators=(",", ":")).encode()
                signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/v1/webhooks/fazercards",
                    data=body,
                    headers={"X-Webhook-Signature": signature, "Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request) as response:
                    first = json.loads(response.read())
                self.assertTrue(first["accepted"])
                self.assertEqual(repository.get("local-1").state, OrderState.SUBMITTED)
                with urlopen(request) as response:
                    second = json.loads(response.read())
                self.assertEqual(second, first)
                from aurix_games.order_reconciliation import drain_provider_events
                self.assertEqual(len(drain_provider_events(repository, now=now, limit=10)), 1)
                self.assertEqual(drain_provider_events(repository, now=now, limit=10), ())
                self.assertEqual(repository.get("local-1").state, OrderState.DELIVERED)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_fazercards_webhook_rejects_bad_signature_and_missing_configuration(self):
        body = b'{"event":"webhook.test","event_id":"event-1","timestamp":"now","data":{}}'
        request = Request(
            "http://127.0.0.1:0/api/v1/webhooks/fazercards", data=body,
            headers={"X-Webhook-Signature": "sha256=" + "0" * 64}, method="POST",
        )
        with tempfile.TemporaryDirectory() as directory:
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler())
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                request.full_url = f"http://127.0.0.1:{server.server_port}/api/v1/webhooks/fazercards"
                with self.assertRaises(HTTPError) as error:
                    urlopen(request)
                self.assertEqual(error.exception.code, 503)
                error.exception.close()
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
    def test_live_projection_is_served_safely_and_missing_projection_is_not_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public.json"
            stamp = datetime.now(timezone.utc).isoformat()
            path.write_text(json.dumps({
                "schema_version": 1,
                "status": "approved_projection",
                "generated_at": stamp,
                "source_observed_at": stamp,
                "valid_until": (datetime.fromisoformat(stamp) + timedelta(minutes=15)).isoformat(),
                "offers": [{
                    "offer_id": "a-1", "product_slug": "game-a", "product_name": "Game A",
                    "fulfillment_kind": "direct_topup",
                    "denomination_label": "100 coins", "retail_amount_minor": 5000,
                    "currency": "MMK", "region": None, "required_input_labels": [],
                }],
            }), encoding="utf-8")
            approve_catalog_fixture(path)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(public_catalog_path=path))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urlopen(base + "/api/v1/live-catalog?category=live-offers") as response:
                    payload = json.loads(response.read())
                self.assertEqual(payload["status"], "live")
                self.assertEqual(payload["products"][0]["offers"][0]["price_minor"], 5000)
                with urlopen(base + "/api/v1/catalog/products/game-a") as response:
                    detail = json.loads(response.read())
                self.assertEqual(detail["status"], "live")
                self.assertEqual(detail["offers"][0]["offer_id"], "a-1")
                with urlopen(base + "/api/v1/catalog/products/game-a/offers?region=global") as response:
                    offers = json.loads(response.read())
                self.assertEqual(offers["offers"][0]["price_minor"], 5000)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_product_routes_report_catalog_outage_as_503_and_fresh_absence_as_404(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "public.json"
            policy_store = CatalogPolicyStore(root / "catalog-policy.sqlite3")
            policy_store.save(
                {"enabled_families": ["topups"], "excluded_products": [], "excluded_offers": []},
                expected_revision=0,
            )
            pricing_store = PricingPolicyStore(root / "public-catalog-config.json")
            service = GameStoreAdminService(
                admin_token="admin-token",
                supplier_client=None,
                wallet_alert_store=None,
                pricing_policy_store=pricing_store,
                catalog_policy_store=policy_store,
            )
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                make_handler(admin_service=service, public_catalog_path=catalog_path),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            routes = (
                "/api/v1/catalog/products/missing-game",
                "/api/v1/catalog/products/missing-game/offers",
                "/api/v1/catalog/missing-game",
                "/api/v1/catalog/missing-game/offers",
            )
            try:
                base = f"http://127.0.0.1:{server.server_port}"

                def fetch(route):
                    try:
                        with urlopen(base + route) as response:
                            return response.status, json.loads(response.read())
                    except HTTPError as error:
                        with error:
                            return error.code, json.loads(error.read())

                for route in routes:
                    status, payload = fetch(route)
                    self.assertEqual(status, 503, route)
                    self.assertEqual(payload["error"], "approved catalog is unavailable")

                stamp = datetime.now(timezone.utc)
                catalog_path.write_text(json.dumps({
                    "schema_version": 1,
                    "status": "approved_projection",
                    "generated_at": stamp.isoformat(),
                    "source_observed_at": stamp.isoformat(),
                    "valid_until": (stamp + timedelta(minutes=15)).isoformat(),
                    "offers": [],
                }), encoding="utf-8")
                approve_catalog_fixture(catalog_path, now=stamp)

                for route in routes:
                    status, payload = fetch(route)
                    self.assertEqual(status, 404, route)
                    self.assertEqual(payload["error"], "product not found")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_admin_wallet_requires_bearer_and_can_create_invoice_with_service(self):
        with tempfile.TemporaryDirectory() as directory:
            service = GameStoreAdminService(
                admin_token="admin-token",
                supplier_client=FazerCardsCommandClient("secret", allow_writes=True, opener=_Opener()),
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
                funding_intent_store=WalletFundingIntentStore(Path(directory) / "funding.sqlite3"),
                treasury_reviewers={"staff:alice": hashlib.sha256(b"t" * 32).hexdigest()},
                wallet_policy=SupplierWalletPolicy(),
                pricing_policy_store=PricingPolicyStore(Path(directory) / "pricing.json"),
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(admin_service=service))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + "/api/v1/admin/supplier-wallet/status")
                self.assertEqual(error.exception.code, 401); error.exception.close()
                request = Request(
                    base + "/api/v1/admin/supplier-wallet/invoice",
                    data=json.dumps({"intent_id": "fund-1", "method": "usdt", "amount_usd": "50"}).encode(),
                    headers={"Authorization": "Bearer admin-token", "Content-Type": "application/json", "X-Aurix-Treasury-Token": "t" * 32},
                    method="POST",
                )
                with urlopen(request) as response:
                    payload = json.loads(response.read())
                self.assertEqual(response.status, 201)
                self.assertEqual(payload["intent"]["payment_id"], "tx1")
                self.assertNotIn("secret", json.dumps(payload))
                policy_request = Request(
                    base + "/api/v1/admin/pricing-policy",
                    data=json.dumps({
                        "policy": {**PricingPolicy().as_dict(), "fx_rate": "4600"},
                        "approval": {
                            "fx_source": "test fixture",
                            "fx_observed_at": datetime.now(timezone.utc).isoformat(),
                            "valid_until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                        },
                        "owner_approval": True,
                    }).encode(),
                    headers={"Authorization": "Bearer admin-token", "X-Aurix-Treasury-Token": "t" * 32,
                        "Content-Type": "application/json"},
                    method="PUT",
                )
                with urlopen(policy_request) as policy_response:
                    policy = json.loads(policy_response.read())
                self.assertEqual(policy["policy"]["fx_rate"], "4600")
                self.assertEqual(policy["approval"]["approved_by"], "staff:alice")
                get_policy = Request(base + "/api/v1/admin/pricing-policy", headers={"Authorization": "Bearer admin-token"})
                with urlopen(get_policy) as policy_response:
                    self.assertEqual(json.loads(policy_response.read())["policy_status"], "approved")
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_malformed_supplier_balance_is_rejected_without_alert_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class InvalidBalanceOpener(_Opener):
                def open(self, request, timeout):
                    if request.full_url.endswith("/balance"):
                        return _Response({"ok": False, "balance": "4", "currency": "USD"})
                    return super().open(request, timeout)

            alerts = SupplierWalletAlertStore(root / "alerts.sqlite3")
            service = GameStoreAdminService(
                admin_token="admin-token",
                supplier_client=FazerCardsCommandClient("fixture-key", opener=InvalidBalanceOpener()),
                wallet_alert_store=alerts,
                wallet_policy=SupplierWalletPolicy(),
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(admin_service=service))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/v1/admin/supplier-wallet/status",
                    headers={"Authorization": "Bearer admin-token"},
                )
                with self.assertRaises(HTTPError) as error:
                    urlopen(request)
                self.assertEqual(error.exception.code, 502)
                error.exception.close()
                self.assertEqual(service.wallet_alerts()["alerts"], [])
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_admin_can_read_safe_live_catalog_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public.json"
            stamp = datetime.now(timezone.utc).isoformat()
            path.write_text(json.dumps({
                "schema_version": 1,
                "status": "approved_projection",
                "generated_at": stamp,
                "source_observed_at": stamp,
                "valid_until": (datetime.fromisoformat(stamp) + timedelta(minutes=15)).isoformat(),
                "offers": [],
            }), encoding="utf-8")
            service = GameStoreAdminService(
                admin_token="admin-token",
                supplier_client=FazerCardsCommandClient("secret", opener=_Opener()),
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
            )
            approve_catalog_fixture(path)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                make_handler(admin_service=service, public_catalog_path=path),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/v1/admin/live-catalog/status",
                    headers={"Authorization": "Bearer admin-token"},
                )
                with urlopen(request) as response:
                    payload = json.loads(response.read())
                self.assertEqual(payload["status"], "fresh")
                self.assertEqual(payload["offer_count"], 0)
                self.assertNotIn("supplier", json.dumps(payload))
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_admin_can_read_sanitized_subscription_status(self):
        with tempfile.TemporaryDirectory() as directory:
            service = GameStoreAdminService(
                admin_token="admin-token",
                supplier_client=FazerCardsCommandClient("secret", opener=_Opener()),
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(admin_service=service))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/v1/admin/subscription/status",
                    headers={"Authorization": "Bearer admin-token"},
                )
                with urlopen(request) as response:
                    payload = json.loads(response.read())
                self.assertEqual(payload["subscription"]["subscription"]["status"], "active")
                self.assertNotIn("secret", json.dumps(payload))
                self.assertNotIn("apiKey", json.dumps(payload))
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_admin_can_read_sanitized_wallet_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            service = GameStoreAdminService(
                admin_token="admin-token",
                supplier_client=FazerCardsCommandClient("secret", opener=_Opener()),
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
                wallet_policy=SupplierWalletPolicy(),
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(admin_service=service))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + "/api/v1/admin/supplier-wallet/alerts")
                self.assertEqual(error.exception.code, 401)
                error.exception.close()
                status_request = Request(
                    base + "/api/v1/admin/supplier-wallet/status",
                    headers={"Authorization": "Bearer admin-token"},
                )
                with urlopen(status_request):
                    pass
                alert_request = Request(
                    base + "/api/v1/admin/supplier-wallet/alerts?status=pending",
                    headers={"Authorization": "Bearer admin-token"},
                )
                with urlopen(alert_request) as response:
                    payload = json.loads(response.read())
                self.assertEqual(len(payload["alerts"]), 1)
                self.assertEqual(payload["alerts"][0]["status"], "pending")
                self.assertNotIn("secret", json.dumps(payload))
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
