import unittest
from datetime import datetime, timezone

from aurix_vpn.commerce_worker import CommerceWorkerMixin
from commerce import CommerceError
from connectivity_adapters import (
    ConnectivityAdapterError,
    ConnectivityAdapterRegistry,
    Hysteria2ConnectivityAdapter,
    OutlineConnectivityAdapter,
    XrayConnectivityAdapter,
)


class _Outline:
    def __init__(self):
        self.keys = {}
        self.usage = {}
        self.create_calls = 0
        self.ambiguous = False

    def get_key(self, key_id):
        value = self.keys.get(str(key_id))
        return dict(value) if value else None

    def create_key_with_id(self, key_id, name, limit_bytes):
        self.create_calls += 1
        value = {"id": str(key_id), "name": name, "accessUrl": f"ss://{key_id}"}
        self.keys[str(key_id)] = value
        self.usage[str(key_id)] = 0
        if self.ambiguous:
            raise TimeoutError("request timed out after commit")
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


class _ProtocolClient:
    def __init__(self):
        self.users = {}
        self.usage = {}
        self.limits = {}
        self.ambiguous = False

    def get_user(self, external_id):
        value = self.users.get(str(external_id))
        return dict(value) if value else None

    def create_user(self, external_id, name, route, intent):
        value = {
            "external_id": str(external_id),
            "name": name,
            "protocol": str(route.get("protocol") or ""),
            "secret": intent["secret"],
        }
        self.users[str(external_id)] = value
        self.usage[str(external_id)] = {"tx": 0, "rx": 0}
        if self.ambiguous:
            raise TimeoutError("request timed out after commit")
        return dict(value)

    def set_user_quota(self, external_id, limit):
        self.limits[str(external_id)] = int(limit)

    def get_user_usage(self, external_id):
        return dict(self.usage.get(str(external_id), {"tx": 0, "rx": 0}))

    def delete_user(self, external_id):
        self.users.pop(str(external_id), None)

    def terminate_user_sessions(self, external_id):
        return True

    def list_users(self):
        return list(self.users.values())

    def server_info(self):
        return {"name": "test-node"}

    def probe_data_plane(self, route):
        return {"exit_ip": route["public_address"]}


class _WorkerHarness(CommerceWorkerMixin):
    def __init__(self, client, route, access_url):
        self.outline = client
        self.connectivity = None
        self.adapter_registry = ConnectivityAdapterRegistry({"xray": XrayConnectivityAdapter})
        self.identity = type(
            "IdentityFixture",
            (),
            {"generations_for_accounting": lambda _self: [{
                "endpoint_id": route["endpoint_id"],
                "protocol": "xray",
                "generation_id": "generation-a",
                "external_id": "uuid-a",
                "access_url_ciphertext": access_url,
                "entitlement_key": "paid:sub-1",
                "status": "active",
                "remote_state": "observed",
            }]},
        )()

    @staticmethod
    def _decrypt_access_url(value):
        return value


