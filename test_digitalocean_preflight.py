import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from deploy.digitalocean_preflight import main


class DigitalOceanPreflightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Path(self.tmp.name) / "bot.db"
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE ready (id INTEGER)")
        self.environment = {
            "TELEGRAM_BOT_TOKEN": "123456:test-token",
            "OWNER_TELEGRAM_ID": "123456",
            "ADMIN_TELEGRAM_IDS": "",
            "AURIX_ACCESS_URL_KEY": Fernet.generate_key().decode(),
            "OUTLINE_API_URL": "https://outline.example:443/secret-path",
            "OUTLINE_CERT_SHA256": "a" * 64,
            "DATABASE_PATH": str(self.database),
            "COMMERCE_DATABASE_URL": "",
            "ALLOW_TEXT_PAYMENT_REFERENCES": "0",
            "RECEIPT_STORAGE_REQUIRED": "1",
            "SUPABASE_URL": "https://project.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service-role-test-only",
            "SUPABASE_RECEIPTS_BUCKET": "payment-receipts",
            "RECEIPT_LLM_BASE_URL": "https://vision.example/v1",
            "RECEIPT_LLM_MODEL": "vision-model",
            "RECEIPT_LLM_API_KEY": "test-key-long-enough",
            "PAYMENT_RECIPIENTS_JSON": '{"kbzpay":{"names":["merchant"]},"wavepay":{"names":["merchant"]},"ayapay":{"names":["merchant"]},"uabpay":{"names":["merchant"]},"cbpay":{"names":["merchant"]}}',
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_configuration_passes_without_live_network_calls(self):
        with patch.dict(os.environ, self.environment, clear=True):
            main([])

    def test_receipt_storage_cannot_be_disabled(self):
        environment = dict(self.environment, RECEIPT_STORAGE_REQUIRED="0")
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(SystemExit, "RECEIPT_STORAGE_REQUIRED"):
                main([])

    def test_receipt_vision_configuration_is_required(self):
        environment = dict(self.environment)
        environment.pop("RECEIPT_LLM_API_KEY")
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(SystemExit, "RECEIPT_LLM_API_KEY"):
                main([])


if __name__ == "__main__":
    unittest.main()
