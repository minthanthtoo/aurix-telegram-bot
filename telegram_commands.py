"""Telegram message and command routing."""

from __future__ import annotations

import sys
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape as html_escape
from typing import Any

from commerce import CommerceError
from entitlements import OutlineError
from telegram_formatting import format_user_datetime

UTC = timezone.utc


def _parse_promo_datetime(value: str) -> datetime:
    normalized = str(value).strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _promo_gb_to_bytes(value: str) -> int:
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Promo quota must be a number of decimal GB") from exc
    if not amount.is_finite():
        raise ValueError("Promo quota must be a finite number")
    return int(amount * Decimal(1_000_000_000))


class TelegramCommandMixin:
    CONTROL_GROUP_REQUEST_ID = 60421

    def handle(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        if (
            not isinstance(chat, dict)
            or not isinstance(user, dict)
            or chat.get("type") != "private"
            or not isinstance(chat.get("id"), int)
            or not isinstance(user.get("id"), int)
            or int(chat.get("id")) != int(user.get("id"))
        ):
            return
        telegram_id = user["id"]
        first_name = user.get("first_name") or ""
        if not isinstance(first_name, str):
            first_name = str(first_name)
        username = user.get("username")
        if username is not None and not isinstance(username, str):
            username = str(username)
        self.service.track_user(telegram_id, first_name, username=username)
        if isinstance(message.get("chat_shared"), dict):
            self._handle_control_group_shared(message, chat["id"], telegram_id)
            return
        if message.get("photo") or message.get("document"):
            persisted_receipt_test = None
            if self._is_admin(telegram_id) and telegram_id not in self._receipt_test_waiting:
                persisted_receipt_test = self._load_interaction_state(telegram_id, "receipt_test")
                if persisted_receipt_test is not None:
                    self._receipt_test_providers[telegram_id] = str(
                        persisted_receipt_test.get("provider") or ""
                    )
            if self._is_admin(telegram_id) and (
                telegram_id in self._receipt_test_waiting or persisted_receipt_test is not None
            ):
                self._receipt_test_waiting.discard(telegram_id)
                self._clear_interaction_state(telegram_id, "receipt_test")
                self._handle_receipt_diagnostic(message, chat["id"], telegram_id)
                return
            self._handle_receipt(message, chat["id"], telegram_id)
            return
        text = message.get("text") or ""
        if not isinstance(text, str) or not text.strip():
            return
        raw_text = text.strip()
        customer_text: str | None = None
        customer_input = self._customer_inputs.get(int(telegram_id))
        if (
            customer_input is None
            and not raw_text.startswith("/")
            and raw_text not in self.CUSTOMER_BUTTON_COMMANDS
            and raw_text not in self.ADMIN_BUTTON_COMMANDS
        ):
            persisted_input = self._load_interaction_state(telegram_id, "customer_input")
            if persisted_input is not None and isinstance(persisted_input.get("action"), str):
                customer_input = {
                    "action": persisted_input["action"],
                    "expires_at": time.monotonic() + 600,
                }
                self._customer_inputs[int(telegram_id)] = customer_input
        if customer_input:
            if (
                float(customer_input.get("expires_at", 0)) <= time.monotonic()
                or raw_text in self.CUSTOMER_BUTTON_COMMANDS
                or raw_text.startswith("/")
            ):
                self._customer_inputs.pop(int(telegram_id), None)
                self._clear_interaction_state(telegram_id, "customer_input")
            elif customer_input.get("action") == "topup_amount":
                normalized_amount = raw_text.replace(",", "")
                if not normalized_amount.isdigit():
                    self.send(
                        chat["id"],
                        "Send a whole MMK amount from 1,000 to 1,000,000. Example: 7500",
                    )
                    return
                self._customer_inputs.pop(int(telegram_id), None)
                self._clear_interaction_state(telegram_id, "customer_input")
                customer_text = f"/topup {normalized_amount}"
        pending_receipt = self._receipt_verify_inputs.get(int(telegram_id))
        if pending_receipt is None and self._is_admin(telegram_id) and not raw_text.startswith("/"):
            persisted_receipt = self._load_interaction_state(telegram_id, "receipt_verify")
            if persisted_receipt is not None and isinstance(persisted_receipt.get("evidence_id"), str):
                pending_receipt = persisted_receipt["evidence_id"]
                self._receipt_verify_inputs[int(telegram_id)] = pending_receipt
        menu_navigation = (
            raw_text in self.CUSTOMER_BUTTON_COMMANDS or raw_text in self.ADMIN_BUTTON_COMMANDS
        )
        if pending_receipt and (menu_navigation or raw_text.startswith("/")):
            self._receipt_verify_inputs.pop(int(telegram_id), None)
            self._clear_interaction_state(telegram_id, "receipt_verify")
            pending_receipt = None
        if pending_receipt and self._is_admin(telegram_id) and not raw_text.startswith("/"):
            values = raw_text.rsplit(maxsplit=1)
            try:
                amount = int(values[1].replace(",", "")) if len(values) == 2 else 0
            except ValueError:
                amount = 0
            if len(values) != 2 or not values[0].strip() or amount <= 0:
                self.send(
                    chat["id"],
                    "Send the receiving-account transaction ID and amount, for example:\n"
                    "123456789 3000",
                )
                return
            self._receipt_verify_inputs.pop(int(telegram_id), None)
            self._clear_interaction_state(telegram_id, "receipt_verify")
            self._queue_admin_confirmation(
                chat["id"],
                telegram_id,
                "/verify",
                [pending_receipt, values[0].strip(), str(amount)],
                "Confirm these details match the actual receiving account.",
                "✅ I Checked · Verify",
                f"a:r:{pending_receipt}",
            )
            return
        text = customer_text
        persisted_admin_add = None
        if self._is_owner(telegram_id) and telegram_id not in self._admin_add_waiting:
            persisted_admin_add = self._load_interaction_state(telegram_id, "admin_add")
        if self._is_owner(telegram_id) and (
            telegram_id in self._admin_add_waiting or persisted_admin_add is not None
        ):
            self._admin_add_waiting.discard(telegram_id)
            self._clear_interaction_state(telegram_id, "admin_add")
            if raw_text.isdigit():
                text = f"/addadmin {raw_text}"
            else:
                self.send(
                    chat["id"],
                    "That is not a numeric Telegram ID. No access was changed. Open Staff & Access and try again.",
                    self._owner_keyboard(),
                )
                return
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,31}", raw_text):
            promo = self.service.giveaway_status(telegram_id, raw_text)
            if promo["exists"]:
                text = f"/claimpromo {promo['code']}"
        if text is None:
            text = self.CUSTOMER_BUTTON_COMMANDS.get(raw_text)
        if text is None and self._is_admin(telegram_id):
            text = self.ADMIN_BUTTON_COMMANDS.get(raw_text, raw_text)
        if text is None:
            text = raw_text
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]
        confirmed = message.get("_admin_confirmed") is True
        if command in self.OWNER_ONLY_COMMANDS and not self._is_owner(telegram_id):
            self._send_customer_fallback(chat["id"], telegram_id)
            return
        if command in self.ADMIN_ONLY_COMMANDS and not self._is_admin(telegram_id):
            self._send_customer_fallback(chat["id"], telegram_id)
            return
        if command == "/setpromo" and not confirmed and len(args) == 7:
            try:
                _promo_gb_to_bytes(args[1])
                int(args[2])
                int(args[3])
                if args[4].lower() not in {"campaign", "daily", "hourly"}:
                    raise ValueError("invalid frequency")
                _parse_promo_datetime(args[5])
                _parse_promo_datetime(args[6])
            except (ValueError, OverflowError):
                self.send(
                    chat["id"],
                    "Invalid promo settings. Use: /setpromo CODE QUOTA_GB DAYS COUNT "
                    "campaign|daily|hourly FROM_UTC TO_UTC",
                )
                return

        if command in self.ADMIN_CONFIRMATION_COMMANDS and not confirmed:
            # Validate syntax before presenting a challenge, but never mutate
            # commerce state from a directly typed administrative command.
            if command in {"/approve", "/reject"} and len(args) != 1:
                pass
            elif command == "/retry" and len(args) not in (1, 2):
                pass
            elif command == "/refund" and not args:
                pass
            elif command == "/verify" and len(args) != 3:
                pass
            elif command == "/rejectreceipt" and not args:
                pass
            elif command == "/setpromo" and len(args) != 7:
                pass
            elif command in {"/stoppromo", "/resumepromo"} and len(args) != 1:
                pass
            elif command == "/receiptmode" and len(args) != 1:
                pass
            elif command in {"/addadmin", "/removeadmin"} and len(args) != 1:
                pass
            elif command == "/serverstate" and len(args) != 2:
                pass
            elif command == "/migratekey" and len(args) != 3:
                pass
            elif command == "/approverepair" and len(args) not in (1, 2):
                pass
            else:
                prompt = {
                    "/approve": lambda: f"Approve order {args[0]} and queue VPN provisioning?",
                    "/reject": lambda: f"Reject order {args[0]} and notify the customer?",
                    "/retry": lambda: f"Retry the failed worker job for order {args[0]}?",
                    "/refund": lambda: f"Refund order {args[0]} to the customer wallet and revoke paid access?",
                    "/verify": lambda: f"Verify receipt {args[0]} for transaction {args[1]} and amount {args[2]}?",
                    "/rejectreceipt": lambda: f"Reject receipt {args[0]} and request a replacement screenshot?",
                    "/setpromo": lambda: f"Activate promo campaign {args[0]} with these settings?",
                    "/stoppromo": lambda: f"Stop promo campaign {args[0]}?",
                    "/resumepromo": lambda: f"Resume promo campaign {args[0]}?",
                    "/receiptmode": lambda: f"Change receipt analysis mode to {args[0]}?",
                    "/addadmin": lambda: f"Grant AuriX administrator access to Telegram user {args[0]}?",
                    "/removeadmin": lambda: f"Revoke AuriX administrator access from Telegram user {args[0]}?",
                    "/serverstate": lambda: f"Change endpoint {args[0]} lifecycle to {args[1]}?",
                    "/migratekey": lambda: f"Move credential {args[1]} from {args[0]} to {args[2]} while preserving remaining quota and expiry?",
                    "/approverepair": lambda: (
                        f"Approve managed-key repair {args[0]} with explicit full-quota restoration?"
                        if len(args) == 2 and args[1].lower() == "full"
                        else f"Approve managed-key repair {args[0]} while preserving observed usage?"
                    ),
                }[command]()
                self._queue_admin_confirmation(
                    chat["id"],
                    telegram_id,
                    command,
                    args,
                    prompt,
                    confirm_label={
                        "/approve": "Confirm Approve",
                        "/reject": "Confirm Reject",
                        "/retry": "🔁 Confirm Retry",
                        "/refund": "💸 Confirm Refund",
                        "/verify": "✅ Confirm Verify",
                        "/rejectreceipt": "🛑 Confirm Receipt Rejection",
                        "/setpromo": "🎁 Confirm Promo",
                        "/stoppromo": "⏸ Confirm Stop",
                        "/resumepromo": "▶ Confirm Resume",
                        "/receiptmode": "✅ Confirm Mode Change",
                        "/addadmin": "✅ Confirm Add Admin",
                        "/removeadmin": "🛑 Confirm Remove Admin",
                        "/serverstate": "✅ Confirm Endpoint State",
                        "/migratekey": "🔁 Confirm Key Migration",
                        "/approverepair": "✅ Confirm Repair",
                    }[command],
                )
                return
        if command in ("/start", "/help"):
            if command == "/start":
                giveaway = self.service.giveaway_status(telegram_id)
                remaining = int(giveaway["remaining_slots"])
                if giveaway["exists"] and giveaway["campaign_state"] in {
                    "active",
                    "scheduled",
                }:
                    quota = self._promo_quota_label(giveaway["quota_bytes"])
                    availability = (
                        f"🔥 {remaining}/{giveaway['winner_limit']} gifts available "
                        f"{self._promo_frequency_label(giveaway['frequency'])}."
                        if giveaway["campaign_state"] == "active" and remaining > 0
                        else "⏳ This promo is scheduled; Redeem appears when it starts."
                    )
                    welcome_text = (
                        "🎉 AuriX VPN မှ ကြိုဆိုပါတယ်!\n\n"
                        f"🎁 Promo: {giveaway['code']}\n"
                        f"{quota} Outline VPN • {giveaway['duration_days']} days • Free\n"
                        f"{availability}\n\n"
                        f"👇 Tap Redeem or send {giveaway['code']} exactly. "
                        "No payment or receipt.\n\n"
                        "While both the promo season and your gift are active, other plans pause. "
                        "Daily 300 MB, monthly 3 GB, and paid plans return automatically when "
                        "the season or your gift ends. One gift per account per campaign.\n\n"
                        "ℹ️ This is Outline VPN allowance, not SIM/mobile data. Network speed "
                        "depends on your ISP and server conditions.\n\n"
                        "အကူအညီ — https://t.me/+oA18TDWAD9NiNWU1\n"
                        "သတင်း — https://t.me/AurixDigitalStore\n\n"
                        "AuriX is not an official Outline Foundation partner."
                    )
                else:
                    welcome_text = (
                        "🎉 Welcome to AuriX VPN!\n\n"
                        "The seasonal promo is currently closed. Your regular choices are ready: "
                        "daily 300 MB, monthly 3 GB, and paid 50/100 GB plans."
                    )
            else:
                welcome_text = (
                    "🧭 Connect with Outline · quick setup\n\n"
                    "1️⃣ Install the official Outline app\n"
                    "Choose your device below. Android users can use Google Play or the direct "
                    "APK when Play Store is unavailable.\n\n"
                    "2️⃣ Copy your AuriX key\n"
                    "Tap Get / Copy My Key. An Outline key starts with ss://. Use the Copy button "
                    "beside your active key.\n\n"
                    "3️⃣ Connect\n"
                    "Open Outline. If it detects the copied key, accept it and tap Connect. "
                    "Otherwise tap +, paste the complete key, add the server, then Connect.\n\n"
                    "4️⃣ Confirm it works\n"
                    "Tap Check My IP after connecting. Your public IP should change from your "
                    "normal mobile/Wi-Fi address.\n\n"
                    "🛡 Keep the ss:// key private—anyone who has it can use its quota. "
                    "If connection fails, copy the key again without spaces, switch between "
                    "Wi-Fi/mobile data, then ask AuriX Support."
                )
            if command == "/help":
                reply_markup = self._outline_help_keyboard()
            elif giveaway["active"] and remaining > 0 and not giveaway["winner"]:
                reply_markup = self._launch_promo_keyboard(str(giveaway["code"]))
            else:
                reply_markup = self._customer_keyboard(telegram_id)
            self.send(chat["id"], welcome_text, reply_markup)
        elif command == "/whoami":
            access = "\nAdmin access: enabled" if self._is_admin(telegram_id) else ""
            self.send(
                chat["id"],
                f"Your Telegram ID: {telegram_id}{access}",
                self._customer_keyboard(telegram_id),
            )
        elif command in {"/claimpromo", "/giveaway100gb"}:
            promo_code = args[0] if command == "/claimpromo" and args else None
            try:
                result = self.service.claim_giveaway(
                    telegram_id, first_name, username=username, code=promo_code
                )
            except OutlineError:
                self.send(
                    chat["id"],
                    "The giveaway key could not be provisioned. No winner slot was consumed; try again.",
                )
                return
            if result.outcome == "won":
                quota = self._promo_quota_label(int(result.quota_bytes or 0))
                self.send(
                    chat["id"],
                    f"🎉 Promo gift #{result.winner_number}: {result.code}\n\n"
                    f"Your {quota} / {result.duration_days}-day Outline key:\n\n"
                    f"{result.access_url}\n\n"
                    f"Expires: {format_user_datetime(result.expires_at)}\n\n"
                    "Enjoy your gift—no payment or receipt was needed. Other AuriX plans rest "
                    "while this gift and its promo season are active, then return automatically:\n"
                    "• Daily Free — 300 MB for 24 hours\n"
                    "• Monthly Free — 3 GB for 30 days\n"
                    "• Paid 50 GB — 3,000 MMK for 30 days\n"
                    "• Paid 100 GB — 6,000 MMK for 30 days",
                    self._key_delivery_keyboard(str(result.access_url)),
                )
            elif result.pending:
                self.send(
                    chat["id"],
                    "⏳ Your promo slot is reserved and the Outline key is being prepared. "
                    "Open My VPN in a moment to retrieve it.",
                    self._customer_keyboard(telegram_id),
                )
            elif result.outcome == "already_won":
                self.send(
                    chat["id"],
                    f"You already won slot #{result.winner_number}. No second key or slot was created.\n"
                    f"Expires: {format_user_datetime(result.expires_at)}\n"
                    "Open My VPN to retrieve the key and track usage. Regular plans are available "
                    "again after the gift or season ends.",
                    self._customer_keyboard(telegram_id),
                )
            elif result.outcome == "ineligible":
                self.send(chat["id"], f"This account is not eligible: {result.reason}")
            elif result.outcome == "scheduled":
                self.send(
                    chat["id"], "This promo has not started yet. Open Plans later to refresh."
                )
            elif result.outcome in {"ended", "paused", "unavailable"}:
                self.send(
                    chat["id"],
                    result.reason
                    or "This promo is not active. Your regular plans remain available.",
                    self._customer_keyboard(telegram_id),
                )
            else:
                self.send(chat["id"], "This promo's current giveaway window is fully claimed.")
        elif command == "/admin":
            self._send_admin_home(chat["id"], telegram_id)
        elif command == "/owner":
            self._send_owner_home(chat["id"], telegram_id)
        elif command == "/staff":
            self._send_staff_panel(chat["id"], telegram_id)
        elif command == "/notifications":
            self._send_staff_notifications(chat["id"], telegram_id)
        elif command == "/addadmin":
            if len(args) != 1:
                self.send(chat["id"], "Usage: /addadmin <Telegram numeric ID>")
            else:
                try:
                    staff = self.staff_access.add_admin(int(args[0]), telegram_id)
                except (ValueError, Exception) as exc:
                    self.send(chat["id"], str(exc) or "Administrator could not be added.")
                else:
                    self._refresh_staff_scopes()
                    self.send(
                        chat["id"],
                        f"Administrator added: {staff.get('effective_username') or staff['telegram_id']}",
                        self._owner_keyboard(),
                    )
        elif command == "/removeadmin":
            if len(args) != 1:
                self.send(chat["id"], "Usage: /removeadmin <Telegram numeric ID>")
            else:
                try:
                    target_id = int(args[0])
                    self.staff_access.remove_admin(target_id, telegram_id)
                except (ValueError, Exception) as exc:
                    self.send(chat["id"], str(exc) or "Administrator could not be removed.")
                else:
                    self._refresh_staff_scopes()
                    self.send(
                        chat["id"],
                        f"Administrator {target_id} was revoked immediately.",
                        self._owner_keyboard(),
                    )
        elif command == "/serverstate":
            if len(args) != 2 or args[1].lower() not in {"active", "draining", "retired"}:
                self.send(
                    chat["id"],
                    "Usage: /serverstate <server-id> active|draining|retired",
                    self._owner_keyboard(),
                )
            else:
                try:
                    result = self._admin_owner_call(
                        telegram_id,
                        "set_server_lifecycle",
                        args[0],
                        args[1].lower(),
                        telegram_id,
                        reason="owner Telegram lifecycle control",
                    )
                except Exception as exc:
                    self.send(
                        chat["id"],
                        str(exc) or "Endpoint lifecycle change was not saved.",
                        self._owner_keyboard(),
                    )
                else:
                    self.send(
                        chat["id"],
                        f"✅ Endpoint {args[0]} is now {result.get('lifecycle_state', args[1]).title()}. "
                        "No provider VM action was performed.",
                        self._owner_keyboard(),
                    )
        elif command == "/migratekey":
            if len(args) != 3:
                self.send(
                    chat["id"],
                    "Usage: /migratekey <source-server> <outline-key-id> <target-server>",
                    self._owner_keyboard(),
                )
            else:
                try:
                    result = self._admin_owner_call(
                        telegram_id,
                        "queue_endpoint_migration",
                        args[0],
                        args[1],
                        args[2],
                        telegram_id,
                    )
                except Exception as exc:
                    self.send(
                        chat["id"],
                        str(exc) or "Endpoint migration could not be queued.",
                        self._owner_keyboard(),
                    )
                else:
                    self.send(
                        chat["id"],
                        "✅ Credential migration queued. A fresh source-usage check will run before cutover; the old key is deleted only after the replacement is persisted.",
                        self._owner_keyboard(),
                    )
        elif command == "/groupsync":
            if self.control_group_id is None:
                self._send_control_group_picker(chat["id"])
            else:
                try:
                    group_owner, group_admins = self._control_group_staff()
                    preview = self.staff_access.group_sync_preview(
                        self.control_group_id, telegram_id, group_owner, group_admins
                    )
                except Exception as exc:
                    self.send(
                        chat["id"],
                        f"Group sync preview unavailable: {str(exc)[:240]}",
                        self._owner_keyboard(),
                    )
                else:
                    self.send(
                        chat["id"],
                        "🔄 AuriX Group Sync Preview\n\n"
                        f"Group creator: {preview.get('group_owner_id') or '-'}\n"
                        f"Current owner: {preview.get('current_owner_id') or '-'}\n"
                        f"Potential additions: {', '.join(map(str, preview['additions'])) or 'none'}\n"
                        f"Review removals: {', '.join(map(str, preview['review_removals'])) or 'none'}\n\n"
                        "Nothing was changed. Additions and removals require owner confirmation from Staff & Access.",
                        self._owner_keyboard(),
                    )
        elif command == "/receiptsystem":
            self._send_receipt_system(chat["id"], telegram_id)
        elif command == "/receiptmode":
            if len(args) != 1:
                self.send(chat["id"], "Usage: /receiptmode manual|assisted")
            else:
                try:
                    policy = self._admin_call(telegram_id, "set_receipt_mode", args[0], telegram_id)
                except Exception as exc:
                    self.send(chat["id"], str(exc) or "Receipt mode could not be changed.")
                else:
                    self.send(
                        chat["id"],
                        f"Receipt workflow changed to {policy['mode'].title()}. Financial approval remains human-verified.",
                        self._receipt_system_keyboard(),
                    )
        elif command == "/receipttest":
            self.send(
                chat["id"],
                "🧪 Safe Receipt Test\n\nFirst choose the payment method shown on the receipt. This activates its provider-specific labels; the test cannot create an order, credit, subscription or VPN key.",
                self._inline_keyboard(
                    [
                        [("1 · KBZPay", "a:t:method:kbzpay"), ("2 · WavePay", "a:t:method:wavepay")],
                        [("3 · AYA Pay", "a:t:method:ayapay"), ("4 · UABPay", "a:t:method:uabpay")],
                        [("5 · CB Pay", "a:t:method:cbpay")],
                        [("Cancel Test", "a:t:cancel"), ("Last Test", "a:t:last")],
                    ]
                ),
            )
        elif command == "/promo":
            promo = self._admin_service_call(telegram_id, "giveaway_status", telegram_id)
            if not promo["exists"]:
                self.send(chat["id"], "No promo campaign is configured.")
            else:
                quota = self._promo_quota_label(promo["quota_bytes"])
                example = "/setpromo NEWCODE 100 30 5 campaign 2026-09-01T00:00Z 2026-09-30T23:59Z"
                buttons = self._promo_code_buttons(str(promo["code"]), include_copy=True)
                rows: list[list[dict[str, Any]]] = []
                if buttons:
                    rows.append(buttons)
                copy_setup = self._copy_text_button("📋 Copy Setup Example", example)
                if copy_setup:
                    rows.append([copy_setup])
                action = "stop" if promo["campaign_state"] != "paused" else "resume"
                rows.append(
                    [
                        {
                            "text": "⏸ Stop Promo" if action == "stop" else "▶ Resume Promo",
                            "callback_data": f"a:g:{action}:{promo['code']}"[:64],
                        },
                        {"text": "🏠 Admin Home", "callback_data": "a:n:admin"},
                    ]
                )
                self.send(
                    chat["id"],
                    "Promo campaign\n\n"
                    f"Code: {promo['code']}\n"
                    f"State: {promo['campaign_state']}\n"
                    f"Gift: {quota} / {promo['duration_days']} days\n"
                    f"Capacity: {promo['winner_limit']} {self._promo_frequency_label(promo['frequency'])}\n"
                    f"Current window: {promo['window_claimed_count']} claimed · "
                    f"{promo['remaining_slots']} remaining\n"
                    f"Lifetime claims: {promo['claimed_count']}\n"
                    f"From: {format_user_datetime(promo['starts_at'], 'open')}\n"
                    f"To: {format_user_datetime(promo['ends_at'], 'open')}\n\n"
                    "Setup syntax:\n"
                    "/setpromo CODE QUOTA_GB DAYS COUNT campaign|daily|hourly FROM_UTC TO_UTC\n\n"
                    "Each account can claim once per campaign. Daily/hourly resets the slot count, "
                    "not the same account's eligibility.",
                    {"inline_keyboard": rows},
                )
        elif command == "/setpromo":
            if len(args) != 7:
                self.send(
                    chat["id"],
                    "Usage: /setpromo CODE QUOTA_GB DAYS COUNT campaign|daily|hourly "
                    "FROM_UTC TO_UTC",
                )
            else:
                try:
                    promo = self._admin_service_call(
                        telegram_id,
                        "configure_giveaway",
                        code=args[0],
                        quota_bytes=_promo_gb_to_bytes(args[1]),
                        duration_days=int(args[2]),
                        winner_limit=int(args[3]),
                        frequency=args[4],
                        starts_at=_parse_promo_datetime(args[5]),
                        ends_at=_parse_promo_datetime(args[6]),
                    )
                except (ValueError, OverflowError) as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(
                        chat["id"],
                        f"Promo {promo['code']} saved. Current state: {promo['campaign_state']}.",
                        self._admin_keyboard(telegram_id),
                    )
        elif command in {"/stoppromo", "/resumepromo"}:
            if len(args) != 1:
                self.send(chat["id"], f"Usage: {command} <promo-code>")
            else:
                try:
                    promo = self._admin_service_call(
                        telegram_id,
                        "set_giveaway_active",
                        args[0],
                        command == "/resumepromo",
                    )
                except ValueError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(
                        chat["id"],
                        f"Promo {promo['code']} is now {promo['campaign_state']}. "
                        "Customer plan choices update immediately.",
                        self._admin_keyboard(telegram_id),
                    )
        elif command == "/myorders":
            self._send_my_orders(chat["id"], telegram_id)
        elif command == "/order":
            if self.commerce is None or len(args) != 1:
                self.send(chat["id"], "Usage: /order <order-id>")
            else:
                self._send_order_detail(
                    chat["id"], telegram_id, args[0], admin_view=self._is_admin(telegram_id)
                )
        elif command == "/plans":
            self._send_plans(chat["id"], telegram_id)
        elif command in ("/buy", "/upgrade"):
            if self.commerce is None:
                self.send(chat["id"], "Paid plans are not configured in this staging process.")
            elif len(args) != 1:
                self.send(chat["id"], "Usage: /buy <plan-code>\n\nUse /plans first.")
            else:
                try:
                    order = self.commerce.create_order(
                        telegram_id, first_name, args[0], username=username
                    )
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    if order.plan_conflict:
                        detail = self.commerce.order_detail(order.order_id, telegram_id)
                        untouched = bool(
                            detail
                            and detail.get("status") == "awaiting_payment"
                            and not detail.get("payment_status")
                            and not detail.get("receipt_status")
                        )
                        if not untouched:
                            self._send_order_detail(
                                chat["id"],
                                telegram_id,
                                order.order_id,
                                heading="Existing open order",
                            )
                            return
                        self.send(
                            chat["id"],
                            f"You already have an open order for {order.plan.name}. Choose whether to replace that untouched order with {args[0]}.",
                            self._inline_keyboard(
                                [
                                    [
                                        ("Replace Open Order", f"p:x:{order.order_id}:{args[0]}"),
                                        ("Keep Existing", f"o:v:{order.order_id}"),
                                    ]
                                ]
                            ),
                        )
                        return
                    if not order.created:
                        self._send_order_detail(
                            chat["id"], telegram_id, order.order_id, heading="Existing open order"
                        )
                        return
                    self._send_payment_method_chooser(
                        chat["id"], telegram_id, order.order_id, heading="✅ Order created"
                    )
        elif command == "/paid":
            if self.commerce is None:
                self.send(chat["id"], "Paid plans are not configured in this staging process.")
            elif len(args) < 1:
                self.send(chat["id"], "Usage: /paid <order-id> then send the receipt screenshot")
            elif len(args) == 1:
                self.send(
                    chat["id"],
                    f"Now send the receipt screenshot for order {args[0]}. You may caption it with /paid {args[0]}.",
                )
            elif not self.allow_text_payment:
                self.send(
                    chat["id"],
                    "Text payment references are disabled. Send the receipt screenshot instead.",
                )
            else:
                try:
                    result = self.commerce.submit_payment(
                        telegram_id,
                        args[0],
                        "manual",
                        " ".join(args[1:]) if len(args) > 1 else "pending-receipt",
                    )
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(chat["id"], f"Payment recorded ({result}). An admin will review it.")
                    for admin_id in self.admin_ids:
                        try:
                            self.send(
                                admin_id,
                                f"Payment submitted for order {args[0]} by Telegram user {telegram_id}.",
                            )
                        except Exception as exc:
                            print(
                                f"admin notification error: {type(exc).__name__}", file=sys.stderr
                            )
        elif command in ("/myvpn", "/status", "/usage"):
            # /status and /usage remain safe aliases for links and old Telegram
            # keyboards, but My VPN is the single customer-facing dashboard.
            self._send_my_vpn(chat["id"], telegram_id)
        elif command == "/pair":
            device_url = str(getattr(self, "device_api_url", "") or "").rstrip("/")
            identity = getattr(self.service, "identity", None)
            if not device_url or identity is None or not callable(getattr(identity, "create_pairing_token", None)):
                self.send(chat["id"], "Managed-device pairing is not enabled on this AuriX deployment.")
            else:
                try:
                    token = identity.create_pairing_token(telegram_id)
                except Exception as exc:
                    print(f"device pairing token error: {type(exc).__name__}", file=sys.stderr)
                    self.send(chat["id"], "A device pairing token could not be created. Try again later.")
                else:
                    self.send(
                        chat["id"],
                        "📱 AuriX managed-device pairing\n\n"
                        f"API: {html_escape(device_url)}\n"
                        f"Open in the AuriX app (if installed): <code>aurix://pair/{html_escape(token)}</code>\n"
                        f"One-time token (expires in 5 minutes):\n<code>{html_escape(token)}</code>\n\n"
                        "Enter this token in the official AuriX client. Do not post it in a group or share it with anyone.",
                        parse_mode="HTML",
                    )
        elif command == "/alerts":
            self._send_quota_alert_settings(chat["id"], telegram_id)
        elif command == "/keysastext":
            self._send_my_vpn(chat["id"], telegram_id, show_key_text=True)
        elif command == "/renew":
            if self.commerce is None:
                self.send(chat["id"], "Paid plans are not configured in this staging process.")
            else:
                requested_plan = args[0] if args else None
                subscriptions = (
                    self.commerce.user_vpns(telegram_id)
                    if hasattr(self.commerce, "user_vpns")
                    else []
                )
                subscription = (
                    next(
                        (item for item in subscriptions if item.get("plan_code") == requested_plan),
                        None,
                    )
                    if requested_plan
                    else self.commerce.user_vpn(telegram_id)
                )
                if subscription is None and requested_plan:
                    self.send(chat["id"], "That plan is not one of your previous plans.")
                    return
                if subscription is None:
                    self.send(chat["id"], "No previous plan found. Use /plans and /buy first.")
                else:
                    try:
                        order = self.commerce.create_order(
                            telegram_id,
                            first_name,
                            requested_plan or subscription["plan_code"],
                            username=username,
                        )
                    except CommerceError as exc:
                        self.send(chat["id"], str(exc))
                    else:
                        heading = (
                            "Renewal order created" if order.created else "Existing open order"
                        )
                        self._send_payment_method_chooser(
                            chat["id"], telegram_id, order.order_id, heading=heading
                        )
        elif command == "/trial":
            if not self._trial_allowed(telegram_id):
                self.send(
                    chat["id"],
                    "The monthly trial is currently invite-only. Use /claim or /plans instead.",
                )
                return
            if self._free_claim_blocked_by_paid(telegram_id):
                self.send(
                    chat["id"], "Your paid account is already active; the free trial is not needed."
                )
                return
            try:
                result = self.service.claim_trial(telegram_id, first_name, username=username)
            except OutlineError:
                self.send(chat["id"], "Trial service temporarily unavailable. Try again later.")
                return
            if result.denied_reason == "active_promo":
                self.send(
                    chat["id"],
                    "Your promo gift is active. Monthly 3 GB returns automatically when the "
                    "gift or promo season ends.",
                )
            elif result.pending:
                self.send(
                    chat["id"],
                    "⏳ Your monthly 3 GB key is being prepared safely. "
                    "Open My VPN in a moment to retrieve it; your trial slot is reserved.",
                    self._customer_keyboard(telegram_id),
                )
            elif result.access_url:
                self.send(
                    chat["id"],
                    f"Your monthly 3 GB key:\n\n{result.access_url}\n\nExpires: {format_user_datetime(result.expires_at)}",
                    self._key_delivery_keyboard(str(result.access_url)),
                )
            else:
                retry = (
                    format_user_datetime(result.next_claim_at)
                    if result.next_claim_at
                    else "later"
                )
                self.send(chat["id"], f"Monthly 3 GB already claimed. Come back after {retry}.")
        elif command == "/wallet":
            if self.commerce is None:
                self.send(chat["id"], "Wallet is not configured.")
            else:
                balance = self.commerce.wallet_balance(telegram_id)
                history = self.commerce.wallet_history(telegram_id, limit=5)
                history_text = ""
                if history:
                    history_text = "\n\nRecent wallet events:\n" + "\n".join(
                        f"{format_user_datetime(item['created_at'])} · {item['kind']} {int(item['amount_minor']):,} {item['currency']} · {item['reference_id']}"
                        for item in history
                    )
                self.send(
                    chat["id"],
                    f"💰 AuriX Wallet\n\nBalance: {balance:,} MMK\n"
                    f"Top-ups are credited only after receipt verification.{history_text}",
                    self._inline_keyboard([[("➕ Top up wallet", "t:a:menu")]]),
                )
        elif command == "/topup":
            if self.commerce is None:
                self.send(chat["id"], "Wallet is not configured.")
            elif not args:
                self.send(
                    chat["id"],
                    "Choose a wallet top-up amount, or tap Other amount and type your own.",
                    self._topup_amount_keyboard(),
                )
            elif len(args) != 1:
                self.send(chat["id"], "Choose one top-up amount.", self._topup_amount_keyboard())
            else:
                try:
                    order = self.commerce.create_wallet_topup(
                        telegram_id,
                        first_name,
                        int(args[0].replace(",", "")),
                        username=username,
                    )
                except (ValueError, CommerceError) as exc:
                    self.send(chat["id"], str(exc) or "Top-up amount is invalid.")
                else:
                    if not order.created and order.plan_conflict:
                        self.send(
                            chat["id"],
                            "Finish or cancel your current open order before starting this top-up.",
                            self._inline_keyboard(
                                [[("Open current order", f"o:v:{order.order_id}")]]
                            ),
                        )
                    else:
                        heading = (
                            "Wallet top-up created" if order.created else "Existing wallet top-up"
                        )
                        self._send_payment_method_chooser(
                            chat["id"], telegram_id, order.order_id, heading=heading
                        )
        elif command == "/walletpay":
            if self.commerce is None:
                self.send(chat["id"], "Wallet is not configured.")
            elif len(args) != 1:
                self.send(chat["id"], "Usage: /walletpay <order-id>")
            else:
                try:
                    result = self.commerce.pay_order_with_wallet(telegram_id, args[0])
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(
                        chat["id"],
                        f"Wallet payment {result}; an admin will review and approve the order.",
                    )
        elif command == "/replace":
            if self.commerce is None or len(args) not in (1, 2):
                self.send(chat["id"], "Usage: /replace <plan-code> [expected-order-id]")
            else:
                try:
                    order = self.commerce.replace_open_order(
                        telegram_id,
                        first_name,
                        args[0],
                        username=username,
                        expected_order_id=args[1] if len(args) == 2 else None,
                    )
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(
                        chat["id"],
                        f"Order replaced: {order.order_id}\nPlan: {order.plan.name}\nAmount: {order.plan.price_minor:,} {order.plan.currency}\n\nPay through the approved channel, then send the receipt screenshot.",
                        self._inline_keyboard(
                            [
                                [
                                    ("📷 Send Receipt", f"o:r:{order.order_id}"),
                                    ("View Order", f"o:v:{order.order_id}"),
                                ]
                            ]
                        ),
                    )
        elif command == "/cancelorder":
            if self.commerce is None or len(args) != 1:
                self.send(chat["id"], "Usage: /cancelorder <order-id>")
            else:
                try:
                    result = self.commerce.cancel_order(telegram_id, args[0])
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(
                        chat["id"],
                        f"Order {args[0]} {result}.",
                        self._customer_keyboard(telegram_id),
                    )
        elif command == "/receipt":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None or len(args) != 1:
                self.send(chat["id"], "Usage: /receipt <evidence-id> (admin)")
            else:
                receipt = self._admin_call(telegram_id, "get_receipt", args[0])
                if receipt is None:
                    self.send(chat["id"], "Receipt evidence not found.")
                else:
                    try:
                        self._send_receipt_review(chat["id"], receipt)
                    except Exception as exc:
                        print(f"receipt review media error: {type(exc).__name__}", file=sys.stderr)
                        self.send(
                            chat["id"],
                            "⚠️ Receipt evidence is recorded, but its image could not be loaded "
                            "from private storage or Telegram right now. Retry first; request a new "
                            "screenshot only if it remains unavailable.",
                            self._inline_keyboard(
                                [
                                    [("🔄 Retry Receipt", f"a:r:{receipt['id']}")],
                                    [("View Order", f"a:o:{receipt['order_id']}")],
                                ]
                            ),
                        )
        elif command == "/verify":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            elif len(args) != 3:
                self.send(chat["id"], "Usage: /verify <evidence-id> <transaction-id> <amount>")
            else:
                try:
                    amount = int(args[2].replace(",", ""))
                    order_id = self._admin_call(
                        telegram_id, "verify_receipt", args[0], telegram_id, args[1], amount
                    )
                except (CommerceError, ValueError) as exc:
                    self.send(chat["id"], str(exc) or "Verified amount must be an integer.")
                else:
                    self._send_order_detail(
                        chat["id"],
                        telegram_id,
                        order_id,
                        admin_view=True,
                        heading="✅ Receipt verified · ready for approval",
                    )
        elif command == "/rejectreceipt":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None or not args:
                self.send(chat["id"], "Usage: /rejectreceipt <evidence-id> [reason]")
            else:
                try:
                    order_id = self._admin_call(
                        telegram_id,
                        "reject_receipt",
                        args[0],
                        telegram_id,
                        " ".join(args[1:])
                        or "Receipt rejected; please submit a clearer screenshot.",
                    )
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(
                        chat["id"],
                        f"Receipt rejected for order {order_id}; the customer can submit a replacement.",
                        self._inline_keyboard([[("📥 Orders", "a:n:orders")]]),
                    )
        elif command in ("/orders", "/receipts"):
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            else:
                view = "receipts" if command == "/receipts" else "orders"
                items = self._panel_data(telegram_id, view)
                if not items:
                    self.send(
                        chat["id"],
                        "No unreviewed receipts." if view == "receipts" else "No pending orders.",
                    )
                else:
                    self._open_admin_panel(chat["id"], telegram_id, view)
        elif command == "/capacity":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            else:
                try:
                    self._show_capacity(chat["id"], telegram_id)
                except Exception as exc:
                    self.send(chat["id"], "Outline capacity metrics are temporarily unavailable.")
                    print(f"capacity error: {type(exc).__name__}", file=sys.stderr)
        elif command == "/probes":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.probe_service is None:
                self.send(chat["id"], "Fleet probes are not configured.", self._admin_keyboard(telegram_id))
            else:
                try:
                    self._show_probes(chat["id"], telegram_id)
                except Exception as exc:
                    self.send(chat["id"], "Fleet probe results are temporarily unavailable.", self._admin_keyboard(telegram_id))
                    print(f"probe panel error: {type(exc).__name__}", file=sys.stderr)
        elif command == "/reconcile":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            else:
                report = self._admin_call(telegram_id, "consistency_report")
                issue_keys = {
                    "duplicate_open_orders",
                    "duplicate_payment_references",
                    "approved_missing_subscription",
                    "approved_missing_provision_job",
                    "stale_receipts",
                    "pending_receipt_uploads",
                    "failed_receipt_uploads",
                    "failed_jobs",
                    "failed_activations",
                    "failed_revocations",
                    "pending_revocations",
                    "dead_notifications",
                    "wallet_balance_mismatches",
                    "managed_key_missing",
                    "managed_key_repairs_pending",
                    "managed_key_repairs_failed",
                    "managed_key_repairs_manual",
                }
                healthy = all(report.get(key, 0) == 0 for key in issue_keys)
                lines = [
                    "AuriX consistency scan",
                    "Status: " + ("OK" if healthy else "ACTION REQUIRED"),
                ]
                lines.extend(
                    f"{key.replace('_', ' ').title()}: {value}" for key, value in report.items()
                )
                self.send(chat["id"], "\n".join(lines), self._admin_keyboard(telegram_id))
        elif command == "/enforcement":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            else:
                events = self._admin_service_call(telegram_id, "termination_summary")
                if not events:
                    self.send(
                        chat["id"],
                        "No free/trial termination events recorded.",
                        self._admin_keyboard(telegram_id),
                    )
                else:
                    self._open_admin_panel(chat["id"], telegram_id, "enforcement")
        elif command == "/failed":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            else:
                jobs = self._admin_call(telegram_id, "failed_jobs", include_nonterminal=True)
                if not jobs:
                    self.send(
                        chat["id"],
                        "No terminal worker failures.",
                        self._admin_keyboard(telegram_id),
                    )
                else:
                    self._open_admin_panel(chat["id"], telegram_id, "failed")
        elif command == "/repairs":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            else:
                jobs = self._admin_call(
                    telegram_id, "managed_key_repair_jobs", status="open", limit=100
                )
                if not jobs:
                    self.send(
                        chat["id"],
                        "No managed-key repairs are waiting for action.",
                        self._admin_keyboard(telegram_id),
                    )
                else:
                    self._open_admin_panel(chat["id"], telegram_id, "repairs")
        elif command == "/migrations":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            else:
                jobs = self._admin_call(telegram_id, "endpoint_migration_jobs", limit=100)
                if not jobs:
                    self.send(
                        chat["id"],
                        "No open endpoint migrations.",
                        self._admin_keyboard(telegram_id),
                    )
                else:
                    self._open_admin_panel(chat["id"], telegram_id, "migrations")
        elif command == "/failover":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            else:
                decisions = self._admin_call(telegram_id, "route_failover_decisions", limit=100)
                if not decisions:
                    self.send(
                        chat["id"],
                        "No route failover decisions recorded.",
                        self._admin_keyboard(telegram_id),
                    )
                else:
                    self._open_admin_panel(chat["id"], telegram_id, "failover")
        elif command == "/retry":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None or len(args) not in (1, 2):
                self.send(chat["id"], "Usage: /retry <order-id> [provision|revoke]")
            else:
                try:
                    operation = self._admin_call(
                        telegram_id,
                        "retry_failed_job",
                        args[0],
                        telegram_id,
                        operation=args[1] if len(args) == 2 else None,
                    )
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(
                        chat["id"],
                        f"{operation.title()} job requeued for order {args[0]}.",
                        self._inline_keyboard([[("🔄 Refresh Order", f"a:o:{args[0]}")]]),
                    )
        elif command == "/retryjob":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None or len(args) != 1:
                self.send(chat["id"], "Usage: /retryjob <job-id>")
            else:
                try:
                    operation = self._admin_call(telegram_id, "retry_job", args[0], telegram_id)
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(
                        chat["id"],
                        f"{operation.title()} job {args[0]} requeued.",
                        self._admin_keyboard(telegram_id),
                    )
        elif command == "/refund":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None or not args:
                self.send(chat["id"], "Usage: /refund <order-id> [reason]")
            else:
                try:
                    result = self._admin_call(
                        telegram_id,
                        "refund_order",
                        args[0],
                        telegram_id,
                        " ".join(args[1:]) or "refunded by admin",
                    )
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
                else:
                    self.send(
                        chat["id"],
                        f"Order {args[0]} {result}; wallet reversal recorded and access revocation queued.",
                        self._inline_keyboard([[("🔄 Refresh Order", f"a:o:{args[0]}")]]),
                    )
        elif command == "/ledger":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None or len(args) != 1:
                self.send(chat["id"], "Usage: /ledger <telegram-id>")
            else:
                try:
                    customer_id = int(args[0])
                    balance = self._admin_call(telegram_id, "wallet_balance", customer_id)
                    history = self._admin_call(telegram_id, "wallet_history", customer_id, limit=20)
                except (ValueError, CommerceError) as exc:
                    self.send(chat["id"], str(exc) or "Telegram ID must be numeric.")
                else:
                    lines = [f"Wallet ledger · tg:{customer_id}", f"Balance: {balance:,} MMK"]
                    lines.extend(
                        f"{format_user_datetime(item['created_at'])} · {item['kind']} {int(item['amount_minor']):,} {item['currency']} · {item['reference_id']}"
                        for item in history
                    )
                    self.send(chat["id"], "\n".join(lines), self._admin_keyboard(telegram_id))
        elif command == "/approverepair":
            if not self._is_owner(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            elif len(args) not in (1, 2) or (len(args) == 2 and args[1].lower() != "full"):
                self.send(chat["id"], "Usage: /approverepair <repair-id> [full]")
            else:
                try:
                    result = self._admin_owner_call(
                        telegram_id,
                        "approve_managed_key_repair",
                        args[0],
                        telegram_id,
                        allow_unknown_usage=len(args) == 2,
                    )
                except (CommerceError, PermissionError) as exc:
                    self.send(
                        chat["id"],
                        str(exc),
                        self._inline_keyboard([[ ("🧩 Key Repairs", "a:n:repairs") ]]),
                    )
                else:
                    self.send(
                        chat["id"],
                        (
                            f"✅ Repair {result.get('repair_id') or args[0]} queued. "
                            "The worker will re-check the endpoint, usage and expiry before changing the key."
                        ),
                        self._inline_keyboard([[ ("🧩 Key Repairs", "a:n:repairs"), ("🏠 Admin Home", "a:n:admin") ]]),
                    )
        elif command in ("/approve", "/reject"):
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            elif len(args) != 1:
                self.send(chat["id"], f"Usage: {command} <order-id>")
            else:
                try:
                    if command == "/approve":
                        result = self._admin_call(
                            telegram_id, "approve_order", args[0], telegram_id
                        )
                        outcome = (
                            "Wallet top-up approved and credited."
                            if result.status in {"wallet_credited", "already_credited"}
                            else f"Order {result.order_id} approved; provisioning queued."
                        )
                        self.send(
                            chat["id"],
                            outcome,
                            self._inline_keyboard(
                                [
                                    [
                                        ("View Order", f"a:o:{result.order_id}"),
                                        ("📥 Orders", "a:n:orders"),
                                    ]
                                ]
                            ),
                        )
                    else:
                        result = self._admin_call(telegram_id, "reject_order", args[0], telegram_id)
                        self.send(
                            chat["id"],
                            f"Order {args[0]} {result}.",
                            self._inline_keyboard([[("📥 Pending Orders", "a:n:orders")]]),
                        )
                except CommerceError as exc:
                    self.send(chat["id"], str(exc))
        elif command == "/claim":
            if self.trial_ids and telegram_id not in self.trial_ids:
                self.send(
                    chat["id"], "Free staging claims are limited to the configured test accounts."
                )
                return
            if self._free_claim_blocked_by_paid(telegram_id):
                self.send(
                    chat["id"], "Your paid account is active; free claims are paused until it ends."
                )
                return
            try:
                result = self.service.claim(telegram_id, first_name, username=username)
            except OutlineError:
                self.send(
                    chat["id"],
                    "Service temporarily unavailable. Your claim was not consumed. Try again later.",
                )
                return
            if result.denied_reason == "active_promo":
                self.send(
                    chat["id"],
                    "Your promo gift is active. Daily 300 MB returns automatically when the "
                    "gift or promo season ends.",
                )
            elif result.pending:
                self.send(
                    chat["id"],
                    "⏳ Your daily 300 MB key is being prepared safely. "
                    "Open My VPN in a moment to retrieve it; your claim is reserved.",
                    self._customer_keyboard(telegram_id),
                )
            elif result.access_url:
                expiry = format_user_datetime(result.expires_at)
                amount = self.service.limit_bytes / 1_000_000
                self.send(
                    chat["id"],
                    f"Your {amount:g} MB Outline key:\n\n{result.access_url}\n\nExpires: {expiry}",
                    self._key_delivery_keyboard(str(result.access_url)),
                )
            elif result.next_claim_at:
                retry = format_user_datetime(result.next_claim_at)
                self.send(chat["id"], f"Already claimed. Come back after {retry}.")
            else:
                self.send(chat["id"], "Claims are unavailable for this account.")
        else:
            self._send_customer_fallback(chat["id"], telegram_id)

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
