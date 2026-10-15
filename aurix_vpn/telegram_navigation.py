"""Inline activity screens over the existing Telegram/business operations."""

from __future__ import annotations

import secrets
import re
import threading
import time
from contextlib import contextmanager
from html import escape
from typing import Any

from .staff_access import ADMIN_CAPABILITIES, MAX_TELEGRAM_ID, ROLES, StaffAccessError


class TelegramNavigationMixin:
    # Keep Telegram's command menu and old inline messages inside the same
    # activity model as the current Home tree. The commands remain supported;
    # only their presentation transport changes.
    ACTIVITY_COMMANDS = frozenset(
        {
            "/myvpn",
            "/status",
            "/usage",
            "/claim",
            "/claimpromo",
            "/giveaway100gb",
            "/trial",
            "/plans",
            "/buy",
            "/upgrade",
            "/renew",
            "/replace",
            "/myorders",
            "/order",
            "/wallet",
            "/topup",
            "/walletpay",
            "/paid",
            "/cancelorder",
            "/whoami",
            "/pair",
            "/revoke_device",
            "/devices",
            "/help",
            "/admin",
            "/orders",
            "/receipts",
            "/capacity",
            "/failsafety",
            "/setsafety",
            "/drain",
            "/serverstate",
            "/accountstatus",
            "/suspendaccount",
            "/reactivateaccount",
            "/provisionnode",
            "/disableprotocol",
            "/protocolreadiness",
            "/promoteprotocol",
            "/reconcile",
            "/enforcement",
            "/promo",
            "/setpromo",
            "/stoppromo",
            "/resumepromo",
            "/failed",
            "/retry",
            "/retryjob",
            "/refund",
            "/ledger",
            "/receipt",
            "/rejectreceipt",
            "/verify",
            "/approve",
            "/reject",
        }
    )
    PRESERVED_ACTIVITY_COMMANDS = frozenset(
        {
            "/claim",
            "/claimpromo",
            "/giveaway100gb",
            "/trial",
            "/buy",
            "/upgrade",
            "/renew",
            "/replace",
            "/wallet",
            "/walletpay",
            "/paid",
            "/cancelorder",
            "/pair",
            "/revoke_device",
            "/verify",
            "/approve",
            "/reject",
            "/rejectreceipt",
            "/retry",
            "/retryjob",
            "/refund",
            "/setpromo",
            "/stoppromo",
            "/resumepromo",
            "/setsafety",
            "/drain",
            "/serverstate",
            "/suspendaccount",
            "/reactivateaccount",
            "/provisionnode",
            "/disableprotocol",
            "/promoteprotocol",
        }
    )
    STAFF_ACTIVITY_COMMANDS = frozenset(
        {
            "/admin",
            "/orders",
            "/receipts",
            "/capacity",
            "/failsafety",
            "/setsafety",
            "/drain",
            "/serverstate",
            "/accountstatus",
            "/suspendaccount",
            "/reactivateaccount",
            "/provisionnode",
            "/disableprotocol",
            "/protocolreadiness",
            "/promoteprotocol",
            "/reconcile",
            "/enforcement",
            "/promo",
            "/setpromo",
            "/stoppromo",
            "/resumepromo",
            "/failed",
            "/retry",
            "/retryjob",
            "/refund",
            "/ledger",
            "/receipt",
            "/rejectreceipt",
            "/verify",
            "/approve",
            "/reject",
        }
    )

    NAVIGATION_TTL = 1800
    NAVIGATION_LIMIT = 256
    # These are presentation destinations. They never create business records.
    NAVIGATION_MENUS = {
        "buy": (
            "🛍 Buy VPN",
            [
                [("💎 Paid plans", "n:plans")],
                [("🎁 Free access & promotions", "ui:free")],
            ],
        ),
        "free": (
            "🎁 Free access & promotions",
            [
                [("🎁 Daily allowance", "ui:daily")],
                [("🚀 Monthly trial", "ui:monthly")],
                [("🎉 Seasonal promotion", "ui:promo")],
            ],
        ),
        "help": (
            "❓ Help & account",
            [
                [("🧭 Connect with Outline", "n:menu")],
                [
                    ("💳 Payment help", "ui:paymenthelp"),
                    ("👤 My account", "ui:account"),
                ],
            ],
        ),
        "setup": (
            "🧭 Connect with Outline",
            [
                [("1 · Install Outline", "ui:install")],
                [("2 · Add your key", "ui:addkey")],
                [("3 · Connect & check", "ui:connect")],
                [("🆘 Connection help", "ui:trouble")],
            ],
        ),
        "staff": (
            "🛠 Admin workspace",
            [
                [("📥 Sales & payments", "ui:sales")],
                [("🔁 Recovery", "ui:recovery"), ("📈 Fleet", "ui:fleet")],
                [("🎁 Promotions", "a:n:promo"), ("👤 Account access", "ui:accounts")],
                [("⚙️ Advanced operations", "ui:advanced")],
            ],
        ),
        "sales": (
            "📥 Sales & payments",
            [
                [("📥 Pending orders", "a:n:orders")],
                [("🧾 Receipt review", "a:n:receipts")],
            ],
        ),
        "recovery": (
            "🔁 Recovery",
            [
                [("🔁 Failed jobs", "a:n:failed")],
                [
                    ("🚨 Enforcement", "a:n:enforcement"),
                    ("🔎 Consistency", "a:n:reconcile"),
                ],
            ],
        ),
        "promotions": (
            "🎁 Promotions",
            [
                [("🎁 Current campaign", "a:n:promo")],
                [("✍️ Configure campaign", "form:setpromo")],
            ],
        ),
        "fleet": (
            "📈 Fleet",
            [
                [("📈 Capacity & plan slots", "a:n:capacity")],
                [
                    ("🔀 Drain endpoint", "form:drain"),
                    ("⏯ Endpoint state", "form:serverstate"),
                ],
            ],
        ),
        "accounts": (
            "👤 Account access",
            [
                [("👤 Account status", "form:accountstatus")],
                [
                    ("⏸ Suspend account", "form:suspendaccount"),
                    ("▶️ Reactivate", "form:reactivateaccount"),
                ],
            ],
        ),
        "advanced": (
            "⚙️ Advanced operations",
            [
                [("🛡 Failover safety", "cmd:failsafety")],
                [("⚙️ Configure safety", "form:setsafety")],
                [("🔬 Protocol readiness", "form:protocolreadiness")],
                [
                    ("✅ Promote protocol", "form:promoteprotocol"),
                    ("⏸ Disable protocol", "form:disableprotocol"),
                ],
                [("➕ Node intent", "form:provisionnode")],
            ],
        ),
    }
    STAFF_MENUS = {
        "staff",
        "owner_staff",
        "staff_members",
        "staff_assignments",
        "staff_audit",
        "seller_cases",
        "sales",
        "recovery",
        "fleet",
        "accounts",
        "advanced",
        "promotions",
    }
    INPUT_FORMS = {
        "topup": "Amount in MMK, a whole number from 1,000 to 1,000,000. Example: 7500",
        "accountstatus": "Account ID",
        "suspendaccount": "Account ID followed by the reason",
        "reactivateaccount": "Account ID",
        "drain": "Source endpoint, optional target endpoint and optional limit (1–200)",
        "serverstate": "Endpoint and state: active, draining or retired",
        "setsafety": "global pause|resume MAX WINDOW_SECONDS, or region|endpoint KEY pause|resume MAX WINDOW_SECONDS",
        "protocolreadiness": "Endpoint, protocol, comma-separated health signals; optional capabilities",
        "promoteprotocol": "Endpoint, protocol, comma-separated health signals; optional capabilities",
        "disableprotocol": "Endpoint and protocol",
        "provisionnode": "Region, size and image from the configured allowlists",
        "setpromo": "CODE QUOTA_GB DAYS COUNT campaign|daily|hourly FROM_UTC TO_UTC",
        "verify": "Transaction ID and verified amount in MMK, after checking your receiving account. Example: TX123 3000",
        "staff_role": "Telegram ID and role: 123456789 seller|admin|owner",
        "staff_revoke": "Telegram ID of the staff member to revoke",
        "staff_assign": "Seller Telegram ID and customer Telegram ID: 123456789 987654321",
        "staff_unassign": "Seller Telegram ID and customer Telegram ID: 123456789 987654321",
        "staff_capability": "Admin Telegram ID, capability, and allow|deny|inherit",
    }
    OWNER_INPUT_FORMS = frozenset(
        {"staff_role", "staff_revoke", "staff_assign", "staff_unassign", "staff_capability"}
    )

    def handle(self, message: dict[str, Any]) -> Any:
        """Route command shortcuts and receipt uploads through activity screens."""
        if getattr(self._navigation_local, "current", None) is not None:
            return super().handle(message)

        if message.get("photo") or message.get("document"):
            if self._route_activity_input(message):
                return None
            if self._route_receipt_upload_activity(message):
                return None

        if self._route_command_activity(message):
            return None
        return super().handle(message)

    def _route_command_activity(self, message: dict[str, Any]) -> bool:
        user = message.get("from") or {}
        chat = message.get("chat") or {}
        text = message.get("text")
        if (
            not isinstance(text, str)
            or not isinstance(user.get("id"), int)
            or not isinstance(chat.get("id"), int)
            or chat.get("type") != "private"
            or int(chat["id"]) != int(user["id"])
        ):
            return False
        normalized = text.strip()
        if not normalized.startswith("/"):
            normalized = self.CUSTOMER_BUTTON_COMMANDS.get(normalized, "")
            if not normalized and self._is_admin(user["id"]):
                normalized = self.ADMIN_BUTTON_COMMANDS.get(text.strip(), "")
        parts = normalized.split()
        if not parts or not parts[0].startswith("/"):
            return False
        command = parts[0].split("@", 1)[0].lower()
        if command not in self.ACTIVITY_COMMANDS:
            return False
        args = parts[1:]
        entry = "cmd:" + " ".join([command[1:], *args])
        activity = self._new_activity(chat["id"], user["id"], entry)
        activity["entry"] = entry
        activity["history"] = [
            "ui:staff" if command in self.STAFF_ACTIVITY_COMMANDS else "ui:home"
        ]
        source_message_id = message.get("_message_id")
        if isinstance(source_message_id, int):
            activity["message_id"] = source_message_id
        query = {
            "from": dict(user),
            "message": {
                "chat": {"id": chat["id"], "type": "private"},
                "message_id": activity.get("message_id"),
            },
        }
        preserve = command in self.PRESERVED_ACTIVITY_COMMANDS or (
            command == "/topup" and bool(args)
        )
        with self._activity_context(activity, preserve=preserve):
            self._run_activity_entry(activity, entry, query)
        return True

    def _route_receipt_upload_activity(self, message: dict[str, Any]) -> bool:
        user = message.get("from") or {}
        chat = message.get("chat") or {}
        if (
            not (message.get("photo") or message.get("document"))
            or not isinstance(user.get("id"), int)
            or not isinstance(chat.get("id"), int)
            or chat.get("type") != "private"
            or int(chat["id"]) != int(user["id"])
        ):
            return False
        activity = self._new_activity(chat["id"], user["id"], "receipt-upload")
        activity["history"] = ["ui:home"]
        caption = str(message.get("caption") or "")
        try:
            order_id = self._pending_order_id(user["id"], caption)
            order = (
                self.commerce.order_detail(order_id, user["id"])
                if order_id and self.commerce is not None
                else None
            )
            if order is not None:
                activity["order_id"] = str(order_id)
        except Exception:
            # Receipt ownership and validation remain in _handle_receipt.
            # This lookup only lets the acknowledgement offer a safe shortcut.
            pass
        with self._activity_context(activity, preserve=True):
            super().handle(message)
        return True

    def handle_callback(self, query: dict[str, Any]) -> Any:
        """Upgrade legacy inline buttons to revision-bound activity actions."""
        if (
            getattr(self._navigation_local, "current", None) is not None
            or query.get("_navigation_forwarded")
            or str(query.get("data") or "").startswith("u:")
            or query.get("data") == "n:close"
        ):
            return super().handle_callback(query)
        user = query.get("from") or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        data = query.get("data")
        if (
            not isinstance(data, str)
            or not isinstance(user.get("id"), int)
            or not isinstance(chat.get("id"), int)
            or chat.get("type") != "private"
            or int(chat["id"]) != int(user["id"])
        ):
            return super().handle_callback(query)

        if data == "n:start":
            return self._run_legacy_root_callback(query, "ui:home")
        if data == "n:menu":
            return self._run_legacy_root_callback(query, "ui:setup")

        activity = self._new_activity(chat["id"], user["id"], data)
        activity["entry"] = data
        activity["history"] = [self._legacy_callback_parent(data)]
        source_message_id = message.get("message_id")
        if isinstance(source_message_id, int):
            activity["message_id"] = source_message_id
        preserve = self._legacy_callback_preserves(data)
        with self._activity_context(activity, preserve=preserve):
            return super().handle_callback(query)

    def _run_legacy_root_callback(self, query: dict[str, Any], entry: str) -> None:
        user = query["from"]
        message = query["message"]
        chat_id = int(message["chat"]["id"])
        activity = self._new_activity(chat_id, int(user["id"]), entry)
        activity["entry"] = entry
        activity["history"] = (
            [] if entry == "ui:home" else [self._legacy_callback_parent(str(query["data"]))]
        )
        if isinstance(message.get("message_id"), int):
            activity["message_id"] = message["message_id"]
        query_id = query.get("id")
        if isinstance(query_id, str):
            self.request("answerCallbackQuery", {"callback_query_id": query_id})
        with self._activity_context(activity):
            self._run_activity_entry(activity, entry, query)

    @staticmethod
    def _legacy_callback_parent(data: str) -> str:
        if data.startswith(("c2:", "v2:")):
            parts = data.split(":", 2)
            if len(parts) >= 2:
                return "panel:" + parts[1]
        if data.startswith("a:n:"):
            section = data.split(":", 2)[2]
            if section in {"orders", "receipts"}:
                return "ui:sales"
            if section in {"failed", "enforcement", "reconcile"}:
                return "ui:recovery"
            if section == "capacity":
                return "ui:fleet"
            if section == "promo":
                return "ui:promotions"
            return "ui:staff"
        if data.startswith(("a:pl:", "a:ep:", "a:e:")):
            return "ui:fleet"
        if data.startswith("a:promo:"):
            return "ui:promotions"
        if data == "n:adminorders":
            return "ui:staff"
        if data.startswith("a:"):
            return "ui:sales"
        if data.startswith("history:"):
            return "n:myorders"
        if data.startswith("vpnkey:") or data == "ui:key":
            return "n:myvpn"
        if data.startswith("o:"):
            return "n:myorders"
        if data.startswith(("p:", "plan:")):
            return "n:plans"
        if data.startswith("t:"):
            return "n:wallet"
        if data.startswith("g:"):
            return "ui:free"
        if data.startswith(("device:", "devrevoke:", "devconfirm:")):
            return "ui:devices"
        if data.startswith("ui:"):
            section = data[3:]
            parents = {
                "buy": "ui:home",
                "free": "ui:buy",
                "daily": "ui:free",
                "monthly": "ui:free",
                "promo": "ui:free",
                "help": "ui:home",
                "paymenthelp": "ui:help",
                "account": "ui:help",
                "setup": "ui:help",
                "install": "ui:setup",
                "addkey": "ui:setup",
                "connect": "ui:setup",
                "trouble": "ui:setup",
                "devices": "n:myvpn",
                "staff": "ui:home",
                "owner_staff": "ui:staff",
                "staff_members": "ui:owner_staff",
                "staff_assignments": "ui:owner_staff",
                "staff_audit": "ui:owner_staff",
                "seller_cases": "ui:staff",
                "sales": "ui:staff",
                "recovery": "ui:staff",
                "fleet": "ui:staff",
                "accounts": "ui:staff",
                "advanced": "ui:staff",
                "promotions": "ui:staff",
            }
            return parents.get(section, "ui:home")
        if data.startswith("form:"):
            form = data[5:].split(":", 1)[0]
            if form in {"topup"}:
                return "n:wallet"
            if form in {"staff_role", "staff_revoke", "staff_assign", "staff_unassign", "staff_capability"}:
                return "ui:owner_staff"
            if form in {"accountstatus", "suspendaccount", "reactivateaccount"}:
                return "ui:accounts"
            if form in {"drain", "serverstate"}:
                return "ui:fleet"
            if form in {"setsafety", "protocolreadiness", "promoteprotocol", "disableprotocol", "provisionnode"}:
                return "ui:advanced"
            if form in {"setpromo", "stoppromo", "resumepromo"}:
                return "ui:promotions"
            return "ui:sales"
        if data == "cmd:failsafety":
            return "ui:advanced"
        if data in {"staffapply", "staffcancel"}:
            return "ui:owner_staff"
        return "ui:home"

    @staticmethod
    def _legacy_callback_preserves(data: str) -> bool:
        if data.startswith(
            (
                "p:b:",
                "p:r:",
                "p:x:",
                "n:claim",
                "n:trial",
                "g:c:",
                "o:w:",
                "o:x:",
                "a:k:",
                "purchase:",
                "walletconfirm:",
                "devconfirm:",
            )
        ):
            return True
        return data.startswith("t:a:") and data[4:].isdigit()

    def _init_navigation(self) -> None:
        self._navigation_lock = threading.RLock()
        self._navigation_local = threading.local()
        self._activities: dict[str, dict[str, Any]] = {}

    @contextmanager
    def _activity_context(self, activity: dict[str, Any], preserve: bool = False):
        previous = getattr(self._navigation_local, "current", None)
        self._navigation_local.current = (activity, preserve)
        try:
            yield
        finally:
            self._navigation_local.current = previous

    def _new_activity(
        self, chat_id: int, telegram_id: int, entry: str
    ) -> dict[str, Any]:
        with self._navigation_lock:
            now = time.monotonic()
            self._activities = {
                k: v
                for k, v in self._activities.items()
                if now - v["updated_at"] < self.NAVIGATION_TTL
            }
            while len(self._activities) >= self.NAVIGATION_LIMIT:
                self._activities.pop(next(iter(self._activities)))
            token = secrets.token_hex(5)
            activity = {
                "token": token,
                "chat_id": chat_id,
                "telegram_id": telegram_id,
                "entry": entry,
                "history": [],
                "actions": {},
                "revision": 0,
                "message_id": None,
                "protected": False,
                "updated_at": now,
                "input": None,
                "order_messages": {},
            }
            self._activities[token] = activity
            return activity

    def _activity_markup(
        self, activity: dict[str, Any], markup: dict[str, Any] | None
    ) -> dict[str, Any]:
        activity["revision"] += 1
        revision = activity["revision"]
        actions: dict[str, str] = {}
        rows = []
        source_rows = list((markup or {}).get("inline_keyboard", []))
        if self._is_admin(activity["telegram_id"]):
            parent = next(
                (
                    e[6:]
                    for e in reversed(activity["history"])
                    if e.startswith("panel:")
                    and self._panels.get(e[6:], {}).get("view")
                    in {"orders", "receipts", "failed"}
                ),
                None,
            )
            if parent and not any(
                b.get("callback_data", "").startswith("v2:")
                for row in source_rows
                for b in row
                if isinstance(b, dict)
            ):
                source_rows.append(
                    [
                        {
                            "text": "Next pending item ▶",
                            "callback_data": "nextpending:" + parent,
                        }
                    ]
                )
        for row in source_rows:
            buttons = []
            for original in row:
                button = (
                    dict(original)
                    if isinstance(original, dict)
                    else {"text": original[0], "callback_data": original[1]}
                )
                data = button.get("callback_data")
                if isinstance(data, str):
                    if data in {"n:close", "n:start"}:
                        continue
                    if data.startswith("p:b:"):
                        data = "plan:" + data[4:]
                    elif data.startswith("o:w:"):
                        data = "walletreview:" + data[4:]
                    elif data.startswith("p:t:"):
                        data = "ui:monthly"
                    key = str(len(actions))
                    actions[key] = data
                    button["callback_data"] = f"u:{activity['token']}:{revision}:{key}"
                    # Keep existing guarded decisions and make color supplemental.
                    if data.startswith(("a:k:", "p:b:", "g:c:")):
                        button["style"] = "success"
                    elif data.startswith(("o:c:", "o:x:", "a:x:", "a:f:")):
                        button["style"] = "danger"
                    else:
                        button.setdefault("style", "primary")
                buttons.append(button)
            if buttons:
                rows.append(buttons)
        footer = []
        if activity["history"]:
            footer.append(
                {
                    "text": "↩ Back",
                    "callback_data": f"u:{activity['token']}:{revision}:back",
                }
            )
        footer.append({"text": "🏠 Home", "callback_data": "u:h:home"})
        rows.append(footer)
        activity["actions"] = actions
        return {"inline_keyboard": rows}

    def _capture_activity_screen(
        self, chat_id: int, text: str, markup: dict[str, Any] | None
    ) -> tuple[bool, Any]:
        context = getattr(self._navigation_local, "current", None)
        if context is None:
            return False, None
        activity, preserve = context
        if activity["chat_id"] != chat_id:
            # Receipt notifications are records in each Admin's private chat.
            # Give them their own protected activity instead of inheriting the
            # customer's navigation token or leaving raw callback buttons.
            has_inline_actions = bool((markup or {}).get("inline_keyboard"))
            if chat_id > 0 and self._is_admin(chat_id) and has_inline_actions:
                notification = self._new_activity(chat_id, chat_id, "ui:sales")
                notification["entry"] = "ui:sales"
                notification["history"] = ["ui:sales"]
                with self._activity_context(notification, preserve=True):
                    return self._capture_activity_screen(chat_id, text, markup)
            return False, None
        with self._navigation_lock:
            order_action = next(
                (
                    b.get("callback_data", "")
                    for row in (markup or {}).get("inline_keyboard", [])
                    for b in row
                    if b.get("callback_data", "").startswith(
                        ("o:v:", "o:r:", "o:w:", "t:m:", "a:o:")
                    )
                ),
                "",
            )
            if order_action:
                activity["order_id"] = order_action.split(":", 2)[2]
                if preserve:
                    activity["entry"] = "o:v:" + activity["order_id"]
            # Panel tokens retain the originating page/filter for Back.
            panel_data = next(
                (
                    b.get("callback_data", "")
                    for row in (markup or {}).get("inline_keyboard", [])
                    for b in row
                    if b.get("callback_data", "").startswith(("c2:", "v2:"))
                ),
                "",
            )
            if panel_data:
                activity["entry"] = "panel:" + panel_data.split(":")[1]
                panel = self._panels.get(panel_data.split(":")[1], {})
                if panel.get("view") == "customer_orders":
                    markup = {
                        "inline_keyboard": list(
                            (markup or {}).get("inline_keyboard", [])
                        )
                        + [
                            [
                                {
                                    "text": "📚 Full order history",
                                    "callback_data": "history:0",
                                }
                            ]
                        ]
                    }
            if preserve and activity.get("order_id") and not markup:
                markup = self._inline_keyboard(
                    [[("🧾 Review order", "o:v:" + activity["order_id"])]]
                )
            wrapped = self._activity_markup(activity, markup)
            payload = {
                "chat_id": chat_id,
                "text": text[:4096],
                "disable_web_page_preview": True,
                "reply_markup": wrapped,
            }
            self._add_html_parse_mode(payload, text)
            message_id = activity.get("message_id")
            if (
                isinstance(message_id, int)
                and not preserve
                and not activity["protected"]
            ):
                try:
                    result = self.request(
                        "editMessageText", {**payload, "message_id": message_id}
                    )
                    activity["updated_at"] = time.monotonic()
                    self._remember_order_message(activity, message_id)
                    return True, result
                except Exception as exc:
                    if "message is not modified" in str(exc).lower():
                        return True, {"message_id": message_id}
            result = self.request("sendMessage", payload)
            activity["message_id"] = (
                result.get("message_id") if isinstance(result, dict) else None
            )
            activity["protected"] = preserve
            self._remember_order_message(activity, activity["message_id"])
            activity["updated_at"] = time.monotonic()
            return True, result

    def _navigation_media_markup(
        self, chat_id: int, markup: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        context = getattr(self._navigation_local, "current", None)
        if context is None or context[0]["chat_id"] != chat_id:
            return markup
        activity = context[0]
        order = next(
            (
                b.get("callback_data", "")
                for row in (markup or {}).get("inline_keyboard", [])
                for b in row
                if isinstance(b, dict)
                and b.get("callback_data", "").startswith(("a:o:", "o:v:"))
            ),
            "",
        )
        if order:
            activity["order_id"] = order.split(":", 2)[2]
        activity["protected"] = True
        return self._activity_markup(activity, markup)

    @staticmethod
    def _remember_order_message(activity: dict[str, Any], message_id: Any) -> None:
        if isinstance(message_id, int) and activity.get("order_id"):
            activity["order_messages"][message_id] = activity["order_id"]
            while len(activity["order_messages"]) > 32:
                activity["order_messages"].pop(next(iter(activity["order_messages"])))

    def _navigation_media_result(self, chat_id: int, result: Any) -> Any:
        context = getattr(self._navigation_local, "current", None)
        if context and context[0]["chat_id"] == chat_id and isinstance(result, dict):
            context[0]["message_id"] = result.get("message_id")
            context[0]["protected"] = True
            self._remember_order_message(context[0], result.get("message_id"))
        return result

    def _navigation_home_markup(self, telegram_id: int) -> dict[str, Any]:
        rows = [
            [("🛍 Buy VPN", "u:h:buy")],
            [("🔐 My VPN", "u:h:vpn"), ("🧾 My Orders", "u:h:orders")],
            [("💰 Wallet", "u:h:wallet"), ("❓ Help & account", "u:h:help")],
        ]
        if self._is_staff(telegram_id):
            rows.append([("🛠 Staff workspace", "u:h:staff")])
        markup = self._inline_keyboard(rows)
        if self.web_app_url:
            markup["inline_keyboard"].append(
                [{"text": "🌐 VPN portal", "web_app": {"url": self.web_app_url}}]
            )
        return markup

    def _admin_keyboard(self, telegram_id: int) -> dict[str, Any]:
        """Build the same role-filtered staff tree for legacy /admin entrypoints."""
        if not self._is_admin(telegram_id):
            raise PermissionError("admin keyboard requested by non-admin")
        rows = []
        if self._staff_category_available("sales", telegram_id):
            rows.append([("📥 Sales & payments", "u:h:sales")])
        recovery = self._staff_category_available("recovery", telegram_id)
        fleet = self._staff_category_available("fleet", telegram_id)
        if recovery or fleet:
            row = []
            if recovery:
                row.append(("🔁 Recovery", "u:h:recovery"))
            if fleet:
                row.append(("📈 Fleet", "u:h:fleet"))
            rows.append(row)
        promotions = self._staff_category_available("promotions", telegram_id)
        accounts = self._staff_category_available("accounts", telegram_id)
        if promotions or accounts:
            row = []
            if promotions:
                row.append(("🎁 Promo Settings", "u:h:promotions"))
            if accounts:
                row.append(("👤 Account access", "u:h:accounts"))
            rows.append(row)
        if self._staff_category_available("advanced", telegram_id):
            rows.append([("⚙️ Advanced operations", "u:h:advanced")])
        if self._is_owner(telegram_id):
            rows.append([("👑 Staff roles & customer cases", "u:h:owner_staff")])
        rows.append([("🏠 Customer Menu", "u:h:home")])
        return self._inline_keyboard(rows)

    def _navigation_home_content(
        self, telegram_id: int
    ) -> tuple[str, dict[str, Any]]:
        text = (
            "<b>🎉 Welcome to AuriX VPN</b>\n\n"
            "Choose an activity below.\n"
            "Review your orders, access and wallet at any time."
        )
        pending = (
            self.commerce.pending_order_for_user(telegram_id)
            if self.commerce is not None
            else None
        )
        markup = self._navigation_home_markup(telegram_id)
        promo = self.service.giveaway_status(telegram_id)
        if promo.get("exists"):
            text += (
                "\n\n🎁 "
                + escape(str(promo.get("code") or "Promotion"))
                + ": "
                + escape(str(promo.get("campaign_state") or "unavailable"))
            )
            markup["inline_keyboard"].insert(
                0, [{"text": "🎁 Promotion details", "callback_data": "u:h:free"}]
            )
        if pending:
            markup["inline_keyboard"].insert(
                0,
                [
                    {
                        "text": "🕒 Resume open order",
                        "callback_data": f"u:o:{pending['id']}",
                    }
                ],
            )
        return text, markup

    def _staff_category_available(self, category: str, telegram_id: int) -> bool:
        needed = {
            "sales": ("sales.read", "sales.decide"),
            "recovery": ("recovery.manage",),
            "fleet": ("fleet.read", "fleet.manage"),
            "promotions": ("promotions.manage",),
            "accounts": ("accounts.read", "accounts.manage"),
            "advanced": ("fleet.manage", "recovery.manage"),
        }.get(category, ())
        return any(self._has_staff_capability(telegram_id, value) for value in needed)

    def _staff_private_only(self, chat_id: int, telegram_id: int) -> bool:
        if int(chat_id) == int(telegram_id):
            return False
        self.send(chat_id, "Open the AuriX bot in a private chat to use staff screens.")
        return True

    def _show_staff_workspace(self, chat_id: int, telegram_id: int) -> None:
        if self._staff_private_only(chat_id, telegram_id):
            return
        role = self._staff_role(telegram_id)
        if role == "seller":
            self._send_screen(
                chat_id,
                "<b>🧑‍💼 Seller workspace</b>\n\n"
                "Review only customer cases assigned to you. Their payment and VPN records remain read-only.",
                self._inline_keyboard([[ ("📋 Assigned customers", "ui:seller_cases") ]]),
            )
            return
        if role not in {"admin", "owner"}:
            self._send_customer_fallback(chat_id, telegram_id)
            return
        labels = {
            "sales": ("📥 Sales & payments", "ui:sales"),
            "recovery": ("🔁 Recovery", "ui:recovery"),
            "fleet": ("📈 Fleet", "ui:fleet"),
            "promotions": ("🎁 Promotions", "ui:promotions"),
            "accounts": ("👤 Account access", "ui:accounts"),
            "advanced": ("⚙️ Advanced operations", "ui:advanced"),
        }
        rows = [
            [(label, entry)]
            for category, (label, entry) in labels.items()
            if self._staff_category_available(category, telegram_id)
        ]
        if role == "owner":
            rows.append([("👑 Staff roles & customer cases", "ui:owner_staff")])
        rows.append([("🏠 Customer Home", "ui:home")])
        text = (
            "<b>👑 Owner workspace</b>\n\nChoose an operating area or manage staff roles and assigned customer cases."
            if role == "owner"
            else "<b>🛠 Admin workspace</b>\n\nChoose an area available to your role."
        )
        self._send_screen(chat_id, text, self._inline_keyboard(rows))

    def _show_owner_staff_menu(self, chat_id: int, telegram_id: int) -> None:
        if self._staff_private_only(chat_id, telegram_id):
            return
        if not self._is_owner(telegram_id):
            self._send_customer_fallback(chat_id, telegram_id)
            return
        self._send_screen(
            chat_id,
            "<b>👑 Staff roles & customer cases</b>\n\n"
            "Role changes, capability limits and seller assignments are recorded and checked when used.\n"
            "Configured Owner, Admin and Seller IDs must be changed in deployment settings first.",
            self._inline_keyboard(
                [
                    [("👥 Staff members", "ui:staff_members"), ("🎫 Assigned cases", "ui:staff_assignments")],
                    [("🗂 Access changes", "staffaudit:0")],
                    [("➕ Grant or change role", "form:staff_role")],
                    [("⛔ Revoke staff access", "form:staff_revoke")],
                    [("📌 Assign customer case", "form:staff_assign")],
                    [("↩ Remove customer case", "form:staff_unassign")],
                    [("🛡 Set Admin capability", "form:staff_capability")],
                ]
            ),
        )

    def _show_staff_members(self, chat_id: int, telegram_id: int) -> None:
        if self._staff_private_only(chat_id, telegram_id):
            return
        if not self._is_owner(telegram_id):
            self._send_customer_fallback(chat_id, telegram_id)
            return
        members = self.staff_access.staff_members(telegram_id)
        rows = [
            [
                (
                    f"{'✅' if int(item.get('active') or 0) else '⛔'} {item['role'].title()} · {item['telegram_id']}",
                    f"staffmember:{item['telegram_id']}",
                )
            ]
            for item in members[:30]
        ]
        text = (
            "<b>👥 Staff members</b>\n\n"
            + ("Select a person to review their role." if members else "No staff roles are configured.")
            + ("\nShowing the first 30. Use the role form for any Telegram ID." if len(members) > 30 else "")
        )
        self._send_screen(chat_id, text, self._inline_keyboard(rows))

    def _show_staff_assignments(self, chat_id: int, telegram_id: int) -> None:
        if self._staff_private_only(chat_id, telegram_id):
            return
        if not self._is_owner(telegram_id):
            self._send_customer_fallback(chat_id, telegram_id)
            return
        assignments = self.staff_access.assignments(telegram_id)
        lines = ["<b>🎫 Assigned customer cases</b>"]
        if assignments:
            lines.extend(
                f"\nSeller <code>{item['seller_telegram_id']}</code> → customer <code>{item['customer_telegram_id']}</code>"
                for item in assignments[:50]
            )
            if len(assignments) > 50:
                lines.append(f"\nShowing 50 of {len(assignments)} active assignments.")
        else:
            lines.append("\nNo customer cases are assigned.")
        self._send_screen(chat_id, "".join(lines), self._inline_keyboard([]))

    def _show_staff_audit(
        self, chat_id: int, telegram_id: int, offset: int = 0
    ) -> None:
        if self._staff_private_only(chat_id, telegram_id) or not self._is_owner(telegram_id):
            if chat_id == telegram_id:
                self._send_customer_fallback(chat_id, telegram_id)
            return
        events = self.staff_access.audit_events(telegram_id, limit=21, offset=offset)
        visible = events[:20]
        lines = ["<b>🗂 Staff access history</b>"]
        if visible:
            for event in visible:
                actor = "Environment" if int(event.get("actor_id") or 0) == 0 else str(event.get("actor_id"))
                target = str(event.get("target_id") or "—")
                entity = str(event.get("entity_id") or event.get("entity_type") or "—")
                detail = str(event.get("details_json") or "{}")
                lines.append(
                    "\n\n"
                    + escape(str(event.get("created_at") or ""))
                    + f"\n{escape(str(event.get('action') or 'change'))} · {escape(str(event.get('entity_type') or 'staff'))}"
                    + f"\nActor {escape(actor)} · target {escape(target)} · {escape(entity)}"
                    + f"\n{escape(detail[:240])}"
                )
        else:
            lines.append("\n\nNo staff changes are recorded on this page.")
        rows = []
        if offset > 0:
            rows.append([("◀ Newer", f"staffaudit:{max(0, offset - 20)}")])
        if len(events) > 20:
            rows.append([("Older ▶", f"staffaudit:{offset + 20}")])
        self._send_screen(chat_id, "".join(lines), self._inline_keyboard(rows))

    def _show_seller_cases(self, chat_id: int, telegram_id: int) -> None:
        if self._staff_private_only(chat_id, telegram_id):
            return
        if self._staff_role(telegram_id) != "seller" or not self._has_staff_capability(
            telegram_id, "sales.assigned.read"
        ):
            self._send_customer_fallback(chat_id, telegram_id)
            return
        customers = self.staff_access.assigned_customers(telegram_id)
        rows = [
            [(f"👤 Customer {item['customer_id']}", f"staffcase:{item['customer_id']}")]
            for item in customers[:30]
        ]
        if not rows:
            text = "<b>📋 Assigned customers</b>\n\nNo customer cases are assigned to you yet."
        else:
            text = "<b>📋 Assigned customers</b>\n\nOpen a customer case to review recent order status."
            if len(customers) > 30:
                text += f"\nShowing 30 of {len(customers)} assigned customers."
        self._send_screen(chat_id, text, self._inline_keyboard(rows))

    def _show_seller_case(self, chat_id: int, telegram_id: int, customer_id: int) -> None:
        if self._staff_private_only(chat_id, telegram_id):
            return
        try:
            self.staff_access.require_customer_case(telegram_id, customer_id)
        except StaffAccessError:
            self._send_customer_fallback(chat_id, telegram_id)
            return
        if self.commerce is None:
            self._send_screen(chat_id, "Order status is unavailable.", self._inline_keyboard([]))
            return
        orders = [
            order
            for order in self.commerce.list_user_orders(customer_id, limit=20)
            if str(order.get("order_type") or "vpn") != "wallet_topup"
        ][:10]
        lines = [f"<b>🎫 Customer case · {customer_id}</b>"]
        if not orders:
            lines.append("\n\nNo VPN orders are recorded for this customer.")
        else:
            lines.append("\n\nSelect a recent order to review its current stage.")
        rows = []
        for order in orders:
            title = str(order.get("plan_name") or order.get("plan_code") or "VPN order")
            label = f"{title[:22]} · {str(order.get('stage') or order.get('status') or 'unknown')[:20]}"
            rows.append([(label, f"stafforder:{customer_id}:{order['id']}")])
        self._send_screen(chat_id, "".join(lines), self._inline_keyboard(rows))

    def _show_seller_order(
        self, chat_id: int, telegram_id: int, customer_id: int, order_id: str
    ) -> None:
        if self._staff_private_only(chat_id, telegram_id):
            return
        try:
            self.staff_access.require_customer_case(telegram_id, customer_id)
        except StaffAccessError:
            self._send_customer_fallback(chat_id, telegram_id)
            return
        order = (
            self.commerce.order_detail(order_id, customer_id)
            if self.commerce is not None
            else None
        )
        if order is None or str(order.get("order_type") or "vpn") == "wallet_topup":
            self._send_screen(chat_id, "This order is not available in the assigned case.", self._inline_keyboard([]))
            return
        name = str(order.get("plan_name") or order.get("plan_code") or "VPN order")
        stage = str(order.get("stage") or order.get("status") or "unknown")
        lines = [
            "<b>🧾 Assigned order review</b>",
            f"\n\nCustomer: <code>{customer_id}</code>",
            f"\nPlan: {escape(name)}",
            f"\nAmount: {int(order.get('amount_minor') or 0):,} {escape(str(order.get('currency') or ''))}",
            f"\nOrder stage: {escape(stage)}",
            f"\nPayment: {escape(str(order.get('payment_status') or 'not submitted'))}",
            f"\nReceipt review: {escape(str(order.get('receipt_status') or 'none'))}",
            f"\nVPN activation: {escape(str(order.get('subscription_status') or order.get('provisioning_status') or 'not active'))}",
            "\n\nThis view does not reveal wallet details, payment references, or VPN credentials.",
        ]
        self._send_screen(chat_id, "".join(lines), self._inline_keyboard([]))

    def _parse_staff_change(self, form: str, raw_text: str) -> tuple[dict[str, Any], str]:
        tokens = raw_text.split()
        if form == "staff_role" and len(tokens) == 2:
            telegram_id = self._positive_telegram_id(tokens[0])
            role = tokens[1].lower()
            if role not in ROLES:
                raise ValueError("Role must be seller, admin, or owner.")
            self.staff_access.require_configurable_role(0, telegram_id, role)
            return {"action": "role", "telegram_id": telegram_id, "role": role}, f"Set {telegram_id} to {role.title()}"
        if form == "staff_revoke" and len(tokens) == 1:
            telegram_id = self._positive_telegram_id(tokens[0])
            if telegram_id in self.staff_access.owner_ids:
                raise ValueError("The deployment-configured Owner cannot be revoked here.")
            if telegram_id in self.staff_access.admin_ids:
                raise ValueError("Remove this ID from ADMIN_TELEGRAM_IDS before revoking access.")
            if telegram_id in self.staff_access.seller_ids:
                raise ValueError("Remove this ID from SELLER_TELEGRAM_IDS before revoking access.")
            return {"action": "revoke", "telegram_id": telegram_id}, f"Revoke staff access for {telegram_id}"
        if form in {"staff_assign", "staff_unassign"} and len(tokens) == 2:
            seller_id = self._positive_telegram_id(tokens[0])
            customer_id = self._positive_telegram_id(tokens[1])
            action = "assign" if form == "staff_assign" else "unassign"
            verb = "Assign" if action == "assign" else "Remove"
            return {
                "action": action,
                "seller_id": seller_id,
                "customer_id": customer_id,
            }, f"{verb} customer {customer_id} {'to' if action == 'assign' else 'from'} Seller {seller_id}"
        if form == "staff_capability" and len(tokens) == 3:
            telegram_id = self._positive_telegram_id(tokens[0])
            capability = tokens[1].lower()
            effect = tokens[2].lower()
            if capability not in ADMIN_CAPABILITIES:
                raise ValueError("Choose a listed Admin capability.")
            if effect not in {"allow", "deny", "inherit"}:
                raise ValueError("Capability effect must be allow, deny, or inherit.")
            return {
                "action": "capability",
                "telegram_id": telegram_id,
                "capability": capability,
                "effect": None if effect == "inherit" else effect,
            }, f"Set {capability} for Admin {telegram_id} to {effect}"
        raise ValueError("The details do not match this form.")

    @staticmethod
    def _positive_telegram_id(raw_value: str) -> int:
        if not re.fullmatch(r"[0-9]{1,20}", raw_value):
            raise ValueError("Telegram IDs must contain digits only.")
        value = int(raw_value)
        if value <= 0 or value > MAX_TELEGRAM_ID:
            raise ValueError("Telegram ID is outside the supported numeric range.")
        return value

    def _show_staff_change_review(
        self, activity: dict[str, Any], summary: str
    ) -> None:
        self._send_screen(
            activity["chat_id"],
            "<b>Review staff change</b>\n\n"
            + escape(summary)
            + "\n\nThe change is applied only after you confirm. Back cancels it.",
            self._inline_keyboard(
                [[("✅ Confirm change", "staffapply"), ("✖ Cancel", "staffcancel")]]
            ),
        )

    def _apply_staff_change(self, activity: dict[str, Any]) -> None:
        change = activity.get("pending_staff_change")
        if (
            not change
            or activity["chat_id"] != activity["telegram_id"]
            or not self._is_owner(activity["telegram_id"])
        ):
            activity["pending_staff_change"] = None
            self._send_customer_fallback(activity["chat_id"], activity["telegram_id"])
            return
        actor_id = int(activity["telegram_id"])
        try:
            action = change["action"]
            if action == "role":
                changed = self.staff_access.grant_role(
                    actor_id, change["telegram_id"], change["role"]
                )
            elif action == "revoke":
                changed = self.staff_access.revoke_role(actor_id, change["telegram_id"])
            elif action == "assign":
                changed = self.staff_access.assign_customer(
                    actor_id, change["seller_id"], change["customer_id"]
                )
            elif action == "unassign":
                changed = self.staff_access.unassign_customer(
                    actor_id, change["seller_id"], change["customer_id"]
                )
            elif action == "capability":
                changed = self.staff_access.set_capability(
                    actor_id,
                    change["telegram_id"],
                    change["capability"],
                    change["effect"],
                )
            else:
                raise ValueError("Staff change is unavailable")
        except (StaffAccessError, ValueError) as exc:
            activity["pending_staff_change"] = None
            self._send_screen(
                activity["chat_id"],
                "<b>Change not applied</b>\n\n" + escape(str(exc)),
                self._inline_keyboard([]),
            )
            return
        activity["pending_staff_change"] = None
        text = "✅ Change saved." if changed else "No change was needed; the current setting is already in place."
        self._send_screen(
            activity["chat_id"],
            text,
            self._inline_keyboard([[ ("👑 Staff roles & cases", "ui:owner_staff") ]]),
        )

    def _show_navigation_home(self, chat_id: int, telegram_id: int) -> None:
        text, markup = self._navigation_home_content(telegram_id)
        # Remove a legacy reply keyboard and attach inline buttons to the SAME
        # home message, avoiding an extra migration notice in chat history.
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {"remove_keyboard": True},
        }
        if self.welcome_image_source:
            try:
                result = self.send_photo(
                    chat_id, self.welcome_image_source, text, {"remove_keyboard": True}
                )
            except Exception:
                result = self.request("sendMessage", payload)
        else:
            result = self.request("sendMessage", payload)
        if isinstance(result, dict) and isinstance(result.get("message_id"), int):
            try:
                self.request(
                    "editMessageReplyMarkup",
                    {
                        "chat_id": chat_id,
                        "message_id": result["message_id"],
                        "reply_markup": markup,
                    },
                )
            except Exception:
                self.send(chat_id, text, markup)
        else:
            self.send(chat_id, text, markup)

    def _run_activity_entry(
        self, activity: dict[str, Any], entry: str, query: dict[str, Any]
    ) -> None:
        telegram_id, chat_id = activity["telegram_id"], activity["chat_id"]
        if (
            entry.startswith(("o:r:", "t:m:", "t:p:", "qr:", "walletreview:"))
            and self.commerce
        ):
            if entry.startswith("walletreview:"):
                order_id = entry[13:]
            elif entry.startswith("qr:"):
                order_id = entry[3:].rsplit(":", 1)[0]
            else:
                order_id = entry.split(":", 2)[2].split(":", 1)[0]
            order = self.commerce.order_detail(order_id, telegram_id)
            eligible = order and (
                (
                    order.get("status") == "awaiting_payment"
                    and not order.get("payment_status")
                    and not order.get("receipt_status")
                )
                or (
                    order.get("status") in {"awaiting_payment", "payment_submitted"}
                    and order.get("receipt_status") == "rejected"
                )
            )
            if not eligible:
                activity["entry"] = "o:v:" + order_id
                self._send_order_detail(chat_id, telegram_id, order_id)
                return
        if entry == "n:menu":
            entry = "ui:setup"
            activity["entry"] = entry
        if entry == "ui:home":
            text, markup = self._navigation_home_content(telegram_id)
            self._send_screen(chat_id, text, markup)
            return
        if entry == "staffapply":
            self._apply_staff_change(activity)
            return
        if entry == "staffcancel":
            activity["pending_staff_change"] = None
            self._show_owner_staff_menu(chat_id, telegram_id)
            return
        if entry == "ui:staff":
            self._show_staff_workspace(chat_id, telegram_id)
            return
        if entry == "ui:owner_staff":
            self._show_owner_staff_menu(chat_id, telegram_id)
            return
        if entry == "ui:staff_members":
            self._show_staff_members(chat_id, telegram_id)
            return
        if entry == "ui:staff_assignments":
            self._show_staff_assignments(chat_id, telegram_id)
            return
        if entry.startswith("staffaudit:"):
            try:
                offset = max(0, int(entry.split(":", 1)[1]))
            except ValueError:
                self._send_customer_fallback(chat_id, telegram_id)
                return
            self._show_staff_audit(chat_id, telegram_id, offset)
            return
        if entry == "ui:seller_cases":
            self._show_seller_cases(chat_id, telegram_id)
            return
        if entry.startswith("staffmember:"):
            if not self._is_owner(telegram_id):
                self._send_customer_fallback(chat_id, telegram_id)
                return
            try:
                target_id = self._positive_telegram_id(entry.split(":", 1)[1])
            except ValueError:
                self._send_customer_fallback(chat_id, telegram_id)
                return
            member = next(
                (
                    value
                    for value in self.staff_access.staff_members(telegram_id)
                    if int(value["telegram_id"]) == target_id
                ),
                None,
            )
            if member is None:
                self._send_screen(chat_id, "Staff member not found.", self._inline_keyboard([]))
                return
            configured = self.staff_access.configured_role(target_id)
            state = "active" if int(member.get("active") or 0) else "revoked"
            lines = [
                "<b>👤 Staff member</b>",
                f"\n\nTelegram ID: <code>{target_id}</code>",
                f"\nRole: {escape(str(member['role']).title())} · {state}",
            ]
            if configured:
                lines.append(f"\nConfigured role: {escape(configured.title())}")
            overrides = self.staff_access.capability_overrides(telegram_id, target_id)
            if overrides:
                lines.append("\nCapability overrides: " + escape(", ".join(
                    f"{name}={effect}" for name, effect in sorted(overrides.items())
                )))
            self._send_screen(
                chat_id,
                "".join(lines),
                self._inline_keyboard(
                    [
                        [("📝 Change role", "form:staff_role")],
                        [("🛡 Change Admin capability", "form:staff_capability")],
                        [("⛔ Revoke access", "form:staff_revoke")],
                    ]
                ),
            )
            return
        if entry.startswith("staffcase:"):
            try:
                customer_id = self._positive_telegram_id(entry.split(":", 1)[1])
            except ValueError:
                self._send_customer_fallback(chat_id, telegram_id)
                return
            self._show_seller_case(chat_id, telegram_id, customer_id)
            return
        if entry.startswith("stafforder:"):
            _, customer_value, order_id = entry.split(":", 2)
            try:
                customer_id = self._positive_telegram_id(customer_value)
            except ValueError:
                self._send_customer_fallback(chat_id, telegram_id)
                return
            self._show_seller_order(chat_id, telegram_id, customer_id, order_id)
            return
        if entry.startswith("nextpending:"):
            token = entry[12:]
            state = self._panels.get(token)
            if (
                not self._is_admin(telegram_id)
                or not state
                or state.get("telegram_id") != telegram_id
            ):
                self._send_customer_fallback(chat_id, telegram_id)
                return
            items = self._panel_data(telegram_id, state["view"])
            current = activity.get("order_id")
            selected = next(
                (
                    item
                    for item in items
                    if str(item.get("order_id") or item.get("id")) != current
                ),
                None,
            )
            if not selected:
                self._send_screen(
                    chat_id,
                    "No other pending items. Return to the queue to refresh.",
                    self._inline_keyboard([]),
                )
                return
            if state["view"] == "receipts":
                self._send_receipt_review(chat_id, selected)
            else:
                target = str(selected.get("order_id") or selected["id"])
                activity["entry"] = "a:o:" + target
                activity["order_id"] = target
                self._send_order_detail(chat_id, telegram_id, target, admin_view=True)
            return
        if entry == "n:plans":
            plans = self.commerce.plans() if self.commerce else []
            rows = [
                [(f"{p.name} · {p.price_minor:,} {p.currency}", "plan:" + p.code)]
                for p in plans
            ]
            self._send_screen(
                chat_id,
                "<b>💎 Paid VPN plans</b>\n\nChoose a plan to review its allowance, duration and price.\nPayment requires staff approval.",
                self._inline_keyboard(rows),
            )
            return
        if entry.startswith("plan:"):
            plan = self.commerce.get_plan(entry[5:]) if self.commerce else None
            if plan is None:
                self._send_screen(
                    chat_id, "Paid plans are unavailable.", self._inline_keyboard([])
                )
                return
            self._send_screen(
                chat_id,
                "<b>💎 "
                + escape(plan.name)
                + "</b>\n\n"
                + f"{plan.price_minor:,} {escape(plan.currency)}\n"
                + f"{self._format_bytes(plan.quota_bytes)} · {plan.duration_days} days\n\n"
                + "Creating an order does not charge your wallet. Payment and staff approval follow.",
                self._inline_keyboard([[("🛒 Create order", "purchase:" + plan.code)]]),
            )
            return
        if entry.startswith("purchase:"):
            entry = "p:b:" + entry[9:]
        if entry.startswith("walletreview:"):
            order_id = entry[13:]
            order = (
                self.commerce.order_detail(order_id, telegram_id)
                if self.commerce
                else None
            )
            if order is None or str(order.get("order_type")) == "wallet_topup":
                self._send_screen(
                    chat_id,
                    "This order cannot be paid from your wallet.",
                    self._inline_keyboard([]),
                )
                return
            balance = self.commerce.wallet_balance(telegram_id, str(order["currency"]))
            self._send_screen(
                chat_id,
                "<b>💰 Review wallet payment</b>\n\n"
                + f"Order: {escape(str(order_id)[:10])}\nAmount: {int(order['amount_minor']):,} {escape(str(order['currency']))}\n"
                + f"Wallet balance: {balance:,} {escape(str(order['currency']))}\n\n"
                + "Confirm to reserve this amount. Staff approval is still required.",
                self._inline_keyboard(
                    [[("✅ Confirm wallet payment", "walletconfirm:" + order_id)]]
                ),
            )
            return
        if entry.startswith("walletconfirm:"):
            entry = "o:w:" + entry[14:]
        if entry.startswith("history:"):
            page = max(0, min(int(entry[8:]), 1000000))
            orders = (
                self.commerce.list_user_orders(telegram_id, limit=4, offset=page * 3)
                if self.commerce
                else []
            )
            rows = [
                [
                    (
                        f"{o.get('plan_name') or o['plan_code']} · {str(o['stage']).replace('_', ' ')}"[
                            :55
                        ],
                        "o:v:" + str(o["id"]),
                    )
                ]
                for o in orders[:3]
            ]
            pager = []
            if page:
                pager.append(("◀ Previous", f"history:{page - 1}"))
            if len(orders) > 3:
                pager.append(("Next ▶", f"history:{page + 1}"))
            if pager:
                rows.append(pager)
            self._send_screen(
                chat_id,
                f"<b>📚 Order history · Page {page + 1}</b>\n\n"
                + (
                    "Select an order to review. All your records remain available here."
                    if orders
                    else "No records on this page."
                ),
                self._inline_keyboard(rows),
            )
            return
        if entry.startswith("ui:"):
            name = entry[3:]
            if name in self.STAFF_MENUS and not self._is_staff(telegram_id):
                self._send_customer_fallback(chat_id, telegram_id)
                return
            if name in {"sales", "recovery", "fleet", "promotions", "accounts", "advanced"} and not self._staff_category_available(
                name, telegram_id
            ):
                self._send_customer_fallback(chat_id, telegram_id)
                return
            if name == "staff":
                self._show_staff_workspace(chat_id, telegram_id)
            elif name == "owner_staff":
                self._show_owner_staff_menu(chat_id, telegram_id)
            elif name == "staff_members":
                self._show_staff_members(chat_id, telegram_id)
            elif name == "staff_assignments":
                self._show_staff_assignments(chat_id, telegram_id)
            elif name == "seller_cases":
                self._show_seller_cases(chat_id, telegram_id)
            elif name in self.NAVIGATION_MENUS:
                title, rows = self.NAVIGATION_MENUS[name]
                self._send_screen(
                    chat_id,
                    f"<b>{title}</b>\n\nChoose what you want to do.",
                    self._inline_keyboard(rows),
                )
            elif name in {"daily", "monthly", "promo"}:
                promo = self.service.giveaway_status(telegram_id)
                if name == "promo":
                    code = str(promo.get("code") or "")
                    text = "<b>🎉 Seasonal promotion</b>\n\n" + (
                        f"{self._promo_quota_label(int(promo['quota_bytes']))} · {promo['duration_days']} days\n"
                        f"Campaign: {promo.get('campaign_state', 'unavailable')}\n"
                        "Eligibility and remaining capacity are checked when you claim."
                        if code
                        else "No seasonal promotion is available."
                    )
                    rows = (
                        [[("🎁 Claim promotion", f"g:c:{code}")]]
                        if code and promo.get("active")
                        else []
                    )
                else:
                    monthly = name == "monthly"
                    text = "<b>🎁 Free VPN access</b>\n\n" + (
                        "Monthly 3 GiB · 30 days. Renewable every rolling 30 days."
                        if monthly
                        else "Daily 300 MiB · 24 hours. Renewable every rolling 24 hours."
                    )
                    text += "\n\nPaid access, account status and promo eligibility are checked before issuing a key."
                    rows = [[("🎁 Claim access", "n:trial" if monthly else "n:claim")]]
                self._send_screen(chat_id, text, self._inline_keyboard(rows))
            elif name == "paymenthelp":
                self._send_screen(
                    chat_id,
                    "<b>💳 Payments</b>\n\nOpen your order and choose a payment method.\n"
                    "Pay the displayed amount, then reply to that order's payment instructions with the receipt.\n"
                    "Receipts require staff review. If already submitted, refresh the order instead of paying again.",
                    self._inline_keyboard([[("🧾 My Orders", "n:myorders")]]),
                )
            elif name == "account":
                status = (
                    self._collect_customer_vpn_state(telegram_id).get("account_status")
                    or "active"
                )
                self._send_screen(
                    chat_id,
                    f"<b>👤 My account</b>\n\nTelegram ID: <code>{telegram_id}</code>\nStatus: {escape(str(status))}",
                    self._inline_keyboard(
                        [
                            [("📱 Managed devices", "ui:devices")],
                            [("🔐 Pair device", "cmd:pair")],
                        ]
                    ),
                )
            elif name == "devices":
                identity = getattr(self.commerce, "identity", None)
                devices = identity.devices_for_account(telegram_id) if identity else []
                rows = [
                    [
                        (
                            str(d.get("label") or "Unnamed device")[:40],
                            "device:" + str(d["device_id"]),
                        )
                    ]
                    for d in devices
                ]
                rows.append([("🔐 Pair device", "cmd:pair")])
                self._send_screen(
                    chat_id,
                    "<b>📱 Managed devices</b>\n\n"
                    + (
                        "Select a device to review or revoke."
                        if devices
                        else "No managed devices are enrolled."
                    ),
                    self._inline_keyboard(rows),
                )
            elif name == "key":
                self._show_activity_key(activity)
            elif name in {"install", "addkey", "connect", "trouble"}:
                copy = {
                    "install": "<b>1 · Install Outline</b>\n\nChoose the official app for your device.",
                    "addkey": "<b>2 · Add your key</b>\n\nOpen My VPN and copy your active key.\nOpen Outline, tap + and paste the complete key.\nKeep the key private.",
                    "connect": "<b>3 · Connect & check</b>\n\nIn Outline, select the server and tap Connect.\nCompare your public IP before and after connecting.",
                    "trouble": "<b>🆘 Connection help</b>\n\nCheck your allowance and expiry in My VPN.\nCopy the complete key again without spaces.\nTry switching between Wi-Fi and mobile data.\nContact support if it still fails.",
                }[name]
                if name == "install":
                    rows = [
                        [
                            b
                            for b in row
                            if b.get("url")
                            and not b["url"].startswith(
                                ("https://t.me/", "https://www.google.com/")
                            )
                        ]
                        for row in self._outline_help_keyboard()["inline_keyboard"]
                    ]
                    rows = [row for row in rows if row]
                elif name == "connect":
                    rows = [
                        [
                            {
                                "text": "🌐 Check my public IP",
                                "url": "https://www.google.com/search?q=what+is+my+ip",
                            }
                        ]
                    ]
                else:
                    rows = [[{"text": "🔐 My VPN", "callback_data": "n:myvpn"}]]
                    if name == "trouble":
                        rows.append(
                            [
                                {
                                    "text": "🆘 AuriX support",
                                    "url": "https://t.me/+oA18TDWAD9NiNWU1",
                                }
                            ]
                        )
                self._send_screen(chat_id, copy, {"inline_keyboard": rows})
            return
        if entry.startswith("vpnkey:"):
            _, panel_token, index = entry.split(":", 2)
            state = self._panels.get(panel_token, {})
            if (
                state.get("telegram_id") != telegram_id
                or state.get("chat_id") != chat_id
            ):
                self._send_screen(
                    chat_id,
                    "This access list expired. Reopen My VPN.",
                    self._inline_keyboard([[("🔐 My VPN", "n:myvpn")]]),
                )
                return
            try:
                selected = state["all_items"][int(index)]
            except (IndexError, ValueError, KeyError):
                return
            activity["key_ref"] = self._key_identity(selected)
            activity["entry"] = "ui:key"
            self._show_activity_key(activity)
            return
        if entry.startswith(("device:", "devrevoke:", "devconfirm:")):
            operation, device_id = entry.split(":", 1)
            identity = getattr(self.commerce, "identity", None)
            device = (
                next(
                    (
                        d
                        for d in identity.devices_for_account(telegram_id)
                        if str(d["device_id"]) == device_id
                    ),
                    None,
                )
                if identity
                else None
            )
            if device is None:
                self._send_screen(
                    chat_id,
                    "Device not found on your account.",
                    self._inline_keyboard([]),
                )
                return
            label = escape(str(device.get("label") or "Unnamed device"))
            if operation == "devconfirm":
                identity.revoke_device(telegram_id, device_id)
                self._send_screen(
                    chat_id,
                    f"<b>📱 {label}</b>\n\nDevice revoked. It can no longer request manifests or configurations.",
                    self._inline_keyboard([[("📱 Devices", "ui:devices")]]),
                )
            elif operation == "devrevoke":
                self._send_screen(
                    chat_id,
                    f"<b>Revoke {label}?</b>\n\nThis stops future device configuration requests.",
                    self._inline_keyboard(
                        [[("Confirm revoke", "devconfirm:" + device_id)]]
                    ),
                )
            else:
                self._send_screen(
                    chat_id,
                    f"<b>📱 {label}</b>\n\nStatus: {escape(str(device.get('status') or 'unknown'))}\nLast seen: {escape(str(device.get('last_seen_at') or 'never'))}",
                    self._inline_keyboard(
                        [[("Revoke device…", "devrevoke:" + device_id)]]
                    ),
                )
            return
        if entry.startswith("form:") or entry == "t:a:custom":
            form_spec = entry[5:] if entry.startswith("form:") else "topup"
            form, _, prefix = form_spec.partition(":")
            if form in self.OWNER_INPUT_FORMS and (
                not self._is_owner(telegram_id) or chat_id != telegram_id
            ):
                self._send_customer_fallback(chat_id, telegram_id)
                return
            if form != "topup" and not self._is_admin(telegram_id):
                self._send_customer_fallback(chat_id, telegram_id)
                return
            if form not in self.INPUT_FORMS:
                return
            activity["input"] = {
                "form": form,
                "prefix": prefix,
                "expires_at": time.monotonic() + 600,
            }
            self._send_screen(
                chat_id,
                "<b>✍️ Enter details</b>\n\n"
                + self.INPUT_FORMS[form]
                + "\n\nReply to this screen with the details. Back cancels this input only.",
                self._inline_keyboard([]),
            )
            return
        if entry.startswith("panel:"):
            token = entry[6:]
            state = self._panels.get(token)
            if (
                state is None
                or state.get("telegram_id") != telegram_id
                or state.get("chat_id") != chat_id
            ):
                self._send_screen(
                    chat_id,
                    "This list expired. Open it again.",
                    self._navigation_home_markup(telegram_id),
                )
                return
            if state["view"] == "customer_orders":
                state["all_items"] = self.commerce.list_user_orders(
                    telegram_id, limit=50
                )
                text, markup = self._render_customer_orders_panel(token)
            elif state["view"] == "customer_vpn":
                self._refresh_customer_vpn_panel(token, telegram_id)
                text, markup = self._render_customer_vpn_panel(token)
            else:
                if not self._is_admin(telegram_id):
                    self._send_customer_fallback(chat_id, telegram_id)
                    return
                state["all_items"] = self._panel_data(telegram_id, state["view"])
                text, markup = self._render_panel(token)
            self._send_screen(chat_id, text, markup)
            return
        if entry.startswith("qr:"):
            order_id, provider = entry[3:].rsplit(":", 1)
            order = self.commerce.order_detail(order_id, telegram_id)
            if order:
                self._send_payment_qr(
                    chat_id,
                    order,
                    str(order.get("selected_payment_provider") or provider),
                )
            return
        if entry.startswith("cmd:"):
            command = entry[4:]
            self.handle(
                {
                    "chat": {"id": chat_id, "type": "private"},
                    "from": query.get("from") or {"id": telegram_id},
                    "text": "/" + command,
                }
            )
            return
        forwarded = {
            **query,
            "data": entry,
            "_navigation_forwarded": True,
            "message": {
                "chat": {"id": chat_id, "type": "private"},
                "message_id": activity.get("message_id"),
            },
        }
        self.handle_callback(forwarded)

    @staticmethod
    def _key_identity(entry: dict[str, Any]) -> tuple[str, ...]:
        return tuple(
            str(entry.get(k) or "")
            for k in ("key_type", "endpoint_id", "outline_key_id", "created_at", "tier")
        )

    def _show_activity_key(self, activity: dict[str, Any]) -> None:
        fresh = self._collect_customer_vpn_state(activity["telegram_id"])
        entry = next(
            (
                e
                for e in fresh["all_items"]
                if self._key_identity(e) == activity.get("key_ref")
            ),
            None,
        )
        if entry is None:
            self._send_screen(
                activity["chat_id"],
                "This access record is no longer available. Reopen My VPN.",
                self._inline_keyboard([]),
            )
            return
        lines = [
            "<b>🔐 " + escape(str(entry["tier"])) + "</b>",
            "",
            "Status: " + escape(str(entry.get("status") or "unknown")),
            "Expires: " + escape(str(entry.get("expires_at") or "pending")),
        ]
        rows = []
        access_url = entry.get("access_url")
        if (
            access_url
            and entry.get("status") == "active"
            and not entry.get("access_blocked")
            and fresh.get("account_status") in (None, "active")
        ):
            lines.extend(
                [
                    "",
                    "Keep this key private.",
                    "<code>" + escape(str(access_url)) + "</code>",
                ]
            )
            copy = self._copy_text_button("📋 Copy Outline key", access_url)
            if copy:
                rows.append([copy])
        else:
            lines.append("No usable key can be revealed for this record.")
        if entry.get("plan_code"):
            rows.append(
                [
                    {
                        "text": "🔄 Renew",
                        "callback_data": "p:r:" + str(entry["plan_code"]),
                    }
                ]
            )
        rows.append([{"text": "🔄 Refresh", "callback_data": "ui:key"}])
        self._send_screen(
            activity["chat_id"], "\n".join(lines), {"inline_keyboard": rows}
        )

    @staticmethod
    def _activity_read_entry(entry: str) -> str:
        if entry in {"staffapply", "staffcancel"}:
            return "ui:owner_staff"
        if entry.startswith(("o:w:", "o:x:", "o:c:", "o:r:")):
            return "o:v:" + entry.split(":", 2)[2]
        if entry.startswith("t:p:"):
            return "qr:" + entry[4:]
        if entry.startswith(("a:a:", "a:x:", "a:f:", "a:h:", "a:rv:")):
            return "a:o:" + entry.split(":", 2)[2]
        if entry.startswith("a:k:"):
            return "ui:staff"
        if entry in {"cmd:pair", "cmd:revoke_device"}:
            return "ui:devices"
        if entry.startswith("walletconfirm:"):
            return "o:v:" + entry[14:]
        if entry.startswith("devconfirm:"):
            return "device:" + entry[11:]
        return entry

    def _handle_navigation_callback(self, query: dict[str, Any]) -> bool:
        data = query["data"]
        if not data.startswith("u:"):
            return False
        telegram_id, chat_id = query["from"]["id"], query["message"]["chat"]["id"]
        with self._navigation_lock:
            if data.startswith("u:h:") or data.startswith("u:o:"):
                destination = data[4:]
                if data.startswith("u:o:"):
                    entry = "o:v:" + destination
                elif destination == "home":
                    message_id = (query.get("message") or {}).get("message_id")
                    activity = next(
                        (
                            item
                            for item in self._activities.values()
                            if item["telegram_id"] == telegram_id
                            and item["chat_id"] == chat_id
                            and item.get("message_id") == message_id
                            and time.monotonic() - item["updated_at"]
                            <= self.NAVIGATION_TTL
                        ),
                        None,
                    )
                    if activity is not None and not activity["protected"]:
                        activity["history"].clear()
                        activity["input"] = None
                        activity["entry"] = "ui:home"
                        with self._activity_context(activity):
                            self._run_activity_entry(activity, "ui:home", query)
                    else:
                        self._show_navigation_home(chat_id, telegram_id)
                    return True
                else:
                    entry = {
                        "orders": "n:myorders",
                        "vpn": "n:myvpn",
                        "wallet": "n:wallet",
                    }.get(destination, "ui:" + destination)
                activity = self._new_activity(chat_id, telegram_id, entry)
                activity["history"] = ["ui:home"]
            else:
                parts = data.split(":")
                if len(parts) != 4:
                    return True
                activity = self._activities.get(parts[1])
                if (
                    activity is None
                    or activity["telegram_id"] != telegram_id
                    or activity["chat_id"] != chat_id
                    or time.monotonic() - activity["updated_at"] > self.NAVIGATION_TTL
                ):
                    self.send(
                        chat_id,
                        "This activity expired. Reopen your records from Home.",
                        self._navigation_home_markup(telegram_id),
                    )
                    return True
                if str(activity["revision"]) != parts[2]:
                    self.send(
                        chat_id,
                        "This screen changed. Use its latest buttons.",
                        self._navigation_home_markup(telegram_id),
                    )
                    return True
                action = parts[3]
                if action == "back":
                    activity["input"] = None
                    activity["pending_staff_change"] = None
                    if not activity["history"]:
                        return True
                    entry = activity["history"].pop()
                else:
                    entry = activity["actions"].get(action)
                    if not entry:
                        return True
                    if activity["protected"]:
                        history = list(activity["history"])
                        activity = self._new_activity(chat_id, telegram_id, entry)
                        activity["history"] = history
                    elif entry != activity["entry"] and (
                        not entry.startswith(("c2:", "v2:")) or ":item:" in entry
                    ):
                        activity["history"].append(activity["entry"])
                        activity["history"] = activity["history"][-20:]
                    activity["input"] = None
            preserve = entry == "n:wallet" or entry.startswith(
                (
                    "p:b:",
                    "p:r:",
                    "p:x:",
                    "n:claim",
                    "n:trial",
                    "g:c:",
                    "o:w:",
                    "o:x:",
                    "a:k:",
                    "cmd:pair",
                    "devconfirm:",
                    "purchase:",
                    "walletconfirm:",
                )
            )
            if entry.startswith("t:a:") and entry[4:].isdigit():
                preserve = True
            if entry.startswith(("o:", "t:m:", "t:p:")):
                activity["order_id"] = entry.split(":", 2)[2].split(":", 1)[0]
            activity["entry"] = self._activity_read_entry(entry)
            if entry.startswith(("c2:", "v2:")) and ":item:" in entry:
                _, panel_token, _, index = entry.split(":", 3)
                state = self._panels.get(panel_token, {})
                try:
                    item = state["items"][int(index)]
                    target = (
                        item.get("order_id")
                        if state.get("view") == "failed"
                        else item["id"]
                    )
                    prefix = (
                        "o:v:"
                        if entry.startswith("c2:")
                        else ("a:r:" if state.get("view") == "receipts" else "a:o:")
                    )
                    activity["entry"] = prefix + str(target)
                    activity["order_id"] = str(item.get("order_id") or target)
                except (KeyError, ValueError, IndexError):
                    pass
            with self._activity_context(activity, preserve=preserve):
                self._run_activity_entry(activity, entry, query)
            return True

    def _route_activity_input(self, message: dict[str, Any]) -> bool:
        if getattr(self._navigation_local, "current", None) is not None:
            return False
        user, chat = message.get("from") or {}, message.get("chat") or {}
        if chat.get("type") != "private" or chat.get("id") != user.get("id"):
            return False
        reply = message.get("reply_to_message") or {}
        if not isinstance(reply.get("message_id"), int):
            return False
        attachment = bool(message.get("photo") or message.get("document"))
        # An explicit order caption is handled by the existing ownership checks.
        if attachment and str(message.get("caption") or "").split()[:1] == ["/paid"]:
            return False
        with self._navigation_lock:
            if attachment:
                bound = next(
                    (
                        a
                        for a in self._activities.values()
                        if a["telegram_id"] == user.get("id")
                        and a["chat_id"] == chat.get("id")
                        and reply["message_id"] in a["order_messages"]
                        and time.monotonic() - a["updated_at"] <= self.NAVIGATION_TTL
                    ),
                    None,
                )
                if bound is None:
                    self.send(
                        chat["id"],
                        "This payment screen is no longer available. Open your order from Home and send the receipt with /paid <order-id>.",
                        self._navigation_home_markup(user["id"]),
                    )
                    return True
                with self._activity_context(bound, preserve=True):
                    self.handle(
                        {
                            **message,
                            "caption": "/paid "
                            + bound["order_messages"][reply["message_id"]],
                            "_message_id": None,
                        }
                    )
                return True
            activity = next(
                (
                    a
                    for a in self._activities.values()
                    if a["telegram_id"] == user.get("id")
                    and a["chat_id"] == chat.get("id")
                    and a["message_id"] == reply["message_id"]
                    and (a.get("input") or a.get("order_id"))
                ),
                None,
            )
            if activity is None:
                return False
            if not activity.get("input"):
                return False
            prompt = activity["input"]
            if prompt["expires_at"] < time.monotonic():
                activity["input"] = None
                self.send(
                    chat["id"],
                    "This input expired. Open the activity again.",
                    self._navigation_home_markup(user["id"]),
                )
                return True
            text = str(message.get("text") or "").strip()
            if not text or text.startswith("/"):
                return False
            form = prompt["form"]
            if form in self.OWNER_INPUT_FORMS:
                if not self._is_owner(user["id"]):
                    activity["input"] = None
                    activity["pending_staff_change"] = None
                    self._send_customer_fallback(chat["id"], user["id"])
                    return True
                try:
                    pending, summary = self._parse_staff_change(form, text)
                except (StaffAccessError, ValueError) as exc:
                    with self._activity_context(activity):
                        self.send(
                            chat["id"],
                            "Details were not accepted: " + escape(str(exc)) + "\n\n" + self.INPUT_FORMS[form],
                            self._inline_keyboard([]),
                        )
                    return True
                activity["input"] = None
                activity["pending_staff_change"] = pending
                activity["entry"] = "ui:owner_staff"
                with self._activity_context(activity):
                    self._show_staff_change_review(activity, summary)
                return True
            if form != "topup" and not self._is_admin(user["id"]):
                self._send_customer_fallback(chat["id"], user["id"])
                return True
            if form == "topup" and not text.replace(",", "").isdigit():
                self.send(chat["id"], "Enter a whole MMK amount, or use Back.")
                return True
            activity["input"] = None
            with self._activity_context(activity, preserve=form == "topup"):
                prefix = str(prompt.get("prefix") or "")
                self.handle(
                    {**message, "text": f"/{form} {prefix} {text}", "_message_id": None}
                )
            if form == "topup" and self.commerce:
                order = self.commerce.pending_order_for_user(user["id"])
                if order:
                    activity["entry"] = "o:v:" + str(order["id"])
            return True
