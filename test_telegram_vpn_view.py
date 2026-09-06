import unittest

from telegram_vpn_view import render_vpn_dashboard


class TelegramVpnViewTest(unittest.TestCase):
    def test_empty_view_is_renderable_without_service_or_transport(self):
        view = render_vpn_dashboard(
            [],
            {
                "winner": False,
                "exists": False,
                "active": False,
                "frequency": "campaign",
            },
            [],
            None,
            usage_available=True,
            access_available=True,
            show_key_text=False,
            telegram_id=123,
            format_bytes=lambda value: str(value),
            format_decimal_bytes=lambda value: str(value),
            usage_rollup=lambda entries, user_id: None,
            copy_text_button=lambda label, value: None,
            promo_frequency_label=lambda value: value,
            device_api_url="",
        )
        self.assertIn("No VPN key yet", view.text)
        self.assertIn("inline_keyboard", view.markup)

    def test_active_entry_render_preserves_access_and_actions(self):
        view = render_vpn_dashboard(
            [
                {
                    "tier": "Free",
                    "status": "active",
                    "key_type": "free",
                    "quota_bytes": 100,
                    "used_bytes": 25,
                    "remaining_bytes": 75,
                    "usage_observed": True,
                    "expires_at": "2026-09-07T00:00:00+00:00",
                    "access_url": "ss://example",
                    "created_at": "2026-09-06T00:00:00+00:00",
                }
            ],
            {
                "winner": False,
                "exists": False,
                "active": False,
                "frequency": "campaign",
            },
            [],
            None,
            usage_available=True,
            access_available=True,
            show_key_text=True,
            telegram_id=123,
            format_bytes=lambda value: f"{value} B",
            format_decimal_bytes=lambda value: f"{value} B",
            usage_rollup=lambda entries, user_id: "Rollup",
            copy_text_button=lambda label, value: {"text": label, "callback_data": value},
            promo_frequency_label=lambda value: value,
            device_api_url="https://app.invalid",
        )
        self.assertIn("ss://example", view.text)
        self.assertIn("Rollup", view.text)
        self.assertIn("n:pair", str(view.markup))


if __name__ == "__main__":
    unittest.main()
