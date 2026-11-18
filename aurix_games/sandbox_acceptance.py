"""Synthetic end-to-end acceptance flow for the AuriX Game Store.

This module is deliberately provider-free. It exercises the supplier offer,
pricing, order, admin payment review, fulfilment, webhook, and leaderboard
boundaries with a synthetic supplier and temporary SQLite stores.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
from typing import Any

from cryptography.fernet import Fernet

from .catalog_projection import ApprovedOfferInput
from .customer_ledger import CustomerLedger
from .eligibility import RecipientEligibilityEvidence, RegionRule
from .fazercards_account import SupplierWalletPolicy, WalletFundingIntentStore
from .fulfillment import FulfillmentTaskStore, GameFulfillmentWorker
from .game_admin import GameStoreAdminService
from .live_catalog import OfferMapping, SupplierOfferRecord, build_priced_offers
from .order_reconciliation import SupplierWebhookReconciler
from .order_repository import GameOrderRepository
from .order_service import prepare_order
from .order_state import OrderState
from .payment_review import PaymentReviewService
from .payment_service import CustomerPaymentService
from .pricing import PricingPolicy
from .store_features import StoreFeatureStore
from .wallet_monitor import SupplierWalletAlertStore


UTC = timezone.utc


class _SyntheticSupplier:
    """Minimal deterministic supplier used only by the local acceptance flow."""

    def __init__(self) -> None:
        self.create_calls = 0
        self.order_calls = 0

    def get_balance(self) -> dict[str, Any]:
        return {"ok": True, "balance": "4.00", "currency": "USD"}

    def get_payment_methods(self) -> dict[str, Any]:
        return {
            "ok": True,
            "items": [{"code": "usdt", "label": "USDT", "minAmountUsd": 1, "maxAmountUsd": 1000}],
        }

    def create_balance_payment(self, *, method: str, amount_usd: Decimal) -> dict[str, Any]:
        self.create_calls += 1
        return {
            "ok": True,
            "payment": {
                "id": "sandbox-payment-1",
                "status": "pending",
                "network": "USDT",
                "address": "provider-controlled",
                "amount": str(amount_usd),
                "paymentUrl": "https://pay.fzr.cards/sandbox/tx1",
            },
        }

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        return {
            "ok": True,
            "payment": {"id": payment_id, "status": "completed", "network": "USDT", "paymentUrl": "https://pay.fzr.cards/sandbox/tx1"},
        }

    def verify_binance_payment(self, payment_id: str, *, binance_order_id: str) -> dict[str, Any]:
        return {
            "ok": True,
            "payment": {"id": payment_id, "status": "completed", "binanceId": binance_order_id},
        }

    def submit(
        self, *, fulfillment_kind: str, payload: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        self.order_calls += 1
        return {"ok": True, "order": {"id": "ord-99001", "status": "processing"}}


def _step(name: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "status": "passed"}
    if details:
        result["details"] = details
    return result


def run_sandbox_acceptance(*, root: str | Path | None = None) -> dict[str, Any]:
    """Run the complete local acceptance flow and return a sanitized report.

    ``root`` is intended for tests or an operator-provided temporary folder.
    When omitted, all state is deleted when this function returns.  No
    supplier credentials, network requests, real payment, or external writes
    are used.
    """

    now = datetime.now(UTC).replace(microsecond=0)
    steps: list[dict[str, Any]] = []
    owned_temp: tempfile.TemporaryDirectory[str] | None = None
    if root is None:
        owned_temp = tempfile.TemporaryDirectory(prefix="aurix-game-store-sandbox-")
        work_root = Path(owned_temp.name)
    else:
        work_root = Path(root)
        work_root.mkdir(parents=True, exist_ok=True)

    try:
        supplier = _SyntheticSupplier()
        admin = GameStoreAdminService(
            admin_token="sandbox-admin-token",
            supplier_client=supplier,
            wallet_alert_store=SupplierWalletAlertStore(work_root / "wallet-alerts.sqlite3"),
            wallet_policy=SupplierWalletPolicy(
                low_balance_threshold_usd=Decimal("10"),
                suggested_topup_usd=Decimal("50"),
            ),
            funding_intent_store=WalletFundingIntentStore(work_root / "funding.sqlite3"),
            treasury_reviewers={"staff:synthetic": hashlib.sha256(b"t" * 32).hexdigest()},
        )

        wallet = admin.wallet_status(now=now)
        if not wallet["low_wallet"]["is_low"] or not wallet["low_wallet"]["should_remind"]:
            raise AssertionError("synthetic low-wallet reminder did not trigger")
        steps.append(_step("wallet_low_balance", details={"balance_usd": "4.00"}))

        invoice_payload = {"intent_id": "sandbox-funding-1", "method": "usdt", "amount_usd": "50"}
        invoice = admin.create_wallet_invoice(invoice_payload, treasury_token="t" * 32)
        replay = admin.create_wallet_invoice(invoice_payload, treasury_token="t" * 32)
        if invoice != replay or supplier.create_calls != 1:
            raise AssertionError("wallet funding intent was not idempotent")
        steps.append(_step("idempotent_supplier_funding_intent", details={"method": "usdt"}))

        record = SupplierOfferRecord(
            family="topups",
            provider_category_id="mlbb-global",
            provider_offer_id="d55",
            display_name="55 Diamonds",
            supplier_cost_usd=Decimal("1"),
            observed_at=now - timedelta(minutes=1),
            available=True,
        )
        unavailable_record = SupplierOfferRecord(
            family="topups",
            provider_category_id="mlbb-global",
            provider_offer_id="d99",
            display_name="99 Diamonds",
            supplier_cost_usd=Decimal("1.50"),
            observed_at=now - timedelta(minutes=1),
            available=False,
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
        unavailable_mapping = OfferMapping(
            offer_id="aurix-mlbb-global-d99",
            product_slug="mobile-legends",
            product_name="Mobile Legends",
            fulfillment_kind="direct_topup",
            denomination_label="99 Diamonds",
            region_rule=RegionRule(verified=True, allow_all=True),
            required_input_labels=("player_id",),
        )
        policy = PricingPolicy(
            fx_rate=Decimal("4800"),
            target_profit_pct=Decimal("0.15"),
            minimum_profit_pct=Decimal("0.15"),
            rounding_minor=100,
        )
        priced = build_priced_offers(
            [record, unavailable_record],
            {
                ("topups", "mlbb-global", "d55"): mapping,
                ("topups", "mlbb-global", "d99"): unavailable_mapping,
            },
            policy=policy,
            now=now,
            max_age=timedelta(minutes=15),
        )
        if len(priced) != 1 or priced[0].public_offer.offer_id != mapping.offer_id:
            raise AssertionError("unavailable supplier stock was exposed as a sellable offer")
        steps.append(
            _step(
                "supplier_offer_availability_gate",
                details={"synthetic_available_offers": 1, "synthetic_unavailable_offers_excluded": 1},
            )
        )
        if (priced[0].breakdown.final_price_minor != 5600
                or priced[0].breakdown.applied_promo_discount_minor != 0
                or priced[0].breakdown.profit_pct_on_cost < Decimal("0.15")):
            raise AssertionError("synthetic pricing projection does not match the owner-set pricing scenario")
        public = priced[0].public_offer
        steps.append(
            _step(
                "synthetic_pricing_projection",
                details={
                    "currency": public.currency,
                    "retail_amount_minor": public.retail_amount_minor,
                    "fx_rate_mmk_per_usd": "4800",
                    "profit_pct": "0.15",
                    "added_fees_minor": 0,
                    "rounding_minor": 100,
                    "live_policy_used": False,
                },
            )
        )

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
        repository = GameOrderRepository(work_root / "orders.sqlite3")
        store_features = StoreFeatureStore(repository.path)
        prepared = prepare_order(
            repository,
            order_id="sandbox-order-local-1",
            customer_id="sandbox-customer-1",
            customer_idempotency_key="sandbox-client-1",
            offer=offer,
            recipient=RecipientEligibilityEvidence(
                fulfillment_kind="direct_topup",
                supplied_field_names=frozenset({"player_id"}),
                # Synthetic trusted lookup evidence; never a live provider claim.
                validation_valid=True,
                validation_checked_at=now,
                recipient_binding="opaque-sandbox-recipient",
                validation_binding="opaque-sandbox-recipient",
            ),
            recipient_fingerprint="opaque-sandbox-recipient",
            now=now,
        )
        if prepared.order.state is not OrderState.AWAITING_PAYMENT:
            raise AssertionError("order did not enter awaiting-payment state")
        steps.append(_step("customer_order_awaiting_payment"))

        ledger = CustomerLedger(work_root / "customer-ledger.sqlite3")
        payments = CustomerPaymentService(repository, ledger)
        payment_token = "sandbox-review-token-32-characters"
        payment_review = PaymentReviewService(
            payments,
            reviewers={"staff:sandbox": hashlib.sha256(payment_token.encode()).hexdigest()},
        )
        repository.freeze_payment_intent(
            order_id="sandbox-order-local-1",
            customer_id="sandbox-customer-1",
            rail="kpay",
            receiver_id="sandbox-receiver",
            receiver_revision="sandbox-v1",
            opened_at=now,
            expires_at=now + timedelta(minutes=10),
            instructions={"account": "sandbox-account", "account_name": "Sandbox receiver"},
        )
        payment_review.submit(
            "sandbox-order-local-1",
            customer_id="sandbox-customer-1",
            transaction_reference="sandbox-transfer-1",
            evidence_sha256=hashlib.sha256(b"synthetic receipt evidence").hexdigest(),
            now=now + timedelta(minutes=1),
        )
        if payment_review.customer_status(
            "sandbox-order-local-1", customer_id="sandbox-customer-1"
        )["review_status"] != "in_review" or len(payment_review.pending(payment_token)) != 1:
            raise AssertionError("customer payment claim did not enter the admin review queue")
        payment_decision = payment_review.decide(
            "sandbox-order-local-1",
            token=payment_token,
            approve=True,
            reason="synthetic test transfer matched to the frozen order",
            verified_transfer={
                "rail": "kpay",
                "receiver_id": "sandbox-receiver",
                "transaction_id": "sandbox-transfer-1",
                "amount_minor": public.retail_amount_minor,
                "currency": public.currency,
                "occurred_at": now + timedelta(minutes=1),
            },
            now=now + timedelta(minutes=2),
            revision=1,
        )
        if (not payment_decision["applied"]
                or payment_review.customer_status("sandbox-order-local-1", customer_id="sandbox-customer-1")["review_status"] != "verified"
                or len(payment_review.pending(payment_token)) != 0
                or repository.get("sandbox-order-local-1").state is not OrderState.PAID_RESERVED):
            raise AssertionError("admin payment verification did not reserve the customer order")
        steps.append(
            _step(
                "synthetic_customer_payment_and_admin_review",
                details={"rail": "kpay", "review_queue_cleared": True, "real_transfer": False},
            )
        )

        tasks = FulfillmentTaskStore(work_root / "fulfillment.sqlite3", encryption_key=Fernet.generate_key())
        worker = GameFulfillmentWorker(orders=repository, tasks=tasks, supplier=supplier, payments=payments)
        worker.enqueue_paid_order(
            "sandbox-order-local-1",
            payload={
                "category_id": "mlbb-global",
                "offer_id": "d55",
                "fields": {"player_id": "opaque-sandbox-recipient"},
            },
            now=now,
        )
        submitted = worker.run_once(now=now)
        if len(submitted) != 1 or submitted[0].status != "submitted" or supplier.order_calls != 1:
            raise AssertionError("supplier fulfilment submission was not accepted exactly once")
        steps.append(_step("supplier_fulfillment_submission", details={"supplier_order_id": "ord-99001"}))

        body = json.dumps(
            {
                "event": "order.status_changed",
                "event_id": "sandbox-event-1",
                "timestamp": now.isoformat(),
                "data": {"order_id": "ord-99001", "status": "completed"},
            },
            separators=(",", ":"),
        ).encode()
        signature = "sha256=" + hmac.new(b"sandbox-webhook-secret", body, hashlib.sha256).hexdigest()
        reconciler = SupplierWebhookReconciler(repository, webhook_secret="sandbox-webhook-secret")
        first = reconciler.process(body, signature, now=now)
        second = reconciler.process(body, signature, now=now)
        if first.state != "delivered" or not second.duplicate:
            raise AssertionError("signed webhook completion or duplicate replay failed")
        if repository.get("sandbox-order-local-1").state is not OrderState.DELIVERED:
            raise AssertionError("order did not reach delivered state")
        steps.append(_step("signed_webhook_completion_and_deduplication"))
        if store_features.leaderboard(now=now)["entries"]:
            raise AssertionError("leaderboard exposed a customer before opt-in")
        store_features.save_leaderboard_profile(
            "sandbox-customer-1", {"display_name": "Sandbox Champion", "opted_in": True}, now=now
        )
        board = store_features.leaderboard(now=now)
        if (len(board["entries"]) != 1
                or board["entries"][0]["display_name"] != "Sandbox Champion"
                or board["entries"][0]["completed_orders"] != 1
                or "sandbox-customer-1" in json.dumps(board)):
            raise AssertionError("delivered, opted-in order did not appear safely on the leaderboard")
        steps.append(_step("delivered_order_leaderboard_opt_in"))
        return {
            "status": "passed",
            "generated_at": now.isoformat(),
            "supplier": "synthetic-local-only",
            "payment_simulation": {
                "customer_rail": "synthetic KPay review",
                "supplier_wallet": "synthetic USDT funding intent",
            },
            "external_writes": False,
            "supplier_key_used": False,
            "real_payment": False,
            "checkout_enabled": False,
            "steps": steps,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "generated_at": now.isoformat(),
            "supplier": "synthetic-local-only",
            "external_writes": False,
            "supplier_key_used": False,
            "real_payment": False,
            "checkout_enabled": False,
            "steps": steps,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if owned_temp is not None:
            owned_temp.cleanup()


__all__ = ["run_sandbox_acceptance"]
