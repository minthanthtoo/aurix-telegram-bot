"""Inline activity screens over the existing Telegram/business operations."""

from __future__ import annotations

import secrets
import threading
import time
from contextlib import contextmanager
from html import escape
from typing import Any


class TelegramNavigationMixin:
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
            "🛠 Staff workspace",
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
    }

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
        if context is None or context[0]["chat_id"] != chat_id:
            return False, None
        activity, preserve = context
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
        if self._is_admin(telegram_id):
            rows.append([("🛠 Staff workspace", "u:h:staff")])
        markup = self._inline_keyboard(rows)
        if self.web_app_url:
            markup["inline_keyboard"].append(
                [{"text": "🌐 VPN portal", "web_app": {"url": self.web_app_url}}]
            )
        return markup

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
            if name in self.STAFF_MENUS and not self._is_admin(telegram_id):
                self._send_customer_fallback(chat_id, telegram_id)
                return
            if name in self.NAVIGATION_MENUS:
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
