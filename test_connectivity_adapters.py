import unittest

from connectivity_adapters import ConnectivityAdapterError, OutlineConnectivityAdapter


class _Outline:
    def __init__(self):
        self.keys = {}
        self.usage = {}

    def get_key(self, key_id):
        value = self.keys.get(str(key_id))
        return dict(value) if value else None

    def create_key_with_id(self, key_id, name, limit_bytes):
        value = {"id": str(key_id), "name": name, "accessUrl": f"ss://{key_id}"}
        self.keys[str(key_id)] = value
        self.usage[str(key_id)] = 0
        return dict(value)

    def create_key(self, name, limit_bytes):
        return self.create_key_with_id(f"generated-{len(self.keys)}", name, limit_bytes)

    def set_data_limit(self, key_id, limit_bytes):
        self.limit = (str(key_id), int(limit_bytes))

    def transfer_metrics(self):
        return {"bytesTransferredByUserId": dict(self.usage)}

    def delete_key(self, key_id):
        self.keys.pop(str(key_id), None)

    def server_info(self):
        return {"name": "test-outline"}

    def list_keys(self):
        return {"accessKeys": list(self.keys.values())}


class ConnectivityAdapterTest(unittest.TestCase):
    def test_outline_implements_contract_and_keeps_termination_honest(self):
        client = _Outline()
        adapter = OutlineConnectivityAdapter(client)
        route = {"route_id": "route-a", "protocol": "outline"}
        grant = adapter.provision(
            route,
            {"external_id": "key-a", "name": "AuriX route", "quota_bytes": 1000},
        )
        self.assertEqual(grant["external_id"], "key-a")
        self.assertTrue(grant["created"])
        self.assertEqual(adapter.render_manual_export(grant), "ss://key-a")
        self.assertEqual(adapter.read_usage(grant)["bytes_transferred"], 0)
        self.assertEqual(adapter.probe_management(route)["status"], "healthy")
        self.assertFalse(adapter.capabilities["terminate_sessions"])
        self.assertFalse(adapter.terminate_sessions(grant)["terminated"])

    def test_outline_rejects_malformed_provider_grant(self):
        adapter = OutlineConnectivityAdapter(_Outline())
        with self.assertRaises(ConnectivityAdapterError):
            adapter.render_manual_export({"external_id": "key-a"})


if __name__ == "__main__":
    unittest.main()
