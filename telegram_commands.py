"""Telegram message and command routing."""

from __future__ import annotations

from typing import Any

from telegram_admin_confirmation import intercept_admin_confirmation
from telegram_approval_commands import dispatch_approval_command
from telegram_command_context import prepare_command
from telegram_customer_commerce_commands import dispatch_customer_commerce_command
from telegram_customer_access_commands import dispatch_customer_access_command
from telegram_customer_commands import dispatch_onboarding
from telegram_free_claim_commands import dispatch_free_claim_command
from telegram_operations_commands import dispatch_operations_command
from telegram_promo_admin_commands import dispatch_promo_admin_command
from telegram_receipt_admin_commands import dispatch_receipt_admin_command
from telegram_receipt_review_commands import dispatch_receipt_review_command
from telegram_staff_commands import dispatch_staff_command


class TelegramCommandMixin:
    CONTROL_GROUP_REQUEST_ID = 60421

    def handle(self, message: dict[str, Any]) -> None:
        context = prepare_command(self, message)
        if context is None:
            return
        if intercept_admin_confirmation(self, context):
            return
        if dispatch_staff_command(self, context):
            return
        if dispatch_receipt_admin_command(self, context):
            return
        if dispatch_receipt_review_command(self, context):
            return
        if dispatch_promo_admin_command(self, context):
            return
        if dispatch_onboarding(self, context):
            return
        if dispatch_customer_access_command(self, context):
            return
        if dispatch_customer_commerce_command(self, context):
            return
        if dispatch_operations_command(self, context):
            return
        if dispatch_approval_command(self, context):
            return
        if dispatch_free_claim_command(self, context):
            return
        self._send_customer_fallback(context.chat_id, context.telegram_id)

    def _send_control_group_picker(self, chat_id: int) -> None:
        self.send(
            chat_id,
            "🏢 Choose the AuriX control group\n\n"
            "Telegram will show only groups where this bot is already a member. "
            "AuriX will verify that you are the group creator before saving it.",
            {
                "keyboard": [
                    [
                        {
                            "text": "🏢 Choose Control Group",
                            "request_chat": {
                                "request_id": self.CONTROL_GROUP_REQUEST_ID,
                                "chat_is_channel": False,
                                "bot_is_member": True,
                                "request_title": True,
                                "request_username": True,
                            },
                        }
                    ]
                ],
                "resize_keyboard": True,
                "one_time_keyboard": True,
                "input_field_placeholder": "Tap Choose Control Group",
            },
        )

    def _handle_control_group_shared(
        self,
        message: dict[str, Any],
        chat_id: int,
        telegram_id: int,
    ) -> None:
        shared = message.get("chat_shared") or {}
        if not self._is_owner(telegram_id) or self.staff_access is None:
            self._send_customer_fallback(chat_id, telegram_id)
            return
        if (
            shared.get("request_id") != self.CONTROL_GROUP_REQUEST_ID
            or not isinstance(shared.get("chat_id"), int)
            or int(shared["chat_id"]) >= 0
        ):
            self.send(
                chat_id,
                "That group selection could not be verified. Open Owner Controls and choose it again.",
                {"remove_keyboard": True},
            )
            return
        group_id = int(shared["chat_id"])
        try:
            group_owner, group_admins = self._control_group_staff(group_id)
            if group_owner is None or int(group_owner.get("id") or 0) != telegram_id:
                raise PermissionError(
                    "Your AuriX owner account must also be the Telegram group creator"
                )
            chat_info = self.request("getChat", {"chat_id": group_id})
            member_count = self.request("getChatMemberCount", {"chat_id": group_id})
            title = (
                str(chat_info.get("title") or "").strip() if isinstance(chat_info, dict) else ""
            ) or str(shared.get("title") or "").strip()
            self.staff_access.bind_control_group(group_id, telegram_id, title=title)
            self.control_group_id = group_id
            self.staff_access.bootstrap(
                owner_id=None,
                admin_ids=(),
                group_owner=group_owner,
                group_admins=group_admins,
            )
            self._refresh_staff_scopes()
        except Exception as exc:
            self.send(
                chat_id,
                f"Control-group setup was refused: {str(exc)[:240]}",
                {"remove_keyboard": True},
            )
            return
        active_admins = sum(
            1 for item in self.staff_access.list_staff() if item.get("role") == "admin"
        )
        self.send(
            chat_id,
            "✅ AuriX control group connected\n\n"
            f"Group: {title or group_id}\n"
            f"Telegram members: {int(member_count)} (includes the AuriX bot)\n"
            "Human owner: 1 verified — you\n"
            f"Additional human administrators: {active_admins}\n"
            "Bot accounts imported as staff: 0\n\n"
            "Telegram does not expose a full ordinary-member list to bots. "
            "AuriX can read the member count, creator/admins, and verify a specific member when needed.",
            {"remove_keyboard": True},
        )
