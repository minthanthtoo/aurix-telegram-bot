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
        if data.startswith("c2:"):
            panel_parts = data.split(":", 3)
            if len(panel_parts) == 3:
                panel_parts.append("")
            if len(panel_parts) == 4 and self._handle_customer_panel_callback(
                query, panel_parts[1], panel_parts[2], panel_parts[3] or None
            ):
                return
            self.send(chat_id, "This panel expired. Open My VPN or My Orders again.")
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
        elif scope == "o" and action == "v":
            self._send_order_detail(chat_id, telegram_id, entity_id)
        elif scope == "o" and action == "r":
            order = self.commerce.order_detail(entity_id, telegram_id) if self.commerce else None
            if order is None:
                self.send(chat_id, "Order not found.")
            else:
                self._send_payment_methods(chat_id, order)
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
        elif scope == "t":
            if self.commerce is None:
                self.send(chat_id, "Wallet and payments are not configured.")
                return
            if action == "a":
                if entity_id == "menu":
                    synthetic["text"] = "/topup"
                    self.handle(synthetic)
                    return
                if entity_id == "custom":
                    self._expect_customer_input(telegram_id, "topup_amount")
                    self.send(
                        chat_id,
                        "Type the wallet amount in MMK as a whole number (1,000–1,000,000).\n"
                        "Example: 7500",
                    )
                    return
                synthetic["text"] = f"/topup {entity_id}"
                self.handle(synthetic)
            elif action == "m":
                order = self.commerce.order_detail(entity_id, telegram_id)
                if order is None:
                    self.send(chat_id, "Order not found.")
                else:
                    self._send_payment_methods(chat_id, order)
            elif action == "p":
                try:
                    order_id, provider_code = entity_id.rsplit(":", 1)
                    order = self.commerce.select_payment_provider(
                        telegram_id, order_id, provider_code
                    )
                except (ValueError, CommerceError) as exc:
                    self.send(chat_id, str(exc) or "Payment method could not be selected.")
                    return
                self._send_payment_qr(chat_id, order, provider_code)
            else:
                self.send(chat_id, "This top-up action is no longer valid.")
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
                elif entity_id == "capacity":
                    self._show_capacity(chat_id, telegram_id, message.get("message_id"))
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
            elif action == "e":
                try:
                    operation, endpoint_id = entity_id.split(",", 1)
                    snapshot = self._admin_call(telegram_id, "capacity_snapshot")
                    endpoint = next(
                        item
                        for item in snapshot.get("endpoints", [])
                        if str(item.get("id")) == endpoint_id
                    )
                    current_limit = endpoint.get("max_active_keys")
                    accepting = bool(endpoint.get("accepts_new_assignments"))
                    if operation == "t":
                        accepting = not accepting
                    elif operation == "m":
                        limits = [None, 25, 50, 100]
                        try:
                            index = limits.index(current_limit)
                        except ValueError:
                            index = 0
                        current_limit = limits[(index + 1) % len(limits)]
                    else:
                        raise ValueError("unknown endpoint action")
                    self._admin_call(
                        telegram_id,
                        "configure_endpoint_capacity",
                        endpoint_id,
                        telegram_id,
                        max_active_keys=current_limit,
                        accepts_new_assignments=accepting,
                    )
                except Exception as exc:
                    self.send(chat_id, str(exc) or "Endpoint capacity could not be updated.")
                    return
                self._show_capacity(chat_id, telegram_id, message.get("message_id"))
            elif action == "q":
                try:
                    self._show_endpoint_plans(
                        chat_id, telegram_id, entity_id, message.get("message_id")
                    )
                except Exception as exc:
                    self.send(chat_id, str(exc) or "Endpoint plans are temporarily unavailable.")
            elif action == "l":
                try:
                    plan_code, endpoint_id = entity_id.split(",", 1)
                    plans = self._admin_call(
                        telegram_id, "endpoint_plan_capacity", endpoint_id
                    )
                    plan = next(
                        item for item in plans if str(item.get("plan_code")) == plan_code
                    )
                    current = (
                        plan.get("max_active_assignments") if bool(plan.get("enabled")) else 0
                    )
                    limits = [None, 5, 10, 25, 50, 0]
                    try:
                        index = limits.index(current)
                    except ValueError:
                        index = 0
                    selected = limits[(index + 1) % len(limits)]
                    self._admin_call(
                        telegram_id,
                        "configure_endpoint_plan_limit",
                        endpoint_id,
                        plan_code,
                        telegram_id,
                        max_active_assignments=None if selected == 0 else selected,
                        enabled=selected != 0,
                    )
                    self._show_endpoint_plans(
                        chat_id, telegram_id, endpoint_id, message.get("message_id")
                    )
                except Exception as exc:
                    self.send(chat_id, str(exc) or "Plan capacity could not be updated.")
            elif action == "o":
                self._send_order_detail(chat_id, telegram_id, entity_id, admin_view=True)
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
            elif action == "v":
                receipt = self._admin_call(telegram_id, "get_receipt", entity_id)
                extracted = (receipt or {}).get("extraction") or {}
                transaction_id = str(extracted.get("transaction_id") or "").strip()
                amount = extracted.get("amount_minor")
                flags = list(extracted.get("flags") or [])
                if not receipt or not transaction_id or not amount or flags:
                    self.send(
                        chat_id,
                        "Extracted facts are incomplete or flagged. Verify manually against the "
                        "receiving account.",
                    )
                    return
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/verify",
                    [entity_id, transaction_id, str(int(amount))],
                    "Confirm these extracted facts only after matching the receiving account?",
                    "✅ Confirm Verification",
                    f"a:r:{entity_id}",
                )
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
