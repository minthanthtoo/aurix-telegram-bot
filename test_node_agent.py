import json
import tempfile
import unittest
from pathlib import Path

from aurix_vpn.node_agent import NodeAgentClient, NodeAgentError, XrayConfigWriter


class NodeAgentTest(unittest.TestCase):
    def test_client_translates_lifecycle_contract_without_network(self):
        calls = []

        def request(method, path, payload):
            calls.append((method, path, payload))
            if path == "/v1/users":
                return {"users": [{"id": "u-1"}]}
            if path.endswith("/usage"):
                return {"rx_bytes": 12, "tx_bytes": 30}
            return {"ok": True}

        client = NodeAgentClient(requester=request)
        self.assertEqual(client.list_users(), [{"id": "u-1"}])
        client.create_user("u-2", "name", {"endpoint_id": "sg"}, {"secret": "s"})
        client.set_user_quota("u-2", 100)
        self.assertEqual(client.get_user_usage("u-2"), {"rx_bytes": 12, "tx_bytes": 30})
        client.delete_user("u-2")
        self.assertEqual(calls[1][0:2], ("POST", "/v1/users"))
        self.assertEqual(calls[2][1], "/v1/users/u-2/quota")

    def test_invalid_user_quota_fails_closed(self):
        with self.assertRaises(NodeAgentError):
            NodeAgentClient(requester=lambda *_: {}).set_user_quota("u", 0)

    def test_xray_writer_is_idempotent_and_preserves_unknown_users(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "xray.json"
            path.write_text(
                json.dumps(
                    {
                        "inbounds": [
                            {
                                "tag": "public",
                                "settings": {"clients": [{"id": "unmanaged"}]},
                            },
                            {
                                "tag": "aurix-managed",
                                "settings": {"clients": [{"id": "unknown", "email": "keep"}]},
                            },
                        ],
                        "log": {"loglevel": "warning"},
                    }
                ),
                encoding="utf-8",
            )
            writer = XrayConfigWriter(path)
            self.assertEqual(writer.upsert_user("aurix-1", "AuriX", intent={"flow": "xtls-rprx-vision"}), {"changed": True, "external_id": "aurix-1"})
            self.assertEqual(writer.upsert_user("aurix-1", "AuriX", intent={"flow": "xtls-rprx-vision"}), {"changed": False, "external_id": "aurix-1"})
            self.assertEqual(writer.remove_user("missing"), {"changed": False, "external_id": "missing"})
            value = json.loads(path.read_text(encoding="utf-8"))
            managed = next(item for item in value["inbounds"] if item["tag"] == "aurix-managed")
            self.assertEqual({item["id"] for item in managed["settings"]["clients"]}, {"unknown", "aurix-1"})
            public = next(item for item in value["inbounds"] if item["tag"] == "public")
            self.assertEqual(public["settings"]["clients"][0]["id"], "unmanaged")
            self.assertEqual(value["log"]["loglevel"], "warning")

    def test_writer_requires_explicit_managed_inbound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "xray.json"
            path.write_text(json.dumps({"inbounds": []}), encoding="utf-8")
            with self.assertRaisesRegex(NodeAgentError, "not found"):
                XrayConfigWriter(path).upsert_user("u", "name")


if __name__ == "__main__":
    unittest.main()
