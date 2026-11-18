from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from aurix_games.customer_api import (
    CustomerIdentityService,
    CustomerIdentityStore,
    CustomerOrderService,
    CustomerWalletService,
    SESSION_COOKIE_NAME,
    SignedCustomerTokenVerifier,
)
from aurix_games.customer_ledger import CustomerLedger
from aurix_games.order_repository import GameOrderRepository
from aurix_games.order_state import OrderSnapshot, create_order
from aurix_games.web_api import create_server


SECRET = "customer-auth-http-test-secret-0123456789"
BOT_TOKEN = "123456:telegram-http-test-token"
GOOGLE_CLIENT_ID = "games-client-id.apps.googleusercontent.com"


def _telegram_login(user_id: int, *, first_name: str = "Test") -> dict[str, str]:
    values = {"auth_date": str(int(time.time())), "first_name": first_name, "id": str(user_id)}
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    key = hashlib.sha256(BOT_TOKEN.encode()).digest()
    values["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return values


class CustomerAuthHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.claims: dict[str, dict[str, str]] = {}
        cls.identity_service = CustomerIdentityService(
            SECRET,
            CustomerIdentityStore(Path(cls.temp.name) / "identities.sqlite3"),
            telegram_bot_token=BOT_TOKEN,
            telegram_bot_username="aurix_games_bot",
            google_client_id=GOOGLE_CLIENT_ID,
            google_token_verifier=lambda credential, client_id: cls.claims[credential],
            cookie_secure=False,
        )
        cls.ledger = CustomerLedger(Path(cls.temp.name) / "ledger.sqlite3")
        cls.orders = GameOrderRepository(Path(cls.temp.name) / "orders.sqlite3")
        cls.server = create_server(
            "127.0.0.1",
            0,
            customer_auth=SignedCustomerTokenVerifier(SECRET),
            customer_identity_service=cls.identity_service,
            customer_wallet_service=CustomerWalletService(cls.ledger),
            customer_order_service=CustomerOrderService(cls.orders, cls.ledger),
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.origin = cls.base_url

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def request(self, path: str, *, method: str = "GET", payload=None, headers=None):
        request_headers = dict(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            request_headers.setdefault("Content-Type", "application/json")
        request = Request(self.base_url + path, data=body, method=method, headers=request_headers)
        try:
            with urlopen(request) as response:
                return response.status, response.headers, json.loads(response.read())
        except HTTPError as error:
            with error:
                return error.code, error.headers, json.loads(error.read())

    def post_headers(self, **extra):
        return {"Origin": self.origin, **extra}

    def google_token(self, subject: str, nonce: str) -> str:
        credential = f"fake-google-token-{subject}-{len(self.claims)}"
        self.claims[credential] = {
            "iss": "https://accounts.google.com",
            "nonce": nonce,
            "sub": subject,
        }
        return credential

    def google_challenge(self) -> str:
        status, _, payload = self.request(
            "/api/v1/auth/google/challenge",
            method="POST",
            payload={},
            headers=self.post_headers(),
        )
        self.assertEqual(status, 200)
        return payload["nonce"]

    def test_google_session_cookie_wallet_and_explicit_telegram_link(self):
        status, headers, config = self.request("/api/v1/auth/config")
        self.assertEqual(status, 200)
        self.assertTrue(config["providers"]["telegram"]["enabled"])
        self.assertTrue(config["providers"]["google"]["enabled"])
        self.assertEqual(config["providers"]["google"]["client_id"], GOOGLE_CLIENT_ID)
        self.assertNotIn(BOT_TOKEN, json.dumps(config))
        self.assertNotIn(SECRET, json.dumps(config))
        self.assertIn("https://accounts.google.com/gsi/client", headers["Content-Security-Policy"])
        self.assertIn("https://oauth.telegram.org", headers["Content-Security-Policy"])
        self.assertIn("'sha256-Cmx01mi2Nog0fYVZmwQe0PjoTDJiF9gQYpWwr/UEVMI='", headers["Content-Security-Policy"])
        self.assertNotIn("'unsafe-inline'", headers["Content-Security-Policy"])

        status, _, session = self.request("/api/v1/auth/session")
        self.assertEqual((status, session["authenticated"]), (200, False))
        status, _, _ = self.request(
            "/api/v1/auth/google/challenge", method="POST", payload={}, headers={}
        )
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "/api/v1/auth/google/challenge",
            method="POST",
            payload={},
            headers={"Origin": "https://attacker.example"},
        )
        self.assertEqual(status, 403)

        nonce = self.google_challenge()
        credential = self.google_token("google-customer-1", nonce)
        status, response_headers, signed_in = self.request(
            "/api/v1/auth/google",
            method="POST",
            payload={"credential": credential, "nonce": nonce},
            headers=self.post_headers(),
        )
        self.assertEqual(status, 200)
        self.assertTrue(signed_in["authenticated"])
        self.assertEqual(signed_in["user"]["providers"], ["google"])
        self.assertNotIn("token", json.dumps(signed_in).lower())
        cookie = SimpleCookie()
        cookie.load(response_headers["Set-Cookie"])
        self.assertIn(SESSION_COOKIE_NAME, cookie)
        self.assertTrue(cookie[SESSION_COOKIE_NAME]["httponly"])
        self.assertEqual(cookie[SESSION_COOKIE_NAME]["samesite"], "Lax")
        cookie_header = f"{SESSION_COOKIE_NAME}={cookie[SESSION_COOKIE_NAME].value}"

        status, _, session = self.request("/api/v1/auth/session", headers={"Cookie": cookie_header})
        self.assertEqual(status, 200)
        self.assertTrue(session["authenticated"])
        self.assertEqual(session["user"]["providers"], ["google"])
        status, _, wallet = self.request("/api/v1/wallet", headers={"Cookie": cookie_header})
        self.assertEqual(status, 200)
        self.assertEqual(wallet["wallet"]["available_minor"], 0)
        owner = signed_in["user"]["customer_id"]
        now = datetime.now(timezone.utc)
        self.ledger.credit(customer_id=owner, amount_minor=2500, currency="MMK",
                           event_key="linked-wallet-credit", actor="staff:test",
                           reason="fixture", occurred_at=now)
        self.orders.create(create_order(
            order_id="linked-history-order", customer_id=owner, customer_idempotency_key="fixture-order",
            snapshot=OrderSnapshot(offer_id="offer-1", product_slug="mobile-legends",
                                   fulfillment_kind="direct_topup", amount_minor=1000, currency="MMK",
                                   recipient_fingerprint="fixture-recipient"),
        ), created_at=now)

        proof = _telegram_login(4321)
        status, _, _ = self.request(
            "/api/v1/auth/link/telegram", method="POST",
            payload={"customer_id": "another-customer", "proof": proof},
            headers=self.post_headers(Cookie=cookie_header),
        )
        self.assertEqual(status, 409)
        status, _, linked = self.request(
            "/api/v1/auth/link/telegram",
            method="POST",
            payload={"customer_id": owner, "proof": proof},
            headers=self.post_headers(Cookie=cookie_header),
        )
        self.assertEqual(status, 200)
        self.assertEqual(linked["user"]["providers"], ["google", "telegram"])
        # A fresh signed payload stands in for a new login in another browser.
        proof = _telegram_login(4321, first_name="Test again")
        status, headers, telegram = self.request(
            "/api/v1/auth/telegram", method="POST", payload=proof, headers=self.post_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(telegram["user"]["customer_id"], owner)
        telegram_headers = {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}
        self.assertEqual(self.request("/api/v1/wallet", headers=telegram_headers)[2]["wallet"]["available_minor"], 2500)
        self.assertEqual(self.request("/api/v1/orders", headers=telegram_headers)[2]["orders"][0]["order_id"],
                         "linked-history-order")

        nonce = self.google_challenge()
        credential = self.google_token("google-customer-2", nonce)
        status, _, other_login = self.request(
            "/api/v1/auth/google",
            method="POST",
            payload={"credential": credential, "nonce": nonce},
            headers=self.post_headers(),
        )
        self.assertEqual(status, 200)
        other_customer = other_login["user"]["customer_id"]
        nonce = self.google_challenge()
        credential = self.google_token("google-customer-2", nonce)
        status, _, conflict = self.request(
            "/api/v1/auth/link/google",
            method="POST",
            payload={"customer_id": owner, "proof": {"credential": credential, "nonce": nonce}},
            headers=self.post_headers(Cookie=cookie_header),
        )
        self.assertEqual(status, 409)
        self.assertIn("linked to another account", conflict["error"])
        self.assertNotEqual(other_customer, signed_in["user"]["customer_id"])

        status, logout_headers, _ = self.request(
            "/api/v1/auth/logout", method="POST", payload={}, headers=self.post_headers(Cookie=cookie_header)
        )
        self.assertEqual(status, 200)
        cleared = SimpleCookie()
        cleared.load(logout_headers["Set-Cookie"])
        self.assertEqual(cleared[SESSION_COOKIE_NAME]["max-age"], "0")
        status, _, _ = self.request("/api/v1/wallet", headers={"Cookie": cookie_header})
        self.assertEqual(status, 401, "A copied cookie must stop working after logout")
        status, _, _ = self.request("/api/v1/wallet", headers={"Authorization": f"Bearer {cookie[SESSION_COOKIE_NAME].value}"})
        self.assertEqual(status, 401, "Revoked sessions cannot bypass logout via bearer auth")
        status, _, session = self.request(
            "/api/v1/auth/session", headers={"Cookie": f"{SESSION_COOKIE_NAME}="}
        )
        self.assertEqual((status, session["authenticated"]), (200, False))

    def test_logout_storage_failure_does_not_claim_success(self):
        with patch.object(self.identity_service, "logout", side_effect=RuntimeError("private database path")):
            status, headers, payload = self.request(
                "/api/v1/auth/logout", method="POST", payload={}, headers=self.post_headers()
            )
        self.assertEqual(status, 503)
        self.assertIsNone(headers.get("Set-Cookie"))
        self.assertNotIn("private database", json.dumps(payload))

    def test_session_storage_outage_fails_closed_without_exposing_details(self):
        nonce = self.google_challenge()
        status, headers, _ = self.request(
            "/api/v1/auth/google", method="POST",
            payload={"credential": self.google_token("outage-player", nonce), "nonce": nonce},
            headers=self.post_headers(),
        )
        self.assertEqual(status, 200)
        cookie = SimpleCookie()
        cookie.load(headers["Set-Cookie"])
        request_headers = {"Cookie": f"{SESSION_COOKIE_NAME}={cookie[SESSION_COOKIE_NAME].value}"}
        with patch.object(self.identity_service.identity_store, "session_active", side_effect=RuntimeError("private storage detail")):
            status, _, payload = self.request("/api/v1/auth/session", headers=request_headers)
            self.assertEqual(status, 503)
            self.assertNotIn("private storage", json.dumps(payload))
            status, _, payload = self.request("/api/v1/wallet", headers=request_headers)
            self.assertEqual(status, 500)
            self.assertNotIn("wallet", payload)

    def test_invalid_cookie_cannot_read_private_wallet(self):
        status, _, _ = self.request("/api/v1/wallet", headers={"Cookie": f"{SESSION_COOKIE_NAME}=tampered"})
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
