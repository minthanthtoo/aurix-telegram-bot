"""Customer VPN dashboard presentation workflow."""

from __future__ import annotations

from telegram_customer_vpn_dashboard_data import collect_dashboard_data
from telegram_vpn_view import render_vpn_dashboard


class TelegramCustomerVpnDashboardMixin:
    def _send_my_vpn(
        self,
        chat_id: int,
        telegram_id: int,
        *,
        show_key_text: bool = False,
        message_id: int | None = None,
    ) -> None:
        """Render keys, lifecycle state, usage, and next actions in one dashboard."""
        (
            entries,
            giveaway,
            subscriptions,
            open_order,
            usage_available,
            access_available,
        ) = collect_dashboard_data(self, telegram_id)

        view = render_vpn_dashboard(
            entries, giveaway, subscriptions, open_order,
            usage_available=usage_available, access_available=access_available,
            show_key_text=show_key_text, telegram_id=telegram_id,
            format_bytes=self._format_bytes, format_decimal_bytes=self._format_decimal_bytes,
            usage_rollup=self._usage_rollup_block, copy_text_button=self._copy_text_button,
            promo_frequency_label=self._promo_frequency_label, device_api_url=self.device_api_url,
        )
        if message_id is not None:
            self.edit_message(chat_id, message_id, view.text, view.markup)
        else:
            self.send(chat_id, view.text, view.markup)