class ConnectivityAdapterTest(unittest.TestCase):
    def test_protocol_readiness_distinguishes_enabled_candidates_and_unimplemented(self):
        readiness = ConnectivityAdapterRegistry().protocol_readiness()
        by_protocol = {item["protocol"]: item for item in readiness}
        self.assertEqual(by_protocol["outline"]["status"], "enabled")
        self.assertEqual(by_protocol["xray"]["status"], "candidate")
        self.assertEqual(by_protocol["hysteria2"]["status"], "candidate")
        self.assertEqual(by_protocol["wireguard"]["status"], "unimplemented")

        registered = ConnectivityAdapterRegistry({"xray": XrayConnectivityAdapter})
        registered_xray = {
            item["protocol"]: item for item in registered.protocol_readiness()
        }["xray"]
        self.assertTrue(registered_xray["registered"])
        self.assertEqual(registered_xray["status"], "candidate")
        self.assertIn("endpoint evidence", registered_xray["activation_gate"])

        malformed = ConnectivityAdapterRegistry({"xray": lambda _client: object()})
        self.assertFalse(malformed.is_registered("xray"))
        self.assertFalse(by_protocol["xray"]["registered"])

    def test_ambiguous_readback_is_uncertain_and_not_owned(self):
        client = _Outline()
        client.ambiguous = True
        grant = OutlineConnectivityAdapter(client).provision(
            {"route_id": "route-a", "endpoint_id": "sg-a", "protocol": "outline"},
            {"external_id": "key-a", "name": "AuriX route", "quota_bytes": 1000},
        )
        self.assertEqual(grant["external_id"], "key-a")
        self.assertFalse(grant["created"])
        self.assertEqual(grant["ownership"], "uncertain")
        self.assertEqual(client.create_calls, 1)

    def test_outline_contract_is_explicit_about_force_disconnect(self):
        adapter = OutlineConnectivityAdapter(_Outline())
        self.assertFalse(adapter.capabilities["terminate_sessions"])
        with self.assertRaises(ConnectivityAdapterError):
            adapter.render_manual_export({"external_id": "key-a"})

    def test_provider_usage_rejects_boolean_counters(self):
        client = _ProtocolClient()
        adapter = XrayConnectivityAdapter(client)
        for counters in ({"tx_bytes": True, "rx_bytes": 0}, {"tx_bytes": False, "tx": 10}):
            with self.subTest(counters=counters):
                client.get_user_usage = lambda _external_id, value=counters: value
                with self.assertRaisesRegex(
                    ConnectivityAdapterError, "provider usage is not an integer"
                ):
                    adapter.read_usage(
                        {
                            "external_id": "uuid-a",
                            "access_url": "vless://uuid-a@example.com:18443",
                        }
                    )

    def test_invalid_protocol_quota_fails_before_provider_creation(self):
        for value in (True, 0, 1.2, "not-a-number"):
            with self.subTest(value=value):
                client = _ProtocolClient()
                adapter = XrayConnectivityAdapter(client)
                with self.assertRaisesRegex(
                    ConnectivityAdapterError, "quota is not a positive integer"
                ):
                    adapter.provision(
                        self._xray_route(),
                        {"external_id": "uuid-a", "name": "customer-a", "quota_bytes": value},
                    )
                self.assertEqual(client.users, {})

    def test_invalid_protocol_route_host_and_port_fail_before_provider_creation(self):
        route = self._xray_route()
        for field, values, message in (
            (
                "public_address",
                ("https://example.com/path", "bad/host", "bad host"),
                "route host is invalid",
            ),
            ("port", (True, 0, 65_536, 18_443.0, "18443.0"), "route port is invalid"),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    client = _ProtocolClient()
                    adapter = XrayConnectivityAdapter(client)
                    with self.assertRaisesRegex(ConnectivityAdapterError, message):
                        adapter.provision(
                            {**route, field: value},
                            {"external_id": "uuid-a", "name": "customer-a"},
                        )
                    self.assertEqual(client.users, {})

    def test_adapter_rejects_a_route_for_another_protocol(self):
        client = _ProtocolClient()
        route = self._xray_route()
        route["protocol"] = "hysteria2"
        with self.assertRaisesRegex(
            ConnectivityAdapterError, "cannot handle hysteria2 route"
        ):
            XrayConnectivityAdapter(client).provision(
                route, {"external_id": "uuid-a", "name": "customer-a"}
            )
        self.assertEqual(client.users, {})

    def test_xray_adapter_rejects_an_unsupported_inbound_protocol_before_creation(self):
        client = _ProtocolClient()
        with self.assertRaisesRegex(ConnectivityAdapterError, "xray_protocol=vless"):
            XrayConnectivityAdapter(client).provision(
                {**self._xray_route(), "xray_protocol": "trojan"},
                {"external_id": "customer-a", "name": "Customer A"},
            )
        self.assertEqual(client.users, {})

    def test_adapter_rejects_a_grant_for_another_protocol(self):
        client = _ProtocolClient()
        grant = XrayConnectivityAdapter(client).provision(
            self._xray_route(), {"external_id": "uuid-a", "name": "customer-a"}
        )
        with self.assertRaisesRegex(
            ConnectivityAdapterError, "cannot handle xray grant"
        ):
            Hysteria2ConnectivityAdapter(client).read_usage(grant)

    @staticmethod
    def _xray_route():
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

    def test_xray_lifecycle_is_route_bound_and_per_user(self):
        client = _ProtocolClient()
        adapter = XrayConnectivityAdapter(client)
        route = self._xray_route()
        grant = adapter.provision(
            route,
            {"external_id": "uuid-a", "name": "customer-a", "quota_bytes": 1000},
        )
        self.assertEqual(grant["external_id"], "uuid-a")
        self.assertTrue(grant["access_url"].startswith("vless://uuid-a@198.51.100.10:18443?"))
        self.assertIn("security=reality", grant["access_url"])
        self.assertIn("pbk=public-key", grant["access_url"])
        self.assertEqual(client.limits["uuid-a"], 1000)
        self.assertEqual(adapter.read_usage(grant)["bytes_transferred"], 0)
        client.usage["uuid-a"] = {"tx": 12, "rx": 30}
        self.assertEqual(adapter.read_usage(grant)["bytes_transferred"], 42)
        self.assertTrue(adapter.capabilities["quota_cap"])
        self.assertTrue(adapter.capabilities["terminate_sessions"])
        self.assertEqual(adapter.probe_data_plane(route)["status"], "healthy")
        self.assertEqual(adapter.reconcile(route)["users"], 1)
        self.assertEqual(adapter.inventory(route)["external_ids"], ["uuid-a"])
        rotated = adapter.rotate(grant)
        self.assertNotEqual(rotated["external_id"], grant["external_id"])
        self.assertTrue(rotated["access_url"].startswith("vless://"))
        adapter.terminate_sessions(grant)
        adapter.revoke_auth(grant)
        self.assertEqual(adapter.verify_auth_revoked(grant)["verified"], True)
        adapter.revoke_auth(rotated)
        self.assertIsNone(client.get_user("uuid-a"))

    def test_idempotent_managed_provision_preserves_route_for_rotation(self):
        client = _ProtocolClient()
        adapter = XrayConnectivityAdapter(client)
        route = self._xray_route()
        first = adapter.provision(
            route, {"external_id": "uuid-a", "name": "customer-a"}
        )
        second = adapter.provision(
            route, {"external_id": "uuid-a", "name": "customer-a"}
        )

        self.assertEqual(second["ownership"], "preexisting")
        self.assertEqual(second["route"], route)
        self.assertEqual(second["credential_intent"], {"external_id": "uuid-a", "name": "customer-a"})
        rotated = adapter.rotate(second)
        self.assertNotEqual(rotated["external_id"], first["external_id"])
        self.assertTrue(rotated["access_url"].startswith("vless://"))

    def test_structured_provider_results_are_not_coerced_to_success(self):
        client = _ProtocolClient()
        adapter = XrayConnectivityAdapter(client)
        route = self._xray_route()
        grant = adapter.provision(route, {"external_id": "uuid-a", "name": "customer-a"})
        client.terminate_user_sessions = lambda _external_id: {"terminated": False, "reason": "busy"}
        result = adapter.terminate_sessions(grant)
        self.assertTrue(result["supported"])
        self.assertFalse(result["terminated"])

        client.probe_data_plane = lambda _route: {"status": "failed", "reason": "egress blocked"}
        self.assertEqual(adapter.probe_data_plane(route)["status"], "failed")

    def test_malformed_probe_results_fail_closed(self):
        client = _ProtocolClient()
        adapter = XrayConnectivityAdapter(client)
        route = self._xray_route()
        client.server_info = lambda: "not-an-object"
        client.probe_data_plane = lambda _route: "not-an-object"
        self.assertEqual(adapter.probe_management(route)["status"], "failed")
        self.assertEqual(adapter.probe_data_plane(route)["status"], "failed")

    def test_xray_ambiguous_create_is_not_owned(self):
        client = _ProtocolClient()
        client.ambiguous = True
        grant = XrayConnectivityAdapter(client).provision(
            self._xray_route(), {"external_id": "uuid-a", "name": "customer-a"}
        )
        self.assertEqual(grant["ownership"], "uncertain")
        self.assertFalse(grant["created"])

    def test_hysteria2_uses_customer_scoped_secret_and_stats(self):
        client = _ProtocolClient()
        adapter = Hysteria2ConnectivityAdapter(client)
        route = {
            "route_id": "hysteria2:sg-a",
            "endpoint_id": "sg-a",
            "protocol": "hysteria2",
            "public_address": "198.51.100.10",
            "port": 8444,
            "server_name": "example.com",
            "auth_mode": "http",
        }
        grant = adapter.provision(route, {"external_id": "customer-a", "name": "customer-a", "quota_bytes": 500})
        self.assertTrue(grant["access_url"].startswith("hysteria2://"))
        self.assertIn("@198.51.100.10:8444/", grant["access_url"])
        self.assertNotIn("61604", grant["access_url"])
        with self.assertRaisesRegex(ConnectivityAdapterError, "must be a boolean"):
            adapter.provision(
                {**route, "insecure": "false"},
                {"external_id": "customer-b", "name": "customer-b"},
            )
        with self.assertRaisesRegex(ConnectivityAdapterError, "route port is invalid"):
            adapter.provision(
                {**route, "port": 0},
                {"external_id": "customer-b", "name": "customer-b"},
            )
        self.assertNotIn("customer-b", client.users)
        self.assertEqual(adapter.read_usage(grant)["bytes_transferred"], 0)
        self.assertEqual(adapter.reconcile(route)["users"], 1)
        client.users.clear()
        restored = adapter.reconcile_credentials(route, [grant])
        self.assertEqual(restored["restored"], 1)
        self.assertEqual(client.users["customer-a"]["secret"], grant["secret"])

    def test_protocol_scoped_inventory_excludes_other_declared_transports(self):
        client = _ProtocolClient()
        xray = XrayConnectivityAdapter(client)
        hysteria2 = Hysteria2ConnectivityAdapter(client)
        xray_grant = xray.provision(
            self._xray_route(), {"external_id": "xray-user", "name": "Xray customer"}
        )
        hysteria2_route = {
            "route_id": "hysteria2:sg-a",
            "endpoint_id": "sg-a",
            "protocol": "hysteria2",
            "public_address": "198.51.100.10",
            "port": 18444,
            "server_name": "example.com",
            "auth_mode": "http",
        }
        hysteria2_grant = hysteria2.provision(
            hysteria2_route,
            {"external_id": "hysteria2-user", "name": "Hysteria customer"},
        )

        self.assertEqual(xray.reconcile(self._xray_route())["users"], 1)
        self.assertEqual(hysteria2.reconcile(hysteria2_route)["users"], 1)
        self.assertEqual(xray.inventory(self._xray_route())["external_ids"], ["xray-user"])
        self.assertEqual(
            hysteria2.inventory(hysteria2_route)["external_ids"], ["hysteria2-user"]
        )
        self.assertEqual(xray_grant["protocol"], "xray")
        self.assertEqual(hysteria2_grant["protocol"], "hysteria2")

    def test_hysteria2_rejects_shared_or_undeclared_auth_before_user_creation(self):
        route = {
            "route_id": "hysteria2:sg-a",
            "endpoint_id": "sg-a",
            "protocol": "hysteria2",
            "public_address": "198.51.100.10",
            "port": 8444,
        }
        for auth_mode in (None, "password", "userpass"):
            with self.subTest(auth_mode=auth_mode):
                client = _ProtocolClient()
                candidate = dict(route)
                if auth_mode is not None:
                    candidate["auth_mode"] = auth_mode
                with self.assertRaisesRegex(ConnectivityAdapterError, "auth_mode=http"):
                    Hysteria2ConnectivityAdapter(client).provision(
                        candidate, {"external_id": "customer-a", "name": "customer-a"}
                    )
                self.assertEqual(client.users, {})

    def test_worker_collects_enabled_managed_inventory_by_protocol(self):
        client = _ProtocolClient()
        route = self._xray_route()
        XrayConnectivityAdapter(client).provision(
            route, {"external_id": "uuid-a", "name": "customer-a"}
        )
        client.users["unknown-user"] = {"external_id": "unknown-user", "name": "foreign"}
        harness = _WorkerHarness(client, route, "vless://uuid-a@example.com:18443")
        harness.connectivity = type(
            "ConnectivityFixture",
            (),
            {
                "list_endpoints": lambda _self: [{"id": "sg-a", "state": "ACTIVE"}],
                "list_protocol_profiles": lambda _self, _endpoint_id: [
                    {"protocol": "xray", "status": "enabled"}
                ],
            },
        )()
        harness.managed_route_provider = lambda _endpoint_id, _protocol: dict(route)
        harness.managed_adapter_provider = lambda _route: XrayConnectivityAdapter(client)

        result = harness.collect_managed_inventory()

        self.assertEqual(
            result["byEndpointProtocol"]["sg-a"]["xray"],
            {"uuid-a": "", "unknown-user": ""},
        )
        self.assertEqual(result["errors"], {})
        self.assertEqual(harness.managed_routes(), [route])

    def test_worker_records_bounded_managed_health_observations(self):
        client = _ProtocolClient()
        route = self._xray_route()
        harness = _WorkerHarness(client, route, "vless://uuid-a@example.com:18443")
        recorded = []
        harness.connectivity = type(
            "HealthConnectivityFixture",
            (),
            {
                "list_endpoints": lambda _self: [{"id": "sg-a", "state": "ACTIVE"}],
                "list_protocol_profiles": lambda _self, _endpoint_id: [
                    {"protocol": "xray", "status": "enabled"}
                ],
                "record_protocol_observation": lambda _self, endpoint_id, protocol, **kwargs: recorded.append(
                    (endpoint_id, protocol, kwargs)
                ),
            },
        )()
        harness.managed_route_provider = lambda _endpoint_id, _protocol: dict(route)
        harness.managed_adapter_provider = lambda _route: XrayConnectivityAdapter(client)

        result = harness.collect_managed_protocol_health()

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["routes"], 1)
        self.assertEqual(result["observations"], 2)
        self.assertEqual(result["errors"], {})
        self.assertEqual({item[2]["signal"] for item in recorded}, {"management", "data_plane"})
        self.assertTrue(all(item[2]["source"] == "maintenance" for item in recorded))
        self.assertTrue(all(item[2]["expires_at"] > item[2]["observed_at"] for item in recorded))
        self.assertNotIn("exit_ip", str(recorded))

    def test_worker_retries_pending_session_termination_without_releasing_unproven_lease(self):
        client = _ProtocolClient()
        route = self._xray_route()
        harness = _WorkerHarness(client, route, "vless://uuid-a@example.com:18443")
        pending = {
            "endpoint_id": route["endpoint_id"],
            "protocol": "xray",
            "generation_id": "generation-a",
            "external_id": "uuid-a",
            "access_url_ciphertext": "vless://uuid-a@example.com:18443",
            "status": "retiring",
            "remote_state": "revoked_verified",
        }
        harness.identity.pending_session_termination_generations = lambda **_kwargs: [pending]
        finalized = []
        harness.identity.mark_sessions_terminated = (
            lambda generation_id, **_kwargs: finalized.append(generation_id) or True
        )
        harness.managed_route_provider = lambda _endpoint_id, _protocol: dict(route)
        harness.managed_adapter_provider = lambda _route: XrayConnectivityAdapter(client)

        client.terminate_user_sessions = lambda _external_id: {
            "terminated": False,
            "reason": "existing session remains",
        }
        result = harness.reconcile_managed_session_terminations()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["finalized"], 0)
        self.assertEqual(finalized, [])

        client.terminate_user_sessions = lambda _external_id: True
        result = harness.reconcile_managed_session_terminations()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["finalized"], 1)
        self.assertEqual(finalized, ["generation-a"])

    def test_worker_fails_closed_when_managed_delete_readback_is_unavailable(self):
        client = _ProtocolClient()
        route = self._xray_route()
        harness = _WorkerHarness(client, route, "vless://uuid-a@example.com:18443")
        recorded = []
        harness.identity.mark_remote_revoked = lambda *args, **kwargs: recorded.append(
            (args, kwargs)
        )
        harness.identity.generations_for_accounting = lambda _entitlement: [{
            "endpoint_id": route["endpoint_id"],
            "protocol": "xray",
            "generation_id": "generation-a",
            "external_id": "uuid-a",
            "access_url_ciphertext": "vless://uuid-a@example.com:18443",
        }]
        harness.managed_route_provider = lambda _endpoint_id, _protocol: dict(route)
        harness.managed_adapter_provider = lambda _route: XrayConnectivityAdapter(client)
        client.users["uuid-a"] = {"external_id": "uuid-a", "name": "customer-a"}
        client.get_user = None

        with self.assertRaisesRegex(
            CommerceError, "remote credential deletion could not be verified"
        ):
            harness._revoke_generation_set("paid:sub-1", datetime.now(timezone.utc))
        self.assertEqual(len(recorded), 1)

    def test_worker_requires_recovery_authority_when_available(self):
        client = _ProtocolClient()
        route = self._xray_route()
        harness = _WorkerHarness(client, route, "vless://uuid-a@example.com:18443")
        harness.identity.recovery_authorization = lambda *_args, **_kwargs: {
            "authorized": False,
            "reason": "entitlement_expired",
        }
        result = harness.reconcile_managed_route(route)
        self.assertEqual(result["expected_credentials"], 0)
        self.assertEqual(result["recovery_denied"], 1)
        self.assertNotIn("uuid-a", client.users)

    def test_worker_applies_recovery_lease_cap_only_to_recreated_user(self):
        client = _ProtocolClient()
        route = self._xray_route()
        harness = _WorkerHarness(client, route, "vless://uuid-a@example.com:18443")
        harness.identity.recovery_authorization = lambda *_args, **_kwargs: {
            "authorized": True,
            "remaining_bytes": 321,
        }
        result = harness.reconcile_managed_route(route)
        self.assertEqual(result["restored"], 1)
        self.assertEqual(client.limits["uuid-a"], 321)

        client.users["uuid-a"] = {"external_id": "uuid-a", "name": "customer-a"}
        client.limits["uuid-a"] = 999
        result = harness.reconcile_managed_route(route)
        self.assertEqual(result["already_present"], 1)
        self.assertEqual(client.limits["uuid-a"], 999)

    def test_worker_rehydrates_missing_xray_generation_without_deleting_unknown_users(self):
        client = _ProtocolClient()
        route = self._xray_route()
        grant = XrayConnectivityAdapter(client).provision(
            route, {"external_id": "uuid-a", "name": "customer-a"}
        )
        client.users["unknown-user"] = {"external_id": "unknown-user", "name": "foreign"}
        client.users.pop("uuid-a")
        result = _WorkerHarness(client, route, grant["access_url"]).reconcile_managed_route(route)
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["restored"], 1)
        self.assertEqual(result["unknown_provider_users"], 1)
        self.assertIn("uuid-a", client.users)
        self.assertIn("unknown-user", client.users)

    def test_worker_does_not_recreate_unowned_or_pending_revoke_generation(self):
        client = _ProtocolClient()
        route = self._xray_route()
        harness = _WorkerHarness(client, route, "vless://uuid-a@example.com:18443")
        harness.identity.generations_for_accounting = lambda: [{
            "endpoint_id": route["endpoint_id"],
            "protocol": "xray",
            "external_id": "uuid-a",
            "access_url_ciphertext": "vless://uuid-a@example.com:18443",
            "entitlement_key": "paid:sub-1",
            "status": "active",
            "remote_state": "unknown",
        }]
        result = harness.reconcile_managed_route(route)
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["expected_credentials"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertNotIn("uuid-a", client.users)

    def test_new_protocols_are_opt_in_until_live_evidence(self):
        route = self._xray_route()
        with self.assertRaises(ConnectivityAdapterError):
            ConnectivityAdapterRegistry().for_route(route, _ProtocolClient())
        registry = ConnectivityAdapterRegistry({"xray": XrayConnectivityAdapter})
        self.assertIsInstance(registry.for_route(route, _ProtocolClient()), XrayConnectivityAdapter)


if __name__ == "__main__":
    unittest.main()
