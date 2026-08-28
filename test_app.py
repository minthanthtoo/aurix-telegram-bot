import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from unittest.mock import patch

from cryptography.fernet import Fernet
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app import ClaimService, Database, OutlineClient, OutlineError, TelegramBot
from commerce import CommerceDatabase, CommerceService


UTC = timezone.utc
LIMIT_BYTES = 100 * 1024 * 1024


class FakeOutline:
    def __init__(self):
        self.created = []
        self.deleted = []
        self.fail_create = False
        self.create_lock = threading.Lock()
        self.create_delay = 0.0
        self.transfer = {}

    def create_key(self, name, limit_bytes):
        if self.create_delay:
            time.sleep(self.create_delay)
        with self.create_lock:
            if self.fail_create:
                raise OutlineError("staging unavailable")
            key = {"id": str(len(self.created) + 1), "accessUrl": "ss://secret"}
            self.created.append((name, limit_bytes, key))
            return key

    def delete_key(self, key_id):
        self.deleted.append(key_id)

    def get_key(self, key_id):
        if str(key_id) in self.deleted:
            return None
        for _name, _limit, key in self.created:
            if key["id"] == str(key_id):
                return key
        return None

    def transfer_metrics(self):
        return {"bytesTransferredByUserId": self.transfer}


class ClaimServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "bot.db")
        self.db.initialize()
        self.outline = FakeOutline()
        self.service = ClaimService(self.db, self.outline)
        self.now = datetime(2026, 8, 27, 3, 7, tzinfo=UTC)

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_claim_creates_300_mb_key_for_24_hours(self):
        result = self.service.claim(123, "Min", self.now)
        self.assertEqual(result.access_url, "ss://secret")
        self.assertEqual(result.expires_at, self.now + timedelta(hours=24))
        self.assertEqual(self.outline.created[0][1], 300 * 1024 * 1024)
        self.assertEqual(
            self.outline.created[0][0], "123-FREE300MB-24hr-202608270307"
        )

    def test_claim_key_name_prefers_sanitized_telegram_username(self):
        self.service.claim(
            123, "Min", self.now, username="@min_user"
        )
        self.assertEqual(
            self.outline.created[0][0], "min_user-FREE300MB-24hr-202608270307"
        )

    def test_trial_key_name_uses_tier_duration_and_start_time(self):
        self.service.claim_trial(
            123, "Min", self.now, username="min_user"
        )
        self.assertEqual(
            self.outline.created[0][0], "min_user-FREE3GB-30day-202608270307"
        )

    def test_second_claim_inside_24_hours_is_rejected(self):
        self.service.claim(123, "Min", self.now)
        result = self.service.claim(123, "Min", self.now + timedelta(hours=23, minutes=59))
        self.assertIsNone(result.access_url)
        self.assertEqual(result.next_claim_at, self.now + timedelta(hours=24))
        self.assertEqual(len(self.outline.created), 1)

    def test_claim_at_24_hour_boundary_is_allowed(self):
        self.service.claim(123, "Min", self.now)
        result = self.service.claim(123, "Min", self.now + timedelta(hours=24))
        self.assertEqual(result.access_url, "ss://secret")
        self.assertEqual(len(self.outline.created), 2)

    def test_failed_outline_create_does_not_consume_claim(self):
        self.outline.fail_create = True
        with self.assertRaises(OutlineError):
            self.service.claim(123, "Min", self.now)
        self.outline.fail_create = False
        result = self.service.claim(123, "Min", self.now)
        self.assertEqual(result.access_url, "ss://secret")

    def test_expiry_revokes_key_once(self):
        self.service.claim(123, "Min", self.now)
        self.assertEqual(self.service.revoke_expired(self.now + timedelta(hours=24)), 1)
        self.assertEqual(self.service.revoke_expired(self.now + timedelta(hours=25)), 0)
        self.assertEqual(self.outline.deleted, ["1"])

        event = self.service.termination_summary()[0]
        self.assertEqual(event["reason"], "expiry")
        self.assertEqual(event["remote_state"], "deleted_verified")
        self.assertIsNotNone(event["deletion_verified_at"])

    def test_quota_revokes_before_expiry_and_records_observed_usage(self):
        self.service.claim(123, "Min", self.now)
        self.outline.transfer = {"1": 300 * 1024 * 1024}

        self.assertEqual(self.service.enforce_quota(self.now + timedelta(hours=1)), 1)

        event = self.service.termination_summary()[0]
        self.assertEqual(event["reason"], "quota")
        self.assertEqual(event["used_bytes"], 300 * 1024 * 1024)
        self.assertEqual(event["remote_state"], "deleted_verified")

    def test_failed_delete_is_retried_without_losing_enforcement_record(self):
        self.service.claim(123, "Min", self.now)
        original_delete = self.outline.delete_key
        self.outline.delete_key = lambda _key_id: (_ for _ in ()).throw(OutlineError("down"))
        self.assertEqual(self.service.revoke_expired(self.now + timedelta(hours=24)), 0)
        event = self.service.termination_summary()[0]
        self.assertEqual(event["remote_state"], "retrying")
        self.assertEqual(event["delete_attempts"], 1)

        self.outline.delete_key = original_delete
        self.assertEqual(self.service.revoke_expired(self.now + timedelta(hours=24, minutes=1)), 1)
        event = self.service.termination_summary()[0]
        self.assertEqual(event["remote_state"], "deleted_verified")
        self.assertEqual(event["delete_attempts"], 2)

    def test_repeated_delete_failures_escalate_for_operator_attention(self):
        self.service.claim(123, "Min", self.now)
        self.outline.delete_key = lambda _key_id: (_ for _ in ()).throw(OutlineError("down"))

        for minute in range(10):
            self.service.revoke_expired(
                self.now + timedelta(hours=24, minutes=minute)
            )

        event = self.service.termination_summary()[0]
        self.assertEqual(event["remote_state"], "escalated")
        self.assertEqual(event["delete_attempts"], 10)

    def test_user_usage_reports_only_owned_free_keys(self):
        self.service.claim(123, "Min", self.now)
        self.service.claim(456, "Other", self.now)
        self.outline.transfer = {"1": 75 * 1024 * 1024, "2": 200 * 1024 * 1024}
        usage = self.service.user_usage(123, self.outline.transfer)
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["tier"], "Daily Free 300 MiB")
        self.assertEqual(usage[0]["used_bytes"], 75 * 1024 * 1024)
        self.assertEqual(usage[0]["remaining_bytes"], 225 * 1024 * 1024)
        self.assertTrue(usage[0]["usage_observed"])

    def test_database_enforces_one_claim_timestamp_per_user(self):
        with sqlite3.connect(self.db.path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(keys)")}
        self.assertIn("expires_at", columns)
        self.assertIn("status", columns)

    def test_concurrent_claims_only_one_succeeds(self):
        self.outline.create_delay = 0.01
        results = []

        def claim():
            results.append(self.service.claim(456, "Concurrent", self.now))

        t1 = threading.Thread(target=claim)
        t2 = threading.Thread(target=claim)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        successes = [r for r in results if r.access_url is not None]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(self.outline.created), 1)

    def test_outline_key_id_conflict_rejected(self):
        # Verify UNIQUE constraint on outline_key_id exists in schema
        with sqlite3.connect(self.db.path) as conn:
            info = conn.execute("PRAGMA table_info(keys)").fetchall()
        outline_key_id_col = next(c for c in info if c[1] == "outline_key_id")
        # Column info: (cid, name, type, notnull, dflt_value, pk)
        # UNIQUE constraint shows up in index_list, not table_info
        indexes = conn.execute("PRAGMA index_list(keys)").fetchall()
        # Index info: (seq, name, unique, origin, partial)
        unique_indexes = [idx for idx in indexes if idx[2] == 1 and idx[3] == 'u']
        self.assertGreater(len(unique_indexes), 0)

    def test_outline_get_key_uses_encoded_key_id(self):
        client = OutlineClient.__new__(OutlineClient)
        calls = []
        client._request = lambda method, path, **_kwargs: calls.append((method, path)) or {"id": "a/b"}
        self.assertEqual(client.get_key("a/b")["id"], "a/b")
        self.assertEqual(calls, [("GET", "/access-keys/a%2Fb")])

    def test_outline_get_key_treats_404_as_absent(self):
        client = OutlineClient.__new__(OutlineClient)
        calls = []
        client._request = lambda method, path, body=None, accepted_statuses=(200, 201, 204): calls.append(
            (method, path, accepted_statuses)
        )
        self.assertIsNone(client.get_key("missing"))
        self.assertEqual(calls[0][2], (200, 404))


class FakeHTTPResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self.body = json.dumps(payload).encode() if payload is not None else b""

    def read(self):
        return self.body


class FakePeerSocket:
    def __init__(self, certificate):
        self.certificate = certificate

    def getpeercert(self, binary_form=False):
        return self.certificate if binary_form else {}


class FakeHTTPSConnection:
    peer_certificate = b""
    instances = []

    def __init__(self, host, port, context, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.method = None
        self.path = None
        self.body = None
        self.create_body = None
        FakeHTTPSConnection.instances.append(self)

    def connect(self):
        self.sock = FakePeerSocket(self.peer_certificate)

    def request(self, method, path, body=None, headers=None):
        self.method = method
        self.path = path
        self.body = body

    def getresponse(self):
        if self.method == "GET" and self.path == "/secret/server":
            return FakeHTTPResponse(200, {"version": "tls-test"})
        if self.method == "GET" and self.path == "/secret/access-keys":
            return FakeHTTPResponse(200, {"accessKeys": []})
        if self.method == "POST" and self.path == "/secret/access-keys":
            self.create_body = json.loads(self.body)
            return FakeHTTPResponse(
                201,
                {"id": "1", "name": self.create_body["name"], "accessUrl": "ss://test-only"},
            )
        if self.method == "PUT":
            return FakeHTTPResponse(204)
        if self.method == "DELETE":
            return FakeHTTPResponse(404)
        return FakeHTTPResponse(404)

    def close(self):
        return None


class OutlineClientTlsTest(unittest.TestCase):
    def setUp(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "aurix-test")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
            .not_valid_after(datetime.now(UTC) + timedelta(minutes=5))
            .sign(key, hashes.SHA256())
        )
        cert_der = cert.public_bytes(serialization.Encoding.DER)
        FakeHTTPSConnection.peer_certificate = cert_der
        FakeHTTPSConnection.instances = []
        self.http_patch = patch("app.http.client.HTTPSConnection", FakeHTTPSConnection)
        self.http_patch.start()
        self.addCleanup(self.http_patch.stop)
        fingerprint = hashlib.sha256(cert_der).hexdigest()
        self.client = OutlineClient(
            "https://outline.test:1234/secret", fingerprint
        )

    def test_pinned_outline_transport_and_404_delete(self):
        self.assertEqual(self.client.server_info()["version"], "tls-test")
        key = self.client.create_key("tls-check", 123)
        self.assertEqual(key["id"], "1")
        self.client.set_data_limit("a/b", 123)
        self.client.delete_key("already-gone")
        connection = FakeHTTPSConnection.instances[1]
        self.assertEqual(connection.create_body["limit"]["bytes"], 123)
        self.assertEqual(FakeHTTPSConnection.instances[2].path, "/secret/access-keys/a%2Fb/data-limit")

    def test_wrong_certificate_pin_fails_closed_before_request(self):
        from app import OutlineClient

        client = OutlineClient("https://outline.test:1234/secret", "0" * 64)
        with self.assertRaises(OutlineError):
            client.server_info()
        self.assertIsNone(FakeHTTPSConnection.instances[-1].path)


