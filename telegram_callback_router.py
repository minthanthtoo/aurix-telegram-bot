"""Telegram callback-query routing."""

from __future__ import annotations

from typing import Any

from telegram_callback_admin import (
    handle_admin_fleet_callback,
    handle_admin_navigation_callback,
    handle_admin_workflow_callback,
)
from telegram_callback_customer import (
    handle_quota_alert_callback,
    handle_orders_page_callback,
    handle_promo_claim_callback,
    handle_paid_key_list_callback,
    handle_paid_key_detail_callback,
    handle_order_detail_callback,
    handle_payment_chooser_callback,
    handle_payment_qr_callback,
    handle_receipt_select_callback,
    handle_receipt_upload_callback,
    handle_wallet_payment_callback,
    handle_cancel_order_callback,
    handle_cancel_order_confirm_callback,
    handle_plan_action_callback,
    handle_topup_action_callback,
)

def dispatch_callback(self, query: dict[str, Any]) -> None:
    query_id = query.get("id")
    user = query.get("from") or {}
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    data = query.get("data")
    if (
        not isinstance(query_id, str)
        or not isinstance(data, str)
        or not isinstance(user.get("id"), int)
        or not isinstance(chat.get("id"), int)
        or chat.get("type") != "private"
        or int(chat.get("id")) != int(user.get("id"))
    ):
        return
    self.request("answerCallbackQuery", {"callback_query_id": query_id})
    telegram_id = int(user["id"])
    chat_id = int(chat["id"])
    if data.startswith("v2:"):
        panel_parts = data.split(":", 3)
        if len(panel_parts) == 3:
            panel_parts.append("")
        if len(panel_parts) == 4 and self._handle_panel_callback(
            query, panel_parts[1], panel_parts[2], panel_parts[3] or None
        ):
            return
        self.send(chat_id, "This panel has expired. Open the admin menu again.")
        return
    first_name = str(user.get("first_name") or "")
    username = user.get("username") if isinstance(user.get("username"), str) else None
    synthetic = {
        "chat": {"id": chat_id, "type": "private"},
        "from": {
            "id": telegram_id,
            "first_name": first_name,
            "username": username,
        },
    }
    if data.startswith("a:") and not self._is_admin(telegram_id):
        self._send_customer_fallback(chat_id, telegram_id)
        return
    navigation = {
        "n:myorders": "/myorders",
        "n:myvpn": "/myvpn",
        "n:plans": "/plans",
        "n:wallet": "/wallet",
        "n:usage": "/myvpn",
        "n:claim": "/claim",
        "n:trial": "/trial",
        "n:start": "/start",
        "n:menu": "/help",
        "n:keytext": "/keysastext",
        "n:alerts": "/alerts",
        "n:pair": "/pair",
    }
    # Legacy admin navigation buttons may still exist in Telegram message
    # history. Keep them safe and role-gated while no longer generating
    # them for new messages.
    if data == "n:adminorders":
        if not self._is_admin(telegram_id):
            self._send_customer_fallback(chat_id, telegram_id)
        else:
            synthetic["text"] = "/orders"
            self.handle(synthetic)
        return
    message_id = message.get("message_id")
    can_edit_text = (
        isinstance(message_id, int) and not message.get("photo") and not message.get("document")
    )
    if can_edit_text and data in {"n:myvpn", "n:usage", "n:keytext", "n:myorders", "n:alerts"}:
        if data == "n:myorders":
            self._send_my_orders(chat_id, telegram_id, message_id=message_id)
        elif data == "n:alerts":
            self._send_quota_alert_settings(chat_id, telegram_id, message_id=message_id)
        else:
            self._send_my_vpn(
                chat_id,
                telegram_id,
                show_key_text=data == "n:keytext",
                message_id=message_id,
            )
        return
    if data in navigation:
        synthetic["text"] = navigation[data]
        self.handle(synthetic)
        return
    parts = data.split(":", 2)
    if len(parts) != 3:
        self.send(chat_id, "This button is no longer valid. Refresh the menu.")
        return
    scope, action, entity_id = parts
    if scope == "q":
        handle_quota_alert_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "c" and action == "o":
        handle_orders_page_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "g" and action == "c":
        handle_promo_claim_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "k" and action == "l":
        handle_paid_key_list_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "k" and action == "v":
        handle_paid_key_detail_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "o" and action == "v":
        handle_order_detail_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "o" and action == "p":
        handle_payment_chooser_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "m" and action == "s":
        handle_payment_qr_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "o" and action == "r":
        handle_receipt_select_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "o" and action == "u":
        handle_receipt_upload_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "o" and action == "w":
        handle_wallet_payment_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "o" and action == "c":
        handle_cancel_order_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "o" and action == "x":
        handle_cancel_order_confirm_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "p":
        handle_plan_action_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "t" and action == "a":
        handle_topup_action_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "a":
        if not self._is_admin(telegram_id):
            self._send_customer_fallback(chat_id, telegram_id)
            return
        if action in {"p", "k", "d", "n"}:
            handle_admin_navigation_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
        elif action in {"S", "I", "G", "H", "R", "C", "L"}:
            handle_admin_fleet_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
        else:
            handle_admin_workflow_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    else:
        self.send(chat_id, "This button is no longer valid. Refresh the menu.")
