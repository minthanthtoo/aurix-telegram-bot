import tempfile
import unittest
from pathlib import Path

from free_repository import Database
from quota_alerts import (
    alert_level_labels,
    get_quota_alert_preferences,
    reached_alert,
    set_quota_alert_preferences,
)


class QuotaAlertPreferencesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "bot.db")
        self.database.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def test_default_customer_alerts_preserve_standard_levels(self):
        preferences = get_quota_alert_preferences(self.database, 123)
        self.assertEqual(alert_level_labels(preferences), ["25%", "10%", "5%"])
        self.assertEqual(reached_alert(preferences, 1000, 200), (250, "25%"))

    def test_customer_can_choose_count_units_and_disable_alerts(self):
        preferences = set_quota_alert_preferences(
            self.database, 123, mode="gb", alert_count=2, step_value=5
        )
        self.assertEqual(alert_level_labels(preferences), ["10 GB", "5 GB"])
        preferences = set_quota_alert_preferences(self.database, 123, enabled=False)
        self.assertIsNone(reached_alert(preferences, 50_000_000_000, 4_000_000_000))


if __name__ == "__main__":
    unittest.main()
