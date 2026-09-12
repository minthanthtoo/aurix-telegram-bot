import json
import unittest

from aurix_vpn.connectivity_adapters import ConnectivityAdapterRegistry
from aurix_vpn.node_agent import NodeAgentClient
from aurix_vpn.node_agent_bindings import (
    ManagedNodeAgentBindings,
    NodeAgentBindingError,
)


class NodeAgentBindingsTest(unittest.TestCase):
    @staticmethod
    def _config():
        return json.dumps(
            [
                {
                    "endpoint_id": "sg-a",
                    "protocol": "xray",
                    "base_url": "https://127.0.0.1:18001",
                    "token": "xray-agent-token",
                    "route": {
                        "public_address": "198.51.100.10",
                        "port": 18443,
                        "public_key": "public-key",
                        "server_name": "example.com",
                        "short_id": "abcd",
                    },
                },
                {
                    "endpoint_id": "sg-a",
                    "protocol": "hysteria2",
                    "base_url": "https://127.0.0.1:18002",
                    "token": "h2-agent-token",
                    "route": {
                        "public_address": "198.51.100.10",
                        "port": 18444,
                        "server_name": "example.com",
                        "auth_mode": "http",
                    },
                },
            ]
        )

    def test_explicit_bindings_construct_protocol_clients_and_adapters(self):
        registry = ConnectivityAdapterRegistry()
        constructed = []

        def factory(protocol, base_url, token):
            constructed.append((protocol, base_url, token))
            return NodeAgentClient(base_url, token=token)

        bindings = ManagedNodeAgentBindings.from_json(
            self._config(), adapter_registry=registry, client_factory=factory
        )
        self.assertTrue(bindings.configured)
        self.assertEqual(
            constructed,
            [
                ("xray", "https://127.0.0.1:18001", "xray-agent-token"),
                ("hysteria2", "https://127.0.0.1:18002", "h2-agent-token"),
            ],
        )
        xray_route = bindings.route_for("sg-a", "xray")
        self.assertEqual(xray_route["route_id"], "xray:sg-a")
        self.assertEqual(bindings.adapter_for(xray_route).protocol, "xray")
        self.assertEqual(bindings.adapter_for(bindings.route_for("sg-a", "hysteria2")).protocol, "hysteria2")
        self.assertEqual({item["protocol"] for item in bindings.routes()}, {"xray", "hysteria2"})
        self.assertTrue(registry.is_registered("xray"))
        self.assertTrue(registry.is_registered("hysteria2"))

    def test_empty_configuration_does_not_register_or_bind_candidates(self):
        registry = ConnectivityAdapterRegistry()
        bindings = ManagedNodeAgentBindings.from_json("", adapter_registry=registry)
        self.assertFalse(bindings.configured)
        self.assertFalse(registry.is_registered("xray"))
        with self.assertRaisesRegex(NodeAgentBindingError, "not configured"):
            bindings.route_for("sg-a", "xray")

    def test_binding_validation_rejects_unsafe_or_inconsistent_configuration(self):
        cases = [
            ([{"endpoint_id": "sg-a", "protocol": "xray", "base_url": "ftp://agent", "token": "t", "route": {}}], r"HTTP\(S\)"),
            ([{"endpoint_id": "sg-a", "protocol": "xray", "base_url": "http://agent", "token": "t", "route": {}}], "HTTPS"),
            ([{"endpoint_id": "sg-a", "protocol": "xray", "base_url": "https://user:pass@agent", "token": "t", "route": {}}], "decorations"),
            ([{"endpoint_id": "sg-a", "protocol": "xray", "base_url": "https://agent", "token": "t", "route": {"secret": "leak"}}], "not allowed"),
            ([{"endpoint_id": "sg-a", "protocol": "xray", "base_url": "https://agent", "token": "t", "route": {"endpoint_id": "sg-b"}}], "does not match"),
            ([{"endpoint_id": "sg-a", "protocol": "wireguard", "base_url": "https://agent", "token": "t", "route": {}}], "unsupported"),
            ([{"endpoint_id": "sg-a", "protocol": "hysteria2", "base_url": "https://agent", "token": "t", "route": {}}], "auth_mode=http"),
            ([{"endpoint_id": "sg-a", "protocol": "hysteria2", "base_url": "https://agent", "token": "t", "route": {"auth_mode": "password"}}], "auth_mode=http"),
        ]
        for value, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(NodeAgentBindingError, message):
                ManagedNodeAgentBindings.from_json(json.dumps(value))

    def test_loopback_http_binding_is_allowed_for_local_agent(self):
        bindings = ManagedNodeAgentBindings.from_json(
            self._config().replace("https://127.0.0.1", "http://127.0.0.1")
        )
        route = bindings.route_for("sg-a", "xray")
        self.assertEqual(bindings.client_for(route).base_url, "http://127.0.0.1:18001/")

    def test_route_cannot_be_substituted_for_another_binding(self):
        bindings = ManagedNodeAgentBindings.from_json(self._config(), adapter_registry=ConnectivityAdapterRegistry())
        route = bindings.route_for("sg-a", "xray")
        route["public_address"] = "198.51.100.99"
        with self.assertRaisesRegex(NodeAgentBindingError, "differs"):
            bindings.client_for(route)


if __name__ == "__main__":
    unittest.main()
