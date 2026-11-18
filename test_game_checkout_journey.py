from datetime import datetime, timedelta, timezone
from dataclasses import replace
import hashlib
from pathlib import Path
import json
import tempfile
import unittest
import threading
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from cryptography.fernet import Fernet

from aurix_games.catalog_projection import ApprovedOfferInput
from aurix_games.checkout_journey import CheckoutJourney, GameAccountLookup, build_env_checkout_journey
from aurix_games.customer_api import CustomerOrderService, CustomerPrincipal, CustomerWalletService
from aurix_games.customer_checkout import CustomerCheckoutService, CheckoutInputError, MappingOfferResolver
from aurix_games.customer_ledger import CustomerLedger, InsufficientFundsError
from aurix_games.eligibility import RegionRule
from aurix_games.order_repository import GameOrderRepository, OrderRepositoryError
from aurix_games.recipient_vault import RecipientVault, RecipientVaultError
from aurix_games.supplier_routes import PrivateSupplierRouteStore, SupplierRouteError
from aurix_games.payment_service import CustomerPaymentService
from aurix_games.providers.fazercards_commands import FazerCardsCommandClient, SupplierWriteDisabledError
from test_fazercards_commands import _Opener
from aurix_games.web_api import create_server
from aurix_games.customer_api import SignedCustomerTokenVerifier, issue_test_token


NOW = datetime.now(timezone.utc)


class CheckoutJourneyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        root.chmod(0o700)
        mapping = root / "mapping.json"
        mapping.write_text(json.dumps({"mappings": [{
            "family": "topups", "provider_category_id": "private-cat", "provider_offer_id": "private-sku",
            "validation_category_id": "validation-cat",
            "offer_id": "offer-1", "product_slug": "mobile-legends", "fulfillment_kind": "direct_topup",
            "denomination_label": "55 Diamonds", "required_input_labels": ["player_id"],
            "review_state": "approved", "customer_visible": True, "supplier_enabled": True,
            "resale_authorized": True, "region_rule": {"verified": True, "allow_all": True}}]}))
        mapping.chmod(0o600)
        self.offer = ApprovedOfferInput("offer-1", "mobile-legends", "Mobile Legends", "direct_topup",
            "55 Diamonds", 5000, "MMK", NOW, region_rule=RegionRule(verified=True, allow_all=True),
            required_input_labels=("player_id",))
        self.offers = {"offer-1": self.offer}
        self.checkout = CustomerCheckoutService(GameOrderRepository(root / "orders.db"),
            offer_resolver=MappingOfferResolver(self.offers),
            recipient_vault=RecipientVault(root / "vault.db", encryption_key=Fernet.generate_key()),
            fingerprint_secret=b"x" * 32, supplier_route_store=PrivateSupplierRouteStore(mapping))
        self.principal = CustomerPrincipal("customer-1", "aurix", NOW + timedelta(hours=1))
        self.payload = {"offer_id": "offer-1", "fields": {"player_id": "123456789"}}
        self.response = {"ok": True, "valid": True, "category_id": "validation-cat",
            "player_id": "123456789", "player_name": "Real player", "region": None}
        class Supplier:
            def validate_topup_recipient(_, category, fields):
                self.assertEqual(category, "validation-cat")
                self.assertEqual(fields, self.payload["fields"])
                return self.response
        self.journey = CheckoutJourney(self.checkout, Supplier())

    def test_unapproved_account_check_route_returns_safe_checkout_error(self):
        class Catalog:
            def product_detail(self, _slug, *, now):
                return {"offers": [{
                    "offer_id": "offer-1", "name": "50 Diamonds", "fulfillment_kind": "direct_topup",
                    "required_input_labels": ["player_id", "server_id"],
                }]}

        class Routes:
            def resolve(self, **_kwargs):
                raise SupplierRouteError("private route unavailable")

        lookup = GameAccountLookup(
            supplier=None, repository=None, fingerprint_secret=b"x" * 32,
            catalog_store=Catalog(), supplier_route_store=Routes(),
        )
        with self.assertRaisesRegex(CheckoutInputError, "Approved account check route is unavailable"):
            lookup.check_product(
                "mobile-legends", "customer-1", {"player_id": "123456789", "server_id": "4574"}, now=NOW,
            )

    def test_confirm_persists_one_order_and_replays_after_quote_expiry(self):
        quote = self.journey.review(self.principal, self.payload, now=NOW)["quote"]
        self.assertNotIn("private-cat", json.dumps(quote))
        self.assertNotIn("123456789", json.dumps(quote))
        request = {"quote_id": quote["quote_id"], "confirmed": True}
        first = self.journey.confirm(self.principal, request, now=NOW)
        again = self.journey.confirm(self.principal, request, now=NOW + timedelta(minutes=4))
        self.assertEqual(first["order"]["order_id"], again["order"]["order_id"])
        self.assertEqual(first["order"]["state"], "awaiting_payment")
        self.assertEqual(len(self.checkout.repository.list_for_customer("customer-1")), 1)
        self.assertNotIn(b"123456789", self.checkout.recipient_vault.path.read_bytes())

    def test_detected_region_supports_accounts_outside_myanmar_and_thailand_and_blocks_pack_exclusions(self):
        path = self.checkout.supplier_route_store.path
        mapping = json.loads(path.read_text())
        row = mapping["mappings"][0]
        row["provider_category_id"] = "mobile_legends_global"
        row["validation_region_policy"] = {"mode": "detected", "excluded_values": ["Indonesia", "ID", "Brazil", "BR"],
            "evidence": "reviewed supplier note for this exact standard pack"}
        path.write_text(json.dumps(mapping))
        for index, country in enumerate(["United Kingdom", "Thailand", "Myanmar", "Singapore"]):
            with self.subTest(country=country):
                now = NOW + timedelta(seconds=11 * index)
                self.response["region"] = country
                quote = self.journey.review(self.principal, self.payload, now=now)["quote"]
                self.assertEqual(quote["region"], country)
                result = self.journey.confirm(self.principal, {"quote_id": quote["quote_id"], "confirmed": True}, now=now)
                private = self.checkout.recipient_vault.get(order_id=result["order"]["order_id"], customer_id=self.principal.customer_id)
                self.assertEqual(private["region"]["value"], country.casefold())
        for index, country in enumerate(["Indonesia", "Brazil", "BR", "unknown", None], 4):
            with self.subTest(country=country):
                self.response["region"] = country
                with self.assertRaises(CheckoutInputError):
                    self.journey.review(self.principal, self.payload, now=NOW + timedelta(seconds=11 * index))
        row["validation_region_policy"]["excluded_values"].append("United Kingdom")
        path.write_text(json.dumps(mapping))
        self.response["region"] = "United Kingdom"
        with self.assertRaises(CheckoutInputError):
            self.journey.review(self.principal, self.payload, now=NOW + timedelta(minutes=2))

    def test_enabled_receiver_qr_must_exist_in_private_asset_directory(self):
        qr_dir = Path(self.temp.name) / "configured-qrs"
        qr_dir.mkdir(mode=0o700)
        image = b"\x89PNG\r\n\x1a\nconfigured-qr"
        (qr_dir / "kpay-r1.png").write_bytes(image)
        (qr_dir / "kpay-r1.png").chmod(0o600)
        receiver = {"id": "receiver-1", "revision": "r1", "account": "See QR",
            "account_name": "Approved shop", "qr_file": "kpay-r1.png"}
        env = {
            "AURIX_GAMES_RECIPIENT_VALIDATION_ENABLED": "1",
            "AURIX_GAMES_ACCEPT_TRANSFERS": "1",
            "AURIX_GAMES_APPROVED_RECEIVERS": json.dumps({"kpay": receiver}),
            "AURIX_GAMES_RECEIVER_QR_DIR": str(qr_dir),
            "FAZERCARDS_API_KEY": "test-key-012345678901234567890123456789",
            "AURIX_GAMES_BASE_DIR": self.temp.name,
            "AURIX_GAMES_FULFILLMENT_KEY": Fernet.generate_key().decode(),
            "AURIX_GAMES_SPEND_POLICY": str(Path(self.temp.name) / "spend-policy.json"),
        }
        policy = Path(env["AURIX_GAMES_SPEND_POLICY"])
        policy.write_text(json.dumps({"daily_limit_usd": "3", "minimum_balance_usd": "1", "offers": [{
            "category_id": "private-cat", "offer_id": "private-sku", "max_cost_usd": "1",
            "allow_unknown_stock": True, "fields_sha256": "a" * 64}]}))
        policy.chmod(0o600)
        with patch.dict("os.environ", env, clear=True):
            journey = build_env_checkout_journey(self.checkout)
        self.assertEqual(journey.read_receiver_qr("kpay-r1.png")[1], image)
        receiver_file = Path(self.temp.name) / "receivers.json"
        receiver_file.write_text(json.dumps({"kpay": receiver}))
        receiver_file.chmod(0o600)
        file_env = {**env, "AURIX_GAMES_APPROVED_RECEIVERS_FILE": str(receiver_file)}
        file_env.pop("AURIX_GAMES_APPROVED_RECEIVERS")
        with patch.dict("os.environ", file_env, clear=True):
            self.assertEqual(build_env_checkout_journey(self.checkout).payment_receivers["kpay"]["account"], "See QR")
        receiver_file.chmod(0o644)
        with patch.dict("os.environ", file_env, clear=True), self.assertRaises(CheckoutInputError):
            build_env_checkout_journey(self.checkout)

        env["AURIX_GAMES_APPROVED_RECEIVERS"] = json.dumps({"kpay": {**receiver, "qr_file": "missing.png"}})
        with patch.dict("os.environ", env, clear=True), self.assertRaises(CheckoutInputError):
            build_env_checkout_journey(self.checkout)

    def test_supplier_preflight_blocks_new_instructions_but_preserves_saved_payment_recovery(self):
        from unittest.mock import Mock
        quote = self.journey.review(self.principal, self.payload, now=NOW)["quote"]
        order = self.journey.confirm(self.principal, {"quote_id": quote["quote_id"], "confirmed": True}, now=NOW)["order"]
        guard = Mock()
        guard.check_payment.side_effect = RuntimeError("supplier temporarily unavailable")
        self.journey.payment_preflight = guard
        args = dict(rail="kpay", receiver_id="receiver-1", receiver_revision="v1", now=NOW,
            instructions={"account": "private-receiver", "account_name": "shop"})
        with self.assertRaisesRegex(CheckoutInputError, "payment instructions remain unavailable"):
            self.journey.open_payment(self.principal, quote["quote_id"], **args)
        self.assertIsNone(self.checkout.repository.get_payment_intent(order["order_id"]))
        guard.check_payment.side_effect = None
        self.journey.open_payment(self.principal, quote["quote_id"], **args)
        guard.check_payment.side_effect = RuntimeError("supplier unavailable after saved instructions")
        before = guard.check_payment.call_count
        saved = self.journey.payment(self.principal, {"order_id": order["order_id"], "rail": "kpay"}, now=NOW)
        self.assertEqual(saved["payment"]["account"], "private-receiver")
        self.assertEqual(guard.check_payment.call_count, before)

    def test_review_rejects_mismatched_or_incomplete_provider_identity(self):
        for changes in ({"valid": False}, {"category_id": "wrong"}, {"player_name": None},
                        {"player_id": "other"}, {"ok": False}, {"region": 12}):
            with self.subTest(changes=changes):
                self.response.update(changes)
                journey = CheckoutJourney(self.checkout, self.journey.supplier)
                with self.assertRaises(CheckoutInputError):
                    journey.review(self.principal, self.payload, now=NOW)
                self.response = {"ok": True, "valid": True, "category_id": "validation-cat",
                    "player_id": "123456789", "player_name": "Real player", "region": None}

    def test_stale_or_disabled_offer_never_calls_provider(self):
        for offer in (replace(self.offer, observed_at=NOW - timedelta(hours=1)),
                      replace(self.offer, supplier_enabled=False),
                      replace(self.offer, resale_authorized=False)):
            self.offers["offer-1"] = offer
            with patch.object(self.journey.supplier, "validate_topup_recipient") as lookup:
                with self.assertRaises(CheckoutInputError):
                    self.journey.review(self.principal, self.payload, now=NOW)
                lookup.assert_not_called()

    def test_validation_field_alias_does_not_change_purchase_fields(self):
        path = Path(self.temp.name) / "mapping.json"
        config = json.loads(path.read_text())
        config["mappings"][0]["required_input_labels"] = ["player_id", "server_id"]
        config["mappings"][0]["validation_field_map"] = {"player_id": "player_id", "zone_id": "server_id"}
        path.write_text(json.dumps(config))
        self.offers["offer-1"] = replace(self.offer, required_input_labels=("player_id", "server_id"))
        payload = {"offer_id": "offer-1", "fields": {"player_id": "123456789", "server_id": "1234"}}
        with patch.object(self.journey.supplier, "validate_topup_recipient", return_value=self.response) as lookup:
            quote = self.journey.review(self.principal, payload, now=NOW)["quote"]
            lookup.assert_called_once_with("validation-cat", {"player_id": "123456789", "zone_id": "1234"})
        result = self.journey.confirm(self.principal, {"quote_id": quote["quote_id"], "confirmed": True}, now=NOW)
        private = self.checkout.recipient_vault.get(order_id=result["order"]["order_id"], customer_id="customer-1")
        self.assertEqual(private["fields"], payload["fields"])

    def test_missing_reviewed_validation_category_does_not_call_supplier(self):
        path = Path(self.temp.name) / "mapping.json"
        config = json.loads(path.read_text())
        del config["mappings"][0]["validation_category_id"]
        path.write_text(json.dumps(config))
        with patch.object(self.journey.supplier, "validate_topup_recipient") as lookup:
            with self.assertRaises(CheckoutInputError):
                self.journey.review(self.principal, self.payload, now=NOW)
            lookup.assert_not_called()

    def test_invalid_validation_field_maps_fail_before_provider_call(self):
        path = Path(self.temp.name) / "mapping.json"
        original = json.loads(path.read_text())
        for field_map in ({"player_id": "unapproved"}, {"bad key": "player_id"},
                          {"player_id": "player_id", "zone_id": "player_id"}, []):
            config = json.loads(json.dumps(original))
            config["mappings"][0]["validation_field_map"] = field_map
            path.write_text(json.dumps(config))
            with patch.object(self.journey.supplier, "validate_topup_recipient") as lookup:
                with self.assertRaises(CheckoutInputError):
                    self.journey.review(self.principal, self.payload, now=NOW)
                lookup.assert_not_called()

    def test_echoed_player_identity_uses_validation_key_after_aliasing(self):
        path = Path(self.temp.name) / "mapping.json"
        config = json.loads(path.read_text())
        config["mappings"][0]["required_input_labels"] = ["purchase_account"]
        config["mappings"][0]["validation_field_map"] = {"player_id": "purchase_account"}
        path.write_text(json.dumps(config))
        self.offers["offer-1"] = replace(self.offer, required_input_labels=("purchase_account",))
        with patch.object(self.journey.supplier, "validate_topup_recipient", return_value=self.response):
            quote = self.journey.review(self.principal,
                {"offer_id": "offer-1", "fields": {"purchase_account": "123456789"}}, now=NOW)
        self.assertEqual(quote["quote"]["player_name"], "Real player")

    def test_quote_requires_confirmation_owner_freshness_and_unchanged_price(self):
        quote = self.journey.review(self.principal, self.payload, now=NOW)["quote"]
        request = {"quote_id": quote["quote_id"], "confirmed": True}
        for payload, principal, moment in (
            ({**request, "confirmed": False}, self.principal, NOW),
            (request, replace(self.principal, customer_id="other"), NOW),
            (request, self.principal, NOW + timedelta(seconds=111)),
        ):
            with self.assertRaises(CheckoutInputError):
                self.journey.confirm(principal, payload, now=moment)
        self.offers["offer-1"] = replace(self.offer, retail_amount_minor=6000)
        with self.assertRaisesRegex(CheckoutInputError, "offer changed"):
            self.journey.confirm(self.principal, request, now=NOW)
        self.assertEqual(self.checkout.repository.list_for_customer("customer-1"), ())

    def test_extra_fields_and_repeated_lookups_are_rejected(self):
        with self.assertRaises(CheckoutInputError):
            self.journey.review(self.principal, {**self.payload, "validation": True}, now=NOW)
        self.journey.review(self.principal, self.payload, now=NOW)
        with self.assertRaisesRegex(CheckoutInputError, "please wait"):
            self.journey.review(self.principal, self.payload, now=NOW)

    def test_lookup_cooldown_is_shared_across_repository_instances_and_workers(self):
        second_repository = GameOrderRepository(self.checkout.repository.path)
        second_checkout = CustomerCheckoutService(
            second_repository,
            offer_resolver=MappingOfferResolver(self.offers),
            recipient_vault=self.checkout.recipient_vault,
            fingerprint_secret=b"x" * 32,
            supplier_route_store=PrivateSupplierRouteStore(Path(self.temp.name) / "mapping.json"),
        )
        second_journey = CheckoutJourney(second_checkout, self.journey.supplier)

        self.journey.review(self.principal, self.payload, now=NOW)
        with patch.object(second_journey.supplier, "validate_topup_recipient",
                          wraps=second_journey.supplier.validate_topup_recipient) as lookup:
            with self.assertRaisesRegex(CheckoutInputError, "please wait"):
                second_journey.review(self.principal, self.payload, now=NOW)
            lookup.assert_not_called()
        # The shared claim expires after the cooldown even when a new worker
        # has a new repository and journey object.
        second_journey.review(self.principal, self.payload, now=NOW + timedelta(seconds=11))

        # Simultaneous requests from separate workers cannot both reserve a
        # provider lookup for the same customer.
        parallel_time = NOW + timedelta(seconds=30)
        barrier = threading.Barrier(3)
        outcomes = []

        def check(journey):
            barrier.wait()
            try:
                journey.review(self.principal, self.payload, now=parallel_time)
                outcomes.append("accepted")
            except CheckoutInputError as exc:
                outcomes.append("throttled" if "please wait" in str(exc) else "rejected")

        workers = [threading.Thread(target=check, args=(journey,)) for journey in (self.journey, second_journey)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=5)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertCountEqual(outcomes, ["accepted", "throttled"])

    def test_confirmation_recovers_order_when_recipient_storage_failed(self):
        quote = self.journey.review(self.principal, self.payload, now=NOW)["quote"]
        request = {"quote_id": quote["quote_id"], "confirmed": True}
        with patch.object(self.checkout.recipient_vault, "put", side_effect=RecipientVaultError("disk failure")):
            with self.assertRaises(CheckoutInputError):
                self.journey.confirm(self.principal, request, now=NOW)
        orders = self.checkout.repository.list_for_customer("customer-1")
        self.assertEqual(len(orders), 1)
        with self.assertRaises(RecipientVaultError):
            self.checkout.recipient_vault.get(order_id=orders[0].order_id, customer_id="customer-1")
        result = self.journey.confirm(self.principal, request, now=NOW + timedelta(minutes=4))
        self.assertEqual(result["order"]["order_id"], orders[0].order_id)
        saved = self.checkout.recipient_vault.get(order_id=orders[0].order_id, customer_id="customer-1")
        self.assertEqual(saved["fields"], self.payload["fields"])

    def test_lookup_permission_does_not_enable_purchase_or_treasury(self):
        opener = _Opener({"ok": True})
        client = FazerCardsCommandClient("secret", opener=opener, allow_recipient_validation=True)
        client.validate_topup_recipient("cat", {"player_id": "123"})
        with self.assertRaises(SupplierWriteDisabledError):
            client.order_topup(category_id="cat", offer_id="sku", fields={"player_id": "123"}, idempotency_key="order-1")
        with self.assertRaises(SupplierWriteDisabledError):
            client.create_balance_payment(method="usdt", amount_usd=50)
        self.assertEqual(len(opener.requests), 1)

    def test_lookup_transport_errors_preserve_http_meaning_without_creating_orders(self):
        from aurix_games.providers.fazercards_commands import SupplierCommandError
        from unittest.mock import Mock
        secret = "lookup-http-test-secret-" + "x"*32
        journey = Mock()
        journey.receiver_qr_dir = None
        journey.payment_receivers = {}
        server = create_server("127.0.0.1", 0, checkout_journey=journey,
            customer_auth=SignedCustomerTokenVerifier(secret, issuer="aurix"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        token = issue_test_token("customer-1", secret, expires_at=NOW+timedelta(hours=1), issuer="aurix")
        for upstream, expected, phrase in ((502, 503, "temporarily unavailable"),
                                           (422, 400, "could not confirm"),
                                           (429, 429, "too many account checks")):
            with self.subTest(upstream=upstream):
                journey.review.side_effect = SupplierCommandError("sanitized", http_status=upstream)
                request = Request(f"http://127.0.0.1:{server.server_port}/api/v1/checkout/review",
                    data=json.dumps(self.payload).encode(), method="POST",
                    headers={"Authorization": "Bearer "+token, "Content-Type": "application/json"})
                with self.assertRaises(HTTPError) as failure:
                    urlopen(request)
                self.assertEqual(failure.exception.code, expected)
                self.assertIn(phrase, json.loads(failure.exception.read())["error"])
        journey.confirm.assert_not_called()
        journey.payment.assert_not_called()
        self.assertEqual(len(self.checkout.repository.list_for_customer("customer-1")), 0)

    def test_http_requires_auth_and_cookie_origin_and_confirms_server_quote(self):
        secret = "journey-http-secret-012345678901234567890"
        server = create_server("127.0.0.1", 0, checkout_journey=self.journey,
            customer_auth=SignedCustomerTokenVerifier(secret, issuer="aurix"))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        def post(path, data, headers=None):
            req = Request(base + path, data=json.dumps(data).encode(),
                headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
            with urlopen(req) as response:
                return json.load(response)
        with self.assertRaises(HTTPError) as failure:
            post("/api/v1/checkout/review", self.payload, {"Origin": base})
        self.assertEqual(failure.exception.code, 401)
        with self.assertRaises(HTTPError) as failure:
            post("/api/v1/checkout/review", self.payload, {"Origin": "https://other.example"})
        self.assertEqual(failure.exception.code, 403)
        token = issue_test_token("customer-1", secret, expires_at=NOW + timedelta(hours=1), issuer="aurix")
        headers = {"Authorization": "Bearer " + token}
        quote = post("/api/v1/checkout/review", self.payload, headers)["quote"]
        order = post("/api/v1/checkout/confirm", {"quote_id": quote["quote_id"], "confirmed": True}, headers)
        self.assertEqual(order["order"]["state"], "awaiting_payment")
        qr_dir = Path(self.temp.name) / "payment-qrs"
        qr_dir.mkdir(mode=0o700)
        qr_bytes = b"\x89PNG\r\n\x1a\ncheckout-qr"
        qr_path = qr_dir / "kpay-r1.png"
        qr_path.write_bytes(qr_bytes)
        qr_path.chmod(0o600)
        self.journey.receiver_qr_dir = qr_dir
        self.journey.payment_receivers = {"kpay": {"id": "receiver-1", "revision": "v1",
            "account": "091234", "account_name": "Approved shop", "qr_file": "kpay-r1.png"}}
        payment_request = {"quote_id": quote["quote_id"], "rail": "kpay"}
        payment = post("/api/v1/checkout/payment", payment_request, headers)["payment"]
        self.assertEqual(payment["account"], "091234")
        self.assertEqual(payment["qr_asset"], "kpay-r1.png")
        self.assertEqual(payment["qr_sha256"], hashlib.sha256(qr_bytes).hexdigest())
        self.assertNotIn("receiver_revision", payment)
        recovered = post("/api/v1/checkout/payment", {"order_id": order["order"]["order_id"], "rail": "kpay"}, headers)["payment"]
        self.assertEqual(recovered["account"], payment["account"])
        self.assertEqual(recovered["qr_asset"], "kpay-r1.png")
        self.assertEqual(recovered["qr_sha256"], payment["qr_sha256"])
        self.assertEqual(recovered["expires_at"], payment["expires_at"])
        other_token = issue_test_token("another-customer", secret, expires_at=NOW + timedelta(hours=1), issuer="aurix")
        with self.assertRaises(HTTPError) as failure:
            post("/api/v1/checkout/payment", {"order_id": order["order"]["order_id"], "rail": "kpay"},
                 {"Authorization": "Bearer " + other_token})
        self.assertEqual(failure.exception.code, 400)
        with self.assertRaises(HTTPError) as failure:
            post("/api/v1/checkout/payment", {**payment_request, "receiver_id": "forged"}, headers)
        self.assertEqual(failure.exception.code, 400)

    def test_wallet_payment_is_owner_bound_idempotent_and_visible_in_order_history(self):
        ledger = CustomerLedger(Path(self.temp.name) / "wallet-ledger.sqlite3")
        ledger.credit(customer_id="customer-1", amount_minor=5000, currency="MMK",
            event_key="verified-wallet-credit-1", actor="staff:reviewer", reason="verified refund",
            occurred_at=NOW)
        journey = CheckoutJourney(self.checkout, self.journey.supplier,
            wallet_payment_service=CustomerPaymentService(self.checkout.repository, ledger))
        secret = "wallet-http-secret-012345678901234567890"
        verifier = SignedCustomerTokenVerifier(secret, issuer="aurix")
        orders = CustomerOrderService(self.checkout.repository, ledger)
        wallet = CustomerWalletService(ledger)
        with patch.dict("os.environ", {"AURIX_GAMES_BASE_DIR": self.temp.name}):
            server = create_server("127.0.0.1", 0, checkout_journey=journey,
                customer_auth=verifier, customer_order_service=orders, customer_wallet_service=wallet)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            self.addCleanup(server.server_close)
            self.addCleanup(worker.join, 2)
            self.addCleanup(server.shutdown)
            base = f"http://127.0.0.1:{server.server_port}"
            token = issue_test_token("customer-1", secret, expires_at=NOW + timedelta(hours=1), issuer="aurix")
            headers = {"Authorization": "Bearer " + token}

            def post(path, body, request_headers=None):
                request = Request(base + path, data=json.dumps(body).encode(), method="POST",
                    headers={"Content-Type": "application/json", **(request_headers or {})})
                with urlopen(request) as response:
                    return json.load(response)

            quote = post("/api/v1/checkout/review", self.payload, headers)["quote"]
            order = post("/api/v1/checkout/confirm", {"quote_id": quote["quote_id"], "confirmed": True}, headers)["order"]
            with self.assertRaises(HTTPError) as failure:
                post(f"/api/v1/orders/{order['order_id']}/wallet-payment", {"confirmed": True}, {"Origin": base})
            self.assertEqual(failure.exception.code, 401)
            failure.exception.close()
            with self.assertRaises(HTTPError) as failure:
                post(f"/api/v1/orders/{order['order_id']}/wallet-payment", {"confirmed": False}, headers)
            self.assertEqual(failure.exception.code, 400)
            failure.exception.close()

            paid = post(f"/api/v1/orders/{order['order_id']}/wallet-payment", {"confirmed": True}, headers)
            replay = post(f"/api/v1/orders/{order['order_id']}/wallet-payment", {"confirmed": True}, headers)
            self.assertEqual(paid["order"]["state"], "paid_reserved")
            self.assertEqual(paid["payment"]["rail"], "wallet")
            self.assertEqual(paid["wallet"]["available_minor"], 0)
            self.assertTrue(replay["replayed"])
            self.assertEqual(ledger.balance("customer-1").reserved_minor, 5000)
            self.assertEqual(orders.get_order(self.principal, order["order_id"])["payment"]["rail"], "wallet")

    def test_insufficient_wallet_funds_release_method_claim_for_transfer_fallback(self):
        ledger = CustomerLedger(Path(self.temp.name) / "short-wallet-ledger.sqlite3")
        ledger.credit(customer_id="customer-1", amount_minor=1000, currency="MMK",
            event_key="verified-wallet-credit-short", actor="staff:reviewer", reason="verified refund",
            occurred_at=NOW)
        journey = CheckoutJourney(self.checkout, self.journey.supplier,
            payment_receivers={"kpay": {"id": "receiver-1", "revision": "r1",
                "account": "091234", "account_name": "Approved shop"}},
            wallet_payment_service=CustomerPaymentService(self.checkout.repository, ledger))
        with patch.dict("os.environ", {"AURIX_GAMES_BASE_DIR": self.temp.name}):
            quote = journey.review(self.principal, self.payload, now=NOW)["quote"]
            order = journey.confirm(self.principal, {"quote_id": quote["quote_id"], "confirmed": True}, now=NOW)["order"]
            self.checkout.repository.claim_wallet_payment(order_id=order["order_id"],
                customer_id=self.principal.customer_id, event_key=f"wallet:{order['order_id']}", occurred_at=NOW)
            with self.assertRaises(OrderRepositoryError):
                self.checkout.repository.freeze_payment_intent(order_id=order["order_id"],
                    customer_id=self.principal.customer_id, rail="kpay", receiver_id="receiver-1",
                    receiver_revision="r1", opened_at=NOW, expires_at=NOW + timedelta(minutes=10),
                    instructions={"account": "091234", "account_name": "Approved shop"})
            with self.assertRaises(InsufficientFundsError):
                journey.pay_with_wallet(self.principal, order["order_id"], now=NOW)
            self.assertEqual(ledger.balance("customer-1").available_minor, 1000)
            payment = journey.payment(self.principal,
                {"order_id": order["order_id"], "rail": "kpay"}, now=NOW)["payment"]
        self.assertEqual(payment["account"], "091234")
        self.assertEqual(payment["order_state"], "awaiting_payment")

    def test_wallet_payment_and_external_instructions_are_mutually_exclusive(self):
        ledger = CustomerLedger(Path(self.temp.name) / "mixed-wallet-ledger.sqlite3")
        ledger.credit(customer_id="customer-1", amount_minor=5000, currency="MMK",
            event_key="verified-wallet-credit-mixed", actor="staff:reviewer", reason="verified refund",
            occurred_at=NOW)
        journey = CheckoutJourney(self.checkout, self.journey.supplier,
            wallet_payment_service=CustomerPaymentService(self.checkout.repository, ledger))
        with patch.dict("os.environ", {"AURIX_GAMES_BASE_DIR": self.temp.name}):
            quote = journey.review(self.principal, self.payload, now=NOW)["quote"]
            order = journey.confirm(self.principal, {"quote_id": quote["quote_id"], "confirmed": True}, now=NOW)["order"]
            journey.open_payment(self.principal, quote["quote_id"], rail="kpay", receiver_id="receiver-1",
                receiver_revision="r1", instructions={"account": "091234", "account_name": "Approved shop"}, now=NOW)
            with self.assertRaises(CheckoutInputError):
                journey.pay_with_wallet(self.principal, order["order_id"], now=NOW)
        self.assertEqual(ledger.balance("customer-1").available_minor, 5000)
