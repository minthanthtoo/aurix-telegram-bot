"""Administrator callback action handlers."""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from typing import Any

from commerce import CommerceError
from telegram_callback_admin_fleet import dispatch_fleet_action
from telegram_callback_admin_navigation import dispatch_navigation_action
from telegram_callback_admin_orders import dispatch_order_action

UTC = timezone.utc


def handle_admin_navigation_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    dispatch_navigation_action(
        self,
        query,
        chat_id,
        telegram_id,
        message_id,
        can_edit_text,
        synthetic,
        action,
        entity_id,
    )

def handle_admin_fleet_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    dispatch_fleet_action(
        self,
        query,
        chat_id,
        telegram_id,
        message_id,
        can_edit_text,
        action,
        entity_id,
    )

def handle_admin_receipt_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    message = query.get("message") or {}
    if action == "m":
        if entity_id not in {"manual", "assisted"}:
            self.send(chat_id, "That receipt mode is unavailable.")
            return
        self._queue_admin_confirmation(
            chat_id,
            telegram_id,
            "/receiptmode",
            [entity_id],
            f"Change receipt workflow to {entity_id}?",
            "Confirm Mode Change",
            cancel_data="a:n:receiptsystem",
        )
    elif action == "t":
        if entity_id.startswith("method:"):
            provider = entity_id.split(":", 1)[1]
            if provider not in self.PAYMENT_METHODS:
                self.send(chat_id, "That payment method is unavailable.")
                return
            self._receipt_test_providers[telegram_id] = provider
            self._receipt_test_waiting.add(telegram_id)
            self._save_interaction_state(
                telegram_id, "receipt_test", {"provider": provider}
            )
            self.send(
                chat_id,
                f"🧪 {self.PAYMENT_METHODS[provider]['label']} test ready\n\n"
                "Send one actual completed receipt image now. The original is used only "
                "for this isolated diagnostic and temporary storage is deleted afterward.",
                self._inline_keyboard([[("Cancel Test", "a:t:cancel")]]),
            )
        elif entity_id == "start":
            synthetic["text"] = "/receipttest"
            self.handle(synthetic)
        elif entity_id == "last":
            last = self._admin_call(telegram_id, "last_receipt_diagnostic")
            self._send_receipt_diagnostic_result(chat_id, telegram_id, last)
        elif entity_id == "details":
            last = self._admin_call(telegram_id, "last_receipt_diagnostic")
            if not last:
                self.send(chat_id, "No completed receipt test is available yet.")
                return
            result = last.get("result") or {}
            llm = result.get("llm") or {}
            raw = str(llm.get("raw_response") or "not available")[:3000]
            self.send(
                chat_id,
                "📋 Receipt Test · Technical Details\n\n"
                f"Run: {str(last.get('id') or '-')[:12]}\n"
                f"Status: {last.get('status') or '-'}\n"
                f"Host: {self._mask_technical_value(llm.get('endpoint_host'))}\n"
                f"Model: {llm.get('model') or '-'}\n"
                f"HTTP: {llm.get('http_status') or '-'}\n"
                f"Request ID: {self._mask_technical_value(llm.get('provider_request_id'), 6, 4)}\n"
                f"Latency: {llm.get('duration_ms') or '-'} ms\n"
                f"Validated: {llm.get('validated', False)}\n\n"
                "Sanitized, bounded LLM response:\n"
                f"{raw}",
                self._receipt_system_keyboard(),
            )
        elif entity_id == "cancel":
            self._receipt_test_waiting.discard(telegram_id)
            self._receipt_test_providers.pop(telegram_id, None)
            self._clear_interaction_state(telegram_id, "receipt_test")
            self.send(chat_id, "Receipt test cancelled.", self._receipt_system_keyboard())
        else:
            self.send(chat_id, "That diagnostic action is no longer valid.")

def handle_admin_staff_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    message = query.get("message") or {}
    if action == "s":
        if not self._is_owner(telegram_id):
            self._send_customer_fallback(chat_id, telegram_id)
            return
        if entity_id == "group":
            self._send_control_group_picker(chat_id)
            return
        if entity_id == "add":
            self._admin_add_waiting.add(telegram_id)
            self._save_interaction_state(telegram_id, "admin_add", {})
            self.send(
                chat_id,
                "➕ Add Administrator\n\nSend the numeric Telegram ID shown by that person's /whoami. Access is not granted until you review and confirm the next screen.",
                {"force_reply": True, "input_field_placeholder": "Telegram numeric ID"},
            )
            return
        try:
            staff_action, staff_id = entity_id.split(":", 1)
        except ValueError:
            self.send(chat_id, "That staff action is no longer valid.")
            return
        if staff_action != "remove" or not staff_id.isdigit():
            self.send(chat_id, "That staff action is no longer valid.")
            return
        self._queue_admin_confirmation(
            chat_id,
            telegram_id,
            "/removeadmin",
            [staff_id],
            f"Revoke administrator {staff_id}? Their access and pending confirmations stop immediately.",
            "Confirm Remove Admin",
            cancel_data="a:n:staff",
        )
    elif action == "u":
        if self.staff_access is None:
            self.send(chat_id, "Staff notification controls are not configured.")
            return
        current = self.staff_access.notification_preferences(telegram_id)
        if entity_id not in current:
            self.send(chat_id, "That notification type is unavailable.")
            return
        self.staff_access.set_notification_preference(
            telegram_id, entity_id, not current[entity_id]
        )
        self._send_staff_notifications(
            chat_id,
            telegram_id,
            message_id=message_id if can_edit_text else None,
        )

def handle_admin_order_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    dispatch_order_action(
        self,
        query,
        chat_id,
        telegram_id,
        message_id,
        can_edit_text,
        synthetic,
        action,
        entity_id,
    )

def handle_admin_workflow_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    if action in {"m", "t"}:
        handle_admin_receipt_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif action in {"s", "u"}:
        handle_admin_staff_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    else:
        handle_admin_order_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
