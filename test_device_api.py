import base64
import io
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.fernet import Fernet

from connectivity_registry import ConnectivityRegistry
from commerce_repositories import CommerceDatabase
from device_api import (
    DeviceAPIService,
    ManifestSigner,
    create_device_wsgi_app,
    sign_device_request,
)
from identity import IdentityService


def _encode_key(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class DeviceApiTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = CommerceDatabase(Path(self.tempdir.name) / "device.db")
        self.database.initialize()
        self.identity = IdentityService(self.database)
        self.signer = ManifestSigner(key_id="test-manifest")
        self.service = DeviceAPIService(
            self.database,
            manifest_signer=self.signer,
            route_provider=lambda account_id: [{
                "route_id": "sg-a-outline", "region": "Singapore", "protocol": "outline",
                "credential_ref": "credential-safe-ref",
            }],
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

    def test_pair_manifest_verify_ack_and_revocation(self):
        private_key = Ed25519PrivateKey.generate()
        public_key = _encode_key(
            private_key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        )
        invalid_token = self.identity.create_pairing_token(123)
        status, value = self.request(
            "POST", "/v1/devices/pair",
            json.dumps({"token": invalid_token, "public_key": "not-a-key"}).encode(),
        )
        self.assertEqual(status, "400 Bad Request")
        self.assertEqual(value["error"], "public key is invalid")
        token = self.identity.create_pairing_token(123)
        status, paired = self.request(
            "POST", "/v1/devices/pair",
            json.dumps({"token": token, "public_key": public_key, "label": "Phone"}).encode(),
        )
        self.assertEqual(status, "200 OK")
        device_id = paired["device_id"]
        status, signed = self.request(
            "GET", "/v1/devices/manifest", device_id=device_id, private_key=private_key
        )
        self.assertEqual(status, "200 OK")
        self.assertTrue(ManifestSigner.verify(signed, self.signer.public_key))
        self.assertEqual(signed["manifest"]["routes"][0]["region"], "Singapore")
        self.assertNotIn("management", json.dumps(signed))
        status, ack = self.request(
            "POST", "/v1/devices/ack",
            json.dumps({"route_id": "sg-a-outline", "outcome": "connected"}).encode(),
            device_id=device_id, private_key=private_key,
        )
        self.assertEqual(status, "200 OK")
        self.assertTrue(ack["accepted"])
        self.assertTrue(self.identity.revoke_device(123, device_id))
        status, value = self.request(
            "GET", "/v1/devices/manifest", device_id=device_id, private_key=private_key
        )
        self.assertEqual(status, "401 Unauthorized")
        self.assertIn("not active", value["error"])

    def test_pair_rejects_malformed_public_key_before_consuming_token(self):
        token = self.identity.create_pairing_token(123)
        status, value = self.request(
            "POST",
            "/v1/devices/pair",
            json.dumps({"token": token, "public_key": "not-a-public-key"}).encode(),
        )
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("public key is invalid", value["error"])

        private_key = Ed25519PrivateKey.generate()
        public_key = _encode_key(
            private_key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        )
        status, paired = self.request(
            "POST",
            "/v1/devices/pair",
            json.dumps({"token": token, "public_key": public_key}).encode(),
        )
        self.assertEqual(status, "200 OK")
        self.assertTrue(paired["device_id"].startswith("device-"))

    def test_manifest_signature_rejects_invalid_issue_window(self):
        now = datetime(2026, 9, 5, tzinfo=timezone.utc)
        signed = self.signer.sign(
            {
                "version": 1,
                "issued_at": "2026-09-05T00:00:00+00:00",
                "expires_at": "2026-09-04T23:59:00+00:00",
            }
        )
        self.assertFalse(ManifestSigner.verify(signed, self.signer.public_key, now=now))

    def test_authenticated_config_returns_only_owned_active_route_secret(self):
        access_cipher = Fernet(Fernet.generate_key())
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO users (telegram_id, first_name, created_at)
                   VALUES (123, 'Member', ?)""",
                ("2026-09-05T00:00:00+00:00",),
            )
            connection.execute(
                """INSERT INTO outline_servers
                   (server_id, label, enabled, health_status, lifecycle_state, created_at, updated_at)
                   VALUES ('sg-a', 'Singapore A', 1, 'healthy', 'active', ?, ?)""",
                ("2026-09-05T00:00:00+00:00", "2026-09-05T00:00:00+00:00"),
            )
            endpoint = ConnectivityRegistry.sync_outline_endpoint(
                connection, server_id="sg-a", label="Singapore A", region="Singapore",
                health_status="healthy", now_text="2026-09-05T00:00:00+00:00",
            )
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, status, created_at)
                   VALUES ('order-device', 123, 'basic_50gb', 1, 'MMK', 'approved', ?)""",
                ("2026-09-05T00:00:00+00:00",),
            )
            connection.execute(
                """INSERT INTO subscriptions
                   (id, order_id, telegram_id, plan_code, starts_at, expires_at, status)
                   VALUES ('sub-device', 'order-device', 123, 'basic_50gb', ?, ?, 'active')""",
                ("2026-09-05T00:00:00+00:00", "2026-10-05T00:00:00+00:00"),
            )
            encrypted = access_cipher.encrypt(b"ss://owned-route").decode()
            ConnectivityRegistry.bind_credential(
                connection, telegram_id=123, server_id="sg-a", external_id="key-device",
                secret_ciphertext=encrypted, now_text="2026-09-05T00:00:00+00:00",
                profile_kind="paid", subscription_id="sub-device",
            )
            credential = connection.execute(
                "SELECT credential_id FROM connectivity_credentials WHERE external_id = 'key-device'"
            ).fetchone()
        entitlement_id = self.identity.ensure_subscription_entitlement(
            123, "sub-device", kind="paid", quota_bytes=1000,
            expires_at="2026-10-05T00:00:00+00:00",
            now="2026-09-05T00:00:00+00:00",
        )
        generation_id = self.identity.ensure_generation_for_credential(
            entitlement_id, endpoint["endpoint_id"],
            credential_id=str(credential["credential_id"]),
            now="2026-09-05T00:00:00+00:00",
        )
        service = DeviceAPIService(
            self.database,
            manifest_signer=self.signer,
            route_provider=self.identity.routes_for_account,
            secret_decryptor=lambda value: access_cipher.decrypt(value.encode()).decode(),
            identity=self.identity,
        )
        app = create_device_wsgi_app(service)
        private_key = Ed25519PrivateKey.generate()
        public_key = _encode_key(
            private_key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        )
        token = self.identity.create_pairing_token(123)
        old_service, old_app = self.service, self.app
        self.service = service
        self.app = app
        try:
            _, paired = self.request(
                "POST", "/v1/devices/pair",
                json.dumps({"token": token, "public_key": public_key}).encode(),
            )
            status, config = self.request(
                "GET", f"/v1/devices/config?route_id={generation_id}",
                device_id=paired["device_id"], private_key=private_key,
            )
        finally:
            self.service, self.app = old_service, old_app
        self.assertEqual(status, "200 OK")
        self.assertEqual(config["access_url"], "ss://owned-route")
        self.assertNotIn("management", json.dumps(config))


if __name__ == "__main__":
    unittest.main()
