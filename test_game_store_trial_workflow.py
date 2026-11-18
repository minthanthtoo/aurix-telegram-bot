from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
from pathlib import Path
import tempfile
import unittest
import json

from cryptography.fernet import Fernet
from aurix_games.catalog_projection import ApprovedOfferInput
from aurix_games.customer_ledger import CustomerLedger
from aurix_games.eligibility import RegionRule, RecipientEligibilityEvidence
from aurix_games.fazercards_account import (
    SupplierWalletPolicy,
    WalletFundingIntentStore,
)
from aurix_games.fulfillment import FulfillmentTaskStore, GameFulfillmentWorker
from aurix_games.game_admin import GameStoreAdminService
from aurix_games.live_catalog import OfferMapping, SupplierOfferRecord, build_priced_offers
from aurix_games.order_reconciliation import SupplierWebhookReconciler
from aurix_games.order_repository import GameOrderRepository
from aurix_games.order_service import prepare_order
from aurix_games.order_state import OrderState
from aurix_games.payment_service import CustomerPaymentService
from aurix_games.pricing import PricingPolicy
from aurix_games.wallet_monitor import SupplierWalletAlertStore


UTC = timezone.utc
NOW = datetime(2026, 9, 22, 5, 0, tzinfo=UTC)


class _FakeSupplier:
    def __init__(self) -> None:
        self.create_calls = 0
        self.order_calls = 0

    def get_balance(self):
        return {"ok": True, "balance": "4.00", "currency": "USD"}

    def get_payment_methods(self):
        return {
            "ok": True,
            "items": [{"code": "usdt", "label": "USDT", "minAmountUsd": 1, "maxAmountUsd": 1000}],
        }

    def create_balance_payment(self, *, method, amount_usd):
        self.create_calls += 1
        return {
            "ok": True,
            "payment": {
                "id": "tx-trial-1",
                "status": "pending",
                "network": "USDT",
                "address": "provider-controlled",
                "amount": str(amount_usd),
                "paymentUrl": "https://pay.fzr.cards/invoice/tx-trial-1",
            },
        }

    def get_payment(self, payment_id):
        return {
            "ok": True,
            "payment": {"id": payment_id, "status": "completed", "network": "USDT", "paymentUrl": "https://pay.fzr.cards/invoice/tx-trial-1"},
        }

    def verify_binance_payment(self, payment_id, *, binance_order_id):
        return {
            "ok": True,
            "payment": {"id": payment_id, "status": "completed", "binanceId": binance_order_id},
        }

    def submit(self, *, fulfillment_kind, payload, idempotency_key):
        self.order_calls += 1
        return {"ok": True, "order": {"id": "ord-99101", "status": "processing"}}


