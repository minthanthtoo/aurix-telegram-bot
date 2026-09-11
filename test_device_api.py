import base64
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.fernet import Fernet

from commerce import CommerceDatabase
from device_api import DeviceAPIService, ManifestSigner, create_device_wsgi_app, sign_device_request
from identity import IdentityService
from vpn_web_api import AuriXVpnWebApplication, build_device_api, make_handler


def _public_key(private_key: Ed25519PrivateKey) -> str:
    return base64.urlsafe_b64encode(
        private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode().rstrip("=")


class DeviceAPITest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = CommerceDatabase(Path(self.tempdir.name) / "device.db")
        self.database.initialize()
        self.identity = IdentityService(self.database)
        self.signer = ManifestSigner(key_id="test-manifest")
        self.cipher = Fernet(Fernet.generate_key())
        self.service = DeviceAPIService(
            self.database,
            manifest_signer=self.signer,
            route_provider=self.identity.routes_for_account,
            secret_decryptor=lambda value: self.cipher.decrypt(value.encode()).decode(),
            identity=self.identity,
        )
        self.app = create_device_wsgi_app(self.service)

    def tearDown(self):
        self.tempdir.cleanup()

    def request(self, method, path, body=b"", *, device_id="", private_key=None):
        timestamp = str(time.time())
        headers = {}
        if device_id and private_key is not None:
            headers = {
                "HTTP_X_AURIX_DEVICE_ID": device_id,
                "HTTP_X_AURIX_REQUEST_TIMESTAMP": timestamp,
                "HTTP_X_AURIX_REQUEST_SIGNATURE": sign_device_request(
                    method, path, timestamp, body, private_key
                ),
            }
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path.split("?", 1)[0],
            "QUERY_STRING": path.split("?", 1)[1] if "?" in path else "",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
            **headers,
        }
        captured = {}

        def start_response(status, response_headers):
            captured["status"] = status
            captured["headers"] = response_headers

        response = b"".join(self.app(environ, start_response))
        return captured["status"], json.loads(response)

    def pair(self, private_key=None):
        private_key = private_key or Ed25519PrivateKey.generate()
        token = self.identity.create_pairing_token(123)
        status, paired = self.request(
            "POST",
            "/v1/devices/pair",
            json.dumps({"token": token, "public_key": _public_key(private_key)}).encode(),
        )
        self.assertEqual(status, "200 OK")
        return private_key, paired

    def test_pair_manifest_ack_and_device_revocation(self):
        private_key, paired = self.pair()
        status, signed = self.request(
            "GET", "/v1/devices/manifest", device_id=paired["device_id"], private_key=private_key
        )
        self.assertEqual(status, "200 OK")
        self.assertTrue(ManifestSigner.verify(signed, self.signer.public_key))
        self.assertNotIn("access_url", json.dumps(signed))
        status, ack = self.request(
            "POST",
            "/v1/devices/ack",
            json.dumps({"outcome": "probe"}).encode(),
            device_id=paired["device_id"],
            private_key=private_key,
        )
        self.assertEqual(status, "200 OK")
        self.assertTrue(ack["accepted"])
        self.assertTrue(self.identity.revoke_device(123, paired["device_id"]))
        status, value = self.request(
            "GET", "/v1/devices/manifest", device_id=paired["device_id"], private_key=private_key
        )
        self.assertEqual(status, "401 Unauthorized")
        self.assertIn("not active", value["error"])

    def test_shared_device_builder_applies_configured_cap(self):
        runtime = SimpleNamespace(
            commerce_database=self.database,
            commerce=SimpleNamespace(
                identity=self.identity,
                _decrypt_access_url=lambda value: value,
            ),
        )
        seed = base64.urlsafe_b64encode(b"device-builder-seed".ljust(32, b"!")).decode().rstrip("=")
        with patch.dict(
            os.environ,
            {"AURIX_DEVICE_MANIFEST_PRIVATE_KEY": seed, "AURIX_DEVICE_MANIFEST_KEY_ID": "test-key"},
            clear=False,
        ):
            service = build_device_api(runtime, max_active_devices=2)
        self.assertIsNotNone(service)
        self.assertEqual(service.max_active_devices, 2)
        self.assertEqual(service.manifest_signer.key_id, "test-key")

    def test_pair_rejects_malformed_key_without_consuming_token(self):
        token = self.identity.create_pairing_token(123)
        status, value = self.request(
            "POST",
            "/v1/devices/pair",
            json.dumps({"token": token, "public_key": "not-a-public-key"}).encode(),
        )
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("public key is invalid", value["error"])
        private_key = Ed25519PrivateKey.generate()
        status, paired = self.request(
            "POST",
            "/v1/devices/pair",
            json.dumps({"token": token, "public_key": _public_key(private_key)}).encode(),
        )
        self.assertEqual(status, "200 OK")
        self.assertTrue(paired["device_id"].startswith("device-"))

    def test_pair_enforces_optional_active_device_limit_and_releases_on_revoke(self):
        self.service.max_active_devices = 1
        _, first = self.pair()
        token = self.identity.create_pairing_token(123)
        second_key = Ed25519PrivateKey.generate()
        status, value = self.request(
            "POST",
            "/v1/devices/pair",
            json.dumps({"token": token, "public_key": _public_key(second_key)}).encode(),
        )
        self.assertEqual(status, "409 Conflict")
        self.assertIn("active managed device limit reached", value["error"])
        self.assertTrue(self.identity.revoke_device(123, first["device_id"]))
        status, paired = self.request(
            "POST",
            "/v1/devices/pair",
            json.dumps({"token": token, "public_key": _public_key(second_key)}).encode(),
        )
        self.assertEqual(status, "200 OK")
        self.assertEqual(paired["active_device_count"], 1)
        self.assertEqual(paired["max_active_devices"], 1)

    def test_config_is_protocol_neutral_and_owned_by_the_account(self):
        timestamp = "2026-09-10T00:00:00+00:00"
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO users (telegram_id, first_name, created_at) VALUES (123, 'Member', ?)",
                (timestamp,),
            )
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, status, created_at)
                   VALUES ('order-device', 123, 'basic_50gb', 1, 'MMK', 'approved', ?)""",
                (timestamp,),
            )
            connection.execute(
                """INSERT INTO subscriptions
                   (id, order_id, telegram_id, plan_code, starts_at, expires_at,
                    quota_bytes, duration_days, status, consumed_bytes)
                   VALUES ('sub-device', 'order-device', 123, 'basic_50gb', ?, ?, 1000, 30, 'active', 0)""",
                (timestamp, "2026-10-10T00:00:00+00:00"),
            )
        entitlement = self.identity.ensure_subscription_entitlement(123, "sub-device", quota_bytes=1000)
        access_url = "vless://uuid@example.com:18443?security=reality#AuriX"
        generation = self.identity.ensure_generation_for_credential(
            entitlement,
            "legacy-default",
            external_id="uuid-device",
            protocol="xray",
            access_url_ciphertext=self.cipher.encrypt(access_url.encode()).decode(),
            usage_baseline_provenance="new",
            now=timestamp,
        )
        private_key, paired = self.pair()
        status, config = self.request(
            "GET",
            f"/v1/devices/config?route_id={generation}",
            device_id=paired["device_id"],
            private_key=private_key,
        )
        self.assertEqual(status, "200 OK")
        self.assertEqual(config["access_url"], access_url)
        self.assertEqual(config["protocol"], "xray")

    def test_device_ack_cannot_observe_another_accounts_route(self):
        timestamp = "2026-09-10T00:00:00+00:00"
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO users (telegram_id, first_name, created_at) VALUES (456, 'Other', ?)",
                (timestamp,),
            )
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, status, created_at)
                   VALUES ('order-other', 456, 'basic_50gb', 1, 'MMK', 'approved', ?)""",
                (timestamp,),
            )
            connection.execute(
                """INSERT INTO subscriptions
                   (id, order_id, telegram_id, plan_code, starts_at, expires_at,
                    quota_bytes, duration_days, status, consumed_bytes)
                   VALUES ('sub-other', 'order-other', 456, 'basic_50gb', ?, ?, 1000, 30, 'active', 0)""",
                (timestamp, "2026-10-10T00:00:00+00:00"),
            )
        entitlement = self.identity.ensure_subscription_entitlement(456, "sub-other", quota_bytes=1000)
        foreign_generation = self.identity.ensure_generation_for_credential(
            entitlement,
            "legacy-default",
            external_id="foreign-route",
            protocol="xray",
            access_url_ciphertext=self.cipher.encrypt(b"vless://foreign").decode(),
            now=timestamp,
        )
        private_key, paired = self.pair()
        status, ack = self.request(
            "POST",
            "/v1/devices/ack",
            json.dumps({"route_id": foreign_generation, "outcome": "failed"}).encode(),
            device_id=paired["device_id"],
            private_key=private_key,
        )
        self.assertEqual(status, "200 OK")
        self.assertTrue(ack["accepted"])
        with self.database.connect() as connection:
            observed = connection.execute(
                "SELECT COUNT(*) AS n FROM route_observations WHERE generation_id = ?",
                (foreign_generation,),
            ).fetchone()["n"]
        self.assertEqual(observed, 0)

    def test_signed_api_mounts_under_the_vpn_portal_handler(self):
        runtime = SimpleNamespace(token="unused", commerce=SimpleNamespace(identity=self.identity))
        application = AuriXVpnWebApplication(runtime, device_api=self.service)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(application))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_address[1]}/v1/devices/healthz",
                timeout=3,
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response)["status"], "ok")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
