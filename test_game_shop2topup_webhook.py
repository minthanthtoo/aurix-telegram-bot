from __future__ import annotations

from datetime import timedelta
import hashlib
import hmac
import json
import threading
import unittest
from urllib.request import Request, urlopen

import test_game_fulfillment as fixtures
from aurix_games.order_reconciliation import (
    Shop2TopupWebhookReconciler,
    WebhookAuthenticationError,
    WebhookReconciliationError,
    drain_provider_events,
)
from aurix_games.order_state import OrderState
from aurix_games.web_api import create_server


class Shop2TopupWebhookTests(unittest.TestCase):
    def setUp(self):
        fixtures.GameFulfillmentTests.setUp(self)
        self.addCleanup(fixtures.GameFulfillmentTests.tearDown, self)
        self.order_uuid = "7c54f59f-af6e-50d0-a678-01332036ee8b"
        self.provider_receipt = "s2t-" + self.order_uuid.replace("-", "")
        self.orders.transition("order-1", OrderState.FULFILLMENT_QUEUED,
            at=fixtures.NOW, reason="queue for supplier")
        self.orders.transition("order-1", OrderState.SUBMITTED,
            at=fixtures.NOW, reason="supplier accepted", provider_order_id=self.provider_receipt,
            provider_status="pending")
        self.secret = "test-webhook-secret"
        self.receiver = Shop2TopupWebhookReconciler(self.orders, webhook_secret=self.secret)

    def _signed(self, payload):
        raw = json.dumps(payload, separators=(",", ":")).encode()
        signature = "sha256=" + hmac.new(self.secret.encode(), raw, hashlib.sha256).hexdigest()
        return raw, signature

    def test_signed_order_callback_is_queued_and_applied_once(self):
        payload = {
            "event": "order.completed",
            "timestamp": fixtures.NOW.isoformat(),
            "data": {"order_id": self.order_uuid, "status": "completed"},
        }
        raw, signature = self._signed(payload)
        accepted = self.receiver.receive(raw, signature, "order.completed", now=fixtures.NOW)
        self.assertEqual(accepted.order_id, self.provider_receipt)
        queued = drain_provider_events(self.orders, now=fixtures.NOW, limit=10)
        self.assertEqual(len(queued), 1)
        self.assertTrue(queued[0].applied)
        self.assertEqual(self.orders.get("order-1").state, OrderState.DELIVERED)

        self.receiver.receive(raw, signature, "order.completed", now=fixtures.NOW + timedelta(seconds=1))
        self.assertEqual(drain_provider_events(
            self.orders, now=fixtures.NOW + timedelta(seconds=1), limit=10), ())
        self.assertEqual(self.orders.get("order-1").state, OrderState.DELIVERED)

    def test_signature_and_event_header_are_verified_before_queueing(self):
        payload = {
            "event": "order.refunded",
            "timestamp": fixtures.NOW.isoformat(),
            "data": {"order_id": self.order_uuid, "status": "refunded"},
        }
        raw, signature = self._signed(payload)
        with self.assertRaises(WebhookAuthenticationError):
            self.receiver.receive(raw, "sha256=" + "0" * 64, "order.refunded", now=fixtures.NOW)
        with self.assertRaises(WebhookReconciliationError):
            self.receiver.receive(raw, signature, "order.completed", now=fixtures.NOW)
        self.assertEqual(drain_provider_events(self.orders, now=fixtures.NOW, limit=10), ())

    def test_signed_panel_test_event_is_accepted_without_order_write(self):
        raw, signature = self._signed({
            "event": "webhook.test", "timestamp": fixtures.NOW.isoformat(), "data": {},
        })
        event = self.receiver.receive(raw, signature, "webhook.test", now=fixtures.NOW)
        self.assertIsNone(event.order_id)
        self.assertEqual(drain_provider_events(self.orders, now=fixtures.NOW, limit=10), ())

    def test_status_poll_interval_is_enforced_atomically(self):
        self.assertTrue(self.orders.claim_poll_check("order-1", now=fixtures.NOW))
        self.assertFalse(self.orders.claim_poll_check(
            "order-1", now=fixtures.NOW + timedelta(seconds=19),
        ))
        self.assertTrue(self.orders.claim_poll_check(
            "order-1", now=fixtures.NOW + timedelta(seconds=20),
        ))

    def test_http_endpoint_accepts_signed_test_callback(self):
        receiver = Shop2TopupWebhookReconciler(self.orders, webhook_secret=self.secret)
        server = create_server(
            "127.0.0.1", 0, shop2topup_webhook_reconciler=receiver,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        raw, signature = self._signed({
            "event": "webhook.test", "timestamp": fixtures.NOW.isoformat(), "data": {},
        })
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/v1/webhooks/shop2topup",
            data=raw,
            headers={
                "Content-Type": "application/json",
                "X-Shop2Topup-Signature": signature,
                "X-Shop2Topup-Event": "webhook.test",
            },
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
            result = json.loads(response.read())
        self.assertTrue(result["accepted"])


if __name__ == "__main__":
    unittest.main()
