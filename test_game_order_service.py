from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from aurix_games.catalog_projection import ApprovedOfferInput
from aurix_games.eligibility import (
    RegionDimension,
    RegionRule,
    RegionValue,
    RecipientEligibilityEvidence,
)
from aurix_games.order_repository import GameOrderRepository
from aurix_games.order_service import OrderPreparationError, prepare_order
from aurix_games.order_state import OrderState


UTC = timezone.utc
NOW = datetime(2026, 9, 22, 16, 0, tzinfo=UTC)


def _offer(**changes) -> ApprovedOfferInput:
    values = {
        "offer_id": "aurix-mlbb-asia-55",
        "product_slug": "mobile-legends",
        "product_name": "Mobile Legends",
        "fulfillment_kind": "direct_topup",
        "denomination_label": "55 Diamonds",
        "retail_amount_minor": 455000,
        "currency": "MMK",
        "observed_at": NOW - timedelta(minutes=1),
        "region_rule": RegionRule(
            verified=True,
            dimension=RegionDimension.GAME_SERVER,
            allowed_values=frozenset({"sea"}),
        ),
        "region_label": "SEA server",
        "region_dimension": RegionDimension.GAME_SERVER,
        "required_input_labels": ("player_id", "zone_id"),
    }
    values.update(changes)
    return ApprovedOfferInput(**values)


def _recipient(**changes) -> RecipientEligibilityEvidence:
    values = {
        "fulfillment_kind": "direct_topup",
        "supplied_field_names": frozenset({"player_id", "zone_id"}),
        "region": RegionValue(RegionDimension.GAME_SERVER, "sea"),
        "validation_valid": True,
        "validation_checked_at": NOW,
        "recipient_binding": "opaque-recipient-fp",
        "validation_binding": "opaque-recipient-fp",
    }
    values.update(changes)
    return RecipientEligibilityEvidence(**values)


class GameOrderServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repository = GameOrderRepository(Path(self.tempdir.name) / "db" / "orders.sqlite3")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_prepare_persists_only_through_awaiting_payment(self):
        prepared = prepare_order(
            self.repository,
            order_id="ord-game-prepare-1",
            customer_id="customer-1",
            customer_idempotency_key="client-1",
            offer=_offer(),
            recipient=_recipient(),
            recipient_fingerprint="opaque-recipient-fp",
            now=NOW,
        )
        self.assertEqual(prepared.order.state, OrderState.AWAITING_PAYMENT)
        self.assertEqual(len(prepared.order.events), 1)
        self.assertTrue(prepared.decision.eligible)

    def test_region_mismatch_is_rejected_before_database_write(self):
        recipient = _recipient(
            region=RegionValue(RegionDimension.STORE_COUNTRY, "MM")
        )
        with self.assertRaises(OrderPreparationError) as context:
            prepare_order(
                self.repository,
                order_id="ord-game-prepare-2",
                customer_id="customer-1",
                customer_idempotency_key="client-2",
                offer=_offer(),
                recipient=recipient,
                recipient_fingerprint="opaque-recipient-fp",
                now=NOW,
            )
        self.assertIn("region_dimension_mismatch", context.exception.decision.reasons)
        self.assertEqual(self.repository.list_for_customer("customer-1"), ())

    def test_idempotent_retry_replays_existing_order_without_rewind(self):
        first = prepare_order(
            self.repository,
            order_id="ord-game-prepare-3",
            customer_id="customer-1",
            customer_idempotency_key="client-3",
            offer=_offer(),
            recipient=_recipient(),
            recipient_fingerprint="opaque-recipient-fp",
            now=NOW,
        )
        retry = prepare_order(
            self.repository,
            order_id="different-order-id",
            customer_id="customer-1",
            customer_idempotency_key="client-3",
            offer=_offer(),
            recipient=_recipient(),
            recipient_fingerprint="opaque-recipient-fp",
            now=NOW,
        )
        self.assertEqual(retry.order.order_id, first.order.order_id)
        self.assertEqual(retry.order.state, OrderState.AWAITING_PAYMENT)
        self.assertEqual(len(self.repository.list_for_customer("customer-1")), 1)

    def test_missing_recipient_fingerprint_is_rejected_without_write(self):
        with self.assertRaisesRegex(OrderPreparationError, "fingerprint"):
            prepare_order(
                self.repository,
                order_id="ord-game-prepare-4",
                customer_id="customer-1",
                customer_idempotency_key="client-4",
                offer=_offer(),
                recipient=_recipient(),
                recipient_fingerprint=" ",
                now=NOW,
            )
        self.assertEqual(self.repository.list_for_customer("customer-1"), ())

    def test_direct_topup_cannot_omit_or_forge_validation(self):
        for changes in (
            {"validation_valid": None, "validation_checked_at": None},
            {"validation_valid": False},
            {"validation_binding": "another-recipient"},
            {"recipient_binding": "another-snapshot", "validation_binding": "another-snapshot"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(OrderPreparationError):
                    prepare_order(
                        self.repository, order_id="blocked", customer_id="customer-1",
                        customer_idempotency_key="blocked", offer=_offer(),
                        recipient=_recipient(**changes),
                        recipient_fingerprint="opaque-recipient-fp", now=NOW,
                    )
                self.assertEqual(self.repository.list_for_customer("customer-1"), ())

    def test_required_validation_must_be_recent_and_bound(self):
        recipient = _recipient(
            validation_valid=True,
            validation_checked_at=NOW - timedelta(minutes=3),
            recipient_binding="opaque-recipient-fp",
            validation_binding="opaque-recipient-fp",
        )
        with self.assertRaises(OrderPreparationError) as context:
            prepare_order(
                self.repository,
                order_id="ord-game-prepare-5",
                customer_id="customer-1",
                customer_idempotency_key="client-5",
                offer=_offer(),
                recipient=recipient,
                recipient_fingerprint="opaque-recipient-fp",
                now=NOW,
            )
        self.assertIn("recipient_validation_stale", context.exception.decision.reasons)


if __name__ == "__main__":
    unittest.main()
