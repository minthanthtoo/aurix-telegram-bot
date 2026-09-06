import unittest
from datetime import datetime, timezone

from commerce_capacity_projection import (
    allocations_by_server,
    free_counts,
    tier_allocations_by_server,
    usage_rows,
)
from commerce_worker_capacity_view import build_server_capacity_view


class CapacityProjectionTest(unittest.TestCase):
    def test_usage_projection_sanitizes_negative_and_invalid_counters(self):
        rows = [
            {
                "server_id": "sg-a",
                "outline_key_id": "key-1",
                "telegram_id": 1,
                "quota_bytes": 100,
            },
            {
                "server_id": None,
                "outline_key_id": "key-2",
                "telegram_id": 2,
                "quota_bytes": 200,
            },
        ]
        result = usage_rows(rows, {"sg-a": {"key-1": -3}, "primary": {"key-2": "bad"}}, "primary")
        self.assertEqual([item["used_bytes"] for item in result], [0, 0])

    def test_allocation_projections_are_grouped_and_bounded(self):
        rows = [
            {"server_id": "sg-a", "slot_limit": 2, "active_count": 1, "reserved_count": 1},
        ]
        self.assertEqual(allocations_by_server(rows)["sg-a"][0]["remaining_slots"], 0)
        counts = free_counts(
            [{"server_id": "sg-a", "is_promo": 0, "key_type": "daily_free"}], None
        )
        projected = tier_allocations_by_server(
            [{"server_id": "sg-a", "tier_code": "FREE300MB", "slot_limit": 3}], counts
        )
        self.assertEqual(projected["sg-a"][0]["active_count"], 1)
        self.assertEqual(projected["sg-a"][0]["remaining_slots"], 2)

    def test_server_view_is_pure_over_row_and_commitment_data(self):
        row = {
            "server_id": "sg-a",
            "max_keys": 10,
            "reserved_keys": 2,
            "remote_key_count": 3,
            "remote_orphan_key_count": 0,
            "enabled": 1,
            "lifecycle_state": "active",
            "health_status": "healthy",
            "last_synced_at": "2026-08-27T03:00:00+00:00",
            "monthly_traffic_bytes": 1000,
        }
        commitments = {
            "reserved_order_count": 1,
            "pending_key_count": 1,
            "committed_traffic_bytes": 100,
            "active_free_key_count": 0,
            "active_paid_key_count": 1,
            "open_order_count": 0,
            "pending_provisioning_count": 0,
        }
        result = build_server_capacity_view(
            row,
            commitments,
            current=datetime(2026, 8, 27, 3, 7, tzinfo=timezone.utc),
            registry=None,
            allocation_views={},
            tier_allocation_views={},
            health_max_age_seconds=900,
        )
        self.assertEqual(result["remaining_key_slots"], 3)
        self.assertEqual(result["remaining_traffic_bytes"], 900)
        self.assertEqual(result["admission_status"], "eligible")


if __name__ == "__main__":
    unittest.main()