class GameStoreTrialWorkflowTests(unittest.TestCase):
    def test_wallet_pricing_order_and_signed_completion_are_restart_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            supplier = _FakeSupplier()
            admin = GameStoreAdminService(
                admin_token="admin-secret",
                supplier_client=supplier,
                wallet_alert_store=SupplierWalletAlertStore(root / "wallet-alerts.sqlite3"),
                wallet_policy=SupplierWalletPolicy(
                    low_balance_threshold_usd=Decimal("10"),
                    suggested_topup_usd=Decimal("50"),
                ),
                funding_intent_store=WalletFundingIntentStore(root / "funding.sqlite3"),
                treasury_reviewers={"staff:synthetic": hashlib.sha256(b"t" * 32).hexdigest()},
            )

            wallet = admin.wallet_status(now=NOW)
            self.assertTrue(wallet["low_wallet"]["is_low"])
            self.assertTrue(wallet["low_wallet"]["should_remind"])
            intent = admin.create_wallet_invoice(
                {"intent_id": "trial-funding-1", "method": "usdt", "amount_usd": "50"}, treasury_token="t" * 32
            )
            replay = admin.create_wallet_invoice(
                {"intent_id": "trial-funding-1", "method": "usdt", "amount_usd": "50"}, treasury_token="t" * 32
            )
            self.assertEqual(intent, replay)
            self.assertEqual(supplier.create_calls, 1)

            record = SupplierOfferRecord(
                family="topups",
                provider_category_id="mlbb-global",
                provider_offer_id="d55",
                display_name="55 Diamonds",
                supplier_cost_usd=Decimal("1"),
                observed_at=NOW - timedelta(minutes=1),
                available=True,
            )
            mapping = OfferMapping(
                offer_id="aurix-mlbb-global-d55",
                product_slug="mobile-legends",
                product_name="Mobile Legends",
                fulfillment_kind="direct_topup",
                denomination_label="55 Diamonds",
                region_rule=RegionRule(verified=True, allow_all=True),
                required_input_labels=("player_id",),
            )
            policy = PricingPolicy(
                fx_rate=Decimal("4500"),
                target_profit_pct=Decimal("0.15"),
                promo_pct=Decimal("0.05"),
                minimum_profit_pct=Decimal("0.10"),
                rounding_minor=100,
            )
            priced = build_priced_offers(
                [record],
                {("topups", "mlbb-global", "d55"): mapping},
                policy=policy,
                now=NOW,
                max_age=timedelta(minutes=15),
            )
            self.assertEqual(len(priced), 1)
            self.assertGreater(priced[0].breakdown.applied_promo_discount_minor, 0)
            public = priced[0].public_offer
            offer = ApprovedOfferInput(
                offer_id=public.offer_id,
                product_slug=public.product_slug,
                product_name=public.product_name,
                fulfillment_kind=public.fulfillment_kind,
                denomination_label=public.denomination_label,
                retail_amount_minor=public.retail_amount_minor,
                currency=public.currency,
                observed_at=record.observed_at,
                review_state="approved",
                customer_visible=True,
                supplier_enabled=True,
                available=True,
                resale_authorized=True,
                region_rule=mapping.region_rule,
                region_label="Global",
                required_input_labels=public.required_input_labels,
            )
            repository = GameOrderRepository(root / "orders.sqlite3")
            prepared = prepare_order(
                repository,
                order_id="order-trial-1",
                customer_id="customer-trial-1",
                customer_idempotency_key="client-trial-1",
                offer=offer,
                recipient=RecipientEligibilityEvidence(
                    fulfillment_kind="direct_topup",
                    supplied_field_names=frozenset({"player_id"}),
                    # Synthetic trusted lookup evidence; never a live provider claim.
                    validation_valid=True,
                    validation_checked_at=NOW,
                    recipient_binding="opaque-recipient-fingerprint",
                    validation_binding="opaque-recipient-fingerprint",
                ),
                recipient_fingerprint="opaque-recipient-fingerprint",
                now=NOW,
            )
            self.assertEqual(prepared.order.state, OrderState.AWAITING_PAYMENT)
            ledger = CustomerLedger(root / "customer-ledger.sqlite3")
            payments = CustomerPaymentService(repository, ledger)
            payments.record_verified_credit(
                customer_id="customer-trial-1",
                amount_minor=public.retail_amount_minor,
                currency=public.currency,
                event_key="customer-payment-trial-1",
                actor="staff:trial",
                reason="synthetic verified payment for local trial",
                occurred_at=NOW,
            )
            payments.reserve_order(
                "order-trial-1",
                event_key="customer-reserve-trial-1",
                actor="system:trial",
                reason="reserve local trial order",
                occurred_at=NOW,
            )
            tasks = FulfillmentTaskStore(
                root / "fulfillment.sqlite3", encryption_key=Fernet.generate_key()
            )
            worker = GameFulfillmentWorker(
                orders=repository,
                tasks=tasks,
                supplier=supplier,
                payments=payments,
            )
            worker.enqueue_paid_order(
                "order-trial-1",
                payload={
                    "category_id": "mlbb-global",
                    "offer_id": "d55",
                    "fields": {"player_id": "opaque-trial-recipient"},
                },
                now=NOW,
            )
            submitted = worker.run_once(now=NOW)
            self.assertEqual(submitted[0].status, "submitted")
            self.assertEqual(supplier.order_calls, 1)

            body = json.dumps(
                {
                    "event": "order.status_changed",
                    "event_id": "event-trial-1",
                    "timestamp": NOW.isoformat(),
                    "data": {"order_id": "ord-99101", "status": "completed"},
                },
                separators=(",", ":"),
            ).encode()
            signature = "sha256=" + hmac.new(b"trial-secret", body, hashlib.sha256).hexdigest()
            reconciler = SupplierWebhookReconciler(repository, webhook_secret="trial-secret")
            first = reconciler.process(body, signature, now=NOW)
            second = reconciler.process(body, signature, now=NOW)
            self.assertEqual(first.state, "delivered")
            self.assertTrue(second.duplicate)
            self.assertEqual(repository.get("order-trial-1").state, OrderState.DELIVERED)


if __name__ == "__main__":
    unittest.main()
