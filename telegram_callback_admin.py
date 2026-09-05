"""Administrator callback action handlers."""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from typing import Any

from commerce import CommerceError

UTC = timezone.utc


def handle_admin_navigation_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    message = query.get("message") or {}
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

def handle_admin_fleet_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    message = query.get("message") or {}
    if action == "S":
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
    message = query.get("message") or {}
    if action == "o":
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

def handle_admin_workflow_callback(self, query: dict[str, Any], chat_id: int, telegram_id: int, message_id: Any, can_edit_text: bool, synthetic: dict[str, Any], action: str, entity_id: str, scope: str) -> None:
    if action in {"m", "t"}:
        handle_admin_receipt_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    elif action in {"s", "u"}:
        handle_admin_staff_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
    else:
        handle_admin_order_callback(self, query, chat_id, telegram_id, message_id, can_edit_text, synthetic, action, entity_id, scope)
