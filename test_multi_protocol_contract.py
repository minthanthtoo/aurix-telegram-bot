import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from connectivity_adapters import Hysteria2ConnectivityAdapter, XrayConnectivityAdapter
from node_agent import NodeAgentClient, NodeAgentError
from node_agent_app import NodeAgentService, create_node_agent_wsgi_app


class ThreadSafeProtocolProvider:
    """Small in-memory provider for bounded mixed-protocol contract tests."""

    def __init__(self):
        self.lock = threading.RLock()
        self.users = {}
        self.quotas = {}

    def server_info(self):
        return {"version": "matrix-agent", "capabilities": ["usage", "quota"]}

    def list_users(self):
        with self.lock:
            return [dict(value) for value in self.users.values()]

    def get_user(self, external_id):
        with self.lock:
            value = self.users.get(str(external_id))
            return dict(value) if value else None

    def create_user(self, external_id, name, route, intent):
        with self.lock:
            value = {
                "external_id": str(external_id),
                "name": str(name),
                "protocol": str(route.get("protocol")),
                "secret": str(intent.get("secret")),
            }
            self.users[str(external_id)] = value
            self.quotas[str(external_id)] = int(intent.get("quota_bytes") or 0)
            return dict(value)

    def set_user_quota(self, external_id, quota_bytes):
        with self.lock:
            self.quotas[str(external_id)] = int(quota_bytes)

    def get_user_usage(self, external_id):
        with self.lock:
            return {"external_id": str(external_id), "tx_bytes": 7, "rx_bytes": 11}

    def delete_user(self, external_id):
        with self.lock:
            self.users.pop(str(external_id), None)

    def terminate_user_sessions(self, _external_id):
        return {"terminated": True}

    def probe_data_plane(self, route):
        return {"status": "healthy", "exit_ip": route["public_address"]}


class MultiProtocolContractTest(unittest.TestCase):
    def setUp(self):
        provider = ThreadSafeProtocolProvider()
        self.provider = provider
        self.app = create_node_agent_wsgi_app(
            NodeAgentService(provider, bearer_token="matrix-token")
        )

    def requester(self, method, path, payload):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_LENGTH": str(len(body)),
            "HTTP_AUTHORIZATION": "Bearer matrix-token",
            "wsgi.input": io.BytesIO(body),
        }
        captured = {}

        def start_response(status, _headers):
            captured["status"] = status

        value = json.loads(b"".join(self.app(environ, start_response)))
        code = int(str(captured["status"]).split(" ", 1)[0])
        if code >= 400:
            raise NodeAgentError(value.get("error") or "agent request failed", status_code=code)
        return value

    @staticmethod
    def xray_route():
        return {
            "route_id": "xray:sg-a",
            "endpoint_id": "sg-a",
            "protocol": "xray",
            "public_address": "198.51.100.10",
            "port": 18443,
            "public_key": "public-key",
            "server_name": "example.com",
            "short_id": "abcd",
        }

    @staticmethod
    def hysteria2_route():
        return {
            "route_id": "hysteria2:sg-a",
            "endpoint_id": "sg-a",
            "protocol": "hysteria2",
            "public_address": "198.51.100.10",
            "port": 18444,
            "server_name": "example.com",
        }

    def test_xray_and_hysteria2_are_isolated_under_bounded_concurrency(self):
        def issue(index):
            if index % 2:
                protocol = "xray"
                adapter = XrayConnectivityAdapter(NodeAgentClient(requester=self.requester))
                route = self.xray_route()
            else:
                protocol = "hysteria2"
                adapter = Hysteria2ConnectivityAdapter(NodeAgentClient(requester=self.requester))
                route = self.hysteria2_route()
            external_id = f"{protocol}-customer-{index}"
            grant = adapter.provision(
                route,
                {"external_id": external_id, "name": external_id, "quota_bytes": 1_000_000},
            )
            return adapter, route, grant

        with ThreadPoolExecutor(max_workers=8) as executor:
            grants = list(executor.map(issue, range(16)))

        self.assertEqual(len(self.provider.users), 16)
        self.assertEqual(
            {value["protocol"] for value in self.provider.users.values()},
            {"xray", "hysteria2"},
        )
        self.assertEqual(set(self.provider.quotas.values()), {1_000_000})

        for adapter, route, grant in grants:
            self.assertEqual(adapter.read_usage(grant)["bytes_transferred"], 18)
            self.assertEqual(adapter.probe_data_plane(route)["status"], "healthy")
            self.assertEqual(adapter.reconcile(route)["users"], 16)

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda item: item[0].revoke_auth(item[2]), grants))

        self.assertEqual(self.provider.users, {})


if __name__ == "__main__":
    unittest.main()
