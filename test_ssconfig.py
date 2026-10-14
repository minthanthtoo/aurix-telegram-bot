import base64
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from commerce import CommerceDatabase
from ssconfig import (
    SsconfError,
    SsconfProfileService,
    parse_shadowsocks_uri,
    render_outline_ssconf_document,
    render_ssconf_uri,
)


def _ss_url(method: str, password: str, host: str, port: int, remark: str) -> str:
    credentials = base64.urlsafe_b64encode(
        f"{method}:{password}".encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"ss://{credentials}@{host}:{port}/?outline=1#{remark}"


class _Identity:
    def __init__(self, routes, secrets):
        self.routes = routes
        self.secrets = secrets

    def routes_for_account(self, _account_id):
        return [dict(route) for route in self.routes]

    def ensure_account(self, _telegram_id):
        return "account-1"

    def route_secret_record(self, _account_id, route_id):
        value = self.secrets.get(route_id)
        return {"secret_ciphertext": value} if value else None


class SsconfTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = CommerceDatabase(Path(self.tempdir.name) / "ssconf.db")
        self.database.initialize()
        self.cipher = Fernet(Fernet.generate_key())
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO accounts (account_id, status, created_at, updated_at)
                   VALUES ('account-1', 'active', '2026-09-10T00:00:00+00:00',
                           '2026-09-10T00:00:00+00:00')"""
            )
        self.routes = [
            {
                "route_id": "generation-old",
                "generation_id": "generation-old",
                "endpoint_id": "sg-a",
                "region": "Singapore",
                "entitlement_key": "paid:sub-sg",
                "protocol": "outline",
                "generation": 1,
            },
            {
                "route_id": "generation-new",
                "generation_id": "generation-new",
                "endpoint_id": "bkk-a",
                "region": "Bangkok",
                "entitlement_key": "paid:sub-bkk",
                "protocol": "outline",
                "generation": 2,
            },
        ]
        self.urls = {
            "generation-old": _ss_url(
                "chacha20-ietf-poly1305", "old-secret", "sg-a.example", 45524, "old"
            ),
            "generation-new": _ss_url(
                "chacha20-ietf-poly1305", "new-secret", "bkk-a.example", 443, "new"
            ),
        }
        self.identity = _Identity(
            self.routes,
            {
                route_id: self.cipher.encrypt(url.encode()).decode()
                for route_id, url in self.urls.items()
            },
        )
        self.service = SsconfProfileService(
            self.database,
            identity=self.identity,
            secret_encryptor=lambda value: self.cipher.encrypt(value.encode()).decode(),
            secret_decryptor=lambda value: self.cipher.decrypt(value.encode()).decode(),
            public_base_url="https://vpn.example.test",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_static_outline_key_is_converted_to_single_dynamic_document(self):
        access_url = self.urls["generation-new"]
        parsed = parse_shadowsocks_uri(access_url)
        self.assertEqual(parsed["server"], "bkk-a.example")
        self.assertEqual(parsed["server_port"], 443)
        self.assertEqual(parsed["password"], "new-secret")
        self.assertEqual(
            render_outline_ssconf_document(access_url, remarks="AuriX VPN"),
            {
                "server": "bkk-a.example",
                "server_port": 443,
                "method": "chacha20-ietf-poly1305",
                "password": "new-secret",
                "remarks": "AuriX VPN",
            },
        )

    def test_legacy_profile_pins_to_one_entitlement_and_keeps_existing_url(self):
        issued = self.service.issue_for_account("account-1")
        self.assertEqual(issued["protocol"], "ssconf")
        self.assertFalse(issued["multi_server_pool"])
        self.assertEqual(issued["compatible_route_count"], 1)
        self.assertEqual(issued["entitlement_key"], "paid:sub-bkk")
        self.assertTrue(issued["profile_url"].startswith("ssconf://vpn.example.test/api/ssconf/"))
        token = issued["profile_url"].split("/api/ssconf/", 1)[1].split("#", 1)[0]

        document = self.service.document(token)
        self.assertEqual(document["server"], "bkk-a.example")
        self.assertEqual(document["password"], "new-secret")
        self.assertNotIsInstance(document, list)

        us_route = {
            "route_id": "generation-us",
            "generation_id": "generation-us",
            "endpoint_id": "sjc-a",
            "region": "United States — West",
            "entitlement_key": "paid:sub-us",
            "protocol": "outline",
            "generation": 99,
        }
        self.identity.routes.append(us_route)
        self.identity.secrets["generation-us"] = self.cipher.encrypt(
            _ss_url("chacha20-ietf-poly1305", "us-secret", "sjc-a.example", 443, "us").encode()
        ).decode()
        next_document = self.service.document(token)
        self.assertEqual(next_document["server"], "bkk-a.example")

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT entitlement_key, served_count FROM dynamic_shadowsocks_profiles WHERE account_id = 'account-1'"
            ).fetchone()
        self.assertEqual(row["entitlement_key"], "paid:sub-bkk")
        self.assertEqual(row["served_count"], 2)

    def test_profile_selection_avoids_an_endpoint_with_failed_health(self):
        service = SsconfProfileService(
            self.database,
            identity=self.identity,
            secret_encryptor=lambda value: self.cipher.encrypt(value.encode()).decode(),
            secret_decryptor=lambda value: self.cipher.decrypt(value.encode()).decode(),
            public_base_url="https://vpn.example.test",
            route_health=lambda endpoint_id: endpoint_id == "sg-a",
        )
        issued = service.issue_for_account("account-1")
        token = issued["profile_url"].split("/api/ssconf/", 1)[1].split("#", 1)[0]
        document = service.document(token)
        self.assertEqual(document["server"], "sg-a.example")

    def test_entitlement_profile_never_selects_a_different_package_route(self):
        issued = self.service.issue_for_entitlement("account-1", "paid:sub-sg")
        token = issued["profile_url"].split("/api/ssconf/", 1)[1].split("#", 1)[0]
        document = self.service.document(token)
        self.assertEqual(document["server"], "sg-a.example")
        self.assertEqual(document["password"], "old-secret")
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT entitlement_key, served_count
                     FROM dynamic_shadowsocks_entitlement_profiles
                    WHERE entitlement_key = ?""",
                ("paid:sub-sg",),
            ).fetchone()
        self.assertEqual(row["entitlement_key"], "paid:sub-sg")
        self.assertEqual(row["served_count"], 1)

    def test_profile_options_are_owned_active_outline_entitlements_only(self):
        options = self.service.options_for_telegram(123)
        self.assertEqual(
            {item["entitlement_key"] for item in options},
            {"paid:sub-sg", "paid:sub-bkk"},
        )
        self.assertTrue(all(item["label"].startswith("Paid · ") for item in options))

    def test_profile_cannot_be_issued_for_an_unowned_entitlement(self):
        with self.assertRaisesRegex(SsconfError, "no active Outline route"):
            self.service.issue_for_entitlement("account-1", "paid:somebody-else")

    def test_unknown_token_and_suspended_account_fail_closed(self):
        self.assertIsNone(self.service.document("not-a-valid-token"))
        issued = self.service.issue_for_account("account-1")
        token = issued["profile_url"].split("/api/ssconf/", 1)[1].split("#", 1)[0]
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE accounts SET status = 'suspended' WHERE account_id = 'account-1'"
            )
        self.assertEqual(
            self.service.document(token),
            {"error": {"message": "AuriX VPN access is not currently active"}},
        )

    def test_uri_validation_rejects_non_https_profile_and_non_shadowsocks_key(self):
        with self.assertRaisesRegex(SsconfError, "HTTPS"):
            render_ssconf_uri("http://vpn.example.test", "a" * 43)
        with self.assertRaisesRegex(SsconfError, "not a Shadowsocks"):
            parse_shadowsocks_uri("vless://uuid@example.test:443")


if __name__ == "__main__":
    unittest.main()
