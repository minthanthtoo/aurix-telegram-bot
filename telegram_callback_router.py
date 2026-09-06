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

def _callback_actor(
    query: dict[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any], str, int, int] | None:
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
        return None
    return (
        query_id,
        user,
        message,
        chat,
        data,
        int(user["id"]),
        int(chat["id"]),
    )


def _handle_callback_navigation(
    host: Any,
    query: dict[str, Any],
    data: str,
    chat_id: int,
    telegram_id: int,
    message: dict[str, Any],
    synthetic: dict[str, Any],
) -> bool:
    if data == "n:adminorders":
        if not host._is_admin(telegram_id):
            host._send_customer_fallback(chat_id, telegram_id)
        else:
            synthetic["text"] = "/orders"
            host.handle(synthetic)
        return True
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
    message_id = message.get("message_id")
    can_edit_text = (
        isinstance(message_id, int)
        and not message.get("photo")
        and not message.get("document")
    )
    if can_edit_text and data in {"n:myvpn", "n:usage", "n:keytext", "n:myorders", "n:alerts"}:
        if data == "n:myorders":
            host._send_my_orders(chat_id, telegram_id, message_id=message_id)
        elif data == "n:alerts":
            host._send_quota_alert_settings(chat_id, telegram_id, message_id=message_id)
        else:
            host._send_my_vpn(
                chat_id,
                telegram_id,
                show_key_text=data == "n:keytext",
                message_id=message_id,
            )
        return True
    if data in navigation:
        synthetic["text"] = navigation[data]
        host.handle(synthetic)
        return True
    return False


def _dispatch_scoped_callback(
    host: Any,
    query: dict[str, Any],
    chat_id: int,
    telegram_id: int,
    message_id: Any,
    can_edit_text: bool,
    synthetic: dict[str, Any],
    scope: str,
    action: str,
    entity_id: str,
) -> None:
    if scope == "q":
        handle_quota_alert_callback(host, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "c" and action == "o":
        handle_orders_page_callback(host, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "g" and action == "c":
        handle_promo_claim_callback(host, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "k" and action == "l":
        handle_paid_key_list_callback(host, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "k" and action == "v":
        handle_paid_key_detail_callback(host, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "o" and action == "v":
        handle_order_detail_callback(host, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "o" and action == "p":
        handle_payment_chooser_callback(host, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "m" and action == "s":
        handle_payment_qr_callback(host, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "o" and action == "r":
        handle_receipt_select_callback(host, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "o" and action == "u":
        handle_receipt_upload_callback(host, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "o" and action == "w":
        handle_wallet_payment_callback(host, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "o" and action == "c":
        handle_cancel_order_callback(host, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "o" and action == "x":
        handle_cancel_order_confirm_callback(host, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "p":
        handle_plan_action_callback(host, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "t" and action == "a":
        handle_topup_action_callback(host, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif scope == "a":
        if not host._is_admin(telegram_id):
            host._send_customer_fallback(chat_id, telegram_id)
            return
        if action in {"p", "k", "d", "n"}:
            handle_admin_navigation_callback(host, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
        elif action in {"S", "I", "G", "H", "R", "C", "L"}:
            handle_admin_fleet_callback(host, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
        else:
            handle_admin_workflow_callback(host, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    else:
        host.send(chat_id, "This button is no longer valid. Refresh the menu.")


def dispatch_callback(self, query: dict[str, Any]) -> None:
    actor = _callback_actor(query)
    if actor is None:
        return
    query_id, user, message, _chat, data, telegram_id, chat_id = actor
    self.request("answerCallbackQuery", {"callback_query_id": query_id})
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
        "from": {"id": telegram_id, "first_name": first_name, "username": username},
    }
    if data.startswith("a:") and not self._is_admin(telegram_id):
        self._send_customer_fallback(chat_id, telegram_id)
        return
    if _handle_callback_navigation(
        self, query, data, chat_id, telegram_id, message, synthetic
    ):
        return
    message_id = message.get("message_id")
    can_edit_text = (
        isinstance(message_id, int)
        and not message.get("photo")
        and not message.get("document")
    )
    parts = data.split(":", 2)
    if len(parts) != 3:
        self.send(chat_id, "This button is no longer valid. Refresh the menu.")
        return
    scope, action, entity_id = parts
    _dispatch_scoped_callback(
        self,
        query,
        chat_id,
        telegram_id,
        message_id,
        can_edit_text,
        synthetic,
        scope,
        action,
        entity_id,
    )
