import unittest
from datetime import datetime, timezone

from telegram_formatting import format_user_datetime


class TelegramFormattingTest(unittest.TestCase):
    def test_utc_timestamp_is_rendered_in_myanmar_time(self):
        self.assertEqual(
            format_user_datetime("2026-09-04T00:00:00+00:00"),
            "04 Sep 2026, 06:30 MMT",
        )

    def test_z_suffix_and_datetime_values_are_supported(self):
        self.assertEqual(
            format_user_datetime(datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)),
            "04 Sep 2026, 06:30 MMT",
        )
        self.assertEqual(
            format_user_datetime("2026-09-04T00:00:00Z"),
            "04 Sep 2026, 06:30 MMT",
        )

    def test_missing_or_invalid_values_use_safe_fallback(self):
        self.assertEqual(format_user_datetime(None), "-")
        self.assertEqual(format_user_datetime("not-a-timestamp", "pending"), "pending")

