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
from connectivity_adapters import ConnectivityAdapterRegistry
from identity import IdentityService
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
        commerce = SimpleNamespace(
            identity=identity,
            adapter_registry=ConnectivityAdapterRegistry(),
            consistency_report=lambda: {"failed_jobs": 0, "pending_receipts": 0},
            failover=SimpleNamespace(decisions=lambda limit=100: []),
            failed_jobs=lambda limit=100, include_nonterminal=True: [],
            list_pending_orders=lambda limit=100: [],
        )
        connectivity = SimpleNamespace(
            list_endpoints=lambda: [{
                "id": "bkk-a", "code": "BKK-A", "provider": "manual", "provider_resource_id": "do-secret",
                "region": "bkk1", "state": "ACTIVE", "accepts_new_assignments": 1,
                "outline_version": "x", "max_active_keys": 100, "reserved_transfer_bytes": 1,
                "active_assignments": 2, "last_healthy_at": "2026-09-10T00:00:00+00:00",
                "public_address": "198.51.100.10", "management_url_ciphertext": "secret",
            }]
        )
        self.runtime = SimpleNamespace(
            token="bot-token", commerce=commerce, commerce_database=self.database,
            connectivity=connectivity,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_summary_and_fleet_are_read_only_and_redacted(self):
        with patch.dict(os.environ, {"ADMIN_TELEGRAM_IDS": "12345"}, clear=False):
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
        self.assertNotIn("public_address", json.dumps(fleet))
        self.assertNotIn("management_url", json.dumps(fleet))
        self.assertNotIn("provider_resource_id", json.dumps(fleet))

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
