import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

from telegram_web_app import TelegramWebAppAuthError, verify_init_data, verify_login_widget


def _init_data(token: str, *, auth_date: int | None = None) -> str:
    values = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AA-test-query",
        "user": json.dumps(
            {"id": 12345, "first_name": "Auri", "username": "aurix_user"},
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def _login_widget_data(token: str, *, auth_date: int | None = None) -> dict[str, object]:
    values: dict[str, object] = {
        "auth_date": auth_date or int(time.time()),
        "first_name": "Auri",
        "id": 12345,
        "username": "aurix_user",
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hashlib.sha256(token.encode()).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return values


class TelegramWebAppAuthTest(unittest.TestCase):
    def test_valid_signed_payload_returns_user(self):
        user = verify_init_data(_init_data("bot-token"), "bot-token")
        self.assertEqual(user.telegram_id, 12345)
        self.assertEqual(user.username, "aurix_user")

    def test_tampered_payload_is_rejected(self):
        payload = _init_data("bot-token").replace("Auri", "Evil")
        with self.assertRaisesRegex(TelegramWebAppAuthError, "signature"):
            verify_init_data(payload, "bot-token")

    def test_expired_payload_is_rejected(self):
        payload = _init_data("bot-token", auth_date=1_000)
        with self.assertRaisesRegex(TelegramWebAppAuthError, "expired"):
            verify_init_data(payload, "bot-token", now=100_000, max_age_seconds=3600)

    def test_duplicate_fields_are_rejected(self):
        payload = _init_data("bot-token") + "&auth_date=1"
        with self.assertRaisesRegex(TelegramWebAppAuthError, "duplicate"):
            verify_init_data(payload, "bot-token")

    def test_valid_login_widget_payload_returns_user(self):
        user = verify_login_widget(_login_widget_data("bot-token"), "bot-token")
        self.assertEqual(user.telegram_id, 12345)
        self.assertEqual(user.username, "aurix_user")

    def test_login_widget_tampering_is_rejected(self):
        payload = _login_widget_data("bot-token")
        payload["first_name"] = "Evil"
        with self.assertRaisesRegex(TelegramWebAppAuthError, "signature"):
            verify_login_widget(payload, "bot-token")


if __name__ == "__main__":
    unittest.main()
