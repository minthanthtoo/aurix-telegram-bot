import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from deploy.render_preflight import main


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
    }


class RenderPreflightTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
