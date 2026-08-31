import contextlib
import io
import os
import unittest
from unittest.mock import patch

import app
import runtime


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        import json

        return json.dumps(self.payload).encode()


class _Database:
    def __init__(self, path):
        self.path = path
        self.initialized = False

    def initialize(self):
        self.initialized = True


class _CommerceDatabase(_Database):
    pass


class _Outline:
    def __init__(self, api_url, fingerprint):
        self.api_url = api_url
        self.fingerprint = fingerprint

    def server_info(self):
        return {"version": "test-outline"}


class _Commerce:
    def __init__(self, database, outline, access_key, **kwargs):
        self.database = database
        self.outline = outline
        self.access_key = access_key
        self.kwargs = kwargs
        self.initialized = False

    def initialize(self):
        self.initialized = True

    def reconcile_duplicate_open_orders(self):
        return {"cancelled": 0, "manual_conflicts": 0}


class _ClaimService:
    def __init__(self, database, outline, limit_bytes):
        self.database = database
        self.outline = outline
        self.limit_bytes = limit_bytes

    def reconcile_giveaway_limits(self):
        return 1


class _StaffAccess:
    def __init__(self, database, owner_id=None):
        self.database = database
        self.owner = owner_id

    def bootstrap(self, **kwargs):
        admins = set(kwargs.get("admin_ids") or ())
        return {"owner_id": self.owner or (min(admins) if admins else None), "admin_ids": admins, "imported_admins": len(admins)}

    def control_group(self):
        return None


class _Bot:
    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.requests = []
        self.configured = False
        self.ran = False
        _Bot.instances.append(self)

    def request(self, method, payload):
        self.requests.append((method, payload))

    def configure_commands(self):
        self.configured = True

    def run(self):
        self.ran = True

    def stop(self):
        return None


class RuntimeCompositionTest(unittest.TestCase):
    def setUp(self):
        _Bot.instances = []

    def test_app_main_remains_the_runtime_entrypoint(self):
        self.assertIs(app.main, runtime.main)

    def test_required_runtime_settings_fail_before_network_io(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "TELEGRAM_BOT_TOKEN"):
                runtime.main()

    def test_main_composes_adapters_and_cleans_polling_webhook(self):
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "OUTLINE_API_URL": "https://outline.invalid/secret",
            "OUTLINE_CERT_SHA256": "0" * 64,
            "AURIX_ACCESS_URL_KEY": "test-access-key",
            "DATABASE_PATH": "/tmp/aurix-runtime-test.db",
            "ADMIN_TELEGRAM_IDS": "10,20",
            "TRIAL_TELEGRAM_IDS": "30",
            "ADMIN_SCOPE_CLEANUP_IDS": "40",
            "AURIX_MAINTENANCE_INTERVAL_SECONDS": "90",
        }
        get_me = _Response({"ok": True, "result": {"username": "aurix_test_bot"}})
        output = io.StringIO()
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("runtime.urllib.request.urlopen", return_value=get_me),
            patch("runtime.Database", _Database),
            patch("runtime.CommerceDatabase", _CommerceDatabase),
            patch("runtime.OutlineClient", _Outline),
            patch("runtime.CommerceService", _Commerce),
            patch("runtime.ClaimService", _ClaimService),
            patch("runtime.StaffAccessControl", _StaffAccess),
            patch("runtime.TelegramBot", _Bot),
            patch("runtime.signal.signal"),
            contextlib.redirect_stdout(output),
        ):
            runtime.main()

        bot = _Bot.instances[0]
        self.assertEqual(bot.requests, [("deleteWebhook", {"drop_pending_updates": False})])
        self.assertTrue(bot.configured)
        self.assertTrue(bot.ran)
        self.assertEqual(bot.args[3], {10, 20})
        self.assertEqual(bot.args[4], {30})
        self.assertEqual(bot.kwargs["command_scope_cleanup_ids"], {40})
        self.assertEqual(bot.kwargs["maintenance_interval_seconds"], 90.0)
        self.assertIsInstance(bot.kwargs["staff_access"], _StaffAccess)
        self.assertIn("Bot authorized: @aurix_test_bot", output.getvalue())
        self.assertIn("Outline connected: version test-outline", output.getvalue())
        self.assertIn("Promo quotas reconciled: 1 active key(s)", output.getvalue())


if __name__ == "__main__":
    unittest.main()
