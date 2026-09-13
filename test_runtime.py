import contextlib
import io
import json
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


class _EndpointRegistry:
    instances = []
    preconfigured = False

    def __init__(self, database, secret_key):
        self.database = database
        self.secret_key = secret_key
        self.bootstrap = None
        self.__class__.instances.append(self)

    def configure_bootstrap(self, api_url, fingerprint, **kwargs):
        self.bootstrap = (api_url, fingerprint, kwargs)

    def has_management_capability(self, _endpoint_id):
        return self.bootstrap is not None or self.__class__.preconfigured

    @staticmethod
    def client(_endpoint_id):
        return _Outline("https://outline.invalid/secret", "0" * 64)

    @staticmethod
    def record_capacity(*_args, **_kwargs):
        return {}


class _Outline:
    def __init__(self, api_url, fingerprint):
        self.api_url = api_url
        self.fingerprint = fingerprint

    def server_info(self):
        return {"version": "test-outline"}


class _Commerce:
    instances = []

    def __init__(self, database, outline, access_key, **kwargs):
        self.database = database
        self.outline = outline
        self.access_key = access_key
        self.kwargs = kwargs
        self.initialized = False
        self.__class__.instances.append(self)

    def initialize(self):
        self.initialized = True

    def reconcile_duplicate_open_orders(self):
        return {"cancelled": 0, "manual_conflicts": 0}


class _ClaimService:
    def __init__(self, database, outline, limit_bytes, **kwargs):
        self.database = database
        self.outline = outline
        self.limit_bytes = limit_bytes
        self.kwargs = kwargs

    def reconcile_giveaway_limits(self):
        return 1


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
        _EndpointRegistry.instances = []
        _EndpointRegistry.preconfigured = False
        _Commerce.instances = []

    def test_app_main_remains_the_runtime_entrypoint(self):
        self.assertIs(app.main, runtime.main)

    def test_required_runtime_settings_fail_before_network_io(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "TELEGRAM_BOT_TOKEN"):
                runtime.main()

    def test_postgres_storage_mode_cannot_silently_fall_back_to_sqlite(self):
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "OUTLINE_API_URL": "https://outline.invalid/secret",
            "OUTLINE_CERT_SHA256": "0" * 64,
            "AURIX_ACCESS_URL_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
            "AURIX_STORAGE_MODE": "postgres",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(SystemExit, "COMMERCE_DATABASE_URL is required"):
                runtime.build_runtime_services(validate_telegram=False)

    def test_main_composes_adapters_and_cleans_polling_webhook(self):
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "OUTLINE_API_URL": "https://outline.invalid/secret",
            "OUTLINE_CERT_SHA256": "0" * 64,
            "AURIX_ACCESS_URL_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
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
            patch("runtime.EndpointRegistry", _EndpointRegistry),
            patch("runtime.CommerceService", _Commerce),
            patch("runtime.ClaimService", _ClaimService),
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
        self.assertIn("Bot authorized: @aurix_test_bot", output.getvalue())
        self.assertIn("Outline connected: version test-outline", output.getvalue())
        self.assertIn("Promo quotas reconciled: 1 active key(s)", output.getvalue())

    def test_web_composition_can_leave_bootstrap_endpoint_unchanged(self):
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "AURIX_ACCESS_URL_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
            "DATABASE_PATH": "/tmp/aurix-runtime-test.db",
        }
        _EndpointRegistry.preconfigured = True
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("runtime.Database", _Database),
            patch("runtime.CommerceDatabase", _CommerceDatabase),
            patch("runtime.EndpointRegistry", _EndpointRegistry),
            patch("runtime.CommerceService", _Commerce),
            patch("runtime.ClaimService", _ClaimService),
        ):
            runtime.build_runtime_services(
                validate_telegram=False,
                check_outline=False,
                reconcile=False,
                configure_bootstrap=False,
            )

        self.assertIsNone(_EndpointRegistry.instances[0].bootstrap)
        self.assertEqual(_Commerce.instances[-1].outline.endpoint_id, "legacy-default")

    def test_fresh_runtime_requires_bootstrap_outline_settings(self):
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "AURIX_ACCESS_URL_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
            "DATABASE_PATH": "/tmp/aurix-runtime-test.db",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("runtime.Database", _Database),
            patch("runtime.CommerceDatabase", _CommerceDatabase),
            patch("runtime.EndpointRegistry", _EndpointRegistry),
            patch("runtime.CommerceService", _Commerce),
        ):
            with self.assertRaisesRegex(SystemExit, "required to initialize the bootstrap endpoint"):
                runtime.build_runtime_services(
                    validate_telegram=False,
                    check_outline=False,
                    reconcile=False,
                    configure_bootstrap=False,
                )

    def test_readonly_runtime_can_bridge_before_bot_persists_bootstrap_endpoint(self):
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "OUTLINE_API_URL": "https://outline.invalid/secret",
            "OUTLINE_CERT_SHA256": "0" * 64,
            "AURIX_ACCESS_URL_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
            "DATABASE_PATH": "/tmp/aurix-runtime-test.db",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("runtime.Database", _Database),
            patch("runtime.CommerceDatabase", _CommerceDatabase),
            patch("runtime.EndpointRegistry", _EndpointRegistry),
            patch("runtime.OutlineClient", _Outline),
            patch("runtime.CommerceService", _Commerce),
            patch("runtime.ClaimService", _ClaimService),
        ):
            runtime.build_runtime_services(
                validate_telegram=False,
                check_outline=False,
                reconcile=False,
                configure_bootstrap=False,
            )

        gateway = _Commerce.instances[-1].outline
        self.assertEqual(gateway.endpoint_id, "legacy-default")
        self.assertEqual(gateway.fallback.api_url, "https://outline.invalid/secret")

    def test_managed_node_agent_bindings_are_explicitly_wired_when_configured(self):
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "AURIX_ACCESS_URL_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
            "DATABASE_PATH": "/tmp/aurix-runtime-test.db",
            "AURIX_MANAGED_NODE_AGENTS_JSON": json.dumps([
                {
                    "endpoint_id": "sg-a",
                    "protocol": "xray",
                    "base_url": "https://127.0.0.1:18001",
                    "token": "agent-token",
                    "route": {
                        "public_address": "198.51.100.10",
                        "port": 18443,
                        "public_key": "public-key",
                        "server_name": "example.com",
                        "short_id": "abcd",
                    },
                }
            ]),
        }
        _EndpointRegistry.preconfigured = True
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("runtime.Database", _Database),
            patch("runtime.CommerceDatabase", _CommerceDatabase),
            patch("runtime.EndpointRegistry", _EndpointRegistry),
            patch("runtime.CommerceService", _Commerce),
            patch("runtime.ClaimService", _ClaimService),
        ):
            runtime.build_runtime_services(
                validate_telegram=False,
                check_outline=False,
                reconcile=False,
                configure_bootstrap=False,
            )

        commerce = _Commerce.instances[-1]
        self.assertEqual(commerce.managed_route_provider("sg-a", "xray")["route_id"], "xray:sg-a")
        self.assertTrue(callable(commerce.managed_adapter_provider))


if __name__ == "__main__":
    unittest.main()
