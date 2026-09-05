"""Customer and administrator order detail views."""

from __future__ import annotations

from typing import Any

from telegram_formatting import format_user_datetime


class TelegramAdminOrderMixin:
    @staticmethod
    def _order_summary(order: dict[str, Any]) -> str:
        return (
            f"{order['id']}\n"
            f"{order.get('plan_name') or order['plan_code']} · "
            f"{int(order['amount_minor']):,} {order['currency']}\n"
            f"Order: {order['status']} · Payment: {order.get('payment_status') or 'not submitted'} · "
            f"Receipt: {order.get('receipt_status') or 'not submitted'} · Stage: {order.get('stage', 'unknown')}"
        )

    @staticmethod
    def _order_detail_text(order: dict[str, Any]) -> str:
        lines = [
            "AuriX Order",
            "",
            f"ID: {order['id']}",
            f"Customer: {order['telegram_id']}",
            f"Plan: {order.get('plan_name') or order['plan_code']}",
            f"Amount: {int(order['amount_minor']):,} {order['currency']}",
            f"Order: {order['status']}",
            f"Refund: {order.get('refund_status') or 'none'}",
            f"Customer stage: {order.get('stage', 'unknown')}",
            f"Payment: {order.get('payment_status') or 'not submitted'}",
            f"Receipt review: {order.get('receipt_status') or 'not submitted'}",
            f"Subscription: {order.get('subscription_status') or 'not created'}",
            f"Provisioning: {order.get('provisioning_status') or 'not queued'}",
            f"Revocation: {order.get('revocation_status') or 'not queued'}",
            f"Created: {format_user_datetime(order['created_at'])}",
        ]
        if order.get("expires_at"):
            lines.append(f"Expires: {format_user_datetime(order['expires_at'])}")
        if order.get("payment_method"):
            lines.append(f"Selected method: {str(order['payment_method']).upper()}")
        if order.get("evidence_id"):
            lines.append(f"Evidence ID: {order['evidence_id']}")
        return "\n".join(lines)

    def _payment_method_keyboard(
        self,
        order_id: str,
        *,
        selected: str | None = None,
        qr_view: bool = False,
        allow_wallet: bool = True,
    ) -> dict[str, Any]:
        buttons = []
        for method in self.PAYMENT_METHOD_ORDER:
            item = self.PAYMENT_METHODS[method]
            label = str(item["button"])
            if method == selected:
                label = "✓ " + label
            buttons.append((label, f"m:s:{method}:{order_id}"))
        rows = [buttons[:2], buttons[2:4], buttons[4:]]
        if qr_view:
            rows.append([("✅ I’ve Paid · Send Receipt", f"o:u:{order_id}")])
        footer = [("🧾 Order", f"o:v:{order_id}")]
        if allow_wallet:
            footer.insert(0, ("💰 Pay Wallet", f"o:w:{order_id}"))
        rows.append(footer)
        return self._inline_keyboard(rows)

    def _send_payment_method_chooser(
        self, chat_id: int, telegram_id: int, order_id: str, heading: str | None = None
    ) -> None:
        if self.commerce is None:
            self.send(chat_id, "Payment is not configured.")
            return
        order = self.commerce.order_detail(order_id, telegram_id)
        if order is None:
            self.send(chat_id, "Order not found.")
            return
        if order.get("status") != "awaiting_payment":
            self._send_order_detail(chat_id, telegram_id, order_id)
            return
        method = str(order.get("payment_method") or "").lower()
        if method not in self.PAYMENT_METHODS:
            method = self.PAYMENT_METHOD_ORDER[0]
        self._show_payment_qr(
            {"message": {}},
            chat_id,
            telegram_id,
            order_id,
            method,
            heading=heading,
        )

    def _show_payment_qr(
        self,
        query: dict[str, Any],
        chat_id: int,
        telegram_id: int,
        order_id: str,
        method: str,
        heading: str | None = None,
    ) -> None:
        order = self.commerce.choose_payment_method(telegram_id, order_id, method)
        item = self.PAYMENT_METHODS[method]
        path = self.PAYMENT_QR_DIR / str(item["asset"])
        if not path.is_file():
            raise RuntimeError("Payment QR asset is unavailable")
        method_number = self.PAYMENT_METHOD_ORDER.index(method) + 1
        prefix = f"{heading}\n\n" if heading else ""
        caption = (
            prefix + f"🏦 Payment QR {method_number}/5 · {item['label']}\n"
            f"Order #{str(order_id)[:8]}\n\n"
            f"Pay exactly {int(order['amount_minor']):,} {order['currency']}.\n"
            "1. Scan this QR in the selected wallet.\n"
            "2. Verify the recipient and amount before confirming.\n"
            "3. Tap “I’ve Paid” and send the completed receipt screenshot.\n\n"
            "Use the numbered buttons below to switch QR in this same message.\n"
            "Never send your PIN, password or OTP."
        )
        markup = self._payment_method_keyboard(
            order_id,
            selected=method,
            qr_view=True,
            allow_wallet=str(order.get("plan_code") or "") != "wallet_topup",
        )
        message = query.get("message") or {}
        message_id = message.get("message_id")
        if isinstance(message_id, int) and message.get("photo"):
            self.edit_local_photo(chat_id, message_id, path, caption, markup)
        else:
            self.send_local_photo(chat_id, path, caption, markup)

    def _order_actions(self, order: dict[str, Any], is_admin: bool) -> dict[str, Any]:
        order_id = str(order["id"])
        rows: list[list[tuple[str, str]]] = []
        if is_admin:
            if order.get("evidence_id"):
                rows.append([("🧾 Open Receipt", f"a:r:{order['evidence_id']}")])
                if order.get("receipt_status") == "pending":
                    rows.append([("🛑 Reject Receipt", f"a:q:{order['evidence_id']}")])
            if order.get("status") == "approved" and order.get("provisioning_status") == "failed":
                rows.append([("🔁 Retry Setup", f"a:h:{order_id}")])
            if order.get("revocation_status") in ("pending", "running"):
                rows.append([("⏳ Revocation in progress", f"a:o:{order_id}")])
            elif order.get("revocation_status") == "failed":
                rows.append([("🔁 Retry Revocation", f"a:g:{order_id}")])
            if order.get("telegram_id"):
                rows.append([("💰 View Ledger", f"a:l:{order['telegram_id']}")])
            if (
                order.get("plan_code") != "wallet_topup"
                and order.get("refund_status") != "refunded"
                and (order.get("status") == "approved" or order.get("payment_status") == "verified")
            ):
                rows.append([("💸 Refund", f"a:f:{order_id}")])
            if order.get("status") == "payment_submitted" and (
                order.get("receipt_status") == "verified"
                or order.get("wallet_reservation_status") == "reserved"
            ):
                label = (
                    "✅ Credit Wallet" if order.get("plan_code") == "wallet_topup" else "✅ Approve"
                )
                rows.append([(label, f"a:a:{order_id}")])
            if (
                order.get("status") in ("awaiting_payment", "payment_submitted")
                and order.get("refund_status") != "refunded"
            ):
                if (
                    order.get("payment_status") == "verified"
                    or order.get("receipt_status") == "verified"
                ):
                    pass
                else:
                    rows.append([("❌ Reject…", f"a:x:{order_id}")])
            rows.append(
                [
                    ("🔄 Refresh", f"a:o:{order_id}"),
                    ("📥 Orders", "a:n:orders"),
                ]
            )
        else:
            allow_wallet = str(order.get("plan_code") or "") != "wallet_topup"
            if (
                order.get("status") == "awaiting_payment"
                and not order.get("payment_status")
                and not order.get("receipt_status")
            ):
                selected_method = str(order.get("payment_method") or "").lower()
                if selected_method in self.PAYMENT_METHODS:
                    selected_label = str(self.PAYMENT_METHODS[selected_method]["label"])
                    rows.append(
                        [(f"🖼 Open {selected_label} QR", f"m:s:{selected_method}:{order_id}")]
                    )
                    rows.append(
                        [("🔁 Change QR", f"o:p:{order_id}")]
                        + ([("💰 Pay Wallet", f"o:w:{order_id}")] if allow_wallet else [])
                    )
                else:
                    rows.append(
                        [("🏦 Choose Payment QR", f"o:p:{order_id}")]
                        + ([("💰 Pay Wallet", f"o:w:{order_id}")] if allow_wallet else [])
                    )
                rows.append([("🗑 Cancel Order", f"o:c:{order_id}")])
            elif order.get("receipt_status") == "rejected":
                rows.append([("📷 Send Replacement Receipt", f"o:r:{order_id}")])
            if order.get("stage") == "fulfilled":
                rows.append([("🔐 My VPN", "n:myvpn")])
            rows.append(
                [
                    ("🔄 Refresh", f"o:v:{order_id}"),
                    ("🧾 My Orders", "n:myorders"),
                ]
            )
        return self._inline_keyboard(rows)

    def _send_order_detail(
        self,
        chat_id: int,
        telegram_id: int,
        order_id: str,
        admin_view: bool = False,
        heading: str | None = None,
        message_id: int | None = None,
    ) -> None:
        if self.commerce is None:
            text = "Order tracking is not configured."
            if message_id is not None:
                self.edit_message(chat_id, message_id, text)
            else:
                self.send(chat_id, text)
            return
        is_admin = bool(admin_view and self._is_admin(telegram_id))
        order = self.commerce.order_detail(order_id, telegram_id, is_admin=is_admin)
        if order is None:
            text = "Order not found."
            if message_id is not None:
                self.edit_message(chat_id, message_id, text)
            else:
                self.send(chat_id, text)
            return
        text = ((heading + "\n\n") if heading else "") + self._order_detail_text(order)
        markup = self._order_actions(order, is_admin)
        if message_id is not None:
            self.edit_message(chat_id, message_id, text, markup)
        else:
            self.send(chat_id, text, markup)
