import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from deploy.render_preflight import _check_payment_qr_assets, _validate_live, main


def valid_environment() -> dict[str, str]:
    return {
        "AURIX_STORAGE_MODE": "postgres",
        "TELEGRAM_BOT_TOKEN": "test-token",
        "ADMIN_TELEGRAM_IDS": "1,2",
        "OUTLINE_API_URL": "https://outline.invalid/management-secret",
        "OUTLINE_CERT_SHA256": "0" * 64,
        "AURIX_ACCESS_URL_KEY": Fernet.generate_key().decode(),
        "SUPABASE_URL": "https://project.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "test-role-key",
        "RECEIPT_STORAGE_REQUIRED": "1",
        "COMMERCE_DATABASE_URL": "postgresql://user:password@database.invalid/aurix",
        "ALLOW_TEXT_PAYMENT_REFERENCES": "0",
        "PAYMENT_RECIPIENTS_JSON": json.dumps(
            {
                method: {"names": ["merchant"]}
                for method in ("kbzpay", "wavepay", "ayapay", "uabpay", "cbpay")
            }
        ),
    }


class RenderPreflightTest(unittest.TestCase):
    def test_all_payment_qr_assets_are_present_and_non_empty(self):
        _check_payment_qr_assets()

    def test_missing_payment_qr_asset_fails_closed(self):
        with patch("deploy.render_preflight.PAYMENT_QR_ASSETS", {"kbzpay": "missing.png"}):
            with self.assertRaisesRegex(SystemExit, "missing or empty payment QR asset.*kbzpay"):
                _check_payment_qr_assets()

    def run_preflight(self, environment: dict[str, str]) -> str:
        output = io.StringIO()
        with patch.dict(os.environ, environment, clear=True):
            with contextlib.redirect_stdout(output):
                main()
        return output.getvalue()

    def test_valid_postgres_profile_passes_without_printing_secrets(self):
        environment = valid_environment()
        output = self.run_preflight(environment)
        self.assertEqual(
            output,
            "Render preflight passed: single-worker hosted PostgreSQL configuration is valid\n",
        )
        self.assertNotIn(environment["TELEGRAM_BOT_TOKEN"], output)
        self.assertNotIn(environment["SUPABASE_SERVICE_ROLE_KEY"], output)

    def test_live_flag_runs_external_canary_after_configuration_validation(self):
        environment = valid_environment()
        with patch.dict(os.environ, environment, clear=True), patch(
            "deploy.render_preflight._validate_live"
        ) as live:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                main(["--live"])
        live.assert_called_once()
        self.assertIn("live dependencies", output.getvalue())

    def test_live_canary_is_read_only_and_checks_outline_and_private_bucket(self):
        with tempfile.TemporaryDirectory() as directory:
            database = os.path.join(directory, "bot.db")
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE health (id INTEGER)")
            values = {
                "telegram_token": "test-token",
                "supabase_url": "https://project.supabase.co",
                "supabase_key": "service-role-key",
                "bucket": "payment-receipts",
                "llm_url": "https://vision.example/v1",
                "llm_key": "vision-key",
                "database_url": "",
                "database_path": database,
                "outline_servers": [
                    {"api_url": "https://outline.example/secret", "cert_sha256": "a" * 64}
                ],
            }

            class FakeOutline:
                def __init__(self, *args, **kwargs):
                    del args, kwargs
                    self.server_checks = 0

                def server_info(self):
                    self.server_checks += 1
                    return {"version": "1.12.3"}

            with patch(
                "deploy.render_preflight._json_request",
                side_effect=[{"ok": True}, {"public": False}, {"data": [{"id": "vision-model"}]}],
            ), patch("outline_adapter.OutlineClient", FakeOutline):
                _validate_live(values)

    def test_live_canary_rejects_public_receipt_bucket(self):
        values = {
            "telegram_token": "test-token",
            "supabase_url": "https://project.supabase.co",
            "supabase_key": "service-role-key",
            "bucket": "payment-receipts",
            "llm_url": "https://vision.example/v1",
            "llm_key": "vision-key",
            "database_url": "postgresql://user:password@db.invalid/aurix",
            "database_path": "",
            "outline_servers": [],
        }
        with patch(
            "deploy.render_preflight._json_request",
            side_effect=[{"ok": True}, {"public": True}],
        ):
            with self.assertRaisesRegex(SystemExit, "must remain private"):
                _validate_live(values)

    def test_live_canary_rejects_unadvertised_receipt_model(self):
        values = {
            "telegram_token": "test-token",
            "supabase_url": "https://project.supabase.co",
            "supabase_key": "service-role-key",
            "bucket": "payment-receipts",
            "llm_url": "https://vision.example/v1",
            "llm_key": "vision-key",
            "llm_model": "missing-vision-model",
            "llm_fallback_models": ["fallback-model"],
            "database_url": "",
            "database_path": "/tmp/does-not-need-to-exist",
            "outline_servers": [],
        }
        with patch(
            "deploy.render_preflight._json_request",
            side_effect=[
                {"ok": True},
                {"public": False},
                {"data": [{"id": "other-model"}, {"id": "fallback-model"}]},
            ],
        ):
            with self.assertRaisesRegex(SystemExit, "not advertised"):
                _validate_live(values)

    def test_missing_required_value_reports_only_its_variable_name(self):
        environment = valid_environment()
        environment["SUPABASE_SERVICE_ROLE_KEY"] = ""
        with self.assertRaisesRegex(SystemExit, "SUPABASE_SERVICE_ROLE_KEY"):
            self.run_preflight(environment)

    def test_unsafe_text_payment_mode_is_rejected(self):
        environment = valid_environment()
        environment["ALLOW_TEXT_PAYMENT_REFERENCES"] = "1"
        with self.assertRaisesRegex(SystemExit, "must remain disabled"):
            self.run_preflight(environment)

    def test_partial_receipt_extractor_configuration_is_rejected(self):
        environment = valid_environment()
        environment["RECEIPT_LLM_MODEL"] = "vision-model"
        with self.assertRaisesRegex(SystemExit, "configure all three"):
            self.run_preflight(environment)

    def test_production_vision_gate_requires_all_receipt_values(self):
        environment = valid_environment()
        environment["RECEIPT_VISION_REQUIRED"] = "1"
        with self.assertRaisesRegex(SystemExit, "RECEIPT_LLM_BASE_URL"):
            self.run_preflight(environment)

    def test_production_vision_gate_passes_with_complete_configuration(self):
        environment = valid_environment()
        environment.update(
            {
                "RECEIPT_VISION_REQUIRED": "1",
                "RECEIPT_LLM_BASE_URL": "https://vision.example/v1",
                "RECEIPT_LLM_MODEL": "vision-model",
                "RECEIPT_LLM_API_KEY": "test-vision-key",
            }
        )
        self.assertIn("preflight passed", self.run_preflight(environment).lower())

    def test_receipt_fallback_requires_primary_configuration(self):
        environment = valid_environment()
        environment["RECEIPT_LLM_FALLBACK_MODELS"] = "fallback-model"
        with self.assertRaisesRegex(SystemExit, "fallback models require"):
            self.run_preflight(environment)

    def test_receipt_selection_mode_is_allowlisted(self):
        environment = valid_environment()
        environment["RECEIPT_LLM_SELECTION_MODE"] = "first_and_best"
        with self.assertRaisesRegex(SystemExit, "SELECTION_MODE"):
            self.run_preflight(environment)

    def test_receipt_consensus_requires_a_fallback_model(self):
        environment = valid_environment()
        environment["RECEIPT_LLM_SELECTION_MODE"] = "consensus"
        with self.assertRaisesRegex(SystemExit, "at least one fallback"):
            self.run_preflight(environment)

    def test_receipt_consensus_with_fallback_passes(self):
        environment = valid_environment()
        environment["RECEIPT_LLM_BASE_URL"] = "https://vision.example/v1"
        environment["RECEIPT_LLM_MODEL"] = "primary-model"
        environment["RECEIPT_LLM_API_KEY"] = "test-vision-key"
        environment["RECEIPT_LLM_SELECTION_MODE"] = "consensus"
        environment["RECEIPT_LLM_FALLBACK_MODELS"] = "second-model"
        self.assertIn("preflight passed", self.run_preflight(environment).lower())

    def test_invalid_postgres_url_is_rejected(self):
        environment = valid_environment()
        environment["COMMERCE_DATABASE_URL"] = "sqlite:///tmp/bot.db"
        with self.assertRaisesRegex(SystemExit, "must be a PostgreSQL URL"):
            self.run_preflight(environment)

    def test_multi_server_secret_can_replace_legacy_outline_pair(self):
        environment = valid_environment()
        environment.pop("OUTLINE_API_URL")
        environment.pop("OUTLINE_CERT_SHA256")
        environment["OUTLINE_SERVERS_JSON"] = json.dumps(
            [
                {
                    "id": "sg1",
                    "label": "Singapore 1",
                    "api_url": "https://outline.invalid/management-secret",
                    "cert_sha256": "a" * 64,
                }
            ]
        )
        environment["OUTLINE_DEFAULT_SERVER_ID"] = "sg1"

        self.assertIn("preflight passed", self.run_preflight(environment).lower())

    def test_registration_endpoint_requires_https_and_matching_fernet_key(self):
        environment = valid_environment()
        environment.update(
            {
                "AURIX_FLEET_REGISTRATION_ENABLED": "1",
                "AURIX_FLEET_REGISTRATION_URL": "http://control.example/fleet/register",
                "AURIX_FLEET_ENROLLMENT_KEY": Fernet.generate_key().decode(),
            }
        )
        with self.assertRaisesRegex(SystemExit, "HTTPS"):
            self.run_preflight(environment)
        environment["AURIX_FLEET_REGISTRATION_URL"] = "https://control.example/fleet/register"
        environment["AURIX_FLEET_ENROLLMENT_KEY"] = "invalid"
        with self.assertRaisesRegex(SystemExit, "Fernet"):
            self.run_preflight(environment)
        environment["AURIX_FLEET_ENROLLMENT_KEY"] = Fernet.generate_key().decode()
        self.assertIn("preflight passed", self.run_preflight(environment).lower())

    def test_auto_registration_cannot_run_without_callback_gate(self):
        environment = valid_environment()
        environment.update(
            {
                "AURIX_FLEET_AUTO_REGISTRATION_ENABLED": "1",
                "AURIX_FLEET_REGISTRATION_URL": "https://control.example/fleet/register",
                "AURIX_FLEET_ENROLLMENT_KEY": Fernet.generate_key().decode(),
            }
        )
        with self.assertRaisesRegex(SystemExit, "requires AURIX_FLEET_REGISTRATION_ENABLED"):
            self.run_preflight(environment)


if __name__ == "__main__":
    unittest.main()
