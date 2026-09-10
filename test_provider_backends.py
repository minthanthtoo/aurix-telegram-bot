import io
import json
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from aurix_vpn.connectivity_adapters import Hysteria2ConnectivityAdapter, XrayConnectivityAdapter
from aurix_vpn.node_agent import XrayConfigWriter
from aurix_vpn.provider_backends import (
    Hysteria2Provider,
    Hysteria2TrafficStatsClient,
    Hysteria2UserStore,
    ProviderBackendError,
    XrayConfigProvider,
    XrayStatsParser,
    create_hysteria2_auth_wsgi_app,
)


class ProviderBackendsTest(unittest.TestCase):
    def test_xray_provider_writes_only_managed_users_and_reloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "xray.json"
            path.write_text(
                json.dumps(
                    {
                        "inbounds": [
                            {"tag": "public", "settings": {"clients": [{"id": "keep"}]}},
                            {
                                "tag": "aurix-managed",
                                "settings": {"clients": [{"id": "existing", "email": "keep"}]},
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            reloads = []
            provider = XrayConfigProvider(
                XrayConfigWriter(path),
                reload_callback=lambda: reloads.append("reload"),
                stats_query=lambda _external_id: {
                    "stat": [
                        {"name": "user>>>xray-1>>>traffic>>>uplink", "value": 7},
                        {"name": "user>>>xray-1>>>traffic>>>downlink", "value": 11},
                    ]
                },
            )
            created = provider.create_user(
                "xray-1", "Customer", {"inbound_tag": "aurix-managed"}, {"flow": "xtls-rprx-vision"}
            )
            self.assertEqual(created["external_id"], "xray-1")
            self.assertEqual(reloads, ["reload"])
            self.assertEqual(provider.get_user_usage("xray-1")["bytes_transferred"], 18)
            self.assertEqual(
                {item["external_id"] for item in provider.list_users()}, {"existing", "xray-1"}
            )
            provider.delete_user("xray-1")
            self.assertEqual(reloads, ["reload", "reload"])
            config = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(config["inbounds"][0]["settings"]["clients"], [{"id": "keep"}])
            self.assertEqual(config["inbounds"][1]["settings"]["clients"], [{"id": "existing", "email": "keep"}])

    def test_xray_stats_parser_rejects_unsafe_shapes(self):
        with self.assertRaises(ProviderBackendError):
            XrayStatsParser.parse({"stat": "not-a-list"}, "user")
        parsed = XrayStatsParser.parse(
            json.dumps({"stat": [{"name": "user>>>u>>>traffic>>>uplink", "value": "3"}]}), "u"
        )
        self.assertEqual(parsed["bytes_transferred"], 3)

    def test_hysteria2_store_encrypts_secrets_and_auth_callback_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.json"
            store = Hysteria2UserStore(path, encryption_key=Fernet.generate_key())
            record = store.create_user("h2-1", "Customer", "customer-secret")
            self.assertEqual(record["secret"], "customer-secret")
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("customer-secret", raw)
            self.assertEqual(store.authenticate("customer-secret"), {"ok": True, "id": "h2-1"})
            self.assertEqual(store.authenticate("wrong"), {"ok": False})

            app = create_hysteria2_auth_wsgi_app(store)

            def request(payload, method="POST"):
                body = json.dumps(payload).encode()
                captured = {}

                def start_response(status, headers):
                    captured["status"] = status
                    captured["headers"] = headers

                result = b"".join(
                    app(
                        {
                            "REQUEST_METHOD": method,
                            "CONTENT_LENGTH": str(len(body)),
                            "wsgi.input": io.BytesIO(body),
                        },
                        start_response,
                    )
                )
                return captured["status"], json.loads(result)

            self.assertEqual(request({"addr": "127.0.0.1:1", "auth": "customer-secret", "tx": 0})[1], {"ok": True, "id": "h2-1"})
            self.assertEqual(request({"auth": "wrong"})[1], {"ok": False})
            self.assertEqual(request({}, method="GET")[0], "405 Error")

    def test_hysteria2_provider_and_adapter_use_stats_and_revoke_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Hysteria2UserStore(
                Path(directory) / "users.json", encryption_key=Fernet.generate_key()
            )
            calls = []

            def requester(method, path, body, headers):
                calls.append((method, path, body, headers))
                if path == "/online":
                    return {"online": 0}
                if path == "/traffic":
                    return {"h2-1": {"tx": 12, "rx": 30}}
                if path == "/kick":
                    return {"ok": True}
                raise AssertionError(path)

            provider = Hysteria2Provider(
                store,
                Hysteria2TrafficStatsClient("http://127.0.0.1:19000", "stats-secret", requester=requester),
            )
            adapter = Hysteria2ConnectivityAdapter(provider)
            route = {
                "route_id": "hysteria2:sg-a",
                "endpoint_id": "sg-a",
                "public_address": "198.51.100.20",
                "port": 443,
                "server_name": "example.com",
            }
            grant = adapter.provision(
                route,
                {"external_id": "h2-1", "name": "Customer", "secret": "customer-secret"},
            )
            self.assertTrue(grant["access_url"].startswith("hysteria2://customer-secret@"))
            self.assertEqual(adapter.read_usage(grant)["bytes_transferred"], 42)
            self.assertEqual(adapter.terminate_sessions(grant)["terminated"], False)
            self.assertEqual(calls[-1][1], "/kick")
            self.assertEqual(calls[-1][3]["Authorization"], "stats-secret")
            adapter.revoke_auth(grant)
            self.assertTrue(adapter.verify_auth_revoked(grant)["verified"])


if __name__ == "__main__":
    unittest.main()
