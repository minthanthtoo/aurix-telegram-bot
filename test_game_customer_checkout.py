from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from test_public_catalog import approve_catalog_fixture
from unittest import mock
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet

from aurix_games.catalog_projection import ApprovedOfferInput
from aurix_games.customer_api import (
    CustomerPrincipal,
    SignedCustomerTokenVerifier,
    build_env_customer_api,
    issue_test_token,
)
from aurix_games.customer_checkout import (
    CheckoutInputError,
    CustomerCheckoutService,
    MappingOfferResolver,
    PublicCatalogOfferResolver,
    build_env_customer_checkout,
)
from aurix_games.eligibility import RegionRule, RegionDimension, RegionValue
from aurix_games.order_repository import GameOrderRepository
from aurix_games.order_state import OrderState
from aurix_games.public_catalog import PublicCatalogStore
from aurix_games.recipient_vault import RecipientVault
from aurix_games.supplier_routes import PrivateSupplierRouteStore
from aurix_games.web_api import make_handler


UTC = timezone.utc
NOW = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)


class CustomerCheckoutTests(unittest.TestCase):
    def test_checkout_resolves_private_route_and_returns_typed_payload_only_after_payment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route_path = root / "public-catalog-config.json"
            route_path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "mappings": [{
                        "family": "topups",
                        "provider_category_id": "mlbb-api-category",
                        "provider_offer_id": "diamond-sku-55",
                        "offer_id": "aurix-mlbb-55",
                        "product_slug": "mobile-legends",
                        "fulfillment_kind": "direct_topup",
                        "denomination_label": "55 Diamonds",
                        "required_input_labels": ["player_id"],
                        "review_state": "approved",
                        "customer_visible": True,
                        "supplier_enabled": True,
                        "resale_authorized": True,
                        "region_rule": {"verified": True, "allow_all": True},
                    }],
                }),
                encoding="utf-8",
            )
            route_path.chmod(0o600)
            offer = ApprovedOfferInput(
                offer_id="aurix-mlbb-55",
                product_slug="mobile-legends",
                product_name="Mobile Legends",
                fulfillment_kind="direct_topup",
                denomination_label="55 Diamonds",
                retail_amount_minor=5500,
                currency="MMK",
                observed_at=NOW,
                region_rule=RegionRule(verified=True, allow_all=True),
                required_input_labels=("player_id",),
            )
            repository = GameOrderRepository(root / "orders.sqlite3")
            service = CustomerCheckoutService(
                repository,
                offer_resolver=MappingOfferResolver({offer.offer_id: offer}),
                recipient_vault=RecipientVault(root / "recipient.sqlite3", encryption_key=Fernet.generate_key()),
                fingerprint_secret=b"r" * 32,
                supplier_route_store=PrivateSupplierRouteStore(route_path),
            )
            principal = CustomerPrincipal("customer-route", "aurix-identity", NOW.replace(year=2027))
            payload = {"offer_id": offer.offer_id, "fields": {"player_id": "secret-123"}}
            validation = {"valid": True, "checked_at": NOW.isoformat(),
                          "binding": service.recipient_validation_binding(principal, payload, now=NOW)}
            result = service.prepare(
                principal,
                {"offer_id": offer.offer_id, "fields": {"player_id": "secret-123"}},
                idempotency_key="route-1",
                validation=validation,
                now=NOW,
            )

            with self.assertRaises(CheckoutInputError):
                service.fulfillment_payload(customer_id=principal.customer_id, order_id=result.order.order_id)
            public_result = result.as_dict()
            self.assertNotIn("mlbb-api-category", json.dumps(public_result))
            repository.transition(
                result.order.order_id,
                OrderState.PAID_RESERVED,
                at=NOW,
                reason="test payment verified",
            )
            unavailable = PublicCatalogOfferResolver(PublicCatalogStore(root / 'missing-catalog.json'))
            with mock.patch.object(service, 'offer_resolver', unavailable):
                worker_payload = service.fulfillment_payload(
                    customer_id=principal.customer_id,
                    order_id=result.order.order_id,
                )
            saved_mapping = json.loads(route_path.read_text(encoding="utf-8"))
            saved_mapping["mappings"][0]["rights_evidence"] = "updated-review-reference"
            route_path.write_text(json.dumps(saved_mapping), encoding="utf-8")
            route_path.chmod(0o600)
            with self.assertRaises(CheckoutInputError):
                service.prepare(principal, payload, idempotency_key="changed-route",
                                validation=validation, now=NOW)
            self.assertEqual(len(repository.list_for_customer(principal.customer_id)), 1)
            with self.assertRaisesRegex(CheckoutInputError, "route changed"):
                service.fulfillment_payload(
                    customer_id=principal.customer_id,
                    order_id=result.order.order_id,
                )

        self.assertEqual(
            worker_payload,
            {
                "category_id": "mlbb-api-category",
                "offer_id": "diamond-sku-55",
                "fields": {"player_id": "secret-123"},
            },
        )

    def test_env_checkout_builder_http_flow_uses_approved_catalog_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(UTC)
            public_path = root / "public.json"
            public_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "approved_projection",
                        "generated_at": now.isoformat(),
                        "source_observed_at": now.isoformat(),
                        "valid_until": (now + timedelta(minutes=15)).isoformat(),
                        "offers": [
                            {
                                "offer_id": "env-global-1",
                                "product_slug": "mobile-legends",
                                "product_name": "Mobile Legends",
                                "fulfillment_kind": "direct_topup",
                                "denomination_label": "55 Diamonds",
                                "retail_amount_minor": 5500,
                                "currency": "MMK",
                                "region": {"type": "global", "label": "Global"},
                                "required_input_labels": ["Player ID"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            mapping_config_path = root / "public-catalog-config.json"
            mapping_config_path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "mappings": [{
                        "family": "topups",
                        "provider_category_id": "mlbb",
                        "provider_offer_id": "supplier-55",
                        "offer_id": "env-global-1",
                        "product_slug": "mobile-legends",
                        "fulfillment_kind": "direct_topup",
                        "denomination_label": "55 Diamonds",
                        "required_input_labels": ["Player ID"],
                        "review_state": "approved",
                        "customer_visible": True,
                        "supplier_enabled": True,
                        "resale_authorized": True,
                        "region_rule": {"verified": True, "allow_all": True},
                    }],
                }),
                encoding="utf-8",
            )
            mapping_config_path.chmod(0o600)
            approve_catalog_fixture(public_path)
            customer_secret = "c" * 32
            env = {
                "AURIX_GAMES_CUSTOMER_CHECKOUT_ENABLED": "1",
                "AURIX_GAMES_CUSTOMER_AUTH_SECRET": customer_secret,
                "AURIX_GAMES_BASE_DIR": str(root),
                "AURIX_GAMES_PUBLIC_CATALOG": str(public_path),
                "AURIX_GAMES_ORDER_DATABASE": str(root / "orders.sqlite3"),
                "AURIX_GAMES_RECIPIENT_VAULT_KEY": Fernet.generate_key().decode("ascii"),
                "AURIX_GAMES_CHECKOUT_FINGERPRINT_SECRET": "f" * 32,
            }
            with mock.patch.dict(os.environ, env, clear=False):
                customer_api = build_env_customer_api(root=root)
                checkout = build_env_customer_checkout(root=root, customer_api=customer_api)
                server = ThreadingHTTPServer(
                    ("127.0.0.1", 0),
                    make_handler(
                        customer_auth=customer_api.verifier,
                        customer_order_service=customer_api.service,
                        customer_wallet_service=customer_api.wallet_service,
                        customer_checkout_service=checkout,
                    ),
                )
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    token, _ = customer_api.identity_service._issue(
                        "customer-env", datetime.now(UTC)
                    )
                    request = Request(
                        f"http://127.0.0.1:{server.server_port}/api/v1/orders",
                        data=json.dumps(
                            {
                                "offer_id": "env-global-1",
                                "fields": {"Player ID": "player-42"},
                            }
                        ).encode(),
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Idempotency-Key": "env-order-1",
                            "Content-Type": "application/json",
                        },
                        method="POST",
                    )
                    with self.assertRaises(HTTPError) as error:
                        urlopen(request)
                    self.assertEqual(error.exception.code, 400)
                    error.exception.close()
                    principal = customer_api.verifier.verify(f"Bearer {token}")
                    self.assertEqual(customer_api.service.list_orders(principal), [])
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

    def test_public_projection_resolver_requires_explicit_regional_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "public.json"
            now = datetime.now(UTC)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "approved_projection",
                        "generated_at": now.isoformat(),
                        "source_observed_at": now.isoformat(),
                        "valid_until": (now + timedelta(minutes=15)).isoformat(),
                        "offers": [
                            {
                                "offer_id": "global-1",
                                "product_slug": "game",
                                "product_name": "Game",
                                "fulfillment_kind": "direct_topup",
                                "denomination_label": "100 coins",
                                "retail_amount_minor": 1000,
                                "currency": "MMK",
                                "region": {"type": "global", "label": "Global"},
                                "required_input_labels": ["Player ID"],
                            },
                            {
                                "offer_id": "sea-1",
                                "product_slug": "game",
                                "product_name": "Game",
                                "fulfillment_kind": "direct_topup",
                                "denomination_label": "100 coins SEA",
                                "retail_amount_minor": 1100,
                                "currency": "MMK",
                                "region": {"type": "game_server", "label": "SEA"},
                                "required_input_labels": ["Player ID"],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = PublicCatalogStore(path, now=now)
            resolver = PublicCatalogOfferResolver(store)
            global_offer = resolver.resolve("global-1")
            self.assertIsNotNone(global_offer)
            self.assertTrue(global_offer.region_rule.allow_all)
            self.assertEqual(global_offer.fulfillment_kind, "direct_topup")
            self.assertIsNone(resolver.resolve("sea-1"))

            mapped = PublicCatalogOfferResolver(
                store,
                regional_values={"sea-1": ["SEA-1"]},
            ).resolve("sea-1")
            self.assertIsNotNone(mapped)
            self.assertEqual(mapped.region_dimension, RegionDimension.GAME_SERVER)
            self.assertEqual(mapped.region_rule.allowed_values, frozenset({"sea-1"}))

    def test_env_checkout_builder_is_opt_in_and_uses_public_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {
                "AURIX_GAMES_CUSTOMER_CHECKOUT_ENABLED": "1",
                "AURIX_GAMES_CUSTOMER_AUTH_SECRET": "a" * 32,
                "AURIX_GAMES_BASE_DIR": str(root),
                "AURIX_GAMES_PUBLIC_CATALOG": str(root / "public.json"),
                "AURIX_GAMES_RECIPIENT_VAULT_KEY": Fernet.generate_key().decode("ascii"),
                "AURIX_GAMES_CHECKOUT_FINGERPRINT_SECRET": "b" * 32,
            }
            with mock.patch.dict(os.environ, env, clear=False):
                service = build_env_customer_checkout(root=root)
                self.assertIsInstance(service, CustomerCheckoutService)
                with mock.patch.dict(os.environ, {"AURIX_GAMES_CUSTOMER_CHECKOUT_ENABLED": "0"}):
                    self.assertIsNone(build_env_customer_checkout(root=root))
                    self.assertIsInstance(build_env_customer_checkout(root=root, for_fulfillment=True), CustomerCheckoutService)
            with mock.patch.dict(
                os.environ,
                {"AURIX_GAMES_CUSTOMER_CHECKOUT_ENABLED": "0"},
                clear=False,
            ):
                self.assertIsNone(build_env_customer_checkout(root=root))

    def test_lookup_evidence_is_bound_and_failures_write_no_order_or_vault(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            offer = ApprovedOfferInput(
                offer_id="verified-offer", product_slug="game", product_name="Game",
                fulfillment_kind="direct_topup", denomination_label="Pack",
                retail_amount_minor=100, currency="MMK", observed_at=NOW,
                region_rule=RegionRule(verified=True, allow_all=True),
                required_input_labels=("player_id",),
            )
            offers = {offer.offer_id: offer}
            repository = GameOrderRepository(root / "orders.sqlite3")
            vault_path = root / "vault.sqlite3"
            service = CustomerCheckoutService(
                repository, offer_resolver=MappingOfferResolver(offers),
                recipient_vault=RecipientVault(vault_path, encryption_key=Fernet.generate_key()),
                fingerprint_secret=b"z" * 32,
            )
            owner = CustomerPrincipal("customer-1", "aurix-identity", NOW + timedelta(hours=1))
            payload = {"offer_id": offer.offer_id, "fields": {"player_id": "123"}}
            proof = {"valid": True, "checked_at": NOW.isoformat(),
                     "binding": service.recipient_validation_binding(owner, payload, now=NOW)}
            other_offer = replace(offer, offer_id="another-offer")
            offers[other_offer.offer_id] = other_offer
            cases = [
                (owner, payload, None),
                (owner, payload, {**proof, "valid": False}),
                (owner, payload, {**proof, "binding": "မမှန်"}),
                (owner, payload, {**proof, "checked_at": NOW.replace(tzinfo=None).isoformat()}),
                (owner, payload, {**proof, "checked_at": (NOW-timedelta(minutes=3)).isoformat()}),
                (owner, payload, {**proof, "checked_at": (NOW+timedelta(seconds=1)).isoformat()}),
                (replace(owner, customer_id="customer-2"), payload, proof),
                (owner, {**payload, "fields": {"player_id": "456"}}, proof),
                (owner, {**payload, "offer_id": other_offer.offer_id}, proof),
                (owner, {**payload, "region": {"dimension": "game_server", "value": "sea"}}, proof),
            ]
            for index, (principal, request, validation) in enumerate(cases):
                with self.subTest(case=index):
                    with self.assertRaises(CheckoutInputError):
                        service.prepare(principal, request, idempotency_key=str(index),
                                        validation=validation, now=NOW)
                    self.assertEqual(repository.list_for_customer(principal.customer_id), ())
                    self.assertFalse(vault_path.exists())
            for changes in (
                {"retail_amount_minor": 101}, {"currency": "USD"},
                {"product_slug": "other-game"}, {"required_input_labels": ("player_id", "zone_id")},
                {"region_rule": RegionRule(verified=True, dimension=RegionDimension.GAME_SERVER,
                                            allowed_values=frozenset({"sea"}))},
            ):
                offers[offer.offer_id] = replace(offer, **changes)
                with self.subTest(policy=changes):
                    with self.assertRaises(CheckoutInputError):
                        service.prepare(owner, payload, idempotency_key="policy-change",
                                        validation=proof, now=NOW)
                    self.assertEqual(repository.list_for_customer(owner.customer_id), ())
            offers[offer.offer_id] = offer
            result = service.prepare(owner, payload, idempotency_key="valid", validation=proof, now=NOW)
            self.assertEqual(result.order.state, OrderState.AWAITING_PAYMENT)

    def test_expired_principal_is_rejected_before_order_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            offer = ApprovedOfferInput(
                offer_id="offer-expired",
                product_slug="game",
                product_name="Game",
                fulfillment_kind="direct_topup",
                denomination_label="Pack",
                retail_amount_minor=100,
                currency="MMK",
                observed_at=NOW,
                region_rule=RegionRule(verified=True, allow_all=True),
                required_input_labels=("player_id",),
            )
            repository = GameOrderRepository(root / "orders.sqlite3")
            service = CustomerCheckoutService(
                repository,
                offer_resolver=MappingOfferResolver({offer.offer_id: offer}),
                recipient_vault=RecipientVault(root / "recipient.sqlite3", encryption_key=Fernet.generate_key()),
                fingerprint_secret=b"e" * 32,
            )
            expired = CustomerPrincipal("customer-1", "aurix-identity", NOW)
            with self.assertRaises(CheckoutInputError):
                service.prepare(
                    expired,
                    {"offer_id": offer.offer_id, "fields": {"player_id": "123"}},
                    idempotency_key="expired-1",
                    now=NOW,
                )
            self.assertEqual(repository.list_for_customer("customer-1"), ())

    def test_prepare_is_owner_scoped_idempotent_and_keeps_raw_fields_encrypted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            offer = ApprovedOfferInput(
                offer_id="aurix-mlbb-55",
                product_slug="mobile-legends",
                product_name="Mobile Legends",
                fulfillment_kind="direct_topup",
                denomination_label="55 Diamonds",
                retail_amount_minor=5500,
                currency="MMK",
                observed_at=NOW,
                region_rule=RegionRule(verified=True, allow_all=True),
                region_label="Global",
                required_input_labels=("player_id",),
            )
            repository = GameOrderRepository(root / "orders.sqlite3")
            vault = RecipientVault(root / "recipient.sqlite3", encryption_key=Fernet.generate_key())
            service = CustomerCheckoutService(
                repository,
                offer_resolver=MappingOfferResolver({offer.offer_id: offer}),
                recipient_vault=vault,
                fingerprint_secret=b"f" * 32,
            )
            principal = CustomerPrincipal("customer-1", "aurix-identity", NOW.replace(year=2027))
            payload = {"offer_id": offer.offer_id, "fields": {"player_id": "123456"}}
            validation = {"valid": True, "checked_at": NOW.isoformat(),
                          "binding": service.recipient_validation_binding(principal, payload, now=NOW)}
            first = service.prepare(principal, payload, idempotency_key="client-1", now=NOW, validation=validation)
            retry = service.prepare(principal, payload, idempotency_key="client-1", now=NOW, validation=validation)
            self.assertEqual(first.order.order_id, retry.order.order_id)
            self.assertEqual(first.order.state, OrderState.AWAITING_PAYMENT)
            self.assertNotIn("123456", first.order.snapshot.recipient_fingerprint)
            stored = vault.get(order_id=first.order.order_id, customer_id="customer-1")
            self.assertEqual(stored["fields"], {"player_id": "123456"})
            self.assertNotIn("123456", (root / "recipient.sqlite3").read_bytes().decode("latin1", "ignore"))

            with self.assertRaises(CheckoutInputError):
                service.prepare(
                    principal,
                    {**payload, "validation": {"valid": True, "binding": "x"}},
                    idempotency_key="client-2",
                    now=NOW,
                )

            repository.transition(
                first.order.order_id,
                OrderState.PAID_RESERVED,
                at=NOW,
                reason="test payment reserved",
            )
            fulfillment = service.fulfillment_payload(
                customer_id="customer-1", order_id=first.order.order_id
            )
            self.assertEqual(fulfillment["fields"], {"player_id": "123456"})

    def test_region_is_checked_before_order_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            offer = ApprovedOfferInput(
                offer_id="offer-region",
                product_slug="game",
                product_name="Game",
                fulfillment_kind="direct_topup",
                denomination_label="Pack",
                retail_amount_minor=100,
                currency="MMK",
                observed_at=NOW,
                region_rule=RegionRule(
                    verified=True,
                    dimension=RegionDimension.GAME_SERVER,
                    allowed_values=frozenset({"global"}),
                ),
                region_label="Global",
                region_dimension=RegionDimension.GAME_SERVER,
                required_input_labels=("player_id",),
            )
            repository = GameOrderRepository(root / "orders.sqlite3")
            service = CustomerCheckoutService(
                repository,
                offer_resolver=MappingOfferResolver({offer.offer_id: offer}),
                recipient_vault=RecipientVault(root / "recipient.sqlite3", encryption_key=Fernet.generate_key()),
                fingerprint_secret=b"s" * 32,
            )
            principal = CustomerPrincipal("customer-1", "aurix-identity", NOW.replace(year=2027))
            with self.assertRaises(CheckoutInputError):
                service.prepare(
                    principal,
                    {
                        "offer_id": offer.offer_id,
                        "fields": {"player_id": "123"},
                        "region": {"dimension": "store_country", "value": "MM"},
                    },
                    idempotency_key="client-region",
                    now=NOW,
                )
            self.assertEqual(repository.list_for_customer("customer-1"), ())

    def test_http_checkout_requires_signed_owner_and_returns_payment_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            offer = ApprovedOfferInput(
                offer_id="offer-http",
                product_slug="game-credit",
                product_name="Game Credit",
                fulfillment_kind="gift_card",
                denomination_label="Gift card",
                retail_amount_minor=5500,
                currency="MMK",
                observed_at=datetime.now(UTC),
                region_rule=RegionRule(verified=True, allow_all=True),
                region_label="Global",
                required_input_labels=("player_id",),
            )
            secret = b"c" * 32
            repository = GameOrderRepository(root / "orders.sqlite3")
            checkout = CustomerCheckoutService(
                repository,
                offer_resolver=MappingOfferResolver({offer.offer_id: offer}),
                recipient_vault=RecipientVault(root / "recipient.sqlite3", encryption_key=Fernet.generate_key()),
                fingerprint_secret=b"p" * 32,
            )
            verifier = SignedCustomerTokenVerifier(secret)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                make_handler(customer_auth=verifier, customer_checkout_service=checkout),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                token = issue_test_token(
                    "customer-http", secret, expires_at=datetime.now(UTC) + timedelta(minutes=5)
                )
                request = Request(
                    base + "/api/v1/orders",
                    data=b'{"offer_id":"offer-http","fields":{"player_id":"player-secret"}}',
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Idempotency-Key": "http-order-1",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urlopen(request) as response:
                    body = response.read()
                    self.assertEqual(response.status, 202)
                payload = json.loads(body)
                self.assertEqual(payload["order"]["state"], "awaiting_payment")
                self.assertEqual(payload["payment"]["state"], "required")
                self.assertNotIn("player-secret", body.decode())

                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(
                        base + "/api/v1/orders",
                        data=b"{}",
                        headers={"Origin": base},
                        method="POST",
                    ))
                self.assertEqual(error.exception.code, 401)
                error.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_http_checkout_errors_do_not_expose_exception_details(self):
        class BrokenCheckout:
            def prepare(self, *args, **kwargs):
                raise RuntimeError("private database path and supplier body")

        secret = b"e" * 32
        verifier = SignedCustomerTokenVerifier(secret)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(customer_auth=verifier, customer_checkout_service=BrokenCheckout()),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            token = issue_test_token(
                "customer-error-test", secret, expires_at=datetime.now(UTC) + timedelta(minutes=5)
            )
            request = Request(
                base + "/api/v1/orders",
                data=b"{}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": "checkout-error-test",
                },
                method="POST",
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request)
            self.assertEqual(error.exception.code, 500)
            body = error.exception.read().decode()
            self.assertEqual(json.loads(body), {"error": "customer checkout preparation failed"})
            self.assertNotIn("private database path", body)
            error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
