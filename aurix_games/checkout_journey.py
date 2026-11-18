"""Owner-bound recipient lookup and encrypted, expiring checkout reviews."""

from datetime import datetime, timedelta
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import uuid

from .customer_checkout import CheckoutInputError, _fields, _id, _region, _validate_principal
from .eligibility import OfferEligibilityEvidence, RecipientEligibilityEvidence, RegionDimension, RegionValue, evaluate_offer_eligibility
from .order_repository import (
    OrderNotFoundError,
    OrderRepositoryError,
    RecipientLookupThrottledError,
)
from .customer_ledger import InsufficientFundsError
from .providers.fazercards_commands import FazerCardsCommandClient
from .recipient_vault import RecipientVaultError
from .payment_service import CustomerPaymentService, PaymentBoundaryError
from .supplier_routes import SupplierRouteError, fazercards_order_payload
from .commerce_settings import commerce_paused, env_commerce_settings


class GameAccountLookup:
    """Verify a game recipient before package selection using approved routes."""

    def __init__(self, supplier, repository, *, fingerprint_secret, local_guest=False, zones=(),
                 catalog_store=None, supplier_route_store=None):
        self.supplier = supplier
        self.repository = repository
        self.fingerprint_secret = fingerprint_secret
        self.local_guest = local_guest
        self.catalog_store = catalog_store
        self.supplier_route_store = supplier_route_store
        self.zones = []
        seen = set()
        for row in zones:
            if not isinstance(row, dict) or set(row) != {"id", "name"}:
                raise ValueError("MLBB zone entries require an id and name.")
            zone_id, name = row["id"], row["name"]
            if (not isinstance(zone_id, str) or not re.fullmatch(r"[0-9]{1,10}", zone_id)
                    or zone_id in seen or not isinstance(name, str)
                    or not name.strip() or len(name.strip()) > 100 or not name.isprintable()):
                raise ValueError("MLBB zone directory contains an invalid name or duplicate or invalid ID.")
            seen.add(zone_id)
            self.zones.append({"id": zone_id, "name": name.strip()})

    def resolve_zone(self, value):
        if not isinstance(value, str) or len(value) > 120:
            raise CheckoutInputError("Enter a Zone ID or choose a zone name from the list.")
        text = value.strip().casefold()
        numeric = re.fullmatch(r"(?:zone +)?([0-9]{1,10})", text)
        if numeric:
            return numeric[1]
        matches = [
            row["id"] for row in self.zones
            if text in (row["name"].casefold(), f'{row["name"].casefold()} ({row["id"]})')
        ]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise CheckoutInputError("Several zones share that name. Choose the entry with your Zone ID.")
        raise CheckoutInputError("Zone name not found. Enter the numeric Zone ID from your MLBB profile.")

    def check(self, customer_id, payload, *, now, source_identity=None):
        return self.check_product("mobile-legends", customer_id, payload, now=now,
                                  source_identity=source_identity)

    def check_product(self, product_slug, customer_id, payload, *, now, source_identity=None):
        if not self.local_guest and self.catalog_store is not None and self.supplier_route_store is not None:
            return self._check_approved_product(product_slug, customer_id, payload, now=now,
                                                source_identity=source_identity)
        if product_slug != "mobile-legends":
            raise CheckoutInputError("account check is unavailable for this game")
        return self._check_mlbb_legacy(customer_id, payload, now=now, source_identity=source_identity)

    def _check_mlbb_legacy(self, customer_id, payload, *, now, source_identity=None):
        if set(payload) != {"player_id", "zone_id"}:
            raise CheckoutInputError("Enter your MLBB Account ID and zone.")
        if not isinstance(payload["player_id"], str) or not re.fullmatch(r"[0-9]{4,20}", payload["player_id"]):
            raise CheckoutInputError("Enter a numeric Account ID.")
        fields = {**payload, "zone_id": self.resolve_zone(payload["zone_id"])}
        lookup_actor = customer_id
        if not lookup_actor:
            subject = f"{fields['player_id']}\x00{fields['zone_id']}".encode("utf-8")
            lookup_actor = "anonymous-mlbb-" + hashlib.sha256(subject).hexdigest()
        self.repository.claim_recipient_lookup(
            lookup_actor, now=now, fingerprint_secret=self.fingerprint_secret,
            source_identity=source_identity,
        )
        result = self.supplier.validate_topup_recipient("mobile_legends", fields)
        if (not isinstance(result, dict) or result.get("ok") is not True or result.get("valid") is not True
                or result.get("category_id") != "mobile_legends"
                or result.get("player_id") != payload["player_id"]):
            raise CheckoutInputError("Account not confirmed. Check your Account ID and Zone ID.")
        name, region = result.get("player_name"), result.get("region")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 128
               or any(ord(char) < 32 for char in value) for value in (name, region)):
            raise CheckoutInputError("The provider did not confirm a player name and region.")
        return {"player_name": name.strip(), "region": region.strip()}

    def _check_approved_product(self, product_slug, customer_id, payload, *, now, source_identity=None):
        if not isinstance(product_slug, str) or not re.fullmatch(r"[a-z0-9-]{1,80}", product_slug):
            raise CheckoutInputError("game is invalid")
        if not isinstance(payload, dict):
            raise CheckoutInputError("Enter your game account details.")
        if product_slug == "mobile-legends" and set(payload) == {"player_id", "zone_id"}:
            payload = {"player_id": payload["player_id"], "server_id": payload["zone_id"]}
        detail = self.catalog_store.product_detail(product_slug, now=now)
        offers = detail.get("offers") if isinstance(detail, dict) else None
        if not isinstance(offers, list) or not offers:
            raise CheckoutInputError("Account check is unavailable for this game right now.")
        contracts = []
        for offer in offers:
            if not isinstance(offer, dict) or offer.get("fulfillment_kind") != "direct_topup":
                raise CheckoutInputError("Account check is unavailable for this game's current packages.")
            labels = offer.get("required_input_labels")
            if (not isinstance(labels, list) or not labels
                    or any(not isinstance(label, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", label)
                           for label in labels) or len(set(labels)) != len(labels)):
                raise CheckoutInputError("Approved account fields are unavailable.")
            try:
                route = self.supplier_route_store.resolve(
                    offer_id=offer.get("offer_id"), product_slug=product_slug,
                    fulfillment_kind=offer["fulfillment_kind"], denomination_label=offer.get("name"),
                    required_input_labels=tuple(labels),
                )
            except (SupplierRouteError, KeyError):
                raise CheckoutInputError("Approved account check route is unavailable.") from None
            if route.family != "topups" or not route.validation_category_id:
                raise CheckoutInputError("Approved account check route is unavailable.")
            contracts.append((offer, tuple(labels), route))
        _first_offer, labels, route = contracts[0]
        shared_route = (route.validation_category_id, route.validation_field_map,
                        route.validation_region_map, route.detect_region)
        if any(fields != labels or (candidate.validation_category_id, candidate.validation_field_map,
                                    candidate.validation_region_map, candidate.detect_region) != shared_route
               for _offer, fields, candidate in contracts[1:]):
            raise CheckoutInputError("This game's packages do not share one safe account-check route.")
        if set(payload) != set(labels):
            raise CheckoutInputError("Enter the requested game account details.")
        fields = {}
        for key, value in payload.items():
            if (not isinstance(value, str) or not value.strip() or len(value.strip()) > 128
                    or any(ord(char) < 32 for char in value)):
                raise CheckoutInputError("Game account details are invalid.")
            fields[key] = value.strip()
        if product_slug == "mobile-legends":
            if not re.fullmatch(r"[0-9]{4,20}", fields.get("player_id", "")):
                raise CheckoutInputError("Enter a numeric Account ID.")
            zone_key = "server_id" if "server_id" in fields else "zone_id"
            fields[zone_key] = self.resolve_zone(fields[zone_key])
        subject = json.dumps([product_slug, fields], sort_keys=True, separators=(",", ":")).encode("utf-8")
        lookup_actor = customer_id or "anonymous-game-" + hashlib.sha256(subject).hexdigest()
        self.repository.claim_recipient_lookup(
            lookup_actor, now=now, fingerprint_secret=self.fingerprint_secret,
            source_identity=source_identity,
        )
        validation_fields = ({target: fields[source] for target, source in route.validation_field_map}
                             if route.validation_field_map else fields)
        result = self.supplier.validate_topup_recipient(route.validation_category_id, validation_fields)
        if (not isinstance(result, dict) or result.get("ok") is not True or result.get("valid") is not True
                or result.get("category_id") != route.validation_category_id):
            raise CheckoutInputError("Account not confirmed. Check your game account details.")
        name = result.get("player_name")
        if not isinstance(name, str) or not name.strip() or len(name) > 128 or any(ord(char) < 32 for char in name):
            raise CheckoutInputError("The provider did not confirm a player name.")
        echoed = result.get("player_id")
        ids = [validation_fields[key] for key in ("player_id", "user_id", "uid") if key in validation_fields]
        if echoed is not None and (not isinstance(echoed, str) or len(ids) != 1 or echoed != ids[0]):
            raise CheckoutInputError("The returned player ID does not match.")
        reported_region = result.get("region")
        if reported_region is not None and (not isinstance(reported_region, str) or not reported_region.strip()
                or len(reported_region) > 128 or any(ord(char) < 32 for char in reported_region)):
            raise CheckoutInputError("The returned account region is invalid.")
        region_token = reported_region.strip().casefold() if isinstance(reported_region, str) else None
        eligible_offer_ids = []
        for offer, _fields, candidate in contracts:
            if candidate.detect_region:
                eligible = bool(region_token and region_token not in {"unknown", "global", "n/a", "none", "unavailable"}
                                and region_token not in candidate.excluded_regions)
            elif candidate.validation_region_map:
                eligible = bool(region_token and dict(candidate.validation_region_map).get(region_token))
            else:
                public_region = offer.get("region")
                eligible = isinstance(public_region, dict) and public_region.get("type") == "global"
            if eligible:
                eligible_offer_ids.append(offer["offer_id"])
        return {"player_name": name.strip(), "region": reported_region.strip() if reported_region else None,
                "verified_fields": fields, "eligible_offer_ids": eligible_offer_ids}


MlbbAccountLookup = GameAccountLookup


def read_receiver_qr(directory, asset_name, *, expected_sha256=None):
    if (not isinstance(asset_name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(?:png|jpe?g|webp)", asset_name, re.IGNORECASE)
            or directory is None):
        return None
    try:
        root = Path(directory).resolve(strict=True)
        path = root / asset_name
        if path.is_symlink():
            return None
        resolved = path.resolve(strict=True)
        root_mode = root.stat().st_mode
        file_stat = resolved.stat()
        if (resolved.parent != root or not resolved.is_file() or root_mode & 0o077
                or file_stat.st_mode & 0o077 or not 8 <= file_stat.st_size <= 2_000_000):
            return None
        data = resolved.read_bytes()
    except (OSError, RuntimeError):
        return None
    if len(data) > 2_000_000 or (expected_sha256 is not None and (
        not isinstance(expected_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)
        or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected_sha256)
    )):
        return None
    mime, signature = {
        ".png": ("image/png", lambda value: value.startswith(b"\x89PNG\r\n\x1a\n")),
        ".jpg": ("image/jpeg", lambda value: value.startswith(b"\xff\xd8")),
        ".jpeg": ("image/jpeg", lambda value: value.startswith(b"\xff\xd8")),
        ".webp": ("image/webp", lambda value: value.startswith(b"RIFF") and value[8:12] == b"WEBP"),
    }[Path(asset_name).suffix.casefold()]
    return (mime, data) if signature(data) else None


class CheckoutJourney:
    def __init__(self, checkout, supplier, *, payment_receivers=None, receiver_qr_dir=None,
                 payment_preflight=None, wallet_payment_service: CustomerPaymentService | None = None):
        self.checkout = checkout
        self.supplier = supplier
        self.payment_receivers = payment_receivers or {}
        self.receiver_qr_dir = None if receiver_qr_dir is None else Path(receiver_qr_dir)
        self.payment_preflight = payment_preflight
        self.wallet_payment_service = wallet_payment_service

    def read_receiver_qr(self, asset_name, *, expected_sha256=None):
        return read_receiver_qr(self.receiver_qr_dir, asset_name, expected_sha256=expected_sha256)

    def review(self, principal, payload, *, now):
        _validate_principal(principal, now=now)
        if commerce_paused("checkout_enabled") or commerce_paused("account_checks_enabled"):
            raise CheckoutInputError("Account review is paused in store settings.")
        if not isinstance(payload, dict) or set(payload) - {"offer_id", "fields", "region"}:
            raise CheckoutInputError("review request is invalid")
        offer_id = _id(payload.get("offer_id"), "offer_id")
        fields = _fields(payload.get("fields"))
        region = _region(payload.get("region"))
        offer = self.checkout.offer_resolver.resolve(offer_id)
        if offer is None or not offer.customer_visible or offer.fulfillment_kind != "direct_topup":
            raise CheckoutInputError("recipient lookup is unavailable for this offer")
        maximum = env_commerce_settings().status()["settings"]["maximum_order_mmk"]
        if maximum is not None and offer.currency == "MMK" and offer.retail_amount_minor > maximum:
            raise CheckoutInputError("This package exceeds the store order limit.")
        if set(fields) != set(offer.required_input_labels):
            raise CheckoutInputError("recipient fields do not match the approved offer")
        preflight = evaluate_offer_eligibility(
            OfferEligibilityEvidence(offer.fulfillment_kind, offer.review_state, offer.supplier_enabled,
                offer.available, offer.resale_authorized, offer.observed_at,
                frozenset(offer.required_input_labels), offer.region_rule),
            RecipientEligibilityEvidence(offer.fulfillment_kind, frozenset(fields), region), now=now)
        # Lookup can discover a missing region; every other eligibility gate still applies first.
        if not preflight.eligible and not (region is None and preflight.reasons == ("recipient_region_missing",)):
            raise CheckoutInputError("offer or region is no longer eligible")
        route = self.checkout._supplier_route(offer)
        if route is None or route.family != "topups" or route.validation_category_id is None:
            raise CheckoutInputError("approved recipient lookup route is unavailable")
        try:
            self.checkout.claim_recipient_lookup(principal.customer_id, now=now)
        except RecipientLookupThrottledError:
            raise CheckoutInputError("please wait before checking another account") from None
        validation_fields = {target: fields[source] for target, source in route.validation_field_map} if route.validation_field_map else fields
        response = self.supplier.validate_topup_recipient(route.validation_category_id, validation_fields)
        if (not isinstance(response, dict) or response.get("ok") is not True
                or response.get("valid") is not True or response.get("category_id") != route.validation_category_id):
            raise CheckoutInputError("the game account could not be confirmed")
        name = response.get("player_name")
        if not isinstance(name, str) or not name.strip() or len(name) > 128 or any(ord(c) < 32 for c in name):
            raise CheckoutInputError("the provider did not confirm a player name")
        echoed = response.get("player_id")
        ids = [validation_fields[key] for key in ("player_id", "user_id", "uid") if key in validation_fields]
        if echoed is not None and (not isinstance(echoed, str) or len(ids) != 1 or echoed != ids[0]):
            raise CheckoutInputError("the returned player ID does not match")
        reported_region = response.get("region")
        if reported_region is not None and (not isinstance(reported_region, str)
                or not reported_region.strip() or len(reported_region) > 128
                or any(ord(c) < 32 for c in reported_region)):
            raise CheckoutInputError("the returned account region is invalid")
        if route.detect_region:
            detected = reported_region.strip().casefold() if reported_region else None
            if not detected or detected in {"unknown", "global", "n/a", "none", "unavailable"}:
                raise CheckoutInputError("the supplier did not identify an account region; payment remains unavailable")
            if detected in route.excluded_regions:
                raise CheckoutInputError(f"Detected account region: {reported_region}. This package is not supported for that region.")
            if region is not None and (region.dimension != RegionDimension.GAME_SERVER or region.value != detected):
                raise CheckoutInputError("the detected account region does not match the submitted region")
            region = RegionValue(RegionDimension.GAME_SERVER, detected)
            payload = {**payload, "region": {"dimension": region.dimension.value, "value": region.value}}
        elif not offer.region_rule.allow_all:
            reported_token = reported_region.strip().casefold() if reported_region else None
            if route.validation_region_map:
                reported_token = dict(route.validation_region_map).get(reported_token)
            if region is None and reported_token in (offer.region_rule.allowed_values or ()):
                region = RegionValue(offer.region_rule.dimension, reported_token)
                payload = {**payload, "region": {"dimension": region.dimension.value, "value": region.value}}
            if region is None or reported_token != region.value:
                raise CheckoutInputError(f"Detected account region: {reported_region or 'not reported'}. This package is not confirmed for that region.")
        binding = self.checkout._recipient_binding(principal, offer, fields, region, route)
        decision = evaluate_offer_eligibility(
            OfferEligibilityEvidence(offer.fulfillment_kind, offer.review_state, offer.supplier_enabled,
                offer.available, offer.resale_authorized, offer.observed_at,
                frozenset(offer.required_input_labels), offer.region_rule, True),
            RecipientEligibilityEvidence(offer.fulfillment_kind, frozenset(fields), region,
                True, now, binding, binding), now=now,
        )
        if not decision.eligible:
            raise CheckoutInputError("offer or recipient is no longer eligible")
        quote_id = "quote-" + uuid.uuid4().hex
        expires = now + timedelta(seconds=110)
        record = {"kind": "checkout_review", "request": payload,
            "binding": binding, "checked_at": now.isoformat(), "expires_at": expires.isoformat(),
            "player_name": name.strip(), "region": reported_region,
            "amount_minor": offer.retail_amount_minor, "currency": offer.currency}
        record["fulfillment_payload"] = {"offer_id": offer.offer_id,
            "fulfillment_kind": offer.fulfillment_kind, "fields": fields,
            "region": None if region is None else {"dimension": region.dimension.value, "value": region.value},
            "supplier_route": route.as_vault_dict()}
        self.checkout.recipient_vault.put(order_id=quote_id, customer_id=principal.customer_id,
            payload=record, now=now)
        return {"quote": {"quote_id": quote_id, "expires_at": expires.isoformat(),
            "player_name": name.strip(), "region": reported_region,
            "product_name": offer.product_name, "package": offer.denomination_label,
            "amount_minor": offer.retail_amount_minor, "currency": offer.currency,
            "confirmation_required": True}}

    def confirm(self, principal, payload, *, now):
        _validate_principal(principal, now=now)
        if not isinstance(payload, dict) or set(payload) != {"quote_id", "confirmed"} or payload.get("confirmed") is not True:
            raise CheckoutInputError("explicit recipient confirmation is required")
        quote_id = _id(payload.get("quote_id"), "quote_id")
        try:
            record = self.checkout.recipient_vault.get(order_id=quote_id, customer_id=principal.customer_id)
        except RecipientVaultError:
            raise CheckoutInputError("checkout review was not found") from None
        if record.get("kind") != "checkout_review":
            raise CheckoutInputError("checkout review was not found")
        # A lost confirmation response must remain recoverable after quote expiry.
        existing = self.checkout.repository.get_by_customer_key(principal.customer_id, quote_id)
        if existing is not None:
            # Order persistence can precede vault persistence in a failed confirmation.
            if existing.snapshot.recipient_fingerprint != record["binding"]:
                raise CheckoutInputError("stored order does not match this review")
            self.checkout.recipient_vault.put(order_id=existing.order_id,
                customer_id=principal.customer_id, payload=record["fulfillment_payload"], now=now)
            return {"order": {"order_id": existing.order_id, "state": existing.state.value,
                "amount_minor": existing.snapshot.amount_minor, "currency": existing.snapshot.currency}}
        expires = datetime.fromisoformat(record["expires_at"])
        if now >= expires:
            raise CheckoutInputError("checkout review expired; check the account again")
        binding = self.checkout.recipient_validation_binding(principal, record["request"], now=now)
        if not hmac.compare_digest(binding, record["binding"]):
            raise CheckoutInputError("the offer changed; review the account and price again")
        result = self.checkout.prepare(principal, record["request"], idempotency_key=quote_id,
            now=now, validation={"valid": True, "checked_at": record["checked_at"], "binding": binding})
        return result.as_dict()

    def open_payment(self, principal, quote_id, *, rail, receiver_id, receiver_revision, now, instructions=None):
        """Open payment with a receiver selected from trusted server configuration."""
        _validate_principal(principal, now=now)
        if any(commerce_paused(flag) for flag in (
            "payments_enabled", "checkout_enabled", "account_checks_enabled", "supplier_orders_enabled"
        )):
            raise CheckoutInputError("New payments are paused in store settings.")
        quote_id = _id(quote_id, "quote_id")
        for value in (rail, receiver_id, receiver_revision):
            if not isinstance(value, str) or not value or value != value.strip() or len(value) > 128 or any(ord(c) < 32 for c in value):
                raise CheckoutInputError("approved payment receiver is invalid")
        _record, order, _offer, _fields, _route = self._validated_payment_order(principal, quote_id, now=now)
        try:
            intent = self.checkout.repository.freeze_payment_intent(order_id=order.order_id,
                customer_id=principal.customer_id, rail=rail.casefold(), receiver_id=receiver_id,
                receiver_revision=receiver_revision, opened_at=now, expires_at=now + timedelta(minutes=10), instructions=instructions)
            return {**intent, "amount_minor": order.snapshot.amount_minor, "currency": order.snapshot.currency}
        except (RecipientVaultError, OrderRepositoryError) as exc:
            raise CheckoutInputError("payment cannot be opened for this order") from exc

    def _validated_payment_order(self, principal, quote_id, *, now):
        try:
            record = self.checkout.recipient_vault.get(order_id=quote_id, customer_id=principal.customer_id)
            order = self.checkout.repository.get_by_customer_key(principal.customer_id, quote_id)
            if record.get("kind") != "checkout_review" or order is None:
                raise CheckoutInputError("confirm the reviewed account before opening payment")
            if order.state.value != "awaiting_payment":
                raise CheckoutInputError("this order no longer accepts payment")
            expiry = datetime.fromisoformat(record["expires_at"])
            if now >= expiry:
                raise CheckoutInputError("checkout review expired; check the account again")
            request = record["request"]
            offer = self.checkout.offer_resolver.resolve(request["offer_id"])
            if offer is None or not offer.customer_visible:
                raise CheckoutInputError("offer is unavailable")
            fields = _fields(request.get("fields"))
            region = _region(request.get("region"))
            route = self.checkout._supplier_route(offer)
            binding = self.checkout._recipient_binding(principal, offer, fields, region, route)
            decision = evaluate_offer_eligibility(
                OfferEligibilityEvidence(offer.fulfillment_kind, offer.review_state, offer.supplier_enabled,
                    offer.available, offer.resale_authorized, offer.observed_at,
                    frozenset(offer.required_input_labels), offer.region_rule, True),
                RecipientEligibilityEvidence(offer.fulfillment_kind, frozenset(fields), region, True,
                    datetime.fromisoformat(record["checked_at"]), binding, record["binding"]), now=now)
            if not decision.eligible or not hmac.compare_digest(order.snapshot.recipient_fingerprint, binding):
                raise CheckoutInputError("the offer changed; review the account and price again")
            private = self.checkout.recipient_vault.get(order_id=order.order_id, customer_id=principal.customer_id)
            if private != record["fulfillment_payload"]:
                raise CheckoutInputError("confirmed fulfillment details are unavailable")
            if self.payment_preflight is not None:
                try:
                    supplier_payload = fazercards_order_payload(route=route,
                        fulfillment_kind=offer.fulfillment_kind, fields=fields)
                    self.payment_preflight.check_payment(supplier_payload, now=now)
                except Exception:
                    raise CheckoutInputError("supplier availability cannot be confirmed; payment instructions remain unavailable") from None
            return record, order, offer, fields, route
        except (RecipientVaultError, OrderRepositoryError) as exc:
            raise CheckoutInputError("payment cannot be opened for this order") from exc

    def pay_with_wallet(self, principal, order_id, *, now):
        """Reserve already-verified wallet funds against the exact reviewed order."""
        _validate_principal(principal, now=now)
        if self.wallet_payment_service is None:
            raise CheckoutInputError("wallet payment is not configured")
        order_id = _id(order_id, "order_id")
        try:
            order = self.checkout.repository.get(order_id, customer_id=principal.customer_id)
        except OrderNotFoundError:
            raise CheckoutInputError("order was not found") from None

        event_key = f"wallet:{order_id}"
        existing = self.wallet_payment_service.ledger.event_by_key(event_key)
        if existing is not None:
            if (existing["kind"] != "reserve" or existing["order_id"] != order_id
                    or existing["customer_id"] != principal.customer_id
                    or existing["currency"] != order.snapshot.currency
                    or existing["available_delta_minor"] != -order.snapshot.amount_minor):
                raise CheckoutInputError("wallet payment conflicts with this order")
            try:
                self.checkout.repository.claim_wallet_payment(order_id=order_id,
                    customer_id=principal.customer_id, event_key=event_key, occurred_at=now,
                    existing_reservation=True)
            except OrderRepositoryError as exc:
                raise CheckoutInputError("wallet payment conflicts with this order") from exc
            if order.state.value in {"awaiting_payment", "paid_reserved"}:
                try:
                    result = self.wallet_payment_service.reserve_order(order_id,
                        event_key=event_key, actor="customer_wallet_checkout",
                        reason="customer-confirmed wallet payment", occurred_at=now)
                    order = result.order
                except (PaymentBoundaryError, ValueError) as exc:
                    raise CheckoutInputError("wallet payment recovery needs operator review") from exc
            try:
                self.checkout.repository.complete_wallet_payment_claim(
                    order_id=order_id, event_key=event_key, occurred_at=now)
            except OrderRepositoryError as exc:
                raise CheckoutInputError("wallet payment recovery needs operator review") from exc
            return self._wallet_payment_result(principal, order, replayed=True)

        if any(commerce_paused(flag) for flag in (
            "payments_enabled", "checkout_enabled", "account_checks_enabled", "supplier_orders_enabled"
        )):
            raise CheckoutInputError("New payments are paused in store settings.")
        if order.state.value != "awaiting_payment":
            raise CheckoutInputError("this order no longer accepts wallet payment")
        if self.checkout.repository.get_payment_intent(order_id) is not None:
            raise CheckoutInputError("this order already has external payment instructions")
        self._validated_payment_order(principal, order.customer_idempotency_key, now=now)
        try:
            self.checkout.repository.claim_wallet_payment(order_id=order_id,
                customer_id=principal.customer_id, event_key=event_key, occurred_at=now)
        except OrderRepositoryError as exc:
            raise CheckoutInputError("this order already has a payment method") from exc
        try:
            result = self.wallet_payment_service.reserve_order(order_id,
                event_key=event_key, actor="customer_wallet_checkout",
                reason="customer-confirmed wallet payment", occurred_at=now)
        except InsufficientFundsError:
            self.checkout.repository.release_wallet_payment_claim(
                order_id=order_id, event_key=event_key, occurred_at=now)
            raise
        except (PaymentBoundaryError, ValueError) as exc:
            raise CheckoutInputError("wallet payment could not be completed") from exc
        self.checkout.repository.complete_wallet_payment_claim(
            order_id=order_id, event_key=event_key, occurred_at=now)
        return self._wallet_payment_result(principal, result.order, replayed=result.ledger.replayed)

    def _wallet_payment_result(self, principal, order, *, replayed):
        balance = self.wallet_payment_service.ledger.balance(principal.customer_id)
        if balance is None:
            raise CheckoutInputError("wallet balance is unavailable")
        return {
            "order": {"order_id": order.order_id, "state": order.state.value,
                      "amount_minor": order.snapshot.amount_minor, "currency": order.snapshot.currency},
            "payment": {"rail": "wallet", "status": "reserved"},
            "wallet": {"available_minor": balance.available_minor, "reserved_minor": balance.reserved_minor,
                       "currency": balance.currency},
            "replayed": replayed,
        }

    def payment(self, principal, payload, *, now):
        _validate_principal(principal, now=now)
        if not isinstance(payload, dict) or set(payload) not in ({"quote_id", "rail"}, {"order_id", "rail"}):
            raise CheckoutInputError("payment request is invalid")
        rail = _id(payload["rail"], "rail").casefold()
        if "order_id" in payload:
            order_id = _id(payload["order_id"], "order_id")
            try:
                order = self.checkout.repository.get(order_id, customer_id=principal.customer_id)
            except OrderNotFoundError as exc:
                raise CheckoutInputError("order was not found") from exc
            quote_id = order.customer_idempotency_key
        else:
            quote_id = _id(payload["quote_id"], "quote_id")
        order = self.checkout.repository.get_by_customer_key(principal.customer_id, quote_id)
        if order is not None and order.state.value != "awaiting_payment":
            raise CheckoutInputError("this order no longer accepts payment")
        existing = self.checkout.repository.get_payment_intent(order.order_id) if order else None
        if existing is not None:
            if existing["rail"] != rail:
                raise CheckoutInputError("this order already has a payment method")
            instructions = json.loads(existing["instructions_json"])
            return {"payment": {"order_id": order.order_id, "rail": rail,
                "amount_minor": order.snapshot.amount_minor, "currency": order.snapshot.currency,
                "expires_at": existing["expires_at"], "expired": now >= datetime.fromisoformat(existing["expires_at"]),
                "order_state": order.state.value, **instructions}}
        receiver = self.payment_receivers.get(rail)
        if receiver is None:
            raise CheckoutInputError("this payment method is unavailable")
        instructions = {"account": receiver["account"], "account_name": receiver["account_name"]}
        if receiver.get("qr_file"):
            image = self.read_receiver_qr(receiver["qr_file"])
            if image is None:
                raise CheckoutInputError("this payment QR is unavailable")
            instructions["qr_asset"] = receiver["qr_file"]
            instructions["qr_sha256"] = hashlib.sha256(image[1]).hexdigest()
        intent = self.open_payment(principal, quote_id, rail=rail,
            receiver_id=receiver["id"], receiver_revision=receiver["revision"], now=now,
            instructions=instructions)
        return {"payment": {"order_id": intent["order_id"], "rail": rail, "amount_minor": intent["amount_minor"],
            "currency": intent["currency"], "expires_at": intent["expires_at"], "expired": False, "order_state": "awaiting_payment",
            **instructions}}


def build_env_checkout_journey(checkout, *, wallet_payment_service=None):
    if checkout is None or os.environ.get("AURIX_GAMES_RECIPIENT_VALIDATION_ENABLED") != "1":
        return None
    key = os.environ.get("FAZERCARDS_API_KEY", "")
    if not key:
        raise CheckoutInputError("recipient validation requires a supplier key")
    receivers = {}
    qr_dir = os.environ.get("AURIX_GAMES_RECEIVER_QR_DIR")
    if os.environ.get("AURIX_GAMES_ACCEPT_TRANSFERS") == "1":
        try:
            receiver_file = os.environ.get("AURIX_GAMES_APPROVED_RECEIVERS_FILE", "")
            if receiver_file:
                source = Path(receiver_file)
                if (not source.is_absolute() or source.is_symlink() or not source.is_file()
                        or source.stat().st_mode & 0o077 or source.parent.stat().st_mode & 0o077
                        or source.stat().st_size > 65536):
                    raise ValueError("approved receiver file must be private")
                receivers = json.loads(source.read_text())
            else:
                receivers = json.loads(os.environ.get("AURIX_GAMES_APPROVED_RECEIVERS", ""))
        except (ValueError, OSError) as exc:
            raise CheckoutInputError("approved payment receiver configuration is invalid") from exc
        if not isinstance(receivers, dict) or not receivers or any(
            rail not in {"kpay", "wavepay", "aya", "uab", "cb"} or not isinstance(receiver, dict)
            or set(receiver) not in ({"id", "revision", "account", "account_name"},
                                     {"id", "revision", "account", "account_name", "qr_file"})
            or any(not isinstance(value, str) or not value.strip() or len(value) > 128
                   or value != value.strip() or any(ord(char) < 32 for char in value) for value in receiver.values())
            or ("qr_file" in receiver and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(?:png|jpe?g|webp)", receiver["qr_file"], re.IGNORECASE
            ))
            for rail, receiver in receivers.items()
        ):
            raise CheckoutInputError("approved payment receiver configuration is invalid")
    qr_path = None
    if qr_dir:
        qr_path = Path(qr_dir)
        if not qr_path.is_absolute() or not qr_path.is_dir():
            raise CheckoutInputError("approved payment QR directory is unavailable")
    if any("qr_file" in receiver for receiver in receivers.values()) and qr_path is None:
        raise CheckoutInputError("approved payment QR directory is unavailable")
    journey = CheckoutJourney(checkout, FazerCardsCommandClient(key,
        allow_writes=False, allow_recipient_validation=True), payment_receivers=receivers,
        receiver_qr_dir=qr_path, wallet_payment_service=wallet_payment_service)
    if any("qr_file" in receiver and journey.read_receiver_qr(receiver["qr_file"]) is None
           for receiver in receivers.values()):
        raise CheckoutInputError("approved payment QR asset is unavailable")
    if receivers:
        from .spend_guard import PilotSpendGuard
        from .fulfillment import FulfillmentTaskStore
        from .providers.fazercards import FazerCardsReadOnlyClient
        base = Path(os.environ.get("AURIX_GAMES_BASE_DIR", ".local/fazercards"))
        policy = os.environ.get("AURIX_GAMES_SPEND_POLICY", "")
        encryption_key = os.environ.get("AURIX_GAMES_FULFILLMENT_KEY", "")
        if not policy or not encryption_key:
            raise CheckoutInputError("payment opening requires a supplier spend policy and fulfillment key")
        tasks = FulfillmentTaskStore(os.environ.get("AURIX_GAMES_FULFILLMENT_DATABASE", str(base / "fulfillment.sqlite3")),
            encryption_key=encryption_key)
        journey.payment_preflight = PilotSpendGuard.from_file(policy, tasks=tasks, client=journey.supplier,
            catalog_client=FazerCardsReadOnlyClient(key), checkout=checkout)
    return journey
