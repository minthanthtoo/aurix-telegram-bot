from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from urllib.request import Request
from unittest.mock import patch

from aurix_games.game_admin import (
    GameAdminAuthError,
    GameStoreAdminService,
    build_env_admin_service,
)
from aurix_games.fazercards_account import SupplierWalletPolicy, WalletFundingIntentStore
from aurix_games.providers.fazercards_commands import FazerCardsCommandClient, SupplierWriteDisabledError
from aurix_games.wallet_monitor import SupplierWalletAlertStore
from aurix_games.pricing_store import PricingPolicyStore
from aurix_games.pricing import PricingPolicy
from aurix_games.game_admin import GameAdminInputError
from scripts.seed_fazercards_mapping_queue import build_blocked_queue


class _Response:
    status = 200

    def __init__(self, payload): self.payload = payload
    def read(self, limit=-1): return json.dumps(self.payload).encode()
    def __enter__(self): return self
    def __exit__(self, *args): return False


class _Opener:
    def __init__(self):
        self.requests: list[Request] = []
        self.payload = {"ok": True, "balance": "4.00", "currency": "USD"}

    def open(self, request, timeout):
        self.requests.append(request)
        path = request.full_url.rsplit("/api/v2/", 1)[-1]
        if path == "balance":
            payload = {"ok": True, "balance": "4.00", "currency": "USD"}
        elif path == "subscription":
            payload = {"ok": True, "subscription": {"plan": "trial", "status": "active", "trialEndsAt": "2026-09-23T00:00:00Z", "secret": "do-not-return"}, "apiKey": "do-not-return"}
        elif path == "account/webhook":
            payload = {"ok": True, "webhook": {"url": "https://store.example.test/api/v1/webhooks/fazercards", "enabled": True, "status": "active", "secret": "do-not-return"}, "apiKey": "do-not-return"}
        elif path == "payments/create":
            payload = {"ok": True, "payment": {"id": "tx1", "status": "pending", "network": "USDT", "address": "provider-controlled", "amount": "50", "paymentUrl": "https://pay.fzr.cards/invoice/tx1"}}
        elif path == "payments/methods":
            payload = {"ok": True, "items": [{"code": "usdt", "label": "USDT", "minAmountUsd": 5, "maxAmountUsd": 500}]}
        elif path.startswith("payments/"):
            payload = {"ok": True, "payment": {"id": "tx1", "status": "pending", "network": "USDT", "address": "provider-controlled", "amount": "50", "paymentUrl": "https://pay.fzr.cards/invoice/tx1"}}
        else:
            payload = {"ok": True, "webhook": {"url": "https://store.example.test/api/v1/webhooks/fazercards", "enabled": True, "status": "active", "secret": "do-not-return"}}
        return _Response(payload)


