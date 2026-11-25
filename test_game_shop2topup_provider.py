from __future__ import annotations

import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request

from aurix_games.providers.shop2topup_commands import (
    BASE_URL,
    Shop2TopupCommandClient,
    Shop2TopupCommandError,
    Shop2TopupWriteDisabledError,
    _NoRedirect,
)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def read(self, limit=-1):
        return json.dumps(self.payload).encode()[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Opener:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {"success": True, "account": {"enabled": True}}
        self.error = error
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if self.error:
            raise self.error
        return _Response(self.payload)


class Shop2TopupCommandTests(unittest.TestCase):
    def test_reads_use_official_https_host_and_bearer_auth(self):
        opener = _Opener({"success": True, "price": {"unit_price": "0.950000", "currency": "USD"}})
        client = Shop2TopupCommandClient("key-id.secret-value", opener=opener)
        result = client.get_price(999)
        self.assertEqual(result["price"]["unit_price"], "0.950000")
        request = opener.requests[0]
        self.assertEqual(request.full_url, f"{BASE_URL}/catalog/subcategory/999/price")
        self.assertEqual(request.get_header("Authorization"), "Bearer key-id.secret-value")
        self.assertEqual(request.get_method(), "GET")

    def test_player_validation_has_its_own_explicit_quota_gate(self):
        opener = _Opener()
        client = Shop2TopupCommandClient("key-id.secret-value", opener=opener)
        with self.assertRaises(Shop2TopupWriteDisabledError):
            client.validate_player(sub_category_id=999, player_id="123456", requirements={"server": "Asia"})
        self.assertEqual(opener.requests, [])

        enabled = Shop2TopupCommandClient("key-id.secret-value", allow_player_validation=True, opener=opener)
        enabled.validate_player(sub_category_id=999, player_id="123456", requirements={"server": "Asia"})
        self.assertEqual(json.loads(opener.requests[0].data), {
            "sub_category_id": 999, "player_id": "123456", "server": "Asia",
        })

    def test_dynamic_numeric_fields_and_request_size_are_bounded(self):
        opener = _Opener()
        client = Shop2TopupCommandClient("key-id.secret-value", allow_player_validation=True, opener=opener)
        client.validate_player(sub_category_id=999, player_id="123456", requirements={"account.region": 1.5})
        self.assertEqual(json.loads(opener.requests[0].data)["account.region"], 1.5)
        with self.assertRaises(ValueError):
            client.validate_player(sub_category_id=999, player_id="123456", requirements={
                f"field-{index}": ["x" * 128] * 32 for index in range(32)
            })
        self.assertEqual(len(opener.requests), 1)

    def test_order_write_is_disabled_before_transport(self):
        opener = _Opener()
        client = Shop2TopupCommandClient("key-id.secret-value", opener=opener)
        with self.assertRaises(Shop2TopupWriteDisabledError):
            client.create_order(order_id="01912345-6789-7abc-8def-0123456789ab",
                sub_category_id=999, quantity=1, requirements={"player_id": "123456"})
        self.assertEqual(opener.requests, [])

    def test_order_sends_stable_uuid_price_lock_and_dynamic_requirements(self):
        opener = _Opener({"success": True, "order": {
            "order_id": "01912345-6789-7abc-8def-0123456789ab", "status": "pending",
        }})
        client = Shop2TopupCommandClient("key-id.secret-value", allow_orders=True, opener=opener)
        client.create_order(order_id="01912345-6789-7abc-8def-0123456789ab",
            sub_category_id=999, quantity=1,
            requirements={"player_id": "123456789", "server": "Asia"},
            expected_unit_price="0.950000")
        request = opener.requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, f"{BASE_URL}/orders/create")
        self.assertEqual(json.loads(request.data), {
            "order_id": "01912345-6789-7abc-8def-0123456789ab",
            "sub_category_id": 999,
            "quantity": 1,
            "requirements": {"player_id": "123456789", "server": "Asia"},
            "expected_unit_price": "0.950000",
        })

    def test_ambiguous_post_failure_is_not_retried(self):
        opener = _Opener(error=TimeoutError("transport timeout"))
        client = Shop2TopupCommandClient("key-id.secret-value", allow_orders=True, opener=opener)
        order_id = "01912345-6789-7abc-8def-0123456789ab"
        with self.assertRaises(Shop2TopupCommandError) as caught:
            client.create_order(order_id=order_id, sub_category_id=999, quantity=1,
                                requirements={"player_id": "123456"})
        self.assertEqual(len(opener.requests), 1)
        self.assertIn("failed or timed out", str(caught.exception))
        self.assertNotIn("transport timeout", str(caught.exception))

    def test_http_error_exposes_only_code_status_and_bounded_retry_hint(self):
        body = b'{"success":false,"error":{"code":"RATE_LIMIT_EXCEEDED","message":"private body"}}'
        error = HTTPError("https://shop2topup.com", 429, "nope", {"Retry-After": "12"}, None)
        error.read = lambda limit=-1: body[:limit]
        opener = _Opener(error=error)
        client = Shop2TopupCommandClient("key-id.secret-value", opener=opener)
        with self.assertRaises(Shop2TopupCommandError) as caught:
            client.get_account()
        self.assertEqual(caught.exception.http_status, 429)
        self.assertEqual(caught.exception.code, "RATE_LIMIT_EXCEEDED")
        self.assertEqual(caught.exception.retry_after, 12)
        self.assertNotIn("private body", str(caught.exception))

    def test_rejects_untrusted_ids_values_and_bad_response_envelopes(self):
        opener = _Opener({"success": "true"})
        client = Shop2TopupCommandClient("key-id.secret-value", allow_orders=True, opener=opener)
        with self.assertRaises(ValueError):
            client.get_price("999/../../account")
        with self.assertRaises(ValueError):
            client.get_order("not-a-uuid")
        with self.assertRaises(Shop2TopupCommandError):
            client.get_account()
        with self.assertRaises(ValueError):
            client.create_order(order_id="01912345-6789-7abc-8def-0123456789ab",
                sub_category_id=999, quantity=1, requirements={"player_id": {"bad": "value"}})

    def test_redirect_handler_refuses_redirects(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.test"))


if __name__ == "__main__":
    unittest.main()
