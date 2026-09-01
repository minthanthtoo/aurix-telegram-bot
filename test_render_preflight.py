import contextlib
import io
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

    def test_invalid_postgres_url_is_rejected(self):
        environment = valid_environment()
        environment["COMMERCE_DATABASE_URL"] = "sqlite:///tmp/bot.db"
        with self.assertRaisesRegex(SystemExit, "must be a PostgreSQL URL"):
            self.run_preflight(environment)


if __name__ == "__main__":
    unittest.main()
