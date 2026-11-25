from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from aurix_games.customer_api import (
    CustomerAuthError,
    CustomerOrderService,
    CustomerWalletService,
    SignedCustomerTokenVerifier,
    build_env_customer_api,
    issue_test_token,
)
from aurix_games.customer_ledger import CustomerLedger
from aurix_games.order_repository import GameOrderRepository
from aurix_games.order_state import OrderSnapshot, OrderState, create_order
from aurix_games.web_api import make_handler


UTC = timezone.utc
NOW = datetime(2026, 9, 22, 18, 0, tzinfo=UTC)
SECRET = "customer-secret-012345678901234567890123"


def _create_order(repository: GameOrderRepository, order_id: str, customer_id: str) -> None:
    order = create_order(
        order_id=order_id,
        customer_id=customer_id,
        customer_idempotency_key=f"key-{order_id}",
        snapshot=OrderSnapshot(
            offer_id="offer-1", product_slug="mobile-legends", fulfillment_kind="direct_topup",
            amount_minor=4500, currency="MMK", recipient_fingerprint="opaque-fp",
        ),
    )
    repository.create(order, created_at=NOW)
    repository.transition(order_id, OrderState.AWAITING_PAYMENT, at=NOW, reason="await payment")


class CustomerApiTests(unittest.TestCase):
    def test_signed_principal_rejects_tampering_and_expired_tokens(self):
        verifier = SignedCustomerTokenVerifier(SECRET)
        token = issue_test_token("customer-1", SECRET, expires_at=NOW + timedelta(hours=1))
        principal = verifier.verify(f"Bearer {token}", now=NOW)
        self.assertEqual(principal.customer_id, "customer-1")
        with self.assertRaises(CustomerAuthError):
            verifier.verify(f"Bearer {token[:-1]}x", now=NOW)
        expired = issue_test_token("customer-1", SECRET, expires_at=NOW - timedelta(seconds=1))
        with self.assertRaises(CustomerAuthError):
            verifier.verify(f"Bearer {expired}", now=NOW)

    def test_http_order_history_is_owner_scoped_and_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = GameOrderRepository(Path(directory) / "orders.sqlite3")
            _create_order(repository, "order-1", "customer-1")
            _create_order(repository, "order-2", "customer-2")
            repository.freeze_payment_intent(order_id="order-1", customer_id="customer-1", rail="kpay",
                receiver_id="merchant-1", receiver_revision="v1", opened_at=NOW,
                expires_at=NOW + timedelta(minutes=10), instructions={"account": "091234", "account_name": "AuriX Shop"})
            verifier = SignedCustomerTokenVerifier(SECRET)
            service = CustomerOrderService(repository)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                make_handler(customer_order_service=service, customer_auth=verifier),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            # The HTTP handler verifies against the real clock. Keep this
            # integration token relative to the run time so the fixture does
            # not silently expire when the calendar advances.
            token = issue_test_token(
                "customer-1",
                SECRET,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + "/api/v1/orders")
                self.assertEqual(error.exception.code, 401)
                error.exception.close()
                request = Request(base + "/api/v1/orders", headers={"Authorization": f"Bearer {token}"})
                with urlopen(request) as response:
                    listing = json.loads(response.read())
                self.assertEqual([item["order_id"] for item in listing["orders"]], ["order-1"])
                self.assertNotIn("recipient_fingerprint", json.dumps(listing))
                detail = Request(base + "/api/v1/orders/order-1", headers={"Authorization": f"Bearer {token}"})
                with urlopen(detail) as response:
                    payload = json.loads(response.read())
                self.assertEqual(payload["order_id"], "order-1")
                self.assertTrue(payload["payment"]["instructions_unavailable"])
                self.assertNotIn("account", payload["payment"])
                self.assertNotIn("account_name", payload["payment"])
                self.assertEqual(payload["payment"]["rail"], "kpay")
                self.assertNotIn("merchant-1", json.dumps(payload))
                foreign = Request(base + "/api/v1/orders/order-2", headers={"Authorization": f"Bearer {token}"})
                with self.assertRaises(HTTPError) as error:
                    urlopen(foreign)
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_environment_builder_uses_games_base_for_order_history_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "persistent" / "fazercards"
            with patch.dict(
                os.environ,
                {
                    "AURIX_GAMES_BASE_DIR": str(base),
                    "AURIX_GAMES_CUSTOMER_AUTH_SECRET": SECRET,
                    "AURIX_GAMES_CUSTOMER_AUTH_ISSUER": "aurix-identity",
                },
                clear=False,
            ):
                config = build_env_customer_api()
            self.assertIsNotNone(config)
            assert config is not None
            self.assertEqual(config.service.repository.path, base / "game-orders.sqlite3")
            self.assertEqual(config.wallet_service.ledger.path, base / "game-ledger.sqlite3")

    def test_http_wallet_and_top_up_history_are_owner_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = CustomerLedger(Path(directory) / "ledger.sqlite3")
            ledger.credit(
                customer_id="customer-1", amount_minor=1000, currency="MMK",
                event_key="payment-1", actor="staff:1", reason="verified transfer",
                occurred_at=NOW,
            )
            ledger.credit(
                customer_id="customer-2", amount_minor=2000, currency="MMK",
                event_key="payment-2", actor="staff:2", reason="verified transfer",
                occurred_at=NOW,
            )
            verifier = SignedCustomerTokenVerifier(SECRET)
            wallet = CustomerWalletService(ledger)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                make_handler(customer_wallet_service=wallet, customer_auth=verifier),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            token = issue_test_token(
                "customer-1",
                SECRET,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                wallet_request = Request(base + "/api/v1/wallet", headers={"Authorization": f"Bearer {token}"})
                with urlopen(wallet_request) as response:
                    wallet_payload = json.loads(response.read())
                self.assertEqual(wallet_payload["wallet"]["available_minor"], 1000)
                self.assertNotIn("customer_id", json.dumps(wallet_payload))
                history_request = Request(base + "/api/v1/top-up-history", headers={"Authorization": f"Bearer {token}"})
                with urlopen(history_request) as response:
                    history_payload = json.loads(response.read())
                self.assertEqual([entry["amount_minor"] for entry in history_payload["entries"]], [1000])
                self.assertNotIn("event_key", json.dumps(history_payload))
                self.assertNotIn("customer-2", json.dumps(history_payload))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