class GameAdminTests(unittest.TestCase):
    def test_official_subscription_fields_are_not_dropped(self):
        from aurix_games.game_admin import _safe_subscription
        result = _safe_subscription({"ok": True, "plan": "bronze", "subscriptionActive": True,
            "planExpiresAt": "2026-12-12T19:07:29Z", "planAutoRenew": False, "secret": "private"})
        self.assertIs(result["subscriptionActive"], True)
        self.assertIs(result["planAutoRenew"], False)
        self.assertEqual(result["planExpiresAt"], "2026-12-12T19:07:29Z")
        self.assertNotIn("secret", result)

    def test_supplier_payment_methods_return_validated_account_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            service = GameStoreAdminService(
                admin_token="admin-secret",
                supplier_client=FazerCardsCommandClient("secret", opener=_Opener()),
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
            )

            result = service.supplier_payment_methods()

            self.assertEqual(result["methods"], [{
                "code": "usdt", "label": "USDT",
                "min_amount_usd": "5", "max_amount_usd": "500",
            }])

    def test_bearer_boundary_and_wallet_status_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = _Opener()
            client = FazerCardsCommandClient("secret", allow_writes=True, opener=opener)
            service = GameStoreAdminService(
                admin_token="admin-secret",
                supplier_client=client,
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
                wallet_policy=SupplierWalletPolicy(low_balance_threshold_usd=Decimal("10")),
            )
            with self.assertRaises(GameAdminAuthError):
                service.authenticate("Bearer wrong")
            service.authenticate("Bearer admin-secret")
            status = service.wallet_status(now=datetime(2026, 9, 22, 2, tzinfo=timezone.utc))
            self.assertTrue(status["low_wallet"]["is_low"])
            self.assertTrue(status["low_wallet"]["alert_id"])
            alerts = service.wallet_alerts()
            self.assertEqual(len(alerts["alerts"]), 1)
            self.assertEqual(alerts["alerts"][0]["payload"]["balance_usd"], "4.00")
            subscription = service.subscription_status()
            self.assertEqual(subscription["subscription"]["subscription"]["plan"], "trial")
            self.assertNotIn("apiKey", json.dumps(subscription))
            self.assertNotIn("secret", json.dumps(subscription))

    def test_malformed_supplier_balance_never_creates_a_low_wallet_alert(self):
        now = datetime(2026, 9, 22, 2, tzinfo=timezone.utc)
        malformed = (
            {"ok": False, "balance": "4.00", "currency": "USD"},
            {"balance": "4.00", "currency": "USD"},
            {"ok": True, "balance": 4, "currency": "USD"},
            {"ok": True, "balance": "4.00", "currency": "MMK"},
        )

        class FixedBalanceClient:
            def __init__(self, payload):
                self.payload = payload

            def get_balance(self):
                return self.payload

        with tempfile.TemporaryDirectory() as directory:
            for index, payload in enumerate(malformed):
                with self.subTest(payload=payload):
                    store = SupplierWalletAlertStore(Path(directory) / f"alerts-{index}.sqlite3")
                    service = GameStoreAdminService(
                        admin_token="admin-secret",
                        supplier_client=FixedBalanceClient(payload),
                        wallet_alert_store=store,
                    )
                    with self.assertRaisesRegex(GameAdminInputError, "balance response was malformed"):
                        service.wallet_status(now=now)
                    self.assertEqual(service.wallet_alerts()["alerts"], [])

    def test_wallet_status_requires_timezone_aware_observation_time(self):
        with tempfile.TemporaryDirectory() as directory:
            service = GameStoreAdminService(
                admin_token="admin-secret",
                supplier_client=FazerCardsCommandClient("secret", opener=_Opener()),
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
            )
            with self.assertRaisesRegex(GameAdminInputError, "timezone-aware"):
                service.wallet_status(now=datetime(2026, 9, 22, 2))

    def test_invoice_requires_durable_store_before_supplier_call(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = _Opener()
            service = GameStoreAdminService(admin_token="admin-secret",
                supplier_client=FazerCardsCommandClient("secret", allow_writes=True, opener=opener),
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
                treasury_reviewers={"staff:alice": hashlib.sha256(b"t" * 32).hexdigest()})
            with self.assertRaisesRegex(GameAdminInputError, "persistent funding"):
                service.create_wallet_invoice({"intent_id": "fund-1", "method": "usdt", "amount_usd": "50"}, treasury_token="t" * 32)
            self.assertEqual(opener.requests, [])

    def test_invoice_and_status_never_return_api_key(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = _Opener()
            service = GameStoreAdminService(
                admin_token="admin-secret",
                supplier_client=FazerCardsCommandClient("secret", allow_writes=True, opener=opener),
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
                funding_intent_store=WalletFundingIntentStore(Path(directory) / "funding.sqlite3"),
                treasury_reviewers={"staff:alice": hashlib.sha256(b"t" * 32).hexdigest()},
            )
            with self.assertRaises(GameAdminAuthError):
                service.create_wallet_invoice({"intent_id": "fund-1", "method": "usdt", "amount_usd": "50"})
            intent = service.create_wallet_invoice({"intent_id": "fund-1", "method": "usdt", "amount_usd": "50"}, treasury_token="t" * 32)
            self.assertEqual(intent["intent"]["payment_id"], "tx1")
            self.assertEqual(intent["intent"]["payment_url"], "https://pay.fzr.cards/invoice/tx1")
            self.assertNotIn("secret", json.dumps(intent))
            status = service.payment_status("tx1")
            self.assertEqual(status["payment"]["status"], "pending")

    def test_unknown_invoice_blocks_new_intents_until_provider_absence_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WalletFundingIntentStore(Path(directory) / "funding.sqlite3")
            store.claim_creation("fund-lost", "usdt", Decimal("50"))
            service = GameStoreAdminService(admin_token="admin-secret",
                supplier_client=FazerCardsCommandClient("secret", allow_writes=True, opener=_Opener()),
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
                funding_intent_store=store,
                treasury_reviewers={"staff:alice": hashlib.sha256(b"t" * 32).hexdigest()})
            status = service.funding_reconciliation_status()
            self.assertTrue(status["invoice_creation_blocked"])
            self.assertEqual(status["unresolved_attempts"][0]["intent_id"], "fund-lost")
            with self.assertRaisesRegex(GameAdminInputError, "provider-confirmed"):
                service.resolve_funding_attempt_absent("fund-lost", {
                    "provider_reference": "ticket-17", "confirmed_absent": False}, treasury_token="t" * 32)
            result = service.resolve_funding_attempt_absent("fund-lost", {
                "provider_reference": "ticket-17", "confirmed_absent": True}, treasury_token="t" * 32)
            self.assertFalse(result["invoice_creation_blocked"])
            self.assertEqual(result["resolution"]["status"], "confirmed_absent")

    def test_recover_existing_invoice_requires_named_actor_and_never_creates(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WalletFundingIntentStore(Path(directory) / "funding.sqlite3")
            store.claim_creation("fund-lost", "usdt", Decimal("50"))
            opener = _Opener()
            service = GameStoreAdminService(admin_token="admin", supplier_client=FazerCardsCommandClient("secret", opener=opener),
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"), funding_intent_store=store,
                treasury_reviewers={"staff:alice": hashlib.sha256(b"t" * 32).hexdigest()})
            payload = {"payment_id": "tx1", "provider_reference": "case-17", "confirmed_match": True}
            with self.assertRaises(GameAdminAuthError):
                service.recover_funding_invoice("fund-lost", payload)
            self.assertEqual(opener.requests, [])
            result = service.recover_funding_invoice("fund-lost", payload, treasury_token="t" * 32)
            self.assertFalse(result["invoice_creation_blocked"])
            self.assertEqual(result["intent"]["payment_id"], "tx1")
            self.assertEqual([request.method for request in opener.requests], ["GET"])

    def test_webhook_configuration_is_sanitized_and_write_gated(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = _Opener()
            service = GameStoreAdminService(
                admin_token="admin-secret",
                supplier_client=FazerCardsCommandClient("secret", allow_writes=True, opener=opener),
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
            )
            status = service.webhook_status()
            self.assertEqual(status["webhook"]["status"], "active")
            self.assertNotIn("secret", json.dumps(status))
            configured = service.configure_webhook({"url": "https://store.example.test/api/v1/webhooks/fazercards", "enabled": True})
            self.assertTrue(configured["write_applied"])
            self.assertEqual(configured["webhook"]["url"], "https://store.example.test/api/v1/webhooks/fazercards")
            self.assertNotIn("secret", json.dumps(configured))
            deleted = service.delete_webhook()
            self.assertTrue(deleted["write_applied"])

            disabled = GameStoreAdminService(
                admin_token="admin-secret",
                supplier_client=FazerCardsCommandClient("secret", allow_writes=False, opener=opener),
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts-disabled.sqlite3"),
            )
            with self.assertRaises(SupplierWriteDisabledError) as error:
                disabled.configure_webhook({"url": "https://store.example.test/api/v1/webhooks/fazercards"})
            self.assertIn("disabled", str(error.exception).lower())

    def test_admin_can_update_pricing_policy_without_reaching_supplier(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = _Opener()
            service = GameStoreAdminService(
                admin_token="admin-secret",
                supplier_client=FazerCardsCommandClient("secret", opener=opener),
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
                pricing_policy_store=PricingPolicyStore(Path(directory) / "policy.json"),
            )
            now = datetime.now(timezone.utc)
            policy = PricingPolicy().as_dict()
            policy.update({"fx_rate": "4600", "promo_pct": "0.05"})
            result = service.update_pricing_policy({
                "policy": policy,
                "approval": {
                    "fx_source": "test fixture",
                    "fx_observed_at": now.isoformat(),
                    "valid_until": (now + timedelta(hours=1)).isoformat(),
                },
                "owner_approval": True,
            }, actor="staff:owner")
            self.assertEqual(result["policy"]["fx_rate"], "4600")
            self.assertTrue(result["rebuild_required"])
            self.assertEqual(result["approval"]["approved_by"], "staff:owner")
            self.assertEqual(opener.requests, [])

    def test_admin_can_enable_automatic_fx_with_owner_and_treasury_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = _Opener()
            service = GameStoreAdminService(
                admin_token="admin-secret",
                supplier_client=FazerCardsCommandClient("secret", opener=opener),
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
                pricing_policy_store=PricingPolicyStore(Path(directory) / "policy.json"),
            )
            result = service.update_pricing_policy({
                "policy": {**PricingPolicy().as_dict(), "fx_rate": "4800"},
                "automatic_fx": True,
                "owner_approval": True,
            }, actor="staff:treasury")
            self.assertEqual(result["policy_status"], "awaiting_rate")
            self.assertEqual(result["automatic_fx"]["floor_rate"], "4800")
            self.assertTrue(result["automatic_fx"]["enabled"])
            self.assertEqual(opener.requests, [])

    def test_auto_fx_configuration_does_not_accept_client_source_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            service = GameStoreAdminService(
                admin_token="admin-secret",
                supplier_client=None,
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
                pricing_policy_store=PricingPolicyStore(Path(directory) / "policy.json"),
            )
            with self.assertRaises(GameAdminInputError):
                service.update_pricing_policy({
                    "policy": PricingPolicy().as_dict(),
                    "automatic_fx": True,
                    "source_url": "https://attacker.invalid/rate",
                    "owner_approval": True,
                }, actor="staff:treasury")

    def test_policy_update_requires_explicit_owner_acknowledgement_and_complete_values(self):
        with tempfile.TemporaryDirectory() as directory:
            service = GameStoreAdminService(
                admin_token="admin-secret",
                supplier_client=None,
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
                pricing_policy_store=PricingPolicyStore(Path(directory) / "policy.json"),
            )
            with self.assertRaises(GameAdminInputError):
                service.update_pricing_policy({"policy": {"fx_rate": "4500"}}, actor="staff:owner")
            self.assertEqual(service.get_pricing_policy()["policy_status"], "unconfigured")

    def test_mapping_review_is_aggregate_and_never_returns_private_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            service = GameStoreAdminService(
                admin_token="admin-secret",
                supplier_client=FazerCardsCommandClient("secret"),
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
                mapping_review_provider=lambda: {
                    "status": "blocked",
                    "reason": "mapping_config_missing",
                    "snapshot": "/private/snapshot.json",
                    "config": "/private/config.json",
                    "mapped": 0,
                    "projected": 0,
                    "captured_at": "2026-09-22T03:00:00Z",
                    "block_reasons": {"missing_mapping": 12},
                    "supplier_cost_usd": "1.00",
                    "writes": False,
                    "supplier_key_used": False,
                },
            )
            payload = service.mapping_review_status()
            encoded = json.dumps(payload)
            self.assertEqual(payload["mapping_review"]["reason"], "mapping_config_missing")
            self.assertEqual(payload["mapping_review"]["block_reasons"], {"missing_mapping": 12})
            self.assertNotIn("/private/", encoded)
            self.assertNotIn("supplier_cost", encoded)

    def test_mapping_history_is_bounded_and_redacts_rights_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            service = GameStoreAdminService(
                admin_token="admin-secret",
                supplier_client=FazerCardsCommandClient("secret"),
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
                mapping_history_provider=lambda: {
                    "status": "available",
                    "queue_status": "operator_reviewed",
                    "last_reviewed_at": "2026-09-23T00:00:00+00:00",
                    "count": 1,
                    "events": [{
                        "event_id": "event-1",
                        "provider_key": ["topups", "mlbb", "d55"],
                        "offer_id": "mlbb-d55",
                        "product_slug": "mobile-legends",
                        "fulfillment_kind": "direct_topup",
                        "region_rule": {"verified": True, "allow_all": False, "dimension": "account_region", "allowed_values": ["global"]},
                        "rights_evidence_recorded": True,
                        "reviewer": "operator",
                        "approved_at": "2026-09-23T00:00:00+00:00",
                        "rights_evidence": "https://private.example/evidence",
                    }],
                    "writes": False,
                },
            )
            payload = service.mapping_history_status()
            encoded = json.dumps(payload)
            self.assertEqual(payload["mapping_history"]["count"], 1)
            self.assertEqual(payload["mapping_history"]["events"][0]["provider_key"][2], "d55")
            self.assertTrue(payload["mapping_history"]["events"][0]["rights_evidence_recorded"])
            self.assertNotIn('"rights_evidence":', encoded)
            self.assertNotIn("private.example", encoded)

    def test_catalog_inventory_is_exact_but_paginated_and_admin_only(self):
        with tempfile.TemporaryDirectory() as directory:
            service = GameStoreAdminService(
                admin_token="admin-secret",
                supplier_client=FazerCardsCommandClient("secret"),
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
                catalog_inventory_provider=lambda: {
                    "source_sha256": "abc123",
                    "captured_at": "2026-09-23T00:00:00Z",
                    "source_status": "complete",
                    "counts": {"topups": 2},
                    "category_counts": {"topups": 1},
                    "items": [
                        {"family": "topups", "category_id": "mlbb", "category_name": "Mobile Legends", "sku": "d55", "name": "55 Diamonds", "price_usd": "1.2300", "mapping_status": "blocked", "mapping_gate": "blocked", "projection_status": "not_projected"},
                        {"family": "topups", "category_id": "mlbb", "category_name": "Mobile Legends PUBG Mobile", "sku": "d110", "name": "110 Diamonds", "price_usd": "2.4600", "mapping_status": "blocked", "mapping_gate": "blocked", "projection_status": "not_projected"},
                    ],
                    "auxiliary": {},
                },
            )
            payload = service.catalog_inventory(family="topups", query="d55", limit=1)
            self.assertEqual(payload["total_count"], 1)
            self.assertEqual(payload["items"][0]["sku"], "d55")
            self.assertEqual(payload["mapping_status_counts"], {"blocked": 1})
            self.assertEqual(payload["mapping_gate_counts"], {"blocked": 1})
            self.assertEqual(payload["projection_status_counts"], {"not_projected": 1})
            hint = payload["items"][0]["mapping_candidate"]
            self.assertEqual(hint["status"], "single_title_candidate")
            self.assertEqual(hint["candidates"], [{
                "product_slug": "mobile-legends",
                "product_name": "Mobile Legends",
                "match_method": "exact_title",
            }])
            self.assertTrue(hint["review_required"])
            self.assertEqual(hint["approval_effect"], "none")
            self.assertEqual(payload["items"][0]["mapping_gate"], "blocked")
            self.assertEqual(payload["items"][0]["projection_status"], "not_projected")
            self.assertFalse(payload["customer_visible"])
            self.assertFalse(payload["checkout_enabled"])

            ambiguous = service.catalog_inventory(family="topups", query="d110", limit=1)
            self.assertEqual(
                ambiguous["items"][0]["mapping_candidate"]["status"],
                "ambiguous_title_candidates",
            )
            self.assertEqual(
                {candidate["product_slug"] for candidate in ambiguous["items"][0]["mapping_candidate"]["candidates"]},
                {"mobile-legends", "pubg-mobile"},
            )

    def test_environment_builder_uses_persistent_games_base_for_operator_state(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "persistent" / "fazercards"
            with patch.dict(
                os.environ,
                {
                    "AURIX_GAMES_BASE_DIR": str(base),
                    "AURIX_GAMES_ADMIN_TOKEN": "admin-secret",
                    "FAZERCARDS_API_KEY": "supplier-secret",
                    "FAZERCARDS_ENABLE_WRITES": "0",
                },
                clear=False,
            ):
                service = build_env_admin_service()
            self.assertIsNotNone(service)
            assert service is not None
            self.assertEqual(service.wallet_alert_store.path, base / "wallet-alerts.sqlite3")
            self.assertEqual(service.pricing_policy_store.path, base / "public-catalog-config.json")
            self.assertEqual(service.funding_intent_store.path, base / "funding-intents.sqlite3")

    def test_mapping_approval_provider_is_admin_scoped_and_response_redacts_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            received = []

            def approve(payload):
                received.append(payload)
                return {
                    "status": "approved_row",
                    "provider_key": ["topups", "mlbb", "d55"],
                    "offer_id": "mobile-legends-d55",
                    "product_slug": "mobile-legends",
                    "fulfillment_kind": "direct_topup",
                    "approved_at": "2026-09-23T00:00:00+00:00",
                    "local_config_updated": True,
                    "supplier_write": False,
                    "customer_publication": False,
                    "rights_evidence": "private://contract-do-not-return",
                }

            service = GameStoreAdminService(
                admin_token="admin-secret",
                supplier_client=None,
                wallet_alert_store=SupplierWalletAlertStore(Path(directory) / "alerts.sqlite3"),
                mapping_approval_provider=approve,
            )
            with self.assertRaises(GameAdminAuthError):
                service.authenticate("Bearer wrong")
            service.authenticate("Bearer admin-secret")
            payload = {"family": "topups", "sku": "d55", "rights_evidence": "private ref"}
            result = service.approve_catalog_mapping(payload)
            self.assertEqual(received, [payload])
            encoded = json.dumps(result)
            self.assertIn('"local_config_updated": true', encoded)
            self.assertIn('"customer_publication": false', encoded)
            self.assertNotIn("rights_evidence", encoded)
            self.assertNotIn("private://", encoded)

    def test_environment_builder_allows_admin_only_for_local_mapping_review(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "private" / "fazercards"
            base.mkdir(parents=True, mode=0o700)
            with patch.dict(
                os.environ,
                {
                    "AURIX_GAMES_ADMIN_TOKEN": "admin-secret",
                    "AURIX_GAMES_BASE_DIR": str(base),
                    "FAZERCARDS_API_KEY": "",
                },
                clear=True,
            ):
                service = build_env_admin_service()
            self.assertIsNotNone(service)
            assert service is not None
            self.assertIsNone(service.supplier_client)
            self.assertIsNotNone(service.mapping_approval_provider)

    def test_private_mapping_approval_requires_fresh_complete_exact_category_even_when_global_snapshot_is_stale_and_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "private"
            base.mkdir(mode=0o700)
            snapshot_path = base / "snapshot.json"
            config_path = base / "config.json"
            fresh_observed_at = datetime.now(timezone.utc).isoformat()
            snapshot = {
                "snapshot_version": 1,
                "captured_at": "2026-09-22T00:00:00+00:00",
                "status": "complete",
                "coverage": {},
                "families": {
                    "topups": {
                        "categories": [{"category_id": "mlbb", "name": "Mobile Legends", "note": ""}],
                        "categories_observed_at": fresh_observed_at,
                        "details_observed_at_by_id": {"mlbb": fresh_observed_at},
                        "categories_complete": True,
                        "details_by_id": {"mlbb": {
                            "category_id": "mlbb",
                            "offers": [
                                {"offer_id": "d55", "name": "55 Diamonds", "price_usd": "1.23", "stock": 10},
                                {"offer_id": "d110", "name": "110 Diamonds", "price_usd": "2.46", "stock": 10},
                                {"offer_id": "d165", "name": "165 Diamonds", "price_usd": "3.69", "stock": 10},
                            ],
                            "fields": [{"key": "user_id", "label": "User ID", "type": "text"}],
                        }},
                    },
                    "giftcards": {"categories": [], "details_by_id": {}},
                    "gamekeys": {"categories": [], "details_by_id": {}},
                    "manual_services": {
                        "items_observed_at": fresh_observed_at,
                        "offers_observed_at_by_id": {"manual-boost": fresh_observed_at},
                        "items": [{"id": "manual-boost", "name": "Manual Boost", "kind": "game-service", "chat": False, "info": ""}],
                        "offers_by_id": {"manual-boost": {
                            "manual_service_id": "manual-boost",
                            "items": [{"id": "boost-1", "name": "Boost service", "price_usd": "2.50"}],
                        }},
                    },
                    "steam_gifts": {"games": []},
                    "topup_validation": {"items": []},
                },
            }
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            snapshot_path.chmod(0o600)
            queue = build_blocked_queue(snapshot)
            # Exercise the recovery behavior for exact API rows missing from an
            # older mapping queue; the operator's explicit approval may append
            # only the selected row without replacing existing history.
            queue["mappings"] = [row for row in queue["mappings"] if row["family"] == "topups" and row["provider_offer_id"] == "d55"]
            queue["mapping_rows"] = len(queue["mappings"])
            config_path.write_text(json.dumps(queue), encoding="utf-8")
            config_path.chmod(0o600)
            env = {
                "AURIX_GAMES_ADMIN_TOKEN": "admin-secret",
                "AURIX_GAMES_BASE_DIR": str(base),
                "FAZERCARDS_API_KEY": "",
                "FAZERCARDS_CATALOG_SNAPSHOT": str(snapshot_path),
                "FAZERCARDS_PUBLIC_CATALOG_CONFIG": str(config_path),
                "FAZERCARDS_CATALOG_MAX_AGE_MINUTES": "15",
            }
            with patch.dict(os.environ, env, clear=True):
                service = build_env_admin_service()
                assert service is not None
                request = {
                    "family": "topups",
                    "category_id": "mlbb",
                    "sku": "d55",
                    "offer_id": "mobile-legends-d55",
                    "product_slug": "mobile-legends",
                    "fulfillment_kind": "direct_topup",
                    "region_rule": {"allow_all": False, "dimension": "game_server", "allowed_values": ["mm"]},
                    "region_label": "Myanmar server",
                    "required_input_labels": ["player_id"],
                    "validation_category_id": "mobile_legends",
                    "validation_field_map": {"user_id": "player_id"},
                    "validation_region_map": {"Myanmar": "mm"},
                    "rights_evidence": "private://resale-terms",
                    "reviewer": "owner",
                    "confirm_approval": True,
                }
                before = config_path.read_bytes()
                with self.assertRaises(GameAdminInputError):
                    service.approve_catalog_mapping({**request, "validation_field_map": {"user_id": "missing"}})
                self.assertEqual(config_path.read_bytes(), before)
                result = service.approve_catalog_mapping(request)
                self.assertTrue(result["mapping_approval"]["local_config_updated"])
                self.assertFalse(result["mapping_approval"]["customer_publication"])
                saved = json.loads(config_path.read_text(encoding="utf-8"))
                row = saved["mappings"][0]
                self.assertEqual(row["denomination_label"], "55 Diamonds")
                self.assertEqual(row["product_name"], "Mobile Legends")
                self.assertEqual(row["required_input_labels"], ["player_id"])
                self.assertEqual(row["validation_category_id"], "mobile_legends")
                self.assertEqual(row["validation_field_map"], {"user_id": "player_id"})
                self.assertEqual(row["validation_region_map"], {"Myanmar": "mm"})
                self.assertTrue(row["resale_authorized"])
                self.assertEqual(len(saved["approval_history"]), 1)

                manual_result = service.approve_catalog_mapping({
                    "family": "manual_services",
                    "category_id": "manual-boost",
                    "sku": "boost-1",
                    "offer_id": "mobile-legends-manual-boost",
                    "product_slug": "mobile-legends",
                    "fulfillment_kind": "manual_service",
                    "region_rule": {"allow_all": True},
                    "required_input_labels": ["Player ID"],
                    "rights_evidence": "private://manual-service-terms",
                    "reviewer": "owner",
                    "confirm_approval": True,
                })
                self.assertTrue(manual_result["mapping_approval"]["local_config_updated"])
                saved = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(len(saved["mappings"]), 2)
                self.assertEqual(len(saved["approval_history"]), 2)
                manual_row = next(row for row in saved["mappings"] if row["family"] == "manual_services")
                self.assertEqual(manual_row["fulfillment_kind"], "manual_service")
                self.assertEqual(manual_row["denomination_label"], "Boost service")

                snapshot["status"] = "partial"
                snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
                snapshot_path.chmod(0o600)
                partial_result = service.approve_catalog_mapping({
                    "family": "topups", "category_id": "mlbb", "sku": "d110",
                    "offer_id": "mobile-legends-d110", "product_slug": "mobile-legends",
                    "fulfillment_kind": "direct_topup",
                    "region_rule": {"allow_all": True},
                    "rights_evidence": "private://resale-terms", "reviewer": "owner",
                    "confirm_approval": True,
                })
                self.assertTrue(partial_result["mapping_approval"]["local_config_updated"])
                snapshot["families"]["topups"]["categories_complete"] = False
                snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
                snapshot_path.chmod(0o600)
                with self.assertRaisesRegex(GameAdminInputError, "category listing"):
                    service.approve_catalog_mapping({
                        "family": "topups", "category_id": "mlbb", "sku": "d165",
                        "offer_id": "mobile-legends-d165", "product_slug": "mobile-legends",
                        "fulfillment_kind": "direct_topup",
                        "region_rule": {"allow_all": True},
                        "rights_evidence": "private://resale-terms", "reviewer": "owner",
                        "confirm_approval": True,
                    })


if __name__ == "__main__":
    unittest.main()
