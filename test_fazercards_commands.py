from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request

from aurix_games.providers.fazercards_commands import (
    FazerCardsCommandClient,
    SupplierCommandError,
    SupplierWriteDisabledError,
    parse_webhook_event,
    verify_webhook_signature,
)


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def read(self, limit=-1):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Opener:
    def __init__(self, payload=None):
        self.payload = payload or {"ok": True, "order": {"id": "ord-1", "status": "processing"}}
        self.requests: list[Request] = []

    def open(self, request, timeout):
        self.requests.append(request)
        return _Response(self.payload)


class FazerCardsCommandTests(unittest.TestCase):
    def test_writes_are_disabled_by_default_before_transport(self):
        opener = _Opener()
        client = FazerCardsCommandClient("secret", opener=opener)
        with self.assertRaises(SupplierWriteDisabledError):
            client.order_topup(
                category_id="cat",
                offer_id="offer",
                fields={"player_id": "123"},
                idempotency_key="idem-1",
            )
        self.assertEqual(opener.requests, [])

    def test_topup_order_is_json_and_carries_idempotency(self):
        opener = _Opener()
        client = FazerCardsCommandClient("secret", allow_writes=True, opener=opener)
        result = client.order_topup(
            category_id="cat",
            offer_id="offer",
            fields={"player_id": "123", "zone_id": "7"},
            idempotency_key="idem-1",
        )
        self.assertEqual(result["order"]["id"], "ord-1")
        request = opener.requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://api.fzr.cards/api/v2/topups/order")
        self.assertEqual(request.get_header("Idempotency-key"), "idem-1")
        self.assertEqual(
            json.loads(request.data),
            {"category_id": "cat", "offer_id": "offer", "fields": {"player_id": "123", "zone_id": "7"}},
        )

    def test_all_game_order_builders_use_the_documented_paths(self):
        opener = _Opener()
        client = FazerCardsCommandClient("secret", allow_writes=True, opener=opener)
        client.validate_topup_recipient("cat", {"player_id": "123"})
        client.order_gift_card(
            category_id="cat", card_id="card", quantity=1, idempotency_key="gift-1"
        )
        client.order_game_key(
            game_id="game", key_id="key", quantity=1, idempotency_key="key-1"
        )
        client.order_steam_topup(
            steam_login="login", currency="usd", amount="5", idempotency_key="steam-1"
        )
        client.order_steam_gift(
            invite_url="https://steam.example/invite",
            sub_id=10,
            app_id=730,
            region="US",
            idempotency_key="gift-steam-1",
        )
        client.buy_telegram_stars(telegram_username="@player", quantity=50)
        client.buy_telegram_premium(telegram_username="@player", months=3)
        client.order_manual_service(
            manual_service_id="service",
            product_id="product",
            fields={"note": "hello"},
            idempotency_key="manual-1",
        )
        client.get_manual_service_chat("ord-1")
        client.send_manual_service_chat(
            "ord-1",
            body="hello\nworld",
            image=b"\x89PNG",
            image_filename="proof.png",
            image_content_type="image/png",
        )
        self.assertEqual(
            [request.full_url for request in opener.requests],
            [
                "https://api.fzr.cards/api/v2/topups/validate-id",
                "https://api.fzr.cards/api/v2/giftcards/order",
                "https://api.fzr.cards/api/v2/gamekeys/order",
                "https://api.fzr.cards/api/v2/steam-topup/order",
                "https://api.fzr.cards/api/v2/steam-gifts/order",
                "https://api.fzr.cards/api/v2/telegram/stars/buy",
                "https://api.fzr.cards/api/v2/telegram/premium/buy",
                "https://api.fzr.cards/api/v2/manual-services/order",
                "https://api.fzr.cards/api/v2/manual-services/orders/ord-1/chat",
                "https://api.fzr.cards/api/v2/manual-services/orders/ord-1/chat",
            ],
        )
        manual_service_request = opener.requests[7]
        self.assertEqual(
            json.loads(manual_service_request.data),
            {
                "manual_service_id": "service",
                "product_id": "product",
                "fields": {"note": "hello"},
            },
        )
        self.assertEqual(
            manual_service_request.get_header("Idempotency-key"), "manual-1"
        )
        request = opener.requests[-1]
        content_type = request.get_header("Content-type")
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        boundary = content_type.split("boundary=", 1)[1]
        payload = request.data
        self.assertIn(b'name="body"', payload)
        self.assertIn(b"hello\nworld", payload)
        self.assertIn(b'name="image"; filename="proof.png"', payload)
        self.assertIn(b"Content-Type: image/png", payload)
        self.assertIn(f"--{boundary}--".encode("ascii"), payload)

    def test_manual_chat_write_requires_explicit_gate_and_content(self):
        opener = _Opener()
        client = FazerCardsCommandClient("secret", opener=opener)
        with self.assertRaises(SupplierWriteDisabledError):
            client.send_manual_service_chat("ord-1", body="hello")
        self.assertEqual(opener.requests, [])
        enabled = FazerCardsCommandClient("secret", allow_writes=True, opener=_Opener())
        with self.assertRaises(ValueError):
            enabled.send_manual_service_chat("ord-1")
        with self.assertRaises(ValueError):
            enabled.send_manual_service_chat("ord-1", image=b"x", image_filename='bad"name')

    def test_supplier_wallet_invoice_uses_current_usdt_method(self):
        opener = _Opener({"ok": True, "payment": {"id": "tx1", "paymentUrl": "https://pay.fzr.cards/tx1"}})
        client = FazerCardsCommandClient("secret", allow_writes=True, opener=opener)

        result = client.create_balance_payment(method="usdt", amount_usd="50")

        self.assertEqual(result["payment"]["paymentUrl"], "https://pay.fzr.cards/tx1")
        self.assertEqual(json.loads(opener.requests[0].data), {"method": "usdt", "amount": 50})
        for retired in ("trc20", "bep20", "ton", "aptos", "binancepay"):
            with self.assertRaises(ValueError):
                client.create_balance_payment(method=retired, amount_usd=50)
        self.assertEqual(len(opener.requests), 1)

    def test_account_payment_and_webhook_commands_use_documented_paths(self):
        opener = _Opener({"ok": True, "payment": {"id": "tx1", "status": "pending"}})
        client = FazerCardsCommandClient("secret", allow_writes=True, opener=opener)
        client.get_me()
        client.get_balance()
        client.list_transactions(page=1, limit=10)
        client.get_transaction("tx1")
        client.list_orders(page=1, limit=10)
        client.get_payment_methods()
        client.get_payment("tx1")
        client.get_subscription()
        client.list_subscription_plans()
        client.get_subscription_activation_quote("gold", promo_code="trial-10")
        client.activate_subscription_from_balance("gold", promo_code="trial-10")
        client.set_subscription_auto_renew(True)
        client.change_subscription_plan("silver")
        client.check_steam_login("steam-login")
        client.get_webhook()
        client.list_webhook_deliveries()
        client.create_balance_payment(method="usdt", amount_usd="50")
        client.verify_binance_payment("tx1", binance_order_id="binance-1")
        client.configure_webhook(url="https://games.example.test/webhooks/fz", enabled=True)
        client.delete_webhook()
        client.regenerate_webhook_secret()
        client.test_webhook()
        client.login_funpay(email="owner@example.test", password="not-persisted")
        client.get_funpay_me()
        self.assertEqual(
            [request.full_url for request in opener.requests],
            [
                "https://api.fzr.cards/api/v2/me",
                "https://api.fzr.cards/api/v2/balance",
                "https://api.fzr.cards/api/v2/transactions?page=1&limit=10",
                "https://api.fzr.cards/api/v2/transactions/tx1",
                "https://api.fzr.cards/api/v2/orders?page=1&limit=10",
                "https://api.fzr.cards/api/v2/payments/methods",
                "https://api.fzr.cards/api/v2/payments/tx1",
                "https://api.fzr.cards/api/v2/subscription",
                "https://api.fzr.cards/api/v2/subscription/plans",
                "https://api.fzr.cards/api/v2/subscription/activation-quote?plan=gold&promo_code=trial-10",
                "https://api.fzr.cards/api/v2/subscription/activate-from-balance",
                "https://api.fzr.cards/api/v2/subscription/auto-renew",
                "https://api.fzr.cards/api/v2/subscription/plan",
                "https://api.fzr.cards/api/v2/steam-topup/check-login",
                "https://api.fzr.cards/api/v2/account/webhook",
                "https://api.fzr.cards/api/v2/account/webhook/deliveries",
                "https://api.fzr.cards/api/v2/payments/create",
                "https://api.fzr.cards/api/v2/payments/tx1/verify",
                "https://api.fzr.cards/api/v2/account/webhook",
                "https://api.fzr.cards/api/v2/account/webhook",
                "https://api.fzr.cards/api/v2/account/webhook/secret/regenerate",
                "https://api.fzr.cards/api/v2/account/webhook/test",
                "https://api.fzr.cards/api/v2/funpay/login",
                "https://api.fzr.cards/api/v2/funpay/me",
            ],
        )
        self.assertEqual(json.loads(opener.requests[10].data), {"plan": "gold", "promo_code": "trial-10"})
        self.assertEqual(json.loads(opener.requests[11].data), {"planAutoRenew": True})
        self.assertEqual(json.loads(opener.requests[12].data), {"plan": "silver"})
        self.assertEqual(json.loads(opener.requests[13].data), {"steamLogin": "steam-login"})
        self.assertEqual(opener.requests[11].get_method(), "PATCH")
        self.assertEqual(opener.requests[12].get_method(), "PATCH")

    def test_get_order_is_available_without_write_enablement(self):
        opener = _Opener()
        client = FazerCardsCommandClient("secret", opener=opener)
        client.get_order("ord-123")
        self.assertEqual(opener.requests[0].get_method(), "GET")
        self.assertEqual(opener.requests[0].full_url, "https://api.fzr.cards/api/v2/orders/ord-123")

    def test_command_validation_rejects_bad_ids_and_unsafe_values(self):
        client = FazerCardsCommandClient("secret", allow_writes=True, opener=_Opener())
        with self.assertRaises(ValueError):
            client.order_game_key(game_id="", key_id="key", quantity=1, idempotency_key="x")
        with self.assertRaises(ValueError):
            client.order_gift_card(
                category_id="cat", card_id="card", quantity=0, idempotency_key="x"
            )
        with self.assertRaises(ValueError):
            client.order_topup(
                category_id="cat",
                offer_id="offer",
                fields={"player_id": {"raw": "secret"}},
                idempotency_key="x",
            )
        with self.assertRaises(ValueError):
            client.order_steam_topup(
                steam_login="login", currency="USD", amount="NaN", idempotency_key="x"
            )
        with self.assertRaises(ValueError):
            client.get_subscription_activation_quote("platinum")
        with self.assertRaises(ValueError):
            client.set_subscription_auto_renew("yes")
        with self.assertRaises(ValueError):
            client.change_subscription_plan("platinum")
        with self.assertRaises(ValueError):
            client.check_steam_login("")
        with self.assertRaises(ValueError):
            client.buy_telegram_stars(telegram_username="@player", quantity=49)
        with self.assertRaises(ValueError):
            client.buy_telegram_premium(telegram_username="@player", months=1)
        with self.assertRaises(ValueError):
            client.login_funpay(email="", password="password")

    def test_order_payload_types_and_quantity_bounds_match_public_openapi(self):
        client = FazerCardsCommandClient("secret", allow_writes=True, opener=_Opener())
        for invalid_value in (7, True, 7.0, None, {"nested": "value"}, ["value"]):
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaises(ValueError):
                    client.order_topup(
                        category_id="cat",
                        offer_id="offer",
                        fields={"player_id": invalid_value},
                        idempotency_key="idem-type-check",
                    )

        gift_client = FazerCardsCommandClient("secret", allow_writes=True, opener=_Opener())
        gift_client.order_gift_card(
            category_id="cat", card_id="card", quantity=100, idempotency_key="gift-max"
        )
        with self.assertRaises(ValueError):
            gift_client.order_gift_card(
                category_id="cat", card_id="card", quantity=101, idempotency_key="gift-over-max"
            )

        key_client = FazerCardsCommandClient("secret", allow_writes=True, opener=_Opener())
        key_client.order_game_key(
            game_id="game", key_id="key", quantity=100, idempotency_key="key-max"
        )
        with self.assertRaises(ValueError):
            key_client.order_game_key(
                game_id="game", key_id="key", quantity=101, idempotency_key="key-over-max"
            )

    def test_provider_error_does_not_leak_key_or_body(self):
        class _FailingOpener:
            def open(self, request, timeout):
                raise OSError("private provider body and secret")

        client = FazerCardsCommandClient("secret", opener=_FailingOpener())
        with self.assertRaises(SupplierCommandError) as context:
            client.get_order("ord-1")
        self.assertNotIn("secret", str(context.exception))
        self.assertNotIn("private provider body", str(context.exception))

    def test_http_error_is_sanitized_without_unbound_response_body(self):
        class _HttpFailingOpener:
            def open(self, request, timeout):
                raise HTTPError(request.full_url, 403, "provider body", {}, None)

        client = FazerCardsCommandClient("secret", opener=_HttpFailingOpener())
        with self.assertRaises(SupplierCommandError) as context:
            client.get_balance()
        self.assertEqual(str(context.exception), "supplier GET /balance returned HTTP 403")
        self.assertEqual(context.exception.http_status, 403)

    def test_webhook_signature_is_constant_time_compatible_and_exact(self):
        body = b'{"event":"order.status_changed"}'
        secret = "webhook-secret"
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_webhook_signature(body, signature, secret))
        self.assertFalse(verify_webhook_signature(body + b"x", signature, secret))
        self.assertFalse(verify_webhook_signature(body, "bad", secret))

    def test_webhook_parser_keeps_only_reconciliation_metadata(self):
        body = json.dumps(
            {
                "event": "order.status_changed",
                "event_id": "event-1",
                "timestamp": "2026-09-22T00:00:00Z",
                "data": {
                    "order_id": "ord-12",
                    "type": "topup",
                    "status": "completed",
                    "previous_status": "processing",
                    "redeem_code": "must-not-be-copied",
                },
            }
        ).encode()
        event = parse_webhook_event(body)
        self.assertEqual(event.order_id, "ord-12")
        self.assertEqual(event.status, "completed")
        self.assertFalse(hasattr(event, "redeem_code"))

    def test_webhook_parser_rejects_missing_order_identity(self):
        body = b'{"event":"order.status_changed","event_id":"event-1","timestamp":"now","data":{}}'
        with self.assertRaises(ValueError):
            parse_webhook_event(body)

    def test_webhook_test_event_can_be_acknowledged_without_order_identity(self):
        body = b'{"event":"webhook.test","event_id":"event-1","timestamp":"now","data":{}}'
        event = parse_webhook_event(body)
        self.assertIsNone(event.order_id)


if __name__ == "__main__":
    unittest.main()
