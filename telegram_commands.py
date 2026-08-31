"""Telegram message and command routing."""

from __future__ import annotations

import sys
from typing import Any

from commerce import CommerceError
from entitlements import OutlineError


class TelegramCommandMixin:
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
        if message.get("photo") or message.get("document"):
            self._handle_receipt(message, chat["id"], telegram_id)
            return
        text = message.get("text") or ""
        if not isinstance(text, str) or not text.strip():
            return
        raw_text = text.strip()
        text = "/giveaway100gb" if raw_text.upper() == "100GBFREE" else None
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
        if command in self.ADMIN_ONLY_COMMANDS and not self._is_admin(telegram_id):
            self._send_customer_fallback(chat["id"], telegram_id)
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
            else:
                prompt = {
                    "/approve": lambda: f"Approve order {args[0]} and queue VPN provisioning?",
                    "/reject": lambda: f"Reject order {args[0]} and notify the customer?",
                    "/retry": lambda: f"Retry the failed worker job for order {args[0]}?",
                    "/refund": lambda: f"Refund order {args[0]} to the customer wallet and revoke paid access?",
                    "/verify": lambda: f"Verify receipt {args[0]} for transaction {args[1]} and amount {args[2]}?",
                    "/rejectreceipt": lambda: f"Reject receipt {args[0]} and request a replacement screenshot?",
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
                    }[command],
                )
                return
        if command in ("/start", "/help"):
            self.send(
                chat["id"],
                "AuriX VPN\n\n"
                "Choose an action below. Everyone can claim 300 MB daily or "
                "3 GB every 30 days, with 50 GB and 100 GB paid upgrades. "
                "The first five eligible users to type 100GBFREE receive 100 GiB for 30 days.\n\n"
                "No key is issued until you choose an action. For payment, create an upgrade "
                "order and send only the receipt screenshot.",
                self._customer_keyboard(telegram_id),
            )
        elif command == "/whoami":
            access = "\nAdmin access: enabled" if self._is_admin(telegram_id) else ""
            self.send(
                chat["id"],
                f"Your Telegram ID: {telegram_id}{access}",
                self._customer_keyboard(telegram_id),
            )
        elif command == "/giveaway100gb":
            try:
                result = self.service.claim_giveaway(
                    telegram_id, first_name, username=username
                )
            except OutlineError:
                self.send(
                    chat["id"],
                    "The giveaway key could not be provisioned. No winner slot was consumed; try again.",
                )
                return
            if result.outcome == "won":
                self.send(
                    chat["id"],
                    f"🎉 You are giveaway winner #{result.winner_number} of 5!\n\n"
                    f"Your 100 GiB / 30-day Outline key:\n\n{result.access_url}\n\n"
                    f"Expires: {result.expires_at.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                    "This is your final AuriX entitlement. Daily, monthly-free, paid, "
                    "renewal, and replacement plans are now permanently disabled for this account.",
                    self._customer_keyboard(telegram_id),
                )
            elif result.outcome == "already_won":
                self.send(
                    chat["id"],
                    f"You already won slot #{result.winner_number}. No second key or slot was created.\n"
                    f"Expires: {result.expires_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
                    "Use /status or /usage to track it.",
                    self._customer_keyboard(telegram_id),
                )
            elif result.outcome == "ineligible":
                self.send(chat["id"], f"This account is not eligible: {result.reason}")
            else:
                self.send(chat["id"], "The 5 × 100 GiB giveaway is fully claimed.")
        elif command == "/admin":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            else:
                summary = ""
                if self.commerce is not None:
                    try:
                        report = self._admin_call(telegram_id, "consistency_report")
                        summary = (
                            f"\n\nQueue: {report.get('pending_receipts', 0)} receipt(s) pending · "
                            f"{report.get('pending_receipt_uploads', 0)} upload(s) pending · "
                            f"{report.get('failed_receipt_uploads', 0)} upload(s) failed · "
                            f"{report.get('failed_jobs', 0)} failed job(s) · "
                            f"{report.get('stale_receipts', 0)} stale review(s) · "
                            f"{report.get('dead_notifications', 0)} dead notification(s)"
                        )
                    except Exception as exc:
                        print(f"admin dashboard error: {type(exc).__name__}", file=sys.stderr)
                self.send(
                    chat["id"],
                    "AuriX Admin\n\n"
                    "Daily flow: Pending Orders → open receipt → verify the transaction "
                    "against your receiving account → Approve.\n"
                    "Use Failed Jobs to retry a reviewed Outline failure, open an order "
                    "to inspect its wallet ledger, and run Consistency before taking "
                    "payment decisions." + summary,
                    self._admin_keyboard(telegram_id),
                )
        elif command == "/myorders":
            if self.commerce is None:
                self.send(chat["id"], "Order tracking is not configured.")
            else:
                orders = self.commerce.list_user_orders(telegram_id)
                if not orders:
                    self.send(
                        chat["id"], "You have no orders yet.", self._customer_keyboard(telegram_id)
                    )
                else:
                    text = "Your recent orders\n\n" + "\n\n".join(
                        self._order_summary(order) for order in orders
                    )
                    rows = [
                        [(f"View {str(order['id'])[:8]}", f"o:v:{order['id']}")] for order in orders
                    ]
                    rows.append([("💎 Upgrade", "n:plans"), ("💰 Wallet", "n:wallet")])
                    self.send(chat["id"], text, self._inline_keyboard(rows))
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
                    heading = "Order created"
                    self.send(
                        chat["id"],
                        f"{heading}: {order.order_id}\n"
                        f"Plan: {order.plan.name}\n"
                        f"Amount: {order.plan.price_minor:,} {order.plan.currency}\n\n"
                        f"Pay through the approved channel, then send the receipt screenshot.\n"
                        f"Reply to this order message or caption it with: /paid {order.order_id}",
                        self._inline_keyboard(
                            [
                                [
                                    ("📷 Send Receipt", f"o:r:{order.order_id}"),
                                    ("💰 Pay Wallet", f"o:w:{order.order_id}"),
                                ],
                                [("View Order", f"o:v:{order.order_id}")],
                            ]
                        ),
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
        elif command in ("/status", "/myvpn"):
            self._send_status(chat["id"], telegram_id, include_key=command == "/myvpn")
        elif command == "/usage":
            self._send_usage(chat["id"], telegram_id)
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
                        self.send(
                            chat["id"],
                            f"{heading}: {order.order_id}\nSend /paid {order.order_id} then the receipt screenshot after payment.",
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
            if result.denied_reason == "giveaway_winner":
                self.send(
                    chat["id"],
                    "Your 100 GiB giveaway win is your final AuriX entitlement; no additional free plan is available.",
                )
            elif result.access_url:
                self.send(
                    chat["id"],
                    f"Your monthly 3 GiB key:\n\n{result.access_url}\n\nExpires: {result.expires_at.strftime('%Y-%m-%d %H:%M UTC')}",
                )
            else:
                retry = (
                    result.next_claim_at.strftime("%Y-%m-%d %H:%M UTC")
                    if result.next_claim_at
                    else "later"
                )
                self.send(chat["id"], f"Monthly 3 GiB already claimed. Come back after {retry}.")
        elif command == "/wallet":
            if self.commerce is None:
                self.send(chat["id"], "Wallet is not configured.")
            else:
                balance = self.commerce.wallet_balance(telegram_id)
                history = self.commerce.wallet_history(telegram_id, limit=5)
                history_text = ""
                if history:
                    history_text = "\n\nRecent wallet events:\n" + "\n".join(
                        f"{item['created_at']} · {item['kind']} {int(item['amount_minor']):,} {item['currency']} · {item['reference_id']}"
                        for item in history
                    )
                self.send(
                    chat["id"],
                    f"Wallet balance: {balance:,} MMK\nWallet credits are posted only after staff verify a receipt.{history_text}",
                    self._inline_keyboard(
                        [[("🧾 My Orders", "n:myorders"), ("💎 Upgrade", "n:plans")]]
                    ),
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
                            "Receipt metadata exists, but Telegram no longer accepts its stored "
                            "file ID. Ask the customer to submit the screenshot again.",
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
                    self.send(
                        chat["id"],
                        f"Receipt verified for order {order_id}. Use /approve {order_id} to provision.",
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
                    snapshot = self._admin_call(telegram_id, "capacity_snapshot")
                except Exception as exc:
                    self.send(chat["id"], "Outline capacity metrics are temporarily unavailable.")
                    print(f"capacity error: {type(exc).__name__}", file=sys.stderr)
                else:
                    mapped_usage = sum(item["used_bytes"] for item in snapshot["usage"])
                    giveaway = self.service.giveaway_status(telegram_id)
                    self.send(
                        chat["id"],
                        "AuriX capacity\n"
                        f"Outline version: {snapshot['outline_version']}\n"
                        f"Active subscriptions: {snapshot['active_subscriptions']}\n"
                        f"Active keys: {snapshot['active_keys']}\n"
                        f"Mapped transfer (Outline window): {mapped_usage:,} bytes\n"
                        f"Expiring within 24h: {snapshot['expiring_24h']}\n"
                        f"Pending jobs: {snapshot['pending_jobs']}\n"
                        f"Failed jobs: {snapshot['failed_jobs']}\n"
                        f"100GBFREE winners: {giveaway['claimed_count']} / {giveaway['winner_limit']}\n"
                        f"100GBFREE slots remaining: {giveaway['remaining_slots']}",
                        self._admin_keyboard(telegram_id),
                    )
        elif command == "/reconcile":
            if not self._is_admin(telegram_id):
                self._send_customer_fallback(chat["id"], telegram_id)
            elif self.commerce is None:
                self.send(chat["id"], "Commerce is not configured.")
            else:
                report = self._admin_call(telegram_id, "consistency_report")
                issue_keys = {
                    "duplicate_open_orders",
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
                        f"{item['created_at']} · {item['kind']} {int(item['amount_minor']):,} {item['currency']} · {item['reference_id']}"
                        for item in history
                    )
                    self.send(chat["id"], "\n".join(lines), self._admin_keyboard(telegram_id))
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
                        self.send(
                            chat["id"],
                            f"Order {result.order_id} approved; provisioning queued.",
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
            if result.denied_reason == "giveaway_winner":
                self.send(
                    chat["id"],
                    "Your 100 GiB giveaway win is your final AuriX entitlement; no additional free plan is available.",
                )
            elif result.access_url:
                expiry = result.expires_at.strftime("%Y-%m-%d %H:%M UTC")
                amount = self.service.limit_bytes / 1024**2
                self.send(
                    chat["id"],
                    f"Your {amount:g} MiB Outline key:\n\n{result.access_url}\n\nExpires: {expiry}",
                )
            elif result.next_claim_at:
                retry = result.next_claim_at.strftime("%Y-%m-%d %H:%M UTC")
                self.send(chat["id"], f"Already claimed. Come back after {retry}.")
            else:
                self.send(chat["id"], "Claims are unavailable for this account.")
        else:
            self._send_customer_fallback(chat["id"], telegram_id)
