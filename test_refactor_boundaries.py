import inspect
import unittest

import app
import commerce
import commerce_models
import commerce_repositories
import commerce_postgres_database
import commerce_sqlite_database
import commerce_migrations
import commerce_migrations_capacity
import commerce_migrations_endpoints
import commerce_migrations_identity
import commerce_migrations_probes
import commerce_migrations_quota
import commerce_migrations_receipts
import commerce_migrations_routing
import commerce_service
import commerce_service_dispatch
import commerce_service_wallet_approval
import commerce_service_wallet_payment
import commerce_service_wallet_read
import commerce_service_wallet_refunds
import commerce_worker
import commerce_worker_coordinator
import commerce_worker_dispatch
import entitlements
import identity
import identity_accounts
import identity_core
import identity_entitlements
import identity_generations
import identity_support
import identity_usage
import free_repository
import migrations
import infrastructure
import infrastructure_inventory
import infrastructure_provisioning
import digitalocean_client
import outline_adapter
import runtime
import schema_migrations
import runtime_composition
import runtime_bootstrap
import runtime_models
import runtime_outline
import runtime_settings
from lifecycle_policy import normalize_lifecycle_state
import telegram_transport
import telegram_admin
import telegram_admin_panels
import telegram_admin_navigation
import telegram_admin_capacity
import telegram_admin_state
import telegram_admin_confirmations
import telegram_admin_orders
import telegram_callbacks
import telegram_callback_router
import telegram_callback_customer
import telegram_callback_admin
import telegram_admin_confirmation
import telegram_command_context
import telegram_commands
import telegram_components
import telegram_customer_commands
import telegram_customer_access_commands
import telegram_customer_commerce_commands
import telegram_free_claim_commands
import telegram_operations_commands
import telegram_approval_commands
import telegram_promo_admin_commands
import telegram_receipt_admin_commands
import telegram_receipt_review_commands
import telegram_staff_commands
import telegram_maintenance
from ports import (
    CommerceWorkerPort,
    OutlineGateway,
    ReceiptExtractorGateway,
    ReceiptStorageGateway,
)
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

    def test_schema_execution_is_separate_from_component_migration_definitions(self):
        self.assertIs(schema_migrations.Migration, __import__("migrations").Migration)
        self.assertIs(schema_migrations.apply_migrations, __import__("migrations").apply_migrations)

    def test_identity_database_wallet_and_infrastructure_boundaries_are_explicit(self):
        self.assertIs(identity.IdentityError, identity_support.IdentityError)
        self.assertIn(identity_core.IdentityCoreMixin, identity.IdentityService.__mro__)
        self.assertIn(identity_accounts.IdentityAccountsMixin, identity.IdentityService.__mro__)
        self.assertIn(identity_entitlements.IdentityEntitlementsMixin, identity.IdentityService.__mro__)
        self.assertIn(identity_generations.IdentityGenerationsMixin, identity.IdentityService.__mro__)
        self.assertIn(identity_usage.IdentityUsageMixin, identity.IdentityService.__mro__)
        self.assertIs(commerce_repositories.CommerceDatabase, commerce_sqlite_database.CommerceDatabase)
        self.assertIs(
            commerce_repositories.PostgresCommerceDatabase,
            commerce_postgres_database.PostgresCommerceDatabase,
        )
        self.assertIs(
            commerce_service_dispatch.SERVICE_IMPLEMENTATIONS["approve_order"],
            commerce_service_wallet_approval.approve_order,
        )
        self.assertIs(
            commerce_service_dispatch.SERVICE_IMPLEMENTATIONS["pay_order_with_wallet"],
            commerce_service_wallet_payment.pay_order_with_wallet,
        )
        self.assertIs(
            commerce_service_dispatch.SERVICE_IMPLEMENTATIONS["consistency_report"],
            commerce_service_wallet_read.consistency_report,
        )
        self.assertIs(
            commerce_service_dispatch.SERVICE_IMPLEMENTATIONS["refund_order"],
            commerce_service_wallet_refunds.refund_order,
        )
        self.assertEqual(
            [item.version for item in migrations.COMMERCE_MIGRATIONS],
            list(range(1, 27)),
        )
        self.assertEqual(
            commerce_migrations.COMMERCE_MIGRATIONS,
            commerce_migrations_receipts.COMMERCE_MIGRATIONS_RECEIPTS
            + commerce_migrations_capacity.COMMERCE_MIGRATIONS_CAPACITY
            + commerce_migrations_identity.COMMERCE_MIGRATIONS_IDENTITY
            + commerce_migrations_quota.COMMERCE_MIGRATIONS_QUOTA
            + commerce_migrations_probes.COMMERCE_MIGRATIONS_PROBES
            + commerce_migrations_endpoints.COMMERCE_MIGRATIONS_ENDPOINTS
            + commerce_migrations_routing.COMMERCE_MIGRATIONS_ROUTING,
        )
        self.assertIs(infrastructure.DigitalOceanClient, digitalocean_client.DigitalOceanClient)
        self.assertIn(infrastructure_inventory.FleetInventoryMixin, infrastructure.FleetController.__mro__)
        self.assertIn(
            infrastructure_provisioning.FleetProvisioningMixin,
            infrastructure.FleetController.__mro__,
        )

    def test_admin_callbacks_and_runtime_composition_have_facades(self):
        self.assertIn(
            telegram_admin_navigation.TelegramAdminNavigationMixin,
            telegram_admin_panels.TelegramAdminMixin.__mro__,
        )
        self.assertIn(
            telegram_admin_capacity.TelegramAdminCapacityMixin,
            telegram_admin_panels.TelegramAdminMixin.__mro__,
        )
        self.assertIn(
            telegram_admin_state.TelegramAdminStateMixin,
            telegram_admin_panels.TelegramAdminMixin.__mro__,
        )
        self.assertIn(
            telegram_admin_confirmations.TelegramAdminConfirmationMixin,
            telegram_admin_panels.TelegramAdminMixin.__mro__,
        )
        self.assertIn(
            telegram_admin_orders.TelegramAdminOrderMixin,
            telegram_admin_panels.TelegramAdminMixin.__mro__,
        )
        self.assertIs(
            runtime_composition.RuntimeSettings,
            runtime_settings.RuntimeSettings,
        )
        self.assertIs(runtime_composition.RuntimeFactories, runtime_models.RuntimeFactories)
        self.assertIs(runtime_composition.compose_application, runtime_bootstrap.compose_application)
        self.assertEqual(telegram_callback_router.dispatch_callback.__module__, "telegram_callback_router")
        self.assertIn(
            telegram_callback_admin.handle_admin_workflow_callback,
            telegram_callback_admin.__dict__.values(),
        )

    def test_external_adapters_and_worker_have_explicit_boundaries(self):
        outline = outline_adapter.OutlineClient("https://outline.invalid/secret", "0" * 64)
        self.assertIs(app.OutlineClient, outline_adapter.OutlineClient)
        self.assertIsInstance(outline, OutlineGateway)
        self.assertNotIn("__getattr__", outline_adapter.OutlineServerPool.__dict__)
        self.assertIsInstance(NullReceiptStorage(), ReceiptStorageGateway)
        self.assertIsInstance(OpenAICompatibleReceiptExtractor(), ReceiptExtractorGateway)
        self.assertFalse(
            issubclass(commerce_service.CommerceService, commerce_worker.CommerceWorkerMixin)
        )
        self.assertNotIn("__getattr__", commerce_service.CommerceService.__dict__)
        self.assertNotIn("__getattr__", entitlements.ClaimService.__dict__)
        self.assertTrue(issubclass(commerce_worker_coordinator.CommerceWorker, CommerceWorkerPort))
        self.assertIn("process_jobs", commerce_worker_coordinator.CommerceWorker.__dict__)
        self.assertIn("worker", commerce_service.CommerceService.__init__.__code__.co_names)
        self.assertIn("process_jobs", commerce_worker.CommerceWorkerMixin.__dict__)
        self.assertIs(
            commerce_worker_dispatch.WORKER_IMPLEMENTATIONS["process_jobs"],
            __import__("commerce_worker_lifecycle").process_jobs,
        )
        self.assertNotIn("CommerceWorkerMixin", commerce_worker_coordinator.__dict__)
        self.assertNotIn("service", commerce_worker_coordinator.CommerceWorker.__dict__)
        self.assertNotIn("__getattr__", commerce_worker_coordinator.CommerceWorker.__dict__)
        self.assertIn(
            "CommerceWorkerDependencies", commerce_worker_coordinator.__dict__
        )
        self.assertNotIn(
            "getattr(self.service", inspect.getsource(commerce_worker_coordinator)
        )

    def test_telegram_component_rejects_undeclared_host_forwarding(self):
        host = type("Host", (), {"send": lambda self, *args: args, "secret": "hidden"})()
        component = telegram_components.TelegramComponent(host, telegram_commands.TelegramCommandMixin)
        self.assertEqual(component.send("chat", "text"), ("chat", "text"))
        with self.assertRaises(AttributeError):
            _ = component.secret

    def test_app_keeps_telegram_transport_compatibility_exports(self):
        self.assertIs(app.TelegramBot, telegram_transport.TelegramBot)
        self.assertIs(app.AdminOperations, telegram_transport.AdminOperations)

    def test_app_keeps_runtime_entrypoint_compatibility_export(self):
        self.assertIs(app.main, runtime.main)

    def test_runtime_settings_are_normalized_before_composition(self):
        settings = runtime_composition.RuntimeSettings.from_environment(
            {
                "TELEGRAM_BOT_TOKEN": "token",
                "AURIX_ACCESS_URL_KEY": "access-key",
                "OUTLINE_API_URL": "https://outline.invalid/secret",
                "OUTLINE_CERT_SHA256": "0" * 64,
                "ADMIN_TELEGRAM_IDS": "10,20",
                "AURIX_MAINTENANCE_INTERVAL_SECONDS": "90",
            }
        )
        self.assertEqual(settings.outline_timeout, 5.0)
        self.assertEqual(settings.admin_ids, {10, 20})
        self.assertEqual(settings.maintenance_interval_seconds, 90.0)
        self.assertEqual(settings.database_path.name, "bot.db")
        with self.assertRaisesRegex(SystemExit, "must be numeric"):
            runtime_composition.RuntimeSettings.from_environment(
                {
                    "TELEGRAM_BOT_TOKEN": "token",
                    "AURIX_ACCESS_URL_KEY": "access-key",
                    "OUTLINE_API_URL": "https://outline.invalid/secret",
                    "OUTLINE_CERT_SHA256": "0" * 64,
                    "OUTLINE_REQUEST_TIMEOUT_SECONDS": "fast",
                }
            )

    def test_lifecycle_policy_preserves_strict_validation(self):
        with self.assertRaises(ValueError):
            normalize_lifecycle_state(None, strict=True)
        self.assertEqual(normalize_lifecycle_state(None), "active")

    def test_telegram_transport_delegates_to_bounded_mixins(self):
        bot = telegram_transport.TelegramBot
        self.assertIs(telegram_transport.AdminOperations, telegram_admin.AdminOperations)
        self.assertFalse(issubclass(bot, telegram_commands.TelegramCommandMixin))
        self.assertFalse(issubclass(bot, telegram_callbacks.TelegramCallbackMixin))
        self.assertNotIn("handle", bot.__dict__)
        self.assertNotIn("handle_callback", bot.__dict__)
        self.assertNotIn("_run_maintenance_pass", bot.__dict__)
        self.assertNotIn("_open_admin_panel", bot.__dict__)
        self.assertTrue(hasattr(telegram_components.TelegramComponents, "resolve"))

    def test_telegram_command_boundary_uses_explicit_request_and_routers(self):
        self.assertIs(
            telegram_commands.prepare_command,
            telegram_command_context.prepare_command,
        )
        self.assertIs(
            telegram_callbacks.dispatch_callback,
            telegram_callback_router.dispatch_callback,
        )
        self.assertIs(
            telegram_commands.intercept_admin_confirmation,
            telegram_admin_confirmation.intercept_admin_confirmation,
        )
        self.assertIs(
            telegram_commands.dispatch_onboarding,
            telegram_customer_commands.dispatch_onboarding,
        )
        self.assertIs(
            telegram_commands.dispatch_customer_commerce_command,
            telegram_customer_commerce_commands.dispatch_customer_commerce_command,
        )
        self.assertIs(
            telegram_commands.dispatch_customer_access_command,
            telegram_customer_access_commands.dispatch_customer_access_command,
        )
        self.assertIs(
            telegram_commands.dispatch_free_claim_command,
            telegram_free_claim_commands.dispatch_free_claim_command,
        )
        self.assertIs(
            telegram_commands.dispatch_operations_command,
            telegram_operations_commands.dispatch_operations_command,
        )
        self.assertIs(
            telegram_commands.dispatch_approval_command,
            telegram_approval_commands.dispatch_approval_command,
        )
        self.assertIs(
            telegram_commands.dispatch_receipt_review_command,
            telegram_receipt_review_commands.dispatch_receipt_review_command,
        )
        self.assertIs(
            telegram_commands.dispatch_staff_command,
            telegram_staff_commands.dispatch_staff_command,
        )
        self.assertIs(
            telegram_commands.dispatch_promo_admin_command,
            telegram_promo_admin_commands.dispatch_promo_admin_command,
        )
        self.assertIs(
            telegram_commands.dispatch_receipt_admin_command,
            telegram_receipt_admin_commands.dispatch_receipt_admin_command,
        )
        fields = telegram_command_context.TelegramCommandContext.__dataclass_fields__
        self.assertEqual(
            set(fields),
            {
                "message",
                "chat_id",
                "telegram_id",
                "first_name",
                "username",
                "command",
                "args",
                "confirmed",
            },
        )


if __name__ == "__main__":
    unittest.main()
