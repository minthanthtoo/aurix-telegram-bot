import io
import json
import unittest

from node_agent_app import NodeAgentService, create_node_agent_wsgi_app


class FakeProvider:
    def __init__(self):
        self.users = {}
        self.quotas = {}

    def server_info(self):
        return {"version": "test-agent", "capabilities": ["usage"], "certificate": "secret"}

    def list_users(self):
        return list(self.users.values())

    def get_user(self, external_id):
        return dict(self.users.get(external_id)) if external_id in self.users else None

    def create_user(self, external_id, name, route, intent):
        value = {
            "external_id": external_id,
            "name": name,
            "secret": str(intent.get("secret") or "provider-secret"),
            "access_url": "must-not-cross-agent-response-shape",
        }
        self.users[external_id] = value
        return dict(value)

    def delete_user(self, external_id):
        self.users.pop(external_id, None)

    def set_user_quota(self, external_id, quota_bytes):
        self.quotas[external_id] = quota_bytes

    def get_user_usage(self, external_id):
        return {"tx_bytes": 12, "rx_bytes": 30, "external_id": external_id}

    def terminate_user_sessions(self, _external_id):
        return {"terminated": False, "reason": "provider cannot prove it"}

    def probe_data_plane(self, _route):
        return {"status": "healthy", "exit_ip": "198.51.100.10"}


class NodeAgentAppTest(unittest.TestCase):
    def setUp(self):
        self.provider = FakeProvider()
        self.app = create_node_agent_wsgi_app(
            NodeAgentService(self.provider, bearer_token="agent-token")
        )

    def request(self, method, path, payload=None, token="agent-token"):
        body = b"" if payload is None else json.dumps(payload).encode()
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_LENGTH": str(len(body)),
            "HTTP_AUTHORIZATION": f"Bearer {token}" if token is not None else "",
            "wsgi.input": io.BytesIO(body),
        }
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = headers

        result = b"".join(self.app(environ, start_response))
        return captured["status"], json.loads(result)

    def test_authentication_and_redaction(self):
        status, value = self.request("GET", "/v1/server", token="wrong")
        self.assertEqual(status, "401 Error")
        self.assertNotIn("certificate", json.dumps(value))

        status, value = self.request("GET", "/v1/server")
        self.assertEqual(status, "200 OK")
        self.assertEqual(value["version"], "test-agent")
        self.assertNotIn("certificate", json.dumps(value))

    def test_lifecycle_routes_match_node_agent_client_contract(self):
        status, created = self.request(
            "POST",
            "/v1/users",
            {
                "external_id": "user-a",
                "name": "Customer A",
                "route": {"endpoint_id": "sg-a"},
                "intent": {"secret": "customer-secret"},
            },
        )
        self.assertEqual(status, "200 OK")
        self.assertEqual(created["external_id"], "user-a")
        self.assertEqual(created["secret"], "customer-secret")
        self.assertNotIn("access_url", created)

        status, inventory = self.request("GET", "/v1/users")
        self.assertEqual(status, "200 OK")
        self.assertNotIn("customer-secret", json.dumps(inventory))
        self.assertNotIn("access_url", json.dumps(inventory))

        status, user = self.request("GET", "/v1/users/user-a")
        self.assertEqual(status, "200 OK")
        self.assertEqual(user["secret"], "customer-secret")
        status, quota = self.request(
            "PATCH", "/v1/users/user-a/quota", {"quota_bytes": 500}
        )
        self.assertEqual(status, "200 OK")
        self.assertEqual(quota["quota_bytes"], 500)
        self.assertEqual(self.provider.quotas["user-a"], 500)

        status, usage = self.request("GET", "/v1/users/user-a/usage")
        self.assertEqual(status, "200 OK")
        self.assertEqual(usage["rx_bytes"], 30)
        status, terminated = self.request(
            "POST", "/v1/users/user-a/sessions/terminate"
        )
        self.assertEqual(status, "200 OK")
        self.assertFalse(terminated["terminated"])

        status, probe = self.request("POST", "/v1/probe", {"endpoint_id": "sg-a"})
        self.assertEqual(status, "200 OK")
        self.assertEqual(probe["status"], "healthy")

        status, deleted = self.request("DELETE", "/v1/users/user-a")
        self.assertEqual(status, "200 OK")
        self.assertTrue(deleted["deleted"])
        status, missing = self.request("GET", "/v1/users/user-a")
        self.assertEqual(status, "404 Error")
        self.assertIn("not found", missing["error"])

    def test_request_bounds_and_path_validation_fail_closed(self):
        status, value = self.request("PATCH", "/v1/users/user-a/quota", {"quota_bytes": -1})
        self.assertEqual(status, "400 Error")
        self.assertIn("positive", value["error"])
        status, value = self.request("GET", "/v1/users/a%2Fb")
        self.assertEqual(status, "400 Error")
        self.assertIn("identifier", value["error"])


if __name__ == "__main__":
    unittest.main()
