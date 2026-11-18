from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from aurix_games.eligibility import (
    OfferEligibilityEvidence,
    RecipientEligibilityEvidence,
    RegionDimension,
    RegionRule,
    RegionValue,
    evaluate_offer_eligibility,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def _offer(**overrides) -> OfferEligibilityEvidence:
    values = {
        "fulfillment_kind": "direct_topup",
        "review_state": "approved",
        "supplier_enabled": True,
        "available": True,
        "resale_authorized": True,
        "catalog_observed_at": NOW - timedelta(minutes=1),
        "required_input_fields": frozenset({"player_id", "zone_id"}),
        "region_rule": RegionRule(
            verified=True,
            dimension=RegionDimension.GAME_SERVER,
            allowed_values=frozenset({"asia-01", "asia-02"}),
        ),
    }
    values.update(overrides)
    return OfferEligibilityEvidence(**values)


def _recipient(**overrides) -> RecipientEligibilityEvidence:
    values = {
        "fulfillment_kind": "direct_topup",
        "supplied_field_names": frozenset({"player_id", "zone_id"}),
        "region": RegionValue(RegionDimension.GAME_SERVER, "ASIA-01"),
    }
    values.update(overrides)
    return RecipientEligibilityEvidence(**values)


class GameStoreEligibilityTests(unittest.TestCase):
    def test_approved_fresh_authorized_offer_accepts_matching_recipient(self):
        decision = evaluate_offer_eligibility(_offer(), _recipient(), now=NOW)
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.reasons, ())

    def test_recipient_region_must_match_explicit_allow_list(self):
        decision = evaluate_offer_eligibility(
            _offer(),
            _recipient(region=RegionValue(RegionDimension.GAME_SERVER, "EU-01")),
            now=NOW,
        )
        self.assertFalse(decision.eligible)
        self.assertIn("recipient_region_not_allowed", decision.reasons)

    def test_region_dimensions_are_never_implicitly_cross_mapped(self):
        decision = evaluate_offer_eligibility(
            _offer(),
            _recipient(region=RegionValue(RegionDimension.STORE_COUNTRY, "MM")),
            now=NOW,
        )
        self.assertIn("region_dimension_mismatch", decision.reasons)

    def test_unverified_supplier_region_or_rights_fail_closed(self):
        decision = evaluate_offer_eligibility(
            _offer(region_rule=RegionRule()), _recipient(), now=NOW
        )
        self.assertIn("region_policy_unverified", decision.reasons)

        decision = evaluate_offer_eligibility(
            _offer(resale_authorized=None), _recipient(), now=NOW
        )
        self.assertIn("resale_rights_unverified", decision.reasons)

    def test_global_offer_requires_explicit_verified_allow_all_rule(self):
        rule = RegionRule(verified=True, allow_all=True)
        decision = evaluate_offer_eligibility(
            _offer(region_rule=rule), _recipient(region=None), now=NOW
        )
        self.assertTrue(decision.eligible)

    def test_stale_catalog_and_unavailable_offer_are_denied(self):
        decision = evaluate_offer_eligibility(
            _offer(
                catalog_observed_at=NOW - timedelta(minutes=16),
                available=False,
            ),
            _recipient(),
            now=NOW,
        )
        self.assertIn("catalog_observation_stale", decision.reasons)
        self.assertIn("offer_unavailable", decision.reasons)

    def test_dynamic_recipient_fields_and_fulfillment_kind_are_enforced(self):
        decision = evaluate_offer_eligibility(
            _offer(),
            _recipient(
                fulfillment_kind="game_key",
                supplied_field_names=frozenset({"player_id"}),
            ),
            now=NOW,
        )
        self.assertIn("fulfillment_kind_mismatch", decision.reasons)
        self.assertIn("recipient_fields_missing", decision.reasons)

        unsupported = evaluate_offer_eligibility(
            _offer(fulfillment_kind="unknown_service"),
            _recipient(fulfillment_kind="unknown_service"),
            now=NOW,
        )
        self.assertIn("fulfillment_kind_unsupported", unsupported.reasons)

    def test_required_validation_must_be_successful_and_recent(self):
        offer = _offer(recipient_validation_required=True)
        missing = evaluate_offer_eligibility(offer, _recipient(), now=NOW)
        self.assertIn("recipient_not_validated", missing.reasons)
        self.assertIn("recipient_validation_time_missing", missing.reasons)
        self.assertIn("recipient_validation_binding_missing", missing.reasons)

        stale = evaluate_offer_eligibility(
            offer,
            _recipient(
                validation_valid=True,
                validation_checked_at=NOW - timedelta(minutes=3),
            ),
            now=NOW,
        )
        self.assertIn("recipient_validation_stale", stale.reasons)

        current = evaluate_offer_eligibility(
            offer,
            _recipient(
                validation_valid=True,
                validation_checked_at=NOW,
                recipient_binding="hmac-fingerprint-a",
                validation_binding="hmac-fingerprint-a",
            ),
            now=NOW,
        )
        self.assertTrue(current.eligible)

        mismatched = evaluate_offer_eligibility(
            offer,
            _recipient(
                validation_valid=True,
                validation_checked_at=NOW,
                recipient_binding="hmac-fingerprint-current",
                validation_binding="hmac-fingerprint-previous",
            ),
            now=NOW,
        )
        self.assertIn("recipient_validation_binding_mismatch", mismatched.reasons)

    def test_unknown_approval_or_stock_never_becomes_sellable(self):
        decision = evaluate_offer_eligibility(
            _offer(review_state="blocked", supplier_enabled=None, available=None),
            _recipient(),
            now=NOW,
        )
        self.assertFalse(decision.eligible)
        self.assertEqual(
            decision.reasons,
            (
                "offer_not_approved",
                "supplier_not_enabled",
                "offer_availability_unknown",
            ),
        )

    def test_invalid_region_allow_list_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "empty value"):
            RegionRule(
                verified=True,
                dimension=RegionDimension.GAME_SERVER,
                allowed_values=frozenset({"asia", " "}),
            )
        with self.assertRaisesRegex(ValueError, "combine allow-all"):
            RegionRule(verified=True, allow_all=True, allowed_values=frozenset())

    def test_naive_current_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            evaluate_offer_eligibility(_offer(), _recipient(), now=NOW.replace(tzinfo=None))


if __name__ == "__main__":
    unittest.main()
