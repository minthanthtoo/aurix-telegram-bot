import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from commerce import CommerceDatabase
from connectivity_adapters import ConnectivityAdapterRegistry, XrayConnectivityAdapter
from identity import IdentityService
from route_failover import RouteFailoverService
from test_telegram_web_app import _init_data
from vpn_web_api import AuriXVpnWebApplication, make_handler
from http.server import ThreadingHTTPServer


class ControlCenterTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = CommerceDatabase(Path(self.tempdir.name) / "control.db")
        self.database.initialize()
        identity = IdentityService(self.database)
        identity.ensure_account(12345)
        failover = RouteFailoverService(self.database)
        commerce = SimpleNamespace(
            identity=identity,
            adapter_registry=ConnectivityAdapterRegistry(),
            consistency_report=lambda: {"failed_jobs": 0, "pending_receipts": 0},
            failover=failover,
            failed_jobs=lambda limit=100, include_nonterminal=True: [],
            list_pending_orders=lambda limit=100: [],
            endpoint_plan_capacity=lambda endpoint_id: [
                {
                    "plan_code": "basic",
                    "enabled": True,
                    "max_active_assignments": 10,
                    "active_assignments": 2,
                    "reserved_quota_bytes": 999,
                }
            ],
        )
        connectivity = SimpleNamespace(
            endpoint=lambda endpoint_id: {
                "id": endpoint_id,
                "code": "BKK-A",
                "provider": "manual",
                "region": "bkk1",
                "state": "ACTIVE",
                "accepts_new_assignments": 1,
                "outline_version": "x",
                "max_active_keys": 100,
                "reserved_transfer_bytes": 1,
                "public_address": "198.51.100.10",
                "provider_resource_id": "do-secret",
                "management_url_ciphertext": "secret",
            },
            list_endpoints=lambda: [{
                "id": "bkk-a", "code": "BKK-A", "provider": "manual", "provider_resource_id": "do-secret",
                "region": "bkk1", "state": "ACTIVE", "accepts_new_assignments": 1,
                "outline_version": "x", "max_active_keys": 100, "reserved_transfer_bytes": 1,
                "active_assignments": 2, "last_healthy_at": "2026-09-10T00:00:00+00:00",
                "public_address": "198.51.100.10", "management_url_ciphertext": "secret",
                "protocols": [{
                    "protocol": "outline", "adapter_type": "outline", "status": "enabled",
                    "capabilities": {"usage": True}, "verified_at": "2026-09-10T00:00:00+00:00",
                    "last_healthy_at": "2026-09-10T00:00:00+00:00",
                }],
            }],
            list_protocol_profiles=lambda endpoint_id=None: [{
                "protocol": "outline", "adapter_type": "outline", "status": "enabled",
                "capabilities": {"usage": True}, "verified_at": "2026-09-10T00:00:00+00:00",
                "last_healthy_at": "2026-09-10T00:00:00+00:00",
            }],
            list_protocol_observations=lambda endpoint_id=None, limit=100: [{
                "protocol": "outline", "signal": "management", "status": "healthy",
                "details": {"secret": "must-not-leak", "status_code": 200},
                "source": "test", "observed_at": "2026-09-10T00:00:00+00:00",
            }],
        )
        self.runtime = SimpleNamespace(
            token="bot-token", commerce=commerce, commerce_database=self.database,
            connectivity=connectivity,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_summary_and_fleet_are_read_only_and_redacted(self):
        self.runtime.connectivity.cached_usage_metrics = lambda: {
            "byEndpoint": {"bkk-a": {"key-1": 123}},
            "errors": {},
            "source": "maintenance_snapshot",
            "latest_observed_at": "2026-09-12T00:00:00+00:00",
            "snapshot_max_age_seconds": 1800,
        }
        with patch.dict(
            os.environ,
            {"ADMIN_TELEGRAM_IDS": "12345", "AURIX_ENDPOINT_HEALTH_MAX_AGE_SECONDS": "30"},
            clear=False,
        ):
            app = AuriXVpnWebApplication(self.runtime)
            summary = app.admin_summary()
            fleet = app.admin_fleet()
        self.assertEqual(summary["management_mode"], "read-only")
        self.assertEqual(summary["counts"]["accounts"], 1)
        readiness = {item["protocol"]: item for item in summary["protocol_readiness"]}
        self.assertEqual(readiness["outline"]["status"], "enabled")
        self.assertEqual(readiness["xray"]["status"], "candidate")
        self.assertEqual(readiness["wireguard"]["status"], "unimplemented")
        self.assertEqual(fleet[0]["code"], "BKK-A")
        self.assertFalse(fleet[0]["healthy"])
        self.assertEqual(summary["fleet"], {"endpoints": 1, "healthy": 0})
        self.assertEqual(summary["usage_snapshot"]["status"], "healthy")
        self.assertEqual(summary["usage_snapshot"]["failed_endpoint_count"], 0)
        self.assertEqual(summary["device_policy"], {"status": "unconfigured", "max_active_devices": None})
        self.assertNotIn("public_address", json.dumps(fleet))
        self.assertNotIn("management_url", json.dumps(fleet))
        self.assertNotIn("provider_resource_id", json.dumps(fleet))

        detail = app.admin_endpoint("bkk-a")
        self.assertEqual(detail["endpoint"]["code"], "BKK-A")
        self.assertFalse(detail["endpoint"]["healthy"])
        self.assertNotIn("public_address", json.dumps(detail))
        self.assertNotIn("provider_resource_id", json.dumps(detail))
        self.assertNotIn("management_url_ciphertext", json.dumps(detail))
        self.assertNotIn("must-not-leak", json.dumps(detail))
        self.assertEqual(detail["endpoint"]["protocols"][0]["protocol"], "outline")
        self.assertEqual(detail["capacity_by_plan"][0]["plan_code"], "basic")
        self.assertNotIn("reserved_quota_bytes", json.dumps(detail["capacity_by_plan"]))
        self.assertEqual(detail["protocol_observations"][0]["protocol"], "outline")

    def test_registered_non_outline_adapter_remains_evidence_gated_in_summary(self):
        self.runtime.commerce.adapter_registry.register("xray", XrayConnectivityAdapter)
        with patch.dict(os.environ, {"ADMIN_TELEGRAM_IDS": "12345"}, clear=False):
            summary = AuriXVpnWebApplication(self.runtime).admin_summary()
        readiness = {item["protocol"]: item for item in summary["protocol_readiness"]}
        self.assertTrue(readiness["xray"]["registered"])
        self.assertEqual(readiness["xray"]["status"], "candidate")
        self.assertIn("endpoint evidence", readiness["xray"]["activation_gate"])

    def test_endpoint_detail_exposes_assignment_protocol(self):
        now = "2026-09-12T00:00:00+00:00"
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO users (telegram_id, first_name, created_at) VALUES (?, ?, ?)",
                (12345, "Min", now),
            )
            connection.execute(
                """INSERT INTO vpn_endpoints
                   (id, code, provider, region, state, accepts_new_assignments, created_at)
                   VALUES (?, ?, 'managed', 'sgp1', 'ACTIVE', 1, ?)""",
                ("xray-sgp-a", "XRAY-SGP-A", now),
            )
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, plan_name,
                    quota_bytes_snapshot, duration_days_snapshot, status, created_at)
                   VALUES (?, ?, 'basic_50gb', 3000, 'MMK', '50 GB', ?, 30, 'approved', ?)""",
                ("order-control-xray", 12345, 50 * 1024**3, now),
            )
            connection.execute(
                """INSERT INTO subscriptions
                   (id, order_id, telegram_id, plan_code, starts_at, expires_at,
                    plan_name, quota_bytes, duration_days, status, preferred_protocol)
                   VALUES (?, ?, ?, 'basic_50gb', ?, ?, '50 GB', ?, 30, 'active', 'xray')""",
                (
                    "sub-control-xray",
                    "order-control-xray",
                    12345,
                    now,
                    "2026-10-12T00:00:00+00:00",
                    50 * 1024**3,
                ),
            )
            connection.execute(
                """INSERT INTO endpoint_assignments
                   (id, endpoint_id, subscription_id, plan_code, protocol, status,
                    reason, reserved_quota_bytes, assigned_at)
                   VALUES (?, ?, ?, 'basic_50gb', 'xray', 'active',
                           'test', ?, ?)""",
                ("assignment-control-xray", "xray-sgp-a", "sub-control-xray", 50 * 1024**3, now),
            )
        detail = AuriXVpnWebApplication(self.runtime).admin_endpoint("xray-sgp-a")
        self.assertEqual(detail["assignments"][0]["protocol"], "xray")

    def test_admin_api_requires_signed_allowlisted_telegram_identity(self):
        with patch.dict(os.environ, {"ADMIN_TELEGRAM_IDS": "12345"}, clear=False):
            app = AuriXVpnWebApplication(self.runtime)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/admin/summary",
                headers={"X-Telegram-Init-Data": _init_data("bot-token")},
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.load(response)
            self.assertEqual(payload["management_mode"], "read-only")

            for endpoint, key in (
                ("accounts", "accounts"),
                ("credentials", "credentials"),
                ("devices", "devices"),
                ("operations", "jobs"),
                ("failover", "decisions"),
                ("audit", "events"),
            ):
                detail_request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_address[1]}/api/admin/{endpoint}",
                    headers={"X-Telegram-Init-Data": _init_data("bot-token")},
                )
                with urllib.request.urlopen(detail_request, timeout=3) as detail_response:
                    detail_payload = json.load(detail_response)
                self.assertIn(key, detail_payload)

            accounts_request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/admin/accounts",
                headers={"X-Telegram-Init-Data": _init_data("bot-token")},
            )
            with urllib.request.urlopen(accounts_request, timeout=3) as accounts_response:
                accounts_payload = json.load(accounts_response)
            self.assertEqual(len(accounts_payload["accounts"]), 1)
            account_id = accounts_payload["accounts"][0]["account_id"]
            account_request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/admin/accounts/{account_id}",
                headers={"X-Telegram-Init-Data": _init_data("bot-token")},
            )
            with urllib.request.urlopen(account_request, timeout=3) as account_response:
                account_payload = json.load(account_response)
            self.assertEqual(account_payload["account"]["account_id"], account_id)
            self.assertEqual(account_payload["account"]["subscriptions"], [])
            self.assertNotIn("public_key", json.dumps(account_payload))

            endpoint_request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/admin/fleet/bkk-a",
                headers={"X-Telegram-Init-Data": _init_data("bot-token")},
            )
            with urllib.request.urlopen(endpoint_request, timeout=3) as endpoint_response:
                endpoint_payload = json.load(endpoint_response)
            self.assertEqual(endpoint_payload["endpoint"]["code"], "BKK-A")
            self.assertNotIn("public_address", json.dumps(endpoint_payload))

            with patch.dict(os.environ, {"ADMIN_TELEGRAM_IDS": "999"}, clear=False):
                denied_app = AuriXVpnWebApplication(self.runtime)
                denied_server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(denied_app))
                denied_thread = threading.Thread(target=denied_server.serve_forever, daemon=True)
                denied_thread.start()
            try:
                denied_request = urllib.request.Request(
                    f"http://127.0.0.1:{denied_server.server_address[1]}/api/admin/summary",
                    headers={"X-Telegram-Init-Data": _init_data("bot-token")},
                )
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(denied_request, timeout=3)
                self.assertEqual(error.exception.code, 403)
            finally:
                denied_server.shutdown()
                denied_server.server_close()
                denied_thread.join(timeout=3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_infrastructure_jobs_are_visible_without_provider_identifiers(self):
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO infrastructure_jobs
                   (id, operation, endpoint_id, status, attempts, next_attempt_at,
                    provider_resource_id, provider_action_id, request_fingerprint,
                    last_error, created_at)
                   VALUES (?, 'provision', ?, 'failed', 2, ?, ?, ?, ?, ?, ?)""",
                (
                    "infra-job-1",
                    "bkk-a",
                    "2026-09-11T00:00:00+00:00",
                    "do-12345",
                    "do-action-99",
                    "fingerprint-secret",
                    "ConnectivityError: provider token must not reach the browser",
                    "2026-09-11T00:00:00+00:00",
                ),
            )
        operations = AuriXVpnWebApplication(self.runtime).admin_operations()
        self.assertEqual(operations["infrastructure_jobs"][0]["job_id"], "infra-job-1")
        self.assertEqual(operations["infrastructure_jobs"][0]["error_type"], "ConnectivityError")
        self.assertEqual(operations["safety_controls"][0]["scope"], "global")
        self.assertEqual(operations["safety_controls"][0]["remaining_migrations"], 100)
        payload = json.dumps(operations)
        self.assertNotIn("do-12345", payload)
        self.assertNotIn("do-action-99", payload)
        self.assertNotIn("fingerprint-secret", payload)
        self.assertNotIn("provider token must not reach the browser", payload)

    def test_failover_browser_payload_is_redacted(self):
        self.runtime.commerce.failover.decisions = lambda **_kwargs: [
            {
                "decision_id": "decision-1",
                "idempotency_key": "idempotency-secret",
                "entitlement_key": "entitlement-secret",
                "source_generation_id": "source-generation-secret",
                "source_endpoint_id": "sg-a",
                "target_endpoint_id": "bkk-a",
                "target_generation_id": "target-generation-secret",
                "trigger": "automatic",
                "network_bucket": "mmpt",
                "state": "failed",
                "attempts": 2,
                "next_attempt_at": "2026-09-12T00:00:00+00:00",
                "policy_version": 3,
                "last_error": "TimeoutError: provider request id must not reach browser",
                "created_at": "2026-09-12T00:00:00+00:00",
            }
        ]
        payload = AuriXVpnWebApplication(self.runtime).admin_failover()
        self.assertEqual(payload[0]["error_type"], "TimeoutError")
        encoded = json.dumps(payload)
        for secret in (
            "idempotency-secret",
            "entitlement-secret",
            "source-generation-secret",
            "target-generation-secret",
            "provider request id must not reach browser",
        ):
            self.assertNotIn(secret, encoded)
        self.assertEqual(payload[0]["source_endpoint_id"], "sg-a")
        self.assertEqual(payload[0]["policy_version"], 3)

    def test_failover_decision_explanation_is_redacted(self):
        self.runtime.commerce.failover.decision_explanation = lambda _decision_id: {
            "decision_id": "decision-1",
            "entitlement_key": "entitlement-secret",
            "source_endpoint_id": "sg-a",
            "target_endpoint_id": "bkk-a",
            "trigger": "automatic",
            "network_bucket": "mmpt",
            "state": "committed",
            "attempts": 1,
            "policy_version": 2,
            "policy_enabled": True,
            "policy_failure_threshold": 2,
            "policy_recovery_threshold": 2,
            "policy_cooldown_seconds": 300,
            "policy_snapshot_available": True,
        }
        payload = AuriXVpnWebApplication(self.runtime).admin_failover_decision("decision-1")
        self.assertEqual(payload["decision_id"], "decision-1")
        self.assertEqual(payload["policy_failure_threshold"], 2)
        self.assertNotIn("entitlement_key", payload)

    def test_failover_decision_explanation_http_route_is_admin_only(self):
        self.runtime.commerce.failover.decision_explanation = lambda _decision_id: {
            "decision_id": "decision-1",
            "source_endpoint_id": "sg-a",
            "target_endpoint_id": "bkk-a",
            "trigger": "automatic",
            "state": "committed",
            "policy_version": 2,
            "policy_snapshot_available": True,
            "policy_failure_threshold": 2,
        }
        with patch.dict(os.environ, {"ADMIN_TELEGRAM_IDS": "12345"}, clear=False):
            app = AuriXVpnWebApplication(self.runtime)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/admin/failover/decision-1",
                headers={"X-Telegram-Init-Data": _init_data("bot-token")},
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.load(response)
            self.assertEqual(payload["decision"]["policy_version"], 2)
            self.assertNotIn("entitlement_key", json.dumps(payload))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_admin_static_shell_is_available_without_api_data(self):
        with patch.dict(os.environ, {"ADMIN_TELEGRAM_IDS": "12345"}, clear=False):
            app = AuriXVpnWebApplication(self.runtime)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_address[1]}/admin", timeout=3
            ) as response:
                body = response.read().decode()
            self.assertIn("AuriX Control Center", body)
            self.assertIn("/admin/app.js", body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
