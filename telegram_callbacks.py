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
        elif scope == "g" and action == "c":
            synthetic["text"] = f"/claimpromo {entity_id.upper()}"
            self.handle(synthetic)
        elif scope == "k" and action == "l":
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
                self._expect_receipt_order(telegram_id, entity_id)
                self.send(
                    chat_id,
                    f"Send the receipt screenshot for order #{str(entity_id)[:8]} now.\n\n"
                    "This button has selected the order, so no caption is needed. You can also "
                    f"caption it with /paid {entity_id} if you prefer.",
                    self._inline_keyboard([[("🔄 Refresh Order", f"o:v:{entity_id}")]]),
                )
        elif scope == "o" and action == "u":
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
        elif scope == "t" and action == "a":
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
        elif scope == "a":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat_id, telegram_id)
                return
            if action == "p":
                if entity_id == "enqueue":
                    try:
                        self._admin_probe_call(telegram_id, "enqueue_due_probes", limit=100)
                    except Exception as exc:
                        self.send(chat_id, "Probe jobs could not be queued.", self._admin_keyboard(telegram_id))
                        print(f"probe enqueue error: {type(exc).__name__}", file=sys.stderr)
                    else:
                        self._show_probes(
                            chat_id,
                            telegram_id,
                            message_id=message_id if can_edit_text else None,
                        )
                else:
                    self.send(chat_id, "This probe action is no longer valid.")
            elif action == "k":
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
                    "notifications": "/notifications",
                    "orders": "/orders",
                    "receipts": "/receipts",
                    "capacity": "/capacity",
                    "probes": "/probes",
                    "prepare": "/capacity",
                    "reconcile": "/reconcile",
                    "failed": "/failed",
                    "repairs": "/repairs",
                    "migrations": "/migrations",
                    "failover": "/failover",
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
                elif entity_id == "admin":
                    self._send_admin_home(
                        chat_id,
                        telegram_id,
                        message_id=message_id if can_edit_text else None,
                    )
                elif entity_id == "owner":
                    self._send_owner_home(
                        chat_id,
                        telegram_id,
                        message_id=message_id if can_edit_text else None,
                    )
                elif entity_id == "notifications":
                    self._send_staff_notifications(
                        chat_id,
                        telegram_id,
                        message_id=message_id if can_edit_text else None,
                    )
                elif entity_id == "staff":
                    self._send_staff_panel(
                        chat_id,
                        telegram_id,
                        message_id=message_id if can_edit_text else None,
                    )
                elif entity_id in {"orders", "receipts", "failed", "repairs", "migrations", "failover", "enforcement"}:
                    if self.commerce is None and entity_id != "enforcement":
                        self.send(chat_id, "Commerce is not configured.")
                    else:
                        self._open_admin_panel(
                            chat_id,
                            telegram_id,
                            entity_id,
                            message_id=message.get("message_id"),
                        )
                elif entity_id == "capacity":
                    self._show_capacity(chat_id, telegram_id, message_id=message.get("message_id"))
                elif entity_id == "prepare":
                    try:
                        job_id = self._admin_call(
                            telegram_id,
                            "queue_infrastructure_provision",
                            telegram_id,
                        )
                        self.send(
                            chat_id,
                            "✅ Provisioning request queued. The infrastructure worker will "
                            "re-check capacity, budget and provider state before any change.",
                            self._inline_keyboard([[('📈 Capacity', 'a:n:capacity')]]),
                        )
                        self._show_capacity(
                            chat_id,
                            telegram_id,
                            message_id=message.get("message_id"),
                        )
                    except (CommerceError, ValueError, RuntimeError) as exc:
                        self.send(chat_id, str(exc) or "Provisioning request was not queued.")
                else:
                    synthetic["text"] = target
                    self.handle(synthetic)
            elif action == "S":
                self._show_server_allocation(
                    chat_id,
                    telegram_id,
                    entity_id,
                    message_id=message.get("message_id"),
                )
            elif action == "I":
                try:
                    server_id, status, raw_page = entity_id.split(":", 2)
                    page = max(0, int(raw_page))
                except (TypeError, ValueError):
                    self.send(chat_id, "That remote inventory view is no longer valid.")
                    return
                self._show_remote_inventory(
                    chat_id,
                    telegram_id,
                    server_id,
                    status=status,
                    page=page,
                    message_id=message.get("message_id")
                    if can_edit_text
                    else None,
                )
            elif action == "G":
                try:
                    source_server_id, mode, raw_value = entity_id.split(":", 2)
                    value = max(0, int(raw_value))
                except (TypeError, ValueError):
                    self.send(chat_id, "That migration view is no longer valid.")
                    return
                if mode == "p":
                    self._show_migration_candidates(
                        chat_id,
                        telegram_id,
                        source_server_id,
                        page=value,
                        message_id=message_id if can_edit_text else None,
                    )
                elif mode == "c":
                    self._show_migration_targets(
                        chat_id,
                        telegram_id,
                        source_server_id,
                        candidate_index=value,
                        page=0,
                        message_id=message_id if can_edit_text else None,
                    )
                else:
                    self.send(chat_id, "That migration view is no longer valid.")
            elif action == "H":
                if not self._is_owner(telegram_id):
                    self._send_customer_fallback(chat_id, telegram_id)
                    return
                try:
                    source_server_id, raw_index, target_server_id, raw_page = entity_id.split("|", 3)
                    candidate_index = max(0, int(raw_index))
                    page = max(0, int(raw_page))
                    candidates = list(
                        self._admin_call(
                            telegram_id,
                            "migratable_credentials",
                            source_server_id,
                        )
                        or []
                    )
                    candidate = candidates[candidate_index]
                    external_id = str(candidate.get("external_id") or "").strip()
                    if not external_id:
                        raise ValueError("credential identity missing")
                except Exception as exc:
                    self.send(chat_id, str(exc) or "That migration target is no longer valid.")
                    return
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/migratekey",
                    [source_server_id, external_id, target_server_id],
                    "Move this active credential to the selected healthy endpoint?",
                    "🔁 Confirm Key Migration",
                    cancel_data=f"a:G:{source_server_id}:p:{page}",
                )
            elif action == "R":
                if not self._is_owner(telegram_id):
                    self._send_customer_fallback(chat_id, telegram_id)
                    return
                try:
                    server_id, key_id, next_state, raw_page = entity_id.split("|", 3)
                    page = max(0, int(raw_page))
                    if next_state not in {"unreviewed", "accepted_external"}:
                        raise ValueError
                    self._admin_owner_call(
                        telegram_id,
                        "review_remote_key",
                        server_id,
                        key_id,
                        next_state,
                        telegram_id,
                        note="owner Telegram inventory action",
                    )
                except (CommerceError, PermissionError, ValueError) as exc:
                    self.send(chat_id, str(exc) or "Remote key review could not be saved.")
                    return
                self._show_remote_inventory(
                    chat_id,
                    telegram_id,
                    server_id,
                    status="present",
                    page=page,
                    message_id=message_id if can_edit_text else None,
                )
            elif action == "C":
                try:
                    server_id, field, raw_value = entity_id.split("|", 2)
                    value = int(raw_value)
                except (ValueError, TypeError):
                    self.send(chat_id, "That capacity control is no longer valid.")
                    return
                snapshot = self._admin_call(telegram_id, "capacity_snapshot")
                server = next(
                    (
                        item
                        for item in snapshot.get("servers", [])
                        if str(item["server_id"]) == server_id
                    ),
                    None,
                )
                if server is None:
                    self.send(chat_id, "That Outline server is unavailable.")
                    return
                if field in {"keys", "reserve", "traffic"}:
                    self._admin_call(
                        telegram_id,
                        "configure_server_capacity",
                        server_id,
                        telegram_id,
                        max_keys=value if field == "keys" else server.get("max_keys"),
                        reserved_keys=value
                        if field == "reserve"
                        else int(server.get("reserved_keys") or 0),
                        monthly_traffic_bytes=(
                            value * 1_000_000_000
                            if field == "traffic"
                            else server.get("monthly_traffic_bytes")
                        ),
                    )
                elif field in {"FREE300MB", "FREE3GB", "PROMO"}:
                    self._admin_call(
                        telegram_id,
                        "configure_tier_allocation",
                        server_id,
                        field,
                        value,
                        telegram_id,
                    )
                else:
                    self._admin_call(
                        telegram_id,
                        "configure_plan_allocation",
                        server_id,
                        field,
                        value,
                        telegram_id,
                    )
                self._show_server_allocation(
                    chat_id,
                    telegram_id,
                    server_id,
                    message_id=message.get("message_id"),
                )
            elif action == "L":
                if not self._is_owner(telegram_id):
                    self._send_customer_fallback(chat_id, telegram_id)
                    return
                try:
                    server_id, requested_state = entity_id.split("|", 1)
                    requested_state = requested_state.lower()
                except ValueError:
                    self.send(chat_id, "That endpoint lifecycle action is no longer valid.")
                    return
                if requested_state not in {"active", "draining", "retired"} or not server_id:
                    self.send(chat_id, "That endpoint lifecycle action is no longer valid.")
                    return
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/serverstate",
                    [server_id, requested_state],
                    (
                        f"Change endpoint {server_id} to {requested_state}? "
                        "This changes AuriX admission only; it never destroys a VM or key."
                    ),
                    "✅ Confirm Endpoint State",
                    cancel_data=f"a:S:{server_id}",
                )
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
            elif action == "s":
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
            elif action == "o":
                self._send_order_detail(
                    chat_id,
                    telegram_id,
                    entity_id,
                    admin_view=True,
                    message_id=message_id if can_edit_text else None,
                )
            elif action == "j":
                if not self._is_owner(telegram_id):
                    self._send_customer_fallback(chat_id, telegram_id)
                    return
                try:
                    repair_id, approval_mode = entity_id.split(":", 1)
                except ValueError:
                    self.send(chat_id, "That repair action is no longer valid.")
                    return
                if approval_mode not in {"safe", "full"} or not repair_id:
                    self.send(chat_id, "That repair action is no longer valid.")
                    return
                self._queue_admin_confirmation(
                    chat_id,
                    telegram_id,
                    "/approverepair",
                    [repair_id, *( ["full"] if approval_mode == "full" else [] )],
                    (
                        f"Approve managed-key repair {repair_id[:16]} while preserving observed usage?"
                        if approval_mode == "safe"
                        else f"Approve managed-key repair {repair_id[:16]} with explicit full-quota restoration?"
                    ),
                    "✅ Confirm Repair" if approval_mode == "safe" else "⚠️ Confirm Full Quota",
                    cancel_data="a:n:repairs",
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
                self._receipt_verify_inputs.pop(telegram_id, None)
                self._clear_interaction_state(telegram_id, "receipt_verify")
                synthetic["text"] = f"/receipt {entity_id}"
                self.handle(synthetic)
            elif action == "v":
                receipt = self._admin_call(telegram_id, "get_receipt", entity_id)
                if receipt is None or receipt.get("review_status") != "pending":
                    self.send(chat_id, "This receipt is no longer awaiting verification.")
                    return
                extracted = receipt.get("extraction") or {}
                reference = str(extracted.get("transaction_id") or "").strip()
                amount_value = extracted.get("amount_minor", extracted.get("amount"))
                try:
                    amount = int(str(amount_value).replace(",", ""))
                except (TypeError, ValueError):
                    amount = 0
                if reference and amount > 0:
                    self._queue_admin_confirmation(
                        chat_id,
                        telegram_id,
                        "/verify",
                        [entity_id, reference, str(amount)],
                        "Confirm that these extracted details match the actual receiving account.",
                        "✅ I Checked · Verify",
                        f"a:r:{entity_id}",
                    )
                else:
                    self._receipt_verify_inputs[telegram_id] = entity_id
                    self._save_interaction_state(
                        telegram_id, "receipt_verify", {"evidence_id": entity_id}
                    )
                    self.send(
                        chat_id,
                        "🔎 Check the actual receiving account, then reply with only:\n"
                        "transaction-ID amount\n\nExample: 123456789 3000\n"
                        "The receipt/order ID is already selected for you.",
                        self._inline_keyboard([[("Cancel", f"a:r:{entity_id}")]]),
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
