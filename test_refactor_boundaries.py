import unittest

import app
import commerce
import commerce_models
import commerce_repositories
import commerce_service
import commerce_worker
import entitlements
import free_repository
import outline_adapter
import runtime
import telegram_transport
import telegram_admin
import telegram_admin_panels
import telegram_callbacks
import telegram_commands
import telegram_maintenance
from ports import OutlineGateway, ReceiptExtractorGateway, ReceiptStorageGateway
from receipt_llm import OpenAICompatibleReceiptExtractor
from supabase_storage import NullReceiptStorage


class CompatibilityExportTest(unittest.TestCase):
    def test_app_keeps_free_entitlement_compatibility_exports(self):
        self.assertIs(app.ClaimService, entitlements.ClaimService)
        self.assertIs(app.ClaimResult, entitlements.ClaimResult)
        self.assertIs(app.OutlineError, entitlements.OutlineError)
        self.assertIs(app.Database, free_repository.Database)
        self.assertEqual(app.PUBLIC_LIMIT_BYTES, entitlements.PUBLIC_LIMIT_BYTES)
        self.assertEqual(app.TRIAL_LIMIT_BYTES, entitlements.TRIAL_LIMIT_BYTES)

    def test_commerce_keeps_models_repositories_and_service_exports(self):
        self.assertIs(commerce.CommerceError, commerce_models.CommerceError)
        self.assertIs(commerce.Plan, commerce_models.Plan)
        self.assertIs(commerce.CommerceDatabase, commerce_repositories.CommerceDatabase)
        self.assertIs(
            commerce.PostgresCommerceDatabase,
            commerce_repositories.PostgresCommerceDatabase,
        )
        self.assertIs(commerce.CommerceService, commerce_service.CommerceService)

    def test_external_adapters_and_worker_have_explicit_boundaries(self):
        outline = outline_adapter.OutlineClient("https://outline.invalid/secret", "0" * 64)
        self.assertIs(app.OutlineClient, outline_adapter.OutlineClient)
        self.assertIsInstance(outline, OutlineGateway)
        self.assertIsInstance(NullReceiptStorage(), ReceiptStorageGateway)
        self.assertIsInstance(OpenAICompatibleReceiptExtractor(), ReceiptExtractorGateway)
        self.assertTrue(
            issubclass(commerce_service.CommerceService, commerce_worker.CommerceWorkerMixin)
        )
        self.assertNotIn("process_jobs", commerce_service.CommerceService.__dict__)
        self.assertIn("process_jobs", commerce_worker.CommerceWorkerMixin.__dict__)

    def test_app_keeps_telegram_transport_compatibility_exports(self):
        self.assertIs(app.TelegramBot, telegram_transport.TelegramBot)
        self.assertIs(app.AdminOperations, telegram_transport.AdminOperations)

    def test_app_keeps_runtime_entrypoint_compatibility_export(self):
        self.assertIs(app.main, runtime.main)

    def test_telegram_transport_delegates_to_bounded_mixins(self):
        bot = telegram_transport.TelegramBot
        self.assertIs(telegram_transport.AdminOperations, telegram_admin.AdminOperations)
        for mixin in (
            telegram_admin_panels.TelegramAdminMixin,
            telegram_callbacks.TelegramCallbackMixin,
            telegram_commands.TelegramCommandMixin,
            telegram_maintenance.TelegramMaintenanceMixin,
        ):
            self.assertTrue(issubclass(bot, mixin))
        self.assertIs(bot.handle, telegram_commands.TelegramCommandMixin.handle)
        self.assertIs(bot.handle_callback, telegram_callbacks.TelegramCallbackMixin.handle_callback)
        self.assertIs(
            bot._run_maintenance_pass,
            telegram_maintenance.TelegramMaintenanceMixin._run_maintenance_pass,
        )
        self.assertIs(
            bot._open_admin_panel, telegram_admin_panels.TelegramAdminMixin._open_admin_panel
        )


if __name__ == "__main__":
    unittest.main()
