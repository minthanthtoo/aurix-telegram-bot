import unittest

from commerce_capacity_projection import (
    allocations_by_server,
    free_counts,
    tier_allocations_by_server,
    usage_rows,
)


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


if __name__ == "__main__":
    unittest.main()
