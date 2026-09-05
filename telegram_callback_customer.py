"""Customer-facing callback action handlers."""

from __future__ import annotations

from typing import Any

from commerce import CommerceError


def handle_quota_alert_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    message = query.get("message") or {}
    try:
        current = self.service.quota_alert_preferences(telegram_id)
        changes: dict[str, Any] = {}
        if action == "e" and entity_id == "toggle":
            changes["enabled"] = not current["enabled"]
        elif action == "m" and entity_id in {"percent", "mb", "gb"}:
            changes["mode"] = entity_id
        elif action == "c" and entity_id in {"1", "2", "3"}:
            changes["alert_count"] = int(entity_id)
        elif action == "v" and entity_id.isdigit():
            changes["step_value"] = int(entity_id)
        else:
            raise ValueError
        self.service.set_quota_alert_preferences(telegram_id, **changes)
    except ValueError:
        self.send(chat_id, "That usage-alert setting is no longer valid.")
        return
    self._send_quota_alert_settings(
        chat_id,
        telegram_id,
        message_id=message_id if can_edit_text else None,
    )
    return

def handle_orders_page_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    message = query.get("message") or {}
    if scope == "c" and action == "o":
        selected, _, raw_page = entity_id.partition(":")
        try:
            page = max(0, int(raw_page or "0"))
        except ValueError:
            page = 0
        self._send_my_orders(
            chat_id,
            telegram_id,
            selected=selected,
            page=page,
            message_id=message_id if can_edit_text else None,
        )

def handle_promo_claim_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    message = query.get("message") or {}
    if scope == "g" and action == "c":
        synthetic["text"] = f"/claimpromo {entity_id.upper()}"
        self.handle(synthetic)

def handle_paid_key_list_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    message = query.get("message") or {}
    if scope == "k" and action == "l":
        selected, separator, raw_page = entity_id.partition(":")
        if not separator:
            selected, raw_page = "active", entity_id
        try:
            page = max(0, int(raw_page))
        except ValueError:
            page = 0
        message_id = message.get("message_id")
        self._send_paid_key_list(
            chat_id,
            telegram_id,
            page,
            selected=selected,
            message_id=message_id if isinstance(message_id, int) else None,
        )

def handle_paid_key_detail_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    message = query.get("message") or {}
    if scope == "k" and action == "v":
        message_id = message.get("message_id")
        self._send_paid_key_detail(
            chat_id,
            telegram_id,
            entity_id,
            message_id=message_id if isinstance(message_id, int) else None,
        )

def handle_order_detail_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    message = query.get("message") or {}
    if scope == "o" and action == "v":
        self._send_order_detail(
            chat_id,
            telegram_id,
            entity_id,
            message_id=message_id if can_edit_text else None,
        )

def handle_payment_chooser_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    message = query.get("message") or {}
    if scope == "o" and action == "p":
        self._send_payment_method_chooser(chat_id, telegram_id, entity_id)

def handle_payment_qr_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    message = query.get("message") or {}
    if scope == "m" and action == "s":
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

def handle_receipt_select_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    message = query.get("message") or {}
    if scope == "o" and action == "r":
        order = self.commerce.order_detail(entity_id, telegram_id) if self.commerce else None
        if order is None:
            self.send(chat_id, "Order not found.")
        else:
            self._expect_receipt_order(telegram_id, entity_id)
            self.send(
                chat_id,
                f"Send the receipt screenshot for order #{str(entity_id)[:8]} now.\n\n"
                "This button has selected the order, so no caption is needed. You can also "
                f"caption it with /paid {entity_id} if you prefer.",
                self._inline_keyboard([[("🔄 Refresh Order", f"o:v:{entity_id}")]]),
            )

def handle_receipt_upload_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    message = query.get("message") or {}
    if scope == "o" and action == "u":
        order = self.commerce.order_detail(entity_id, telegram_id) if self.commerce else None
        if order is None:
            self.send(chat_id, "Order not found.")
        elif not order.get("payment_method"):
            self._send_payment_method_chooser(chat_id, telegram_id, entity_id)
        else:
            self._expect_receipt_order(telegram_id, entity_id)
            self.send(
                chat_id,
                f"📷 Send the completed receipt screenshot for order #{str(entity_id)[:8]} now.\n\n"
                "No caption is needed. AuriX records the image for staff verification; "
                "the screenshot alone never activates payment.",
                self._inline_keyboard([[("🧾 View Order", f"o:v:{entity_id}")]]),
            )

def handle_wallet_payment_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    message = query.get("message") or {}
    if scope == "o" and action == "w":
        synthetic["text"] = f"/walletpay {entity_id}"
        self.handle(synthetic)

def handle_cancel_order_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    message = query.get("message") or {}
    if scope == "o" and action == "c":
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

def handle_cancel_order_confirm_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    message = query.get("message") or {}
    if scope == "o" and action == "x":
        synthetic["text"] = f"/cancelorder {entity_id}"
        self.handle(synthetic)

def handle_plan_action_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    message = query.get("message") or {}
    if scope == "p":
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

def handle_topup_action_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    message = query.get("message") or {}
    if scope == "t" and action == "a":
        if self.commerce is None:
            self.send(chat_id, "Wallet is not configured.")
        elif entity_id == "menu":
            synthetic["text"] = "/topup"
            self.handle(synthetic)
        elif entity_id == "custom":
            self._expect_customer_input(telegram_id, "topup_amount")
            self.send(
                chat_id,
                "Type a whole MMK amount from 1,000 to 1,000,000. Example: 7500",
            )
        else:
            synthetic["text"] = f"/topup {entity_id}"
            self.handle(synthetic)