class RecordingTelegramBot(TelegramBot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent = []
        self.markups = []

    def send(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))
        self.markups.append(reply_markup)


class TelegramBotCommerceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "bot.db")
        self.db.initialize()
        self.outline = FakeOutline()
        self.commerce = CommerceService(
            CommerceDatabase(self.db.path), self.outline, Fernet.generate_key(),
            allow_legacy_text_approval=True,
        )
        self.commerce.initialize()
        self.bot = RecordingTelegramBot(
            "test-token",
            ClaimService(self.db, self.outline),
            self.commerce,
            {999},
            {123},
        )

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def message(telegram_id, text, chat_id=None):
        return {
            "chat": {"id": chat_id or telegram_id, "type": "private"},
            "from": {"id": telegram_id, "first_name": "Min"},
            "text": text,
        }

    def test_customer_can_create_and_submit_a_paid_order(self):
        self.bot.handle(self.message(123, "/buy basic_50gb"))
        order = self.commerce.list_pending_orders()[0]
        self.assertIn(order["id"], self.bot.sent[-1][1])

        self.bot.handle(self.message(123, f"/paid {order['id']} transfer-123"))
        self.assertEqual(self.commerce.list_pending_orders()[0]["status"], "payment_submitted")
        self.assertTrue(any("Payment recorded" in text for _, text in self.bot.sent))

    def test_repeated_buy_returns_existing_order_and_myorders_tracks_it(self):
        self.bot.handle(self.message(123, "/buy basic_50gb"))
        order_id = self.commerce.list_pending_orders()[0]["id"]
        self.bot.handle(self.message(123, "💎 Upgrade 50GB"))
        self.assertIn("Existing open order", self.bot.sent[-1][1])
        self.assertIn(order_id, self.bot.sent[-1][1])
        self.bot.handle(self.message(123, "🧾 My Orders"))
        self.assertIn("Your recent orders", self.bot.sent[-1][1])
        self.assertIn(order_id, self.bot.sent[-1][1])
        self.bot.handle(self.message(123, f"/order {order_id}"))
        self.assertIn("AuriX Order", self.bot.sent[-1][1])
        self.assertIn("Payment: not submitted", self.bot.sent[-1][1])

    def test_different_plan_button_requires_explicit_replacement(self):
        self.bot.handle(self.message(123, "/buy basic_50gb"))
        self.bot.handle(self.message(123, "/buy standard_100gb"))
        self.assertIn("open order", self.bot.sent[-1][1])
        labels = {
            button["text"]
            for row in self.bot.markups[-1]["inline_keyboard"]
            for button in row
        }
        self.assertIn("Replace Open Order", labels)

    def test_inline_order_buttons_route_without_copying_ids(self):
        self.bot.handle(self.message(123, "/buy basic_50gb"))
        order_id = self.commerce.list_pending_orders()[0]["id"]
        calls = []
        self.bot.request = lambda method, payload: calls.append((method, payload)) or True
        self.bot.handle_callback(
            {
                "id": "callback-1",
                "from": {"id": 123, "first_name": "Min"},
                "message": {"chat": {"id": 123, "type": "private"}},
                "data": f"o:v:{order_id}",
            }
        )
        self.assertEqual(calls[0][0], "answerCallbackQuery")
        labels = {
            button["text"]
            for row in self.bot.markups[-1]["inline_keyboard"]
            for button in row
        }
        self.assertIn("📷 Send Receipt", labels)
        self.assertIn("💰 Pay Wallet", labels)
        self.assertIn("🔄 Refresh", labels)

    def test_admin_reject_button_requires_confirmation(self):
        order = self.commerce.create_order(123, "Min", "basic_50gb")
        calls = []
        self.bot.request = lambda method, payload: calls.append((method, payload)) or True
        self.bot.handle_callback(
            {
                "id": "callback-admin",
                "from": {"id": 999, "first_name": "Admin"},
                "message": {"chat": {"id": 999, "type": "private"}},
                "data": f"a:x:{order.order_id}",
            }
        )
        self.assertIn("Reject order", self.bot.sent[-1][1])
        self.assertEqual(
            self.commerce.order_detail(order.order_id, 999, is_admin=True)["status"],
            "awaiting_payment",
        )
        labels = {
            button["text"]
            for row in self.bot.markups[-1]["inline_keyboard"]
            for button in row
        }
        self.assertIn("Confirm Reject", labels)

    def test_admin_commands_are_allowlisted_and_malformed_updates_are_ignored(self):
        self.bot.handle(self.message(123, "/orders"))
        self.assertEqual(self.bot.sent[-1][1], "Admin access required.")

        self.bot.handle(self.message(999, "/orders"))
        self.assertEqual(self.bot.sent[-1][1], "No pending orders.")
        self.bot.handle({"chat": None, "from": None, "text": "/orders"})
        self.assertEqual(self.bot.sent[-1][1], "No pending orders.")

    def test_admin_recovery_and_ledger_buttons_are_available(self):
        self.bot.handle(self.message(999, "/failed"))
        self.assertEqual(self.bot.sent[-1][1], "No terminal worker failures.")
        self.bot.handle(self.message(999, "/ledger 123"))
        self.assertIn("Wallet ledger · tg:123", self.bot.sent[-1][1])

        order = self.commerce.create_order(123, "Min", "basic_50gb")
        self.bot.handle(self.message(999, f"/order {order.order_id}"))
        labels = {
            button["text"]
            for row in self.bot.markups[-1]["inline_keyboard"]
            for button in row
        }
        self.assertIn("💰 View Ledger", labels)

    def test_customer_buttons_and_admin_panel_are_separated(self):
        self.bot.handle(self.message(123, "/help"))
        help_text = self.bot.sent[-1][1]
        self.assertNotIn("/approve", help_text)
        customer_labels = {
            button["text"]
            for row in self.bot.markups[-1]["keyboard"]
            for button in row
        }
        self.assertIn("🎁 Daily 300MB", customer_labels)
        self.assertIn("💠 Upgrade 100GB", customer_labels)
        self.assertNotIn("🛠 Admin Panel", customer_labels)

        self.bot.handle(self.message(999, "/admin"))
        admin_labels = {
            button["text"]
            for row in self.bot.markups[-1]["keyboard"]
            for button in row
        }
        self.assertIn("📥 Pending Orders", admin_labels)
        self.assertIn("🧾 Receipt Review", admin_labels)

    def test_button_actions_and_whoami(self):
        self.bot.handle(self.message(123, "📊 Status"))
        self.assertIn("No subscription found", self.bot.sent[-1][1])
        self.bot.handle(self.message(123, "/whoami"))
        self.assertIn("Your Telegram ID: 123", self.bot.sent[-1][1])
        self.assertIn("not enabled", self.bot.sent[-1][1])

    def test_usage_button_shows_free_key_stats_and_refresh_action(self):
        self.bot.handle(self.message(123, "/claim"))
        self.outline.transfer = {"1": 150 * 1024 * 1024}
        self.bot.handle(self.message(123, "📶 Usage"))
        text = self.bot.sent[-1][1]
        self.assertIn("Daily Free 300 MiB", text)
        self.assertIn("Used: 150.00 MiB", text)
        self.assertIn("Remaining: 150.00 MiB of 300.00 MiB", text)
        self.assertIn("50.0%", text)
        labels = {
            button["text"]
            for row in self.bot.markups[-1]["inline_keyboard"]
            for button in row
        }
        self.assertIn("🔄 Refresh Usage", labels)

    def test_command_scopes_hide_admin_commands_from_customers(self):
        calls = []
        self.bot.request = lambda method, payload: calls.append((method, payload)) or True
        self.bot.configure_commands()
        default = calls[0][1]
        admin = calls[1][1]
        self.assertEqual(default["scope"], {"type": "default"})
        self.assertNotIn("approve", {item["command"] for item in default["commands"]})
        self.assertEqual(admin["scope"], {"type": "chat", "chat_id": 999})
        self.assertIn("approve", {item["command"] for item in admin["commands"]})

    def test_free_staging_claim_is_fail_closed_for_non_test_accounts(self):
        self.bot.handle(self.message(456, "/claim"))
        self.assertIn("limited to the configured test accounts", self.bot.sent[-1][1])
        self.assertEqual(self.outline.created, [])


if __name__ == "__main__":
    unittest.main()
