import json
import tempfile
import threading
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
        client = NodeAgentClient(requester=lambda *_: {})
        for value in (0, True, 1.2, None, "not-a-number"):
            with self.subTest(value=value), self.assertRaises(NodeAgentError):
                client.set_user_quota("u", value)

    def test_client_rejects_invalid_user_identifiers_before_transport(self):
        calls = []

        def request(*args):
            calls.append(args)
            return {}

        client = NodeAgentClient(requester=request)
        invalid = ("", "  ", "a/b", "a\\b", "x" * 257)
        for value in invalid:
            with self.subTest(value=value):
                for operation in (
                    lambda: client.get_user(value),
                    lambda: client.create_user(value, "name", {}, {}),
                    lambda: client.delete_user(value),
                    lambda: client.set_user_quota(value, 1),
                    lambda: client.get_user_usage(value),
                    lambda: client.terminate_user_sessions(value),
                ):
                    with self.assertRaisesRegex(NodeAgentError, "user identifier"):
                        operation()
        self.assertEqual(calls, [])

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
                                "protocol": "vless",
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

    def test_xray_writer_serializes_concurrent_read_modify_write_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "xray.json"
            path.write_text(
                json.dumps(
                    {"inbounds": [{"tag": "aurix-managed", "protocol": "vless", "settings": {"clients": []}}]}
                ),
                encoding="utf-8",
            )
            writer = XrayConfigWriter(path)
            errors = []

            def add_user(index):
                try:
                    writer.upsert_user(f"user-{index}", f"Customer {index}")
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=add_user, args=(index,)) for index in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(
                {item["external_id"] for item in writer.list_users()},
                {f"user-{index}" for index in range(16)},
            )

    def test_xray_writer_rejects_non_vless_managed_inbound_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "xray.json"
            original = {
                "inbounds": [
                    {"tag": "aurix-managed", "protocol": "trojan", "settings": {"clients": []}}
                ]
            }
            path.write_text(json.dumps(original), encoding="utf-8")

            with self.assertRaisesRegex(NodeAgentError, "protocol vless"):
                XrayConfigWriter(path).upsert_user("customer-a", "Customer")

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)


if __name__ == "__main__":
    unittest.main()
