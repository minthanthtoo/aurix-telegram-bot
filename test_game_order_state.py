from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from aurix_games.order_state import (
    OrderSnapshot,
    OrderState,
    OrderTransitionError,
    create_order,
    idempotency_fingerprint,
    transition_order,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 22, 14, 0, tzinfo=UTC)


def _snapshot(**changes) -> OrderSnapshot:
    values = {
        "offer_id": "aurix-mlbb-asia-55",
        "product_slug": "mobile-legends",
        "fulfillment_kind": "direct_topup",
        "amount_minor": 455000,
        "currency": "MMK",
        "recipient_fingerprint": "recipient-fp-123",
    }
    values.update(changes)
    return OrderSnapshot(**values)


def _order(**changes):
    return create_order(
        order_id=changes.get("order_id", "ord-game-001"),
        customer_id=changes.get("customer_id", "customer-001"),
        customer_idempotency_key=changes.get("customer_idempotency_key", "client-key-001"),
        snapshot=changes.get("snapshot", _snapshot()),
    )


class GameOrderStateTests(unittest.TestCase):
    def test_create_snapshots_and_derives_stable_provider_key(self):
        order = _order()
        again = _order()
        self.assertEqual(order.state, OrderState.DRAFT)
        self.assertTrue(order.provider_idempotency_key.startswith("aurix-game:"))
        self.assertEqual(order.provider_idempotency_key, again.provider_idempotency_key)
        self.assertEqual(idempotency_fingerprint(order), idempotency_fingerprint(again))

    def test_happy_path_is_explicit_and_append_only(self):
        order = _order()
        states = [OrderState.AWAITING_PAYMENT, OrderState.PAID_RESERVED, OrderState.FULFILLMENT_QUEUED,
                  OrderState.SUBMITTED, OrderState.PROCESSING, OrderState.DELIVERED]
        for state in states:
            order = transition_order(order, state, at=NOW, reason=f"advance to {state.value}")
        self.assertEqual(order.state, OrderState.DELIVERED)
        self.assertEqual(len(order.events), len(states))
        self.assertEqual(order.events[0].from_state, OrderState.DRAFT)
        self.assertEqual(order.events[-1].to_state, OrderState.DELIVERED)

    def test_timeout_goes_to_unknown_then_reconciliation_not_blind_retry(self):
        order = _order()
        for state in (OrderState.AWAITING_PAYMENT, OrderState.PAID_RESERVED, OrderState.FULFILLMENT_QUEUED, OrderState.SUBMITTED):
            order = transition_order(order, state, at=NOW, reason="normal")
        unknown = transition_order(
            order,
            OrderState.UNKNOWN,
            at=NOW + timedelta(seconds=1),
            reason="provider request timed out after submission",
        )
        recon = transition_order(
            unknown,
            OrderState.RECONCILIATION,
            at=NOW + timedelta(seconds=2),
            reason="lookup by provider idempotency key",
        )
        self.assertEqual(recon.state, OrderState.RECONCILIATION)
        with self.assertRaisesRegex(OrderTransitionError, "illegal transition"):
            transition_order(recon, OrderState.SUBMITTED, at=NOW, reason="blind retry")

    def test_provider_identity_and_status_are_recorded_without_overwriting_snapshot(self):
        order = transition_order(
            transition_order(
                _order(), OrderState.AWAITING_PAYMENT, at=NOW, reason="await payment"
            ),
            OrderState.PAID_RESERVED,
            at=NOW,
            reason="reservation recorded",
        )
        queued = transition_order(order, OrderState.FULFILLMENT_QUEUED, at=NOW, reason="enqueue")
        submitted = transition_order(
            queued,
            OrderState.SUBMITTED,
            at=NOW,
            reason="provider submit acknowledged",
            provider_order_id="provider-order-77",
            provider_status="processing",
        )
        self.assertEqual(submitted.provider_order_id, "provider-order-77")
        self.assertEqual(submitted.provider_status, "processing")
        self.assertEqual(submitted.snapshot, _snapshot())

    def test_failed_order_requires_review_before_refund(self):
        order = _order()
        for state in (OrderState.AWAITING_PAYMENT, OrderState.PAID_RESERVED, OrderState.FULFILLMENT_QUEUED, OrderState.SUBMITTED):
            order = transition_order(order, state, at=NOW, reason="normal")
        failed = transition_order(order, OrderState.FAILED, at=NOW, reason="provider rejected")
        review = transition_order(failed, OrderState.REVIEW_REFUND, at=NOW, reason="operator review")
        refunded = transition_order(review, OrderState.REFUNDED, at=NOW, reason="compensating ledger event")
        self.assertEqual(refunded.state, OrderState.REFUNDED)
        with self.assertRaisesRegex(OrderTransitionError, "illegal transition"):
            transition_order(failed, OrderState.REFUNDED, at=NOW, reason="skip review")

    def test_terminal_states_and_invalid_inputs_are_rejected(self):
        delivered = transition_order(
            transition_order(
                transition_order(
                    transition_order(
                        transition_order(
                            transition_order(_order(), OrderState.AWAITING_PAYMENT, at=NOW, reason="a"),
                            OrderState.PAID_RESERVED, at=NOW, reason="b"
                        ), OrderState.FULFILLMENT_QUEUED, at=NOW, reason="c"
                    ), OrderState.SUBMITTED, at=NOW, reason="d"
                ), OrderState.PROCESSING, at=NOW, reason="e"
            ), OrderState.DELIVERED, at=NOW, reason="f"
        )
        with self.assertRaisesRegex(OrderTransitionError, "illegal transition"):
            transition_order(delivered, OrderState.PROCESSING, at=NOW, reason="again")
        with self.assertRaisesRegex(OrderTransitionError, "timezone-aware"):
            transition_order(_order(), OrderState.AWAITING_PAYMENT, at=NOW.replace(tzinfo=None), reason="bad")
        with self.assertRaisesRegex(OrderTransitionError, "amount_minor"):
            _snapshot(amount_minor=-1)


if __name__ == "__main__":
    unittest.main()
