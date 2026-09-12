import io
import json
import tempfile
import threading
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from aurix_vpn.connectivity_adapters import (
    ConnectivityAdapterError,
    Hysteria2ConnectivityAdapter,
    XrayConnectivityAdapter,
)
from aurix_vpn.node_agent import NodeAgentClient, NodeAgentError, XrayConfigWriter
from aurix_vpn.node_agent_app import NodeAgentService, create_node_agent_wsgi_app
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
    def test_hysteria2_stats_client_rejects_nonlocal_or_decorated_urls(self):
        for base_url, message in (
            ("https://stats.example.test:19000", "loopback-local"),
            ("http://user:password@127.0.0.1:19000", "credentials or URL decorations"),
            ("http://127.0.0.1:19000?next=https://example.test", "credentials or URL decorations"),
            ("http://127.0.0.1:19000#fragment", "credentials or URL decorations"),
        ):
            with self.subTest(base_url=base_url):
                with self.assertRaisesRegex(ValueError, message):
                    Hysteria2TrafficStatsClient(base_url, "stats-secret")

        Hysteria2TrafficStatsClient("http://localhost:19000", "stats-secret")
        Hysteria2TrafficStatsClient("http://[::1]:19000", "stats-secret")

    @staticmethod
    def _client_for_app(app):
        def requester(method, path, payload):
            body = b"" if payload is None else json.dumps(payload).encode()
            environ = {
                "REQUEST_METHOD": method,
                "PATH_INFO": path,
                "CONTENT_LENGTH": str(len(body)),
                "HTTP_AUTHORIZATION": "Bearer concrete-agent-token",
                "wsgi.input": io.BytesIO(body),
            }
            captured = {}

            def start_response(status, _headers):
                captured["status"] = status

            value = json.loads(b"".join(app(environ, start_response)))
            code = int(str(captured["status"]).split(" ", 1)[0])
            if code >= 400:
                raise NodeAgentError(value.get("error") or "agent request failed", status_code=code)
            return value

        return NodeAgentClient(requester=requester)

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
            restarted = XrayConfigProvider(
                XrayConfigWriter(path), reload_callback=lambda: None
            )
            self.assertEqual(restarted.get_user("xray-1")["external_id"], "xray-1")
            provider.delete_user("xray-1")
            self.assertEqual(reloads, ["reload", "reload"])
            config = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(config["inbounds"][0]["settings"]["clients"], [{"id": "keep"}])
            self.assertEqual(config["inbounds"][1]["settings"]["clients"], [{"id": "existing", "email": "keep"}])

    def test_xray_provider_restores_config_when_supervised_reload_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "xray.json"
            original = {"inbounds": [{"tag": "aurix-managed", "settings": {"clients": []}}]}
            path.write_text(json.dumps(original), encoding="utf-8")
            reloads = []
            fail_next_reload = [True]

            def reload_callback():
                reloads.append("reload")
                if fail_next_reload[0]:
                    fail_next_reload[0] = False
                    raise RuntimeError("supervisor unavailable")

            provider = XrayConfigProvider(
                XrayConfigWriter(path), reload_callback=reload_callback
            )
            with self.assertRaisesRegex(NodeAgentError, "previous configuration was restored"):
                provider.create_user("xray-1", "Customer", {}, {})
            self.assertEqual(reloads, ["reload", "reload"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
            self.assertIsNone(provider.get_user("xray-1"))

            provider.create_user("xray-1", "Customer", {}, {})
            fail_next_reload[0] = True
            with self.assertRaisesRegex(NodeAgentError, "previous configuration was restored"):
                provider.delete_user("xray-1")
            self.assertIsNotNone(provider.get_user("xray-1"))

    def test_xray_stats_parser_rejects_unsafe_shapes(self):
        with self.assertRaises(ProviderBackendError):
            XrayStatsParser.parse({"stat": "not-a-list"}, "user")
        with self.assertRaises(ProviderBackendError):
            XrayStatsParser.parse({"stat": [{"name": "user>>>u>>>traffic>>>uplink", "value": True}]}, "u")
        with self.assertRaises(ProviderBackendError):
            XrayStatsParser.parse({"stat": [{"name": "user>>>u>>>traffic>>>uplink", "value": 1.2}]}, "u")
        parsed = XrayStatsParser.parse(
            json.dumps({"stat": [{"name": "user>>>u>>>traffic>>>uplink", "value": "3"}]}), "u"
        )
        self.assertEqual(parsed["bytes_transferred"], 3)

    def test_provider_backends_reject_path_like_or_empty_customer_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "xray.json"
            path.write_text(
                json.dumps({"inbounds": [{"tag": "aurix-managed", "settings": {"clients": []}}]}),
                encoding="utf-8",
            )
            provider = XrayConfigProvider(
                XrayConfigWriter(path), reload_callback=lambda: None
            )
            with self.assertRaises(ProviderBackendError):
                provider.create_user("../escape", "name", {}, {})
            store = Hysteria2UserStore(
                Path(directory) / "users.json", encryption_key=Fernet.generate_key()
            )
            with self.assertRaises(ProviderBackendError):
                store.create_user("", "name", "secret")

    def test_hysteria2_store_encrypts_secrets_and_auth_callback_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.json"
            encryption_key = Fernet.generate_key()
            store = Hysteria2UserStore(path, encryption_key=encryption_key)
            record = store.create_user("h2-1", "Customer", "customer-secret")
            self.assertEqual(record["secret"], "customer-secret")
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("customer-secret", raw)
            self.assertEqual(store.authenticate("customer-secret"), {"ok": True, "id": "h2-1"})
            self.assertEqual(store.authenticate("wrong"), {"ok": False})

            restarted = Hysteria2UserStore(path, encryption_key=encryption_key)
            self.assertEqual(restarted.get_user("h2-1")["secret"], "customer-secret")
            self.assertEqual(restarted.authenticate("customer-secret"), {"ok": True, "id": "h2-1"})
            with self.assertRaises(ProviderBackendError):
                Hysteria2UserStore(path, encryption_key=Fernet.generate_key()).get_user("h2-1")

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
                "auth_mode": "http",
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

    def test_hysteria2_store_serializes_concurrent_read_modify_write_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Hysteria2UserStore(
                Path(directory) / "users.json", encryption_key=Fernet.generate_key()
            )
            errors = []

            def add_user(index):
                try:
                    store.create_user(f"user-{index}", f"Customer {index}", f"secret-{index}")
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=add_user, args=(index,)) for index in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(
                {item["external_id"] for item in store.list_users()},
                {f"user-{index}" for index in range(16)},
            )

    def test_hysteria2_quota_request_fails_before_user_creation_without_hard_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Hysteria2UserStore(
                Path(directory) / "users.json", encryption_key=Fernet.generate_key()
            )
            provider = Hysteria2Provider(
                store,
                Hysteria2TrafficStatsClient(
                    "http://127.0.0.1:19000",
                    "stats-secret",
                    requester=lambda *_args: {"online": 0},
                ),
            )
            route = {
                "route_id": "hysteria2:sg-a",
                "endpoint_id": "sg-a",
                "protocol": "hysteria2",
                "public_address": "198.51.100.20",
                "port": 8444,
                "auth_mode": "http",
            }
            with self.assertRaisesRegex(ConnectivityAdapterError, "unsupported"):
                Hysteria2ConnectivityAdapter(provider).provision(
                    route,
                    {"external_id": "h2-quota", "name": "Quota customer", "quota_bytes": 500},
                )
            self.assertEqual(store.list_users(), [])

    def test_hysteria2_usage_rejects_non_integer_counter(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Hysteria2UserStore(
                Path(directory) / "users.json", encryption_key=Fernet.generate_key()
            )

            def requester(_method, path, _body, _headers):
                if path == "/traffic":
                    return {"h2-1": {"tx": 1.2, "rx": 0}}
                raise AssertionError(path)

            provider = Hysteria2Provider(
                store,
                Hysteria2TrafficStatsClient(
                    "http://127.0.0.1:19000", "stats-secret", requester=requester
                ),
            )
            with self.assertRaisesRegex(ProviderBackendError, "not an integer"):
                provider.get_user_usage("h2-1")

    def test_concrete_backends_round_trip_through_authenticated_node_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            xray_path = Path(directory) / "xray.json"
            xray_path.write_text(
                json.dumps(
                    {"inbounds": [{"tag": "aurix-managed", "settings": {"clients": []}}]}
                ),
                encoding="utf-8",
            )
            xray = XrayConfigProvider(
                XrayConfigWriter(xray_path),
                reload_callback=lambda: None,
                stats_query=lambda _external_id: {
                    "stat": [
                        {"name": "user>>>xray-http>>>traffic>>>uplink", "value": 2},
                        {"name": "user>>>xray-http>>>traffic>>>downlink", "value": 5},
                    ]
                },
            )
            xray_client = self._client_for_app(
                create_node_agent_wsgi_app(
                    NodeAgentService(xray, bearer_token="concrete-agent-token")
                )
            )
            xray_adapter = XrayConnectivityAdapter(xray_client)
            xray_grant = xray_adapter.provision(
                {
                    "route_id": "xray:sg-a",
                    "endpoint_id": "sg-a",
                    "public_address": "198.51.100.10",
                    "port": 18443,
                    "public_key": "public-key",
                    "server_name": "example.com",
                    "short_id": "abcd",
                },
                {"external_id": "xray-http", "name": "HTTP Xray"},
            )
            self.assertEqual(xray_adapter.read_usage(xray_grant)["bytes_transferred"], 7)
            self.assertNotIn("secret", json.dumps(xray_client.list_users()))

            h2_store = Hysteria2UserStore(
                Path(directory) / "h2-users.json", encryption_key=Fernet.generate_key()
            )

            def stats_requester(method, path, body, headers):
                if path == "/online":
                    return {}
                if path == "/traffic":
                    return {"h2-http": {"tx": 3, "rx": 4}}
                if path == "/kick":
                    return {"ok": True}
                raise AssertionError((method, path, body, headers))

            h2 = Hysteria2Provider(
                h2_store,
                Hysteria2TrafficStatsClient(
                    "http://127.0.0.1:19000", "stats-secret", requester=stats_requester
                ),
            )
            h2_client = self._client_for_app(
                create_node_agent_wsgi_app(
                    NodeAgentService(h2, bearer_token="concrete-agent-token")
                )
            )
            h2_adapter = Hysteria2ConnectivityAdapter(h2_client)
            h2_grant = h2_adapter.provision(
                {
                    "route_id": "hysteria2:sg-a",
                    "endpoint_id": "sg-a",
                    "public_address": "198.51.100.10",
                    "port": 8444,
                    "server_name": "example.com",
                    "auth_mode": "http",
                },
                {"external_id": "h2-http", "name": "HTTP H2", "secret": "h2-secret"},
            )
            self.assertEqual(h2_adapter.read_usage(h2_grant)["bytes_transferred"], 7)
            self.assertFalse(h2_adapter.terminate_sessions(h2_grant)["terminated"])
            self.assertFalse(h2_adapter.verify_auth_revoked(h2_grant)["verified"])

    def test_hysteria2_provider_rejects_shared_auth_mode_before_user_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Hysteria2UserStore(
                Path(directory) / "users.json", encryption_key=Fernet.generate_key()
            )
            provider = Hysteria2Provider(
                store,
                Hysteria2TrafficStatsClient(
                    "http://127.0.0.1:19000",
                    "stats-secret",
                    requester=lambda *_args: {"online": 0},
                ),
            )
            with self.assertRaisesRegex(ProviderBackendError, "auth_mode=http"):
                provider.create_user(
                    "h2-1", "Customer", {"auth_mode": "password"}, {"secret": "customer-secret"}
                )
            self.assertEqual(store.list_users(), [])


if __name__ == "__main__":
    unittest.main()
