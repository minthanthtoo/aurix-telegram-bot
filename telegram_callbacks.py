"""Telegram callback-query routing."""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from typing import Any

from commerce import CommerceError

UTC = timezone.utc


class TelegramCallbackMixin:
    def handle_callback(self, query: dict[str, Any]) -> None:
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
        can_edit_text = isinstance(message_id, int) and not message.get("photo") and not message.get(
            "document"
        )
        if can_edit_text and data in {"n:myvpn", "n:usage", "n:keytext", "n:myorders"}:
            if data == "n:myorders":
                self._send_my_orders(chat_id, telegram_id, message_id=message_id)
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
        if scope == "g" and action == "c":
            synthetic["text"] = f"/claimpromo {entity_id.upper()}"
            self.handle(synthetic)
        elif scope == "k" and action == "l":
            try:
                page = max(0, int(entity_id))
            except ValueError:
                page = 0
            message_id = message.get("message_id")
            self._send_paid_key_list(
                chat_id,
                telegram_id,
                page,
                message_id=message_id if isinstance(message_id, int) else None,
            )
        elif scope == "k" and action == "v":
            message_id = message.get("message_id")
            self._send_paid_key_detail(
                chat_id,
                telegram_id,
                entity_id,
                message_id=message_id if isinstance(message_id, int) else None,
            )
        elif scope == "o" and action == "v":
            self._send_order_detail(
                chat_id,
                telegram_id,
                entity_id,
                message_id=message_id if can_edit_text else None,
            )
        elif scope == "o" and action == "p":
            self._send_payment_method_chooser(chat_id, telegram_id, entity_id)
        elif scope == "m" and action == "s":
            if ":" not in entity_id:
                self.send(chat_id, "This payment button has expired. Open the order again.")
                return
            method, order_id = entity_id.split(":", 1)
            if method not in self.PAYMENT_METHODS:
                self.send(chat_id, "That payment method is unavailable.")
                return
            try:
                self._show_payment_qr(query, chat_id, telegram_id, order_id, method)
            except CommerceError as exc:
                self.send(chat_id, str(exc), self._customer_keyboard(telegram_id))
            except (OSError, RuntimeError):
                self.send(
                    chat_id,
                    "That payment QR is temporarily unavailable. Choose another method or contact AuriX support.",
                    self._payment_method_keyboard(order_id),
                )
        elif scope == "o" and action == "r":
            order = self.commerce.order_detail(entity_id, telegram_id) if self.commerce else None
            if order is None:
                self.send(chat_id, "Order not found.")
            else:
                self.send(
                    chat_id,
                    f"Send the receipt screenshot now. Caption it with:\n/paid {entity_id}",
                    self._inline_keyboard([[("🔄 Refresh Order", f"o:v:{entity_id}")]]),
                )
        elif scope == "o" and action == "u":
            order = self.commerce.order_detail(entity_id, telegram_id) if self.commerce else None
            if order is None:
                self.send(chat_id, "Order not found.")
            elif not order.get("payment_method"):
                self._send_payment_method_chooser(chat_id, telegram_id, entity_id)
            else:
                self.send(
                    chat_id,
                    f"📷 Send the completed receipt screenshot for order #{str(entity_id)[:8]} now.\n\n"
                    "No caption is needed. AuriX records the image for staff verification; "
                    "the screenshot alone never activates payment.",
                    self._inline_keyboard([[('🧾 View Order', f"o:v:{entity_id}")]]),
                )
        elif scope == "o" and action == "w":
            synthetic["text"] = f"/walletpay {entity_id}"
            self.handle(synthetic)
        elif scope == "o" and action == "c":
            order = self.commerce.order_detail(entity_id, telegram_id) if self.commerce else None
            if order is None:
                self.send(chat_id, "Order not found.")
            else:
                self.send(
                    chat_id,
                    f"Cancel untouched order {entity_id}?",
                    self._inline_keyboard(
                        [
                            [
                                ("Confirm Cancel", f"o:x:{entity_id}"),
                                ("Keep Order", f"o:v:{entity_id}"),
                            ]
                        ]
                    ),
                )
        elif scope == "o" and action == "x":
            synthetic["text"] = f"/cancelorder {entity_id}"
            self.handle(synthetic)
        elif scope == "p":
            if action == "b":
                synthetic["text"] = f"/buy {entity_id}"
            elif action == "t":
                synthetic["text"] = "/trial"
            elif action == "r":
                synthetic["text"] = f"/renew {entity_id}" if entity_id else "/renew"
            elif action == "x":
                if ":" in entity_id:
                    source, target_plan = entity_id.split(":", 1)
                    synthetic["text"] = f"/replace {target_plan} {source}"
                else:
                    synthetic["text"] = f"/replace {entity_id}"
            else:
                self.send(chat_id, "This plan action is no longer valid.")
                return
            self.handle(synthetic)
        elif scope == "a":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat_id, telegram_id)
                return
            if action == "k":
                challenge = self._consume_admin_confirmation(chat_id, telegram_id, entity_id)
                if challenge is None:
                    self.send(
                        chat_id,
                        "This confirmation has expired or was already used. Open the admin panel again.",
                        self._admin_keyboard(telegram_id),
                    )
                else:
                    synthetic["text"] = " ".join([challenge["command"], *challenge["args"]])
                    synthetic["_admin_confirmed"] = True
                    self.handle(synthetic)
            elif action == "d":
                token_hash = hashlib.sha256(entity_id.encode()).hexdigest()
                store = getattr(self.service, "database", None)
                cancelled = False
                if callable(getattr(store, "cancel_admin_challenge", None)):
                    try:
                        cancelled = bool(
                            store.cancel_admin_challenge(
                                token_hash,
                                int(telegram_id),
                                int(chat_id),
                                datetime.now(UTC).isoformat(),
                            )
                        )
                    except Exception as exc:
                        print(
                            f"admin confirmation cancel error: {type(exc).__name__}",
                            file=sys.stderr,
                        )
                else:
                    with self._admin_confirmation_lock:
                        challenge = self._admin_confirmations.get(entity_id)
                        if (
                            challenge
                            and challenge["chat_id"] == chat_id
                            and challenge["telegram_id"] == telegram_id
                        ):
                            del self._admin_confirmations[entity_id]
                            cancelled = True
                self.send(
                    chat_id,
                    "Confirmation cancelled."
                    if cancelled
                    else "This confirmation is no longer valid.",
                    self._admin_keyboard(telegram_id),
                )
            elif action == "n":
                admin_navigation = {
                    "admin": "/admin",
                    "owner": "/owner",
                    "staff": "/staff",
                    "groupsync": "/groupsync",
                    "receiptsystem": "/receiptsystem",
                    "orders": "/orders",
                    "receipts": "/receipts",
                    "capacity": "/capacity",
                    "reconcile": "/reconcile",
                    "failed": "/failed",
                    "enforcement": "/enforcement",
                    "promo": "/promo",
                }
                target = admin_navigation.get(entity_id)
                if target is None:
                    self.send(chat_id, "This admin action is no longer valid.")
                elif entity_id == "receiptsystem":
                    self._send_receipt_system(
                        chat_id,
                        telegram_id,
                        message_id=message_id if can_edit_text else None,
                    )
                elif entity_id in {"orders", "receipts", "failed", "enforcement"}:
                    if self.commerce is None and entity_id != "enforcement":
                        self.send(chat_id, "Commerce is not configured.")
                    else:
                        self._open_admin_panel(
                            chat_id,
                            telegram_id,
                            entity_id,
                            message_id=message.get("message_id"),
                        )
                else:
                    synthetic["text"] = target
                    self.handle(synthetic)
            elif action == "m":
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
                if entity_id == "start":
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
                        f"Host: {llm.get('endpoint_host') or '-'}\n"
                        f"Model: {llm.get('model') or '-'}\n"
                        f"HTTP: {llm.get('http_status') or '-'}\n"
                        f"Request ID: {str(llm.get('provider_request_id') or '-')[:128]}\n"
                        f"Latency: {llm.get('duration_ms') or '-'} ms\n"
                        f"Validated: {llm.get('validated', False)}\n\n"
                        "Sanitized, bounded LLM response:\n"
                        f"{raw}",
                        self._receipt_system_keyboard(),
                    )
                elif entity_id == "cancel":
                    self._receipt_test_waiting.discard(telegram_id)
                    self.send(chat_id, "Receipt test cancelled.", self._receipt_system_keyboard())
                else:
                    self.send(chat_id, "That diagnostic action is no longer valid.")
            elif action == "s":
                if not self._is_owner(telegram_id):
                    self._send_customer_fallback(chat_id, telegram_id)
                    return
                if entity_id == "group":
                    self._send_control_group_picker(chat_id)
                    return
                if entity_id == "add":
                    self._admin_add_waiting.add(telegram_id)
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
            elif action == "o":
                self._send_order_detail(
                    chat_id,
                    telegram_id,
                    entity_id,
                    admin_view=True,
                    message_id=message_id if can_edit_text else None,
                )
            elif action == "p":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/retryjob",
                    [entity_id],
                    f"Retry worker job {entity_id}?",
                    "Confirm Retry",
                )
            elif action == "g":
                try:
                    promo_action, promo_code = entity_id.split(":", 1)
                except ValueError:
                    self.send(chat_id, "This promo action is no longer valid.")
                    return
                command = "/stoppromo" if promo_action == "stop" else "/resumepromo"
                if promo_action not in {"stop", "resume"}:
                    self.send(chat_id, "This promo action is no longer valid.")
                    return
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    command,
                    [promo_code],
                    f"{promo_action.title()} promo {promo_code}?",
                    "Confirm Promo Change",
                    cancel_data="a:n:promo",
                )
            elif action == "h":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/retry",
                    [entity_id, "provision"],
                    f"Retry the failed provisioning job for order {entity_id}?",
                    "Confirm Retry",
                )
            elif action == "g":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/retry",
                    [entity_id, "revoke"],
                    f"Retry the failed revocation job for order {entity_id}?",
                    "Confirm Retry",
                )
            elif action == "l":
                synthetic["text"] = f"/ledger {entity_id}"
                self.handle(synthetic)
            elif action == "f":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/refund",
                    [entity_id],
                    f"Refund order {entity_id}? This credits the customer wallet and revokes paid access.",
                    "Confirm Refund",
                    f"a:o:{entity_id}",
                )
            elif action == "z":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/refund",
                    [entity_id],
                    f"Refund order {entity_id} to the customer wallet and revoke paid access?",
                    "Confirm Refund",
                    f"a:o:{entity_id}",
                )
            elif action == "r":
                synthetic["text"] = f"/receipt {entity_id}"
                self.handle(synthetic)
            elif action == "a":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/approve",
                    [entity_id],
                    f"Approve order {entity_id} and queue VPN provisioning?",
                    "Confirm Approve",
                    f"a:o:{entity_id}",
                )
            elif action == "x":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/reject",
                    [entity_id],
                    f"Reject order {entity_id}? This closes the order and notifies the customer.",
                    "Confirm Reject",
                    f"a:o:{entity_id}",
                )
            elif action == "q":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/rejectreceipt",
                    [entity_id],
                    f"Reject receipt {entity_id}? The order stays open for a replacement screenshot.",
                    "Confirm Reject Receipt",
                    f"a:r:{entity_id}",
                )
            elif action == "y":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/rejectreceipt",
                    [entity_id],
                    f"Reject receipt {entity_id} and request a replacement screenshot?",
                    "Confirm Reject Receipt",
                    f"a:r:{entity_id}",
                )
            elif action == "c":
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/reject",
                    [entity_id],
                    f"Reject order {entity_id} and notify the customer?",
                    "Confirm Reject",
                    f"a:o:{entity_id}",
                )
            else:
                self.send(chat_id, "This admin action is no longer valid.")
        else:
            self.send(chat_id, "This button is no longer valid. Refresh the menu.")
