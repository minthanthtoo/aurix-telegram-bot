from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import threading
from unittest.mock import Mock, patch
from urllib.request import Request

from aurix_games.fazercards_account import (
    SupplierAccountInputError,
    SupplierWalletPolicy,
    SupplierWalletSnapshot,
    WalletFundingService,
    WalletFundingIntentStore,
    evaluate_low_wallet,
    parse_supplier_wallet_snapshot,
)
from aurix_games.providers.fazercards_commands import (
    FazerCardsCommandClient,
    SupplierWriteDisabledError,
)


UTC = timezone.utc


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def read(self, limit=-1):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Opener:
    def __init__(self, payload, methods=None):
        self.payload = payload
        self.methods = methods or {
            "ok": True,
            "items": [{"code": "usdt", "label": "USDT", "minAmountUsd": 1, "maxAmountUsd": 1000}],
        }
        self.requests: list[Request] = []

    def open(self, request, timeout):
        self.requests.append(request)
        if request.full_url.endswith("/payments/methods"):
            return _Response(self.methods)
        return _Response(self.payload)


class FazerCardsAccountTests(unittest.TestCase):
    def test_discovered_invoice_is_read_verified_linked_atomically_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WalletFundingIntentStore(Path(directory) / "funding.sqlite3")
            store.claim_creation("lost", "usdt", Decimal("50"))
            client = Mock()
            payment = {"id": "tx17", "network": "USDT", "amount": "50", "status": "pending"}
            client.get_payment.return_value = {"ok": True, "payment": payment}
            service = WalletFundingService(client, intent_store=store)
            args = dict(intent_id="lost", payment_id="tx17", provider_reference="support-1",
                reviewer="staff:alice", confirmed_match=True)
            for changes in ({"id": "tx18"}, {"amount": "49"}, {"amount": None}, {"network": "TON"}):
                client.get_payment.return_value = {"ok": True, "payment": {**payment, **changes}}
                with self.assertRaises(SupplierAccountInputError):
                    service.recover_existing(**args)
                self.assertEqual(len(store.unresolved_attempts()), 1)
            client.get_payment.return_value = {"ok": True, "payment": payment}
            with self.assertRaises(SupplierAccountInputError):
                service.recover_existing(**{**args, "confirmed_match": False})
            recovered = service.recover_existing(**args)
            self.assertEqual(recovered.payment_id, "tx17")
            self.assertEqual(store.unresolved_attempts(), ())
            client.get_payment.return_value = {"ok": True, "payment": {**payment, "status": "completed"}}
            self.assertEqual(service.recover_existing(**args), recovered)
            with self.assertRaises(SupplierAccountInputError):
                service.recover_existing(**{**args, "provider_reference": "changed"})
            store.claim_creation("another", "usdt", Decimal("50"))
            with self.assertRaises(SupplierAccountInputError):
                service.recover_existing(**{**args, "intent_id": "another"})
            self.assertEqual(len(store.unresolved_attempts()), 1)
            with sqlite3.connect(store.path) as db:
                self.assertEqual(db.execute("SELECT reviewer FROM supplier_wallet_funding_links").fetchone()[0], "staff:alice")
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("DELETE FROM supplier_wallet_funding_links")
            client.create_balance_payment.assert_not_called()

    def test_account_reads_and_pagination_are_bounded(self):
        opener = _Opener({"ok": True, "balance": "12.3400", "currency": "USD"})
        client = FazerCardsCommandClient("secret", opener=opener)
        client.get_me()
        client.get_balance()
        client.list_transactions(page=2, limit=100)
        client.list_orders(page=1, limit=50)
        client.get_payment_methods()
        client.get_webhook()
        client.list_webhook_deliveries()
        self.assertEqual(opener.requests[2].full_url, "https://api.fzr.cards/api/v2/transactions?page=2&limit=100")
        self.assertEqual(opener.requests[3].full_url, "https://api.fzr.cards/api/v2/orders?page=1&limit=50")
        with self.assertRaises(ValueError):
            client.list_transactions(page=0)

    def test_supplier_wallet_writes_stay_disabled_without_explicit_switch(self):
        opener = _Opener({"ok": True, "payment": {"id": "tx1", "status": "pending"}})
        client = FazerCardsCommandClient("secret", opener=opener)
        with self.assertRaises(SupplierWriteDisabledError):
            client.create_balance_payment(method="usdt", amount_usd=50)
        self.assertEqual(opener.requests, [])

    def test_funding_intent_is_restart_serializable_and_refreshable(self):
        payload = {
            "ok": True,
            "payment": {
                "id": "tx1",
                "network": "USDT",
                "amount": "50",
                "uniqueAmount": None,
                "address": "provider-controlled",
                "memo": None,
                "status": "pending",
                "paymentUrl": "https://pay.fzr.cards/invoice/tx1",
                "expiresAt": "2026-09-22T03:00:00Z",
                "createdAt": "2026-09-22T02:00:00Z",
            },
        }
        opener = _Opener(payload)
        client = FazerCardsCommandClient("secret", allow_writes=True, opener=opener)
        service = WalletFundingService(client)
        intent = service.create_intent(intent_id="fund-1", method="usdt", amount_usd="50")
        self.assertEqual(intent.payment_id, "tx1")
        self.assertEqual(intent.as_dict()["payment_url"], "https://pay.fzr.cards/invoice/tx1")
        refreshed = service.refresh(intent)
        self.assertEqual(refreshed.status, "pending")
        self.assertEqual(opener.requests[0].get_method(), "GET")
        self.assertTrue(opener.requests[0].full_url.endswith("/payments/methods"))
        self.assertEqual(opener.requests[1].get_method(), "POST")
        self.assertEqual(json.loads(opener.requests[1].data), {"method": "usdt", "amount": 50})
        self.assertEqual(opener.requests[2].get_method(), "GET")

    def test_funding_intent_never_exposes_non_https_payment_url(self):
        opener = _Opener({"ok": True, "payment": {"id": "tx1", "status": "pending", "paymentUrl": "http://untrusted.example/pay"}})
        client = FazerCardsCommandClient("secret", allow_writes=True, opener=opener)

        intent = WalletFundingService(client).create_intent(
            intent_id="fund-unsafe-url", method="usdt", amount_usd="50"
        )

        self.assertIsNone(intent.payment_url)

    def test_funding_amount_is_checked_against_current_account_bounds_before_invoice(self):
        opener = _Opener(
            {"ok": True, "payment": {"id": "tx1", "status": "pending"}},
            methods={"ok": True, "items": [{"code": "usdt", "label": "USDT", "minAmountUsd": 5, "maxAmountUsd": 500}]},
        )
        client = FazerCardsCommandClient("secret", allow_writes=True, opener=opener)

        with self.assertRaisesRegex(SupplierAccountInputError, "outside the current account limits"):
            WalletFundingService(client).create_intent(
                intent_id="fund-too-small", method="usdt", amount_usd="4.99"
            )

        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(opener.requests[0].get_method(), "GET")
        self.assertTrue(opener.requests[0].full_url.endswith("/payments/methods"))

    def test_persisted_intent_prevents_duplicate_invoice_after_restart(self):
        payload = {"ok": True, "payment": {"id": "tx1", "network": "USDT", "amount": "50", "status": "pending", "paymentUrl": "https://pay.fzr.cards/invoice/tx1"}}
        with tempfile.TemporaryDirectory() as directory:
            opener = _Opener(payload)
            client = FazerCardsCommandClient("secret", allow_writes=True, opener=opener)
            store = WalletFundingIntentStore(Path(directory) / "funding.sqlite3")
            first = WalletFundingService(client, intent_store=store).create_intent(
                intent_id="fund-1", method="usdt", amount_usd="50"
            )
            second = WalletFundingService(client, intent_store=store).create_intent(
                intent_id="fund-1", method="usdt", amount_usd="50"
            )
            self.assertEqual(first, second)
            self.assertEqual(len(opener.requests), 2)
            self.assertEqual(
                [request.get_method() for request in opener.requests], ["GET", "POST"]
            )
            with self.assertRaises(SupplierAccountInputError):
                WalletFundingService(client, intent_store=store).create_intent(
                    intent_id="fund-1", method="usdt", amount_usd="55"
                )

    def test_unknown_create_and_post_create_persistence_failure_cannot_create_again(self):
        for fail_after_create in (False, True):
            with self.subTest(fail_after_create=fail_after_create), tempfile.TemporaryDirectory() as directory:
                client = Mock()
                client.get_payment_methods.return_value = {"ok": True, "items": [{"code": "usdt", "label": "USDT", "minAmountUsd": 1, "maxAmountUsd": 1000}]}
                client.create_balance_payment.return_value = {"ok": True, "payment": {"id": "tx1", "status": "pending"}}
                store = WalletFundingIntentStore(Path(directory) / "funding.sqlite3")
                if fail_after_create:
                    with patch.object(store, "put", side_effect=SupplierAccountInputError("disk failed")):
                        with self.assertRaises(SupplierAccountInputError):
                            WalletFundingService(client, intent_store=store).create_intent(intent_id="fund-1", method="usdt", amount_usd=50)
                else:
                    client.create_balance_payment.side_effect = TimeoutError()
                    with self.assertRaises(TimeoutError):
                        WalletFundingService(client, intent_store=store).create_intent(intent_id="fund-1", method="usdt", amount_usd=50)
                restarted = WalletFundingIntentStore(store.path)
                with self.assertRaisesRegex(SupplierAccountInputError, "uncertain"):
                    WalletFundingService(client, intent_store=restarted).create_intent(intent_id="fund-1", method="usdt", amount_usd=50)
                with self.assertRaisesRegex(SupplierAccountInputError, "previous funding creation is uncertain"):
                    WalletFundingService(client, intent_store=restarted).create_intent(intent_id="fund-2", method="usdt", amount_usd=50)
                self.assertEqual(client.create_balance_payment.call_count, 1)

    def test_provider_confirmed_absence_is_required_and_audited_before_new_invoice(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WalletFundingIntentStore(Path(directory) / "funding.sqlite3")
            store.claim_creation("fund-lost", "usdt", Decimal("50"))
            self.assertEqual(store.unresolved_attempts()[0]["intent_id"], "fund-lost")
            with self.assertRaisesRegex(SupplierAccountInputError, "attestation"):
                store.resolve_confirmed_absent("fund-lost", provider_reference="ticket-17",
                    reviewer="Alice", confirmed_absent=False)
            with self.assertRaisesRegex(SupplierAccountInputError, "unknown funding attempt was not found"):
                store.resolve_confirmed_absent("missing", provider_reference="ticket-17",
                    reviewer="Alice", confirmed_absent=True)

            resolution = store.resolve_confirmed_absent("fund-lost", provider_reference="ticket-17",
                reviewer="Alice", confirmed_absent=True)
            self.assertEqual(resolution["status"], "confirmed_absent")
            self.assertEqual(store.unresolved_attempts(), ())
            self.assertTrue(store.resolve_confirmed_absent("fund-lost", provider_reference="ticket-17",
                reviewer="Alice", confirmed_absent=True)["replayed"] == "true")
            with sqlite3.connect(store.path) as database:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    database.execute("UPDATE supplier_wallet_funding_resolutions SET reviewer='Mallory'")
                with self.assertRaisesRegex(sqlite3.IntegrityError, "retained"):
                    database.execute("DELETE FROM supplier_wallet_funding_resolutions")

            client = Mock()
            client.get_payment_methods.return_value = {"ok": True, "items": [
                {"code": "usdt", "label": "USDT", "minAmountUsd": 1, "maxAmountUsd": 1000}]
            }
            client.create_balance_payment.return_value = {"ok": True, "payment": {"id": "tx2", "status": "pending"}}
            result = WalletFundingService(client, intent_store=store).create_intent(
                intent_id="fund-new", method="usdt", amount_usd=50)
            self.assertEqual(result.payment_id, "tx2")
            self.assertEqual(client.create_balance_payment.call_count, 1)

    def test_concurrent_funding_create_is_fenced_and_disabled_attempt_can_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WalletFundingIntentStore(Path(directory) / "funding.sqlite3")
            opener = _Opener({"ok": True, "payment": {"id": "tx1", "status": "pending"}})
            disabled = FazerCardsCommandClient("secret", opener=opener)
            with self.assertRaises(SupplierWriteDisabledError):
                WalletFundingService(disabled, intent_store=store).create_intent(intent_id="fund-1", method="usdt", amount_usd=50)
            started, release = threading.Event(), threading.Event()
            client = Mock()
            client.get_payment_methods.return_value = opener.methods
            def create(**kwargs):
                started.set()
                release.wait(5)
                return {"ok": True, "payment": {"id": "tx1", "status": "pending"}}
            client.create_balance_payment.side_effect = create
            results = []
            thread = threading.Thread(target=lambda: results.append(WalletFundingService(client, intent_store=store).create_intent(intent_id="fund-1", method="usdt", amount_usd=50)))
            thread.start()
            try:
                self.assertTrue(started.wait(2))
                with self.assertRaisesRegex(SupplierAccountInputError, "in progress"):
                    WalletFundingService(client, intent_store=store).create_intent(intent_id="fund-1", method="usdt", amount_usd=50)
                with self.assertRaisesRegex(SupplierAccountInputError, "previous funding creation is uncertain"):
                    WalletFundingService(client, intent_store=store).create_intent(intent_id="fund-2", method="usdt", amount_usd=50)
            finally:
                release.set()
                thread.join(5)
            self.assertEqual(len(results), 1)
            self.assertEqual(client.create_balance_payment.call_count, 1)

    def test_low_wallet_reminder_is_deduplicated_and_cooldown_aware(self):
        now = datetime(2026, 9, 22, 2, 0, tzinfo=UTC)
        snapshot = SupplierWalletSnapshot(Decimal("4.25"), now)
        policy = SupplierWalletPolicy(
            low_balance_threshold_usd=Decimal("10"),
            suggested_topup_usd=Decimal("50"),
            reminder_cooldown=timedelta(hours=6),
        )
        first = evaluate_low_wallet(snapshot, policy, now=now)
        self.assertTrue(first.should_remind)
        second = evaluate_low_wallet(
            snapshot, policy, now=now + timedelta(minutes=5), last_reminder_at=now
        )
        self.assertFalse(second.should_remind)
        third = evaluate_low_wallet(
            snapshot, policy, now=now + timedelta(hours=7), last_reminder_at=now
        )
        self.assertTrue(third.should_remind)
        recovered = evaluate_low_wallet(
            SupplierWalletSnapshot(Decimal("15"), now), policy, now=now
        )
        self.assertFalse(recovered.is_low)

    def test_wallet_snapshot_rejects_future_observation(self):
        now = datetime(2026, 9, 22, 2, 0, tzinfo=UTC)
        with self.assertRaises(SupplierAccountInputError):
            evaluate_low_wallet(
                SupplierWalletSnapshot(Decimal("1"), now + timedelta(minutes=1)),
                SupplierWalletPolicy(),
                now=now,
            )

    def test_supplier_balance_parser_requires_documented_fields_and_usd(self):
        now = datetime(2026, 9, 22, 2, 0, tzinfo=UTC)
        parsed = parse_supplier_wallet_snapshot(
            {"ok": True, "balance": "0", "currency": "USD"}, observed_at=now
        )
        self.assertEqual(parsed.balance_usd, Decimal("0"))
        for payload in (
            {"ok": True, "currency": "USD"},
            {"ok": True, "balance": 0, "currency": "USD"},
            {"ok": False, "balance": "0", "currency": "USD"},
            {"ok": True, "balance": "1", "currency": "MMK"},
        ):
            with self.subTest(payload=payload), self.assertRaises(SupplierAccountInputError):
                parse_supplier_wallet_snapshot(payload, observed_at=now)


if __name__ == "__main__":
    unittest.main()
